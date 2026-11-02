#!/usr/bin/env python3
"""Confluence -> knowledge base, through the REST API (Cloud and Data Center).

  confluence.py --job kb-sec --cql 'space = SEC and type = page'
  confluence.py --job kb-arch --space ARCH --label hld --attachments
  confluence.py --job kb-svc --ancestor 123456 --since 2025-01-01      # a page tree
  confluence.py --job kb-sec                                           # resume (same job)
  confluence.py --check                                                # test URL + credentials only

Credentials come from environment variables (never from arguments, files or chat):
  CONFLUENCE_URL     Cloud: https://<site>.atlassian.net/wiki   Data Center: https://confluence.corp[/context]
  CONFLUENCE_TOKEN   Cloud: API token (with CONFLUENCE_EMAIL)     Data Center: personal access token
  CONFLUENCE_EMAIL   Cloud only -> Basic auth email:token; unset -> Bearer token (Data Center PAT)

Uses the v1 CQL search endpoint (rest/api/content/search), which both Cloud and Data
Center still serve, and follows `_links.next` for paging. Page bodies (storage format)
become markdown in .agents/cache/<job>/pages/ with page id, space, breadcrumb, version and
URL in the front matter, so later units can cite sources. Long pages are split into parts.
--attachments downloads images and documents (pdf, docx, pptx, xlsx, drawio, vsdx, svg) to
.agents/cache/<job>/attachments/ for convert.py and the diagram step.

Same contract as crawl.py: time-sliced, resumable, memory-checked, one JSON line of output,
rows in the shared job store so `batch.py --job <job>` works unchanged.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from common import (CACHE, STATE, append_jsonl, budget, memory_snapshot, now_iso,  # noqa: E402
                    open_job_db, register_worker, rel, split_text, unregister_worker, write_page)
from crawl import TextExtractor  # noqa: E402

ATTACH_EXT = re.compile(r"\.(png|jpe?g|gif|webp|bmp|svg|pdf|docx|pptx|xlsx|drawio|vsdx|xml)$", re.I)
PAGE_LIMIT = 25            # body.storage expansions are capped at 50 server-side; stay well below
MAX_ATTACH_BYTES = 25_000_000


class Confluence:
    def __init__(self, timeout: int):
        base = os.environ.get("CONFLUENCE_URL", "").rstrip("/")
        token = os.environ.get("CONFLUENCE_TOKEN", "")
        if not base or not token:
            sys.exit("ERROR: set CONFLUENCE_URL and CONFLUENCE_TOKEN (and CONFLUENCE_EMAIL for Cloud). See --help.")
        self.base, self.timeout = base, timeout
        self.host = urllib.parse.urlparse(base).netloc.lower()
        email = os.environ.get("CONFLUENCE_EMAIL")
        if email:
            import base64
            self.auth = "Basic " + base64.b64encode(f"{email}:{token}".encode()).decode()
        else:
            self.auth = "Bearer " + token

    def _req(self, url: str, accept: str = "application/json"):
        if not url.startswith("http"):
            url = self.base + (url if url.startswith("/") else "/" + url)
        headers = {"Accept": accept}
        if urllib.parse.urlparse(url).netloc.lower() == self.host:  # credentials only to Confluence
            headers["Authorization"] = self.auth
        return urllib.request.urlopen(urllib.request.Request(url, headers=headers), timeout=self.timeout)

    def get_json(self, url: str) -> dict:
        for attempt in range(4):
            try:
                with self._req(url) as r:
                    return json.loads(r.read().decode("utf-8", errors="replace"))
            except urllib.error.HTTPError as e:
                if e.code == 429 or e.code >= 500:  # rate limited / transient
                    time.sleep(int(e.headers.get("Retry-After", 2 ** attempt * 3)))
                    continue
                if e.code in (401, 403):
                    sys.exit(f"ERROR: Confluence returned {e.code} - check CONFLUENCE_TOKEN/EMAIL and your access to this content.")
                raise
        raise RuntimeError(f"giving up on {url} after retries")

    def next_url(self, data: dict) -> str | None:
        nxt = data.get("_links", {}).get("next")
        if not nxt:
            return None
        if nxt.startswith("http"):
            return nxt
        # next links are relative to the site root (Cloud: they start with /wiki or /rest)
        root = f"{urllib.parse.urlparse(self.base).scheme}://{self.host}"
        ctx = urllib.parse.urlparse(self.base).path  # e.g. /wiki or /confluence
        return root + (nxt if nxt.startswith(ctx) else ctx + nxt)


def build_cql(a) -> str:
    if a.cql:
        return a.cql
    parts = ["type = page"]
    if a.space:
        parts.append("space in (" + ", ".join(f'"{s}"' for s in a.space) + ")")
    if a.label:
        parts.append("label in (" + ", ".join(f'"{x}"' for x in a.label) + ")")
    if a.ancestor:
        parts.append(f"ancestor = {a.ancestor}")
    if a.since:
        parts.append(f'lastmodified >= "{a.since}"')
    if a.text:
        parts.append(f'text ~ "{a.text}"')
    if len(parts) == 1:
        sys.exit("Give --cql or at least one of --space/--label/--ancestor/--since/--text (no whole-site dumps).")
    return " and ".join(parts) + " order by lastmodified desc"


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--job")
    p.add_argument("--cql")
    p.add_argument("--space", nargs="*")
    p.add_argument("--label", nargs="*")
    p.add_argument("--ancestor", help="page id: everything under this page")
    p.add_argument("--since", help="YYYY-MM-DD, only pages modified since")
    p.add_argument("--text", help="full-text filter")
    p.add_argument("--max-pages", type=int, default=None)
    p.add_argument("--attachments", action="store_true", help="also download images/documents attached to pages")
    p.add_argument("--time-slice", type=int)
    p.add_argument("--check", action="store_true", help="only test URL and credentials")
    a = p.parse_args()
    cfg = budget().get("crawl", {})
    conf = Confluence(cfg.get("timeout_s", 20))

    if a.check:
        d = conf.get_json("/rest/api/content/search?" + urllib.parse.urlencode({"cql": "type = page", "limit": 1}))
        print(json.dumps({"ok": True, "base": conf.base, "auth": conf.auth.split()[0], "sample_size": d.get("size")}))
        return
    if not a.job:
        sys.exit("--job is required")

    db = open_job_db(a.job)
    db.execute("""CREATE TABLE IF NOT EXISTS attachments(
        id TEXT PRIMARY KEY, page_id TEXT, title TEXT, media TEXT, url TEXT, file TEXT, status TEXT, err TEXT)""")
    row = db.execute("SELECT v FROM meta WHERE k='confluence'").fetchone()
    st = json.loads(row[0]) if row else {}
    new_query = any([a.cql, a.space, a.label, a.ancestor, a.since, a.text])
    if st and new_query and build_cql(a) != st["cql"]:
        sys.exit(f"Job {a.job} already holds a different query ({st['cql'][:80]}). Use a new --job name.")
    if not st:
        cql = build_cql(a)
        st = {"cql": cql, "next": "/rest/api/content/search?" + urllib.parse.urlencode(
            {"cql": cql, "limit": PAGE_LIMIT, "expand": "body.storage,version,space,ancestors"}),
            "attachments": a.attachments, "done": False}
    st["max_pages"] = a.max_pages or st.get("max_pages") or 2000
    st["attachments"] = st.get("attachments") or a.attachments
    max_chars = cfg.get("max_text_chars", 40000)
    slice_s = a.time_slice or cfg.get("time_slice_s", 240)
    progress = STATE / "progress" / f"crawl-{a.job}.jsonl"
    att_dir = CACHE / a.job / "attachments"
    import memprobe
    memprobe.lower_priority()
    register_worker(os.getpid(), f"confluence:{a.job}")
    start, status = time.time(), "time_slice"
    try:
        print(f"confluence {a.job}: cql={st['cql'][:120]} slice={slice_s}s", flush=True)
        while True:
            done = db.execute("SELECT COUNT(*) FROM urls WHERE status='done' AND "
                              "(url NOT LIKE '%#part%' OR url LIKE '%#part1')").fetchone()[0]
            if st.get("done") or not st.get("next"):
                status = "complete"
                break
            if done >= st["max_pages"]:
                status = "max_pages"
                break
            if time.time() - start > slice_s:
                break
            if memory_snapshot().get("state") == "PAUSE":
                status = "memory_pause"
                break
            data = conf.get_json(st["next"])
            for res in data.get("results", []):
                pid = res.get("id")
                title = res.get("title", "")
                web = data.get("_links", {}).get("base", conf.base) + res.get("_links", {}).get("webui", f"/pages/{pid}")
                html = res.get("body", {}).get("storage", {}).get("value", "")
                ex = TextExtractor(web, 10 ** 9, 0)
                try:
                    ex.feed(html)
                    ex.close()
                except Exception:
                    pass
                text = ex.text()
                crumbs = " > ".join(x.get("title", "") for x in res.get("ancestors", []))
                meta = {"page_id": pid, "space": res.get("space", {}).get("key", ""), "breadcrumb": crumbs,
                        "version": res.get("version", {}).get("number", ""),
                        "modified": res.get("version", {}).get("when", ""), "url": web}
                parts = split_text(text, max_chars) if text else [""]
                for i, part in enumerate(parts, 1):
                    key = web if len(parts) == 1 else f"{web}#part{i}"
                    t = title if len(parts) == 1 else f"{title} (part {i}/{len(parts)})"
                    f, n = write_page(a.job, key, t, part, meta)
                    db.execute("INSERT OR REPLACE INTO urls(url, depth, status, http, title, file, chars, ts) "
                               "VALUES(?,0,'done',200,?,?,?,?)", (key, title, f, n, now_iso()))
                if st["attachments"]:
                    try:
                        att = conf.get_json(f"/rest/api/content/{pid}/child/attachment?limit=50")
                        for x in att.get("results", []):
                            name = x.get("title", "")
                            dl = x.get("_links", {}).get("download")
                            if dl and ATTACH_EXT.search(name):
                                db.execute("INSERT OR IGNORE INTO attachments(id, page_id, title, media, url, status) "
                                           "VALUES(?,?,?,?,?,'queued')",
                                           (x.get("id"), pid, name, x.get("metadata", {}).get("mediaType", ""), dl))
                    except Exception as e:  # noqa: BLE001
                        db.execute("INSERT OR REPLACE INTO meta VALUES(?, ?)", (f"atterr-{pid}", str(e)[:200]))
            st["next"] = conf.next_url(data)
            st["done"] = st["next"] is None
            db.execute("INSERT OR REPLACE INTO meta VALUES('confluence', ?)", (json.dumps(st),))
            db.commit()
            # download queued attachments, a few per page of results
            for aid, pid, name, url in db.execute(
                    "SELECT id, page_id, title, url FROM attachments WHERE status='queued' LIMIT 40").fetchall():
                target = att_dir / str(pid) / re.sub(r"[^\w.\- ]", "_", name)
                target.parent.mkdir(parents=True, exist_ok=True)
                try:
                    full = conf.base + url if not url.startswith("http") else url
                    with conf._req(full, accept="*/*") as r, open(target, "wb") as out:
                        size = 0
                        while True:
                            c = r.read(65536)
                            if not c:
                                break
                            size += len(c)
                            if size > MAX_ATTACH_BYTES:
                                raise ValueError("attachment over 25 MB")
                            out.write(c)
                    db.execute("UPDATE attachments SET status='done', file=? WHERE id=?", (rel(target), aid))
                except Exception as e:  # noqa: BLE001
                    target.unlink(missing_ok=True)
                    db.execute("UPDATE attachments SET status='failed', err=? WHERE id=?", (str(e)[:200], aid))
            db.commit()
            c = db.execute("SELECT COUNT(*) FROM urls WHERE status='done'").fetchone()[0]
            append_jsonl(progress, {"ts": now_iso(), "job": a.job, "done": c, "queued": 0 if st["done"] else "more",
                                    "failed": 0, "source": "confluence"})
        if status == "time_slice" and (st.get("done") or not st.get("next")):
            status = "complete"
        pending_att = db.execute("SELECT COUNT(*) FROM attachments WHERE status='queued'").fetchone()[0]
        if status == "complete" and pending_att:
            status = "time_slice"  # attachments still to download
        pages = db.execute("SELECT COUNT(*) FROM urls WHERE status='done'").fetchone()[0]
        att = dict(db.execute("SELECT status, COUNT(*) FROM attachments GROUP BY status").fetchall())
        summary = {"job": a.job, "status": status, "page_parts": pages, "attachments": att,
                   "pages_dir": rel(CACHE / a.job / "pages"), "seconds": round(time.time() - start)}
        if status in ("time_slice", "memory_pause"):
            summary["next"] = f"re-run: python .agents/tools/confluence.py --job {a.job}"
        else:
            nxt = [f"python .agents/tools/batch.py --job {a.job} --add-units"]
            if att.get("done"):
                nxt.insert(0, f"python .agents/tools/convert.py --job {a.job}-att --inputs {rel(att_dir)}")
            summary["next"] = " ; ".join(nxt)
        append_jsonl(progress, {"ts": now_iso(), "event": "slice_end", **summary})
        print(json.dumps(summary))
    finally:
        unregister_worker(os.getpid())


if __name__ == "__main__":
    main()
