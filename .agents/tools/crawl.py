#!/usr/bin/env python3
"""Memory-bounded, resumable crawler. Pages go to disk as trimmed markdown;
NOTHING large is printed, so the chat context that runs it stays small.

  crawl.py --job docs --seeds https://example.com/docs/ --max-pages 2000 --max-depth 3
  crawl.py --job docs                    # resume (same job name) - run again until status=complete
  crawl.py --job list --seeds-file urls.txt --max-depth 0     # fetch exactly a URL list
  crawl.py --job spa --seeds https://app.example.com --render  # JS pages via Playwright (optional)
  crawl.py --job wiki --seeds https://intranet/docs --auth-env INTRANET_TOKEN   # authenticated internal site
  crawl.py --job sso --seeds https://portal/docs --cookie-env PORTAL_COOKIE     # reuse a browser session cookie

Secrets: --auth-env / --cookie-env take the NAME of an environment variable. The value is
read at run time, sent only to the seed domains, and never written to disk or printed.
For Confluence use confluence.py (REST API) instead of crawling its HTML.

Design
- Frontier and results live in SQLite on disk -> memory is flat for 10 or 100k URLs.
- Each run is a time slice (budget.json crawl.time_slice_s) so one agent tool call never
  hangs; re-running the same command continues where it stopped.
- Concurrency adapts to memory: halves above 80% of worker_mb, grows below 50%.
- Machine-level backpressure: if free RAM gets low (budget.json memory.pause_below_*), the
  crawler pauses, then exits with status=memory_pause (resumable) instead of starving the PC.
- Runs at below-normal priority so the desktop stays responsive.
- Per-host politeness delay, robots.txt respected (disable with --no-robots).
"""
from __future__ import annotations

import argparse
import gc
import hashlib
import json
import os
import re
import socket
import sqlite3
import sys
import threading
import time
import urllib.error
import urllib.request
import urllib.robotparser
from concurrent.futures import ThreadPoolExecutor
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import urldefrag, urljoin, urlparse

sys.path.insert(0, str(Path(__file__).resolve().parent))
from common import (CACHE, STATE, append_jsonl, auth_header_from_env, budget, memory_snapshot,  # noqa: E402
                    now_iso, register_worker, rel, self_tree_mb, static_limits, unregister_worker)

SKIP_TAGS = {"script", "style", "noscript", "svg", "canvas", "template", "iframe", "head"}
BLOCK_TAGS = {"p", "div", "section", "article", "li", "tr", "br", "h1", "h2", "h3", "h4", "h5", "h6",
              "pre", "blockquote", "table", "ul", "ol", "header", "footer", "nav", "main", "dd", "dt"}
BINARY_EXT = re.compile(r"\.(png|jpe?g|gif|webp|svg|ico|pdf|zip|gz|tgz|rar|7z|exe|msi|dmg|mp[34]|avi|mov|woff2?|ttf|eot|css|js|xml|rss)(\?|$)", re.I)


class SafeRedirect(urllib.request.HTTPRedirectHandler):
    """Drop credentials when a redirect leaves the allowed domains."""

    def __init__(self, domains):
        super().__init__()
        self.domains = domains

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        new = super().redirect_request(req, fp, code, msg, headers, newurl)
        if new is not None and urlparse(newurl).netloc.lower() not in self.domains:
            for h in ("Authorization", "Cookie"):
                new.headers.pop(h, None)
                new.unredirected_hdrs.pop(h, None)
        return new


class TextExtractor(HTMLParser):
    """Streaming HTML -> text + title + links (stdlib only)."""

    def __init__(self, base: str, max_chars: int, max_links: int):
        super().__init__(convert_charrefs=True)
        self.base, self.max_chars, self.max_links = base, max_chars, max_links
        self.parts: list[str] = []
        self.size = 0
        self.links: list[str] = []
        self.title = ""
        self._skip = 0
        self._in_title = False
        self._heading = ""

    def handle_starttag(self, tag, attrs):
        if tag == "title":
            self._in_title = True
        if tag in SKIP_TAGS:
            self._skip += 1
        if tag == "a" and len(self.links) < self.max_links:
            href = dict(attrs).get("href")
            if href and not href.startswith(("mailto:", "javascript:", "tel:", "#")):
                self.links.append(urldefrag(urljoin(self.base, href))[0])
        if tag in BLOCK_TAGS:
            self._emit("\n")
        if tag in ("h1", "h2", "h3"):
            self._heading = "#" * int(tag[1]) + " "
            self._emit(self._heading)

    def handle_endtag(self, tag):
        if tag == "title":
            self._in_title = False
        if tag in SKIP_TAGS and self._skip:
            self._skip -= 1
        if tag in BLOCK_TAGS:
            self._emit("\n")

    def handle_data(self, data):
        if self._in_title:
            self.title += data.strip()
            return
        if self._skip:
            return
        t = re.sub(r"\s+", " ", data)
        if t.strip():
            self._emit(t)

    def unknown_decl(self, data):  # <![CDATA[ ... ]]> e.g. Confluence code macros
        if data.startswith("CDATA[") and not self._skip:
            self._emit("\n" + data[6:] + "\n")

    def _emit(self, s):
        if self.size < self.max_chars:
            self.parts.append(s)
            self.size += len(s)

    def text(self) -> str:
        t = "".join(self.parts)
        t = re.sub(r"[ \t]+\n", "\n", t)
        t = re.sub(r"\n{3,}", "\n\n", t)
        return t.strip()[: self.max_chars]


class Crawler:
    def __init__(self, a, cfg):
        self.a, self.cfg = a, cfg
        self.job_dir = CACHE / a.job
        self.pages = self.job_dir / "pages"
        self.pages.mkdir(parents=True, exist_ok=True)
        self.progress = STATE / "progress" / f"crawl-{a.job}.jsonl"
        self.db = sqlite3.connect(self.job_dir / "crawl.sqlite", check_same_thread=False)
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("""CREATE TABLE IF NOT EXISTS urls(
            url TEXT PRIMARY KEY, depth INT, status TEXT, http INT, title TEXT, file TEXT,
            chars INT, err TEXT, ts TEXT)""")
        self.db.execute("CREATE INDEX IF NOT EXISTS ix_status ON urls(status, depth)")
        self.db.execute("CREATE TABLE IF NOT EXISTS meta(k TEXT PRIMARY KEY, v TEXT)")
        self.db.execute("UPDATE urls SET status='queued' WHERE status='inflight'")
        self.db.commit()
        self.opts = self._load_opts()
        self.host_lock = threading.Lock()
        self.host_next: dict[str, float] = {}
        self.robots: dict[str, urllib.robotparser.RobotFileParser | None] = {}
        self.auth = auth_header_from_env(self.opts.get("auth_env"))
        self.cookie = os.environ.get(self.opts["cookie_env"]) if self.opts.get("cookie_env") else None
        if self.opts.get("cookie_env") and not self.cookie:
            sys.exit(f"ERROR: environment variable {self.opts['cookie_env']} is empty or not set.")
        self.opener = urllib.request.build_opener(SafeRedirect(self.opts["domains"]))
        self.conc = cfg["start_concurrency"]
        self.worker_mb = budget()["memory"].get("worker_mb", 1024)
        socket.setdefaulttimeout(cfg["timeout_s"])

    # ------------------------------------------------------------ options
    def _load_opts(self) -> dict:
        row = self.db.execute("SELECT v FROM meta WHERE k='opts'").fetchone()
        saved = json.loads(row[0]) if row else {}
        a = self.a
        opts = {
            "max_pages": a.max_pages or saved.get("max_pages", 500),
            "max_depth": a.max_depth if a.max_depth is not None else saved.get("max_depth", 2),
            "same_domain": (not a.any_domain) if (a.any_domain or not saved) else saved.get("same_domain", True),
            "include": a.include or saved.get("include"),
            "exclude": a.exclude or saved.get("exclude"),
            "robots": (not a.no_robots) if (a.no_robots or not saved) else saved.get("robots", True),
            "render": a.render or saved.get("render", False),
            "domains": saved.get("domains", []),
            "auth_env": a.auth_env or saved.get("auth_env"),
            "cookie_env": a.cookie_env or saved.get("cookie_env"),
        }
        seeds = list(a.seeds or [])
        if a.seeds_file:
            seeds += [ln.strip() for ln in Path(a.seeds_file).read_text(encoding="utf-8").splitlines()
                      if ln.strip() and not ln.startswith("#")]
        for s in seeds:
            host = urlparse(s).netloc.lower()
            if host and host not in opts["domains"]:
                opts["domains"].append(host)
            self.db.execute("INSERT OR IGNORE INTO urls(url, depth, status) VALUES(?,0,'queued')", (urldefrag(s)[0],))
        self.db.execute("INSERT OR REPLACE INTO meta VALUES('opts', ?)", (json.dumps(opts),))
        self.db.commit()
        if not opts["domains"]:
            sys.exit("No seeds. Pass --seeds or --seeds-file on the first run of a job.")
        return opts

    def allowed(self, url: str) -> bool:
        u = urlparse(url)
        if u.scheme not in ("http", "https") or BINARY_EXT.search(u.path):
            return False
        if self.opts["same_domain"] and u.netloc.lower() not in self.opts["domains"]:
            return False
        if self.opts["include"] and not re.search(self.opts["include"], url):
            return False
        if self.opts["exclude"] and re.search(self.opts["exclude"], url):
            return False
        return True

    def robots_ok(self, url: str) -> bool:
        if not self.opts["robots"]:
            return True
        u = urlparse(url)
        key = f"{u.scheme}://{u.netloc}"
        if key not in self.robots:
            rp = urllib.robotparser.RobotFileParser(key + "/robots.txt")
            try:
                rp.read()
            except Exception:
                rp = None  # unreachable robots -> allow
            self.robots[key] = rp
        rp = self.robots[key]
        return True if rp is None else rp.can_fetch(self.cfg["user_agent"], url)

    def polite_wait(self, url: str):
        host = urlparse(url).netloc
        with self.host_lock:
            t = max(time.time(), self.host_next.get(host, 0))
            self.host_next[host] = t + self.cfg["per_host_delay_s"]
        d = t - time.time()
        if d > 0:
            time.sleep(d)

    # ------------------------------------------------------------ fetching
    def fetch_one(self, url: str, depth: int) -> dict:
        res = {"url": url, "depth": depth, "status": "failed", "http": None, "links": []}
        try:
            if not self.robots_ok(url):
                res.update(status="skipped", err="robots.txt")
                return res
            self.polite_wait(url)
            headers = {"User-Agent": self.cfg["user_agent"], "Accept": "text/html,text/plain;q=0.9,*/*;q=0.5"}
            if urlparse(url).netloc.lower() in self.opts["domains"]:  # never leak credentials off-site
                if self.auth:
                    headers["Authorization"] = self.auth
                if self.cookie:
                    headers["Cookie"] = self.cookie
            req = urllib.request.Request(url, headers=headers)
            with self.opener.open(req, timeout=self.cfg["timeout_s"]) as r:
                res["http"] = r.status
                ctype = r.headers.get("Content-Type", "")
                if not any(t in ctype for t in ("html", "text/plain", "json", "markdown")):
                    res.update(status="skipped", err=f"type {ctype[:40]}")
                    return res
                chunks, size, cap = [], 0, self.cfg["max_page_bytes"]
                while size < cap:
                    c = r.read(min(65536, cap - size))
                    if not c:
                        break
                    chunks.append(c)
                    size += len(c)
                raw = b"".join(chunks)
                del chunks
                charset = r.headers.get_content_charset() or "utf-8"
                final_url = r.geturl()
            body = raw.decode(charset, errors="replace")
            del raw
            if "html" in ctype:
                return self._finish(res, final_url, body)
            res.update(self._save(final_url, "", body[: self.cfg["max_text_chars"]]))
        except urllib.error.HTTPError as e:
            res.update(http=e.code, err=f"HTTP {e.code}")
        except Exception as e:  # noqa: BLE001
            res.update(err=f"{type(e).__name__}: {str(e)[:120]}")
        return res

    def _finish(self, res: dict, url: str, html: str) -> dict:
        ex = TextExtractor(url, self.cfg["max_text_chars"], self.cfg["max_links_per_page"])
        try:
            ex.feed(html)
            ex.close()
        except Exception:
            pass
        del html
        res.update(self._save(url, ex.title, ex.text()))
        res["links"] = ex.links
        return res

    def _save(self, url: str, title: str, text: str) -> dict:
        h = hashlib.sha1(url.encode()).hexdigest()[:16]
        f = self.pages / f"{h}.md"
        f.write_text(f"---\nurl: {url}\ntitle: {title[:200]}\nfetched: {now_iso()}\nchars: {len(text)}\n---\n\n{text}\n",
                     encoding="utf-8")
        return {"status": "done", "title": title[:200], "file": rel(f), "chars": len(text)}

    # ------------------------------------------------------------ render mode
    def render_batch(self, rows):
        try:
            from playwright.sync_api import sync_playwright  # type: ignore
        except ImportError:
            sys.exit("--render needs Playwright: pip install playwright && python -m playwright install chromium")
        out = []
        with sync_playwright() as p:
            browser = p.chromium.launch()
            ctx = browser.new_context(user_agent=self.cfg["user_agent"])
            page = ctx.new_page()
            for i, (url, depth) in enumerate(rows):
                res = {"url": url, "depth": depth, "status": "failed", "http": None, "links": []}
                try:
                    if not self.robots_ok(url):
                        res.update(status="skipped", err="robots.txt")
                    else:
                        self.polite_wait(url)
                        resp = page.goto(url, timeout=self.cfg["timeout_s"] * 1000, wait_until="networkidle")
                        res["http"] = resp.status if resp else None
                        res = self._finish(res, page.url, page.content())
                except Exception as e:  # noqa: BLE001
                    res["err"] = f"{type(e).__name__}: {str(e)[:120]}"
                out.append(res)
                if (i + 1) % self.cfg.get("render_restart_every", 40) == 0:  # free Chromium memory
                    ctx.close()
                    ctx = browser.new_context(user_agent=self.cfg["user_agent"])
                    page = ctx.new_page()
            browser.close()
        return out

    # ------------------------------------------------------------ main loop
    def counts(self) -> dict:
        rows = self.db.execute("SELECT status, COUNT(*) FROM urls GROUP BY status").fetchall()
        return {s: n for s, n in rows}

    def adapt(self) -> dict:
        mine = self_tree_mb()
        snap = memory_snapshot()
        if mine is not None:
            if mine > 0.8 * self.worker_mb and self.conc > 1:
                self.conc = max(1, self.conc // 2)
                gc.collect()
            elif mine < 0.5 * self.worker_mb and self.conc < self.cfg["max_concurrency"]:
                self.conc += 1
        if mine is None:  # memory not measurable -> fixed conservative concurrency
            self.conc = min(self.conc, static_limits()["concurrency"])
        snap["self_mb"] = round(mine, 0) if mine is not None else None
        return snap

    def backpressure(self, snap: dict) -> bool:
        """Return True if we must stop (memory_pause)."""
        if snap.get("state") != "PAUSE":
            return False
        self.conc = 1
        gc.collect()
        for _ in range(12):  # wait up to ~60 s for memory to recover
            time.sleep(5)
            if memory_snapshot()["state"] != "PAUSE":
                return False
        return True

    def run(self) -> dict:
        start = time.time()
        slice_s = self.a.time_slice or self.cfg["time_slice_s"]
        status, last_print = "time_slice", 0
        done0 = self.counts().get("done", 0)
        with ThreadPoolExecutor(max_workers=self.cfg["max_concurrency"]) as pool:
            while True:
                c = self.counts()
                if c.get("done", 0) >= self.opts["max_pages"]:
                    status = "max_pages"; break
                if time.time() - start > slice_s:
                    status = "time_slice"; break
                take = 8 if self.opts["render"] else self.conc * 2
                rows = self.db.execute("SELECT url, depth FROM urls WHERE status='queued' ORDER BY depth, rowid LIMIT ?",
                                       (min(take, self.opts["max_pages"] - c.get("done", 0)),)).fetchall()
                if not rows:
                    status = "complete"; break
                self.db.executemany("UPDATE urls SET status='inflight' WHERE url=?", [(r[0],) for r in rows])
                self.db.commit()
                if self.opts["render"]:
                    results = self.render_batch(rows)
                else:
                    results = list(pool.map(lambda r: self.fetch_one(*r), rows))
                self._store(results)
                snap = self.adapt()
                c = self.counts()
                elapsed = time.time() - start
                rate = round((c.get("done", 0) - done0) / max(elapsed, 1e-6), 2)
                append_jsonl(self.progress, {"ts": now_iso(), "job": self.a.job, "done": c.get("done", 0),
                             "queued": c.get("queued", 0), "failed": c.get("failed", 0), "skipped": c.get("skipped", 0),
                             "rate_pps": rate, "conc": self.conc, "self_mb": snap.get("self_mb"),
                             "vscode_mb": snap.get("vscode_mb"), "mem_state": snap.get("state")})
                if c.get("done", 0) - last_print >= 50:
                    last_print = c.get("done", 0)
                    print(f"  .. done={c.get('done', 0)} queued={c.get('queued', 0)} rate={rate}/s conc={self.conc} mem={snap.get('state')}", flush=True)
                if self.backpressure(snap):
                    status = "memory_pause"; break
        c = self.counts()
        summary = {"job": self.a.job, "status": status, "done": c.get("done", 0), "queued": c.get("queued", 0),
                   "failed": c.get("failed", 0), "skipped": c.get("skipped", 0),
                   "pages_dir": rel(self.pages), "seconds": round(time.time() - start)}
        if status in ("time_slice", "memory_pause"):
            summary["next"] = f"re-run: python .agents/tools/crawl.py --job {self.a.job}"
        elif status in ("complete", "max_pages"):
            summary["next"] = f"python .agents/tools/batch.py --job {self.a.job} --add-units"
        append_jsonl(self.progress, {"ts": now_iso(), "event": "slice_end", **summary})
        return summary

    def _store(self, results: list[dict]):
        new_links = []
        for r in results:
            self.db.execute("UPDATE urls SET status=?, http=?, title=?, file=?, chars=?, err=?, ts=? WHERE url=?",
                            (r["status"], r.get("http"), r.get("title"), r.get("file"), r.get("chars"),
                             r.get("err"), now_iso(), r["url"]))
            if r["status"] == "done" and r["depth"] < self.opts["max_depth"]:
                for link in r.get("links", []):
                    if self.allowed(link):
                        new_links.append((link, r["depth"] + 1))
        # keep the frontier bounded: never queue more than 5x max_pages
        known = self.db.execute("SELECT COUNT(*) FROM urls").fetchone()[0]
        room = max(0, self.opts["max_pages"] * 5 - known)
        self.db.executemany("INSERT OR IGNORE INTO urls(url, depth, status) VALUES(?,?,'queued')", new_links[:room])
        self.db.commit()


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--job", required=True, help="job name (folder under .agents/cache/)")
    p.add_argument("--seeds", nargs="*")
    p.add_argument("--seeds-file")
    p.add_argument("--max-pages", type=int)
    p.add_argument("--max-depth", type=int)
    p.add_argument("--any-domain", action="store_true", help="follow links off the seed domains")
    p.add_argument("--include", help="regex URLs must match")
    p.add_argument("--exclude", help="regex URLs must not match")
    p.add_argument("--no-robots", action="store_true")
    p.add_argument("--render", action="store_true", help="render JS with Playwright (sequential, more memory)")
    p.add_argument("--time-slice", type=int, help="seconds for this run (default budget.json)")
    p.add_argument("--auth-env", help="NAME of env var holding a token ('Bearer x', 'user:token' or raw token)")
    p.add_argument("--cookie-env", help="NAME of env var holding a Cookie header value (SSO session)")
    a = p.parse_args()
    cfg = budget().get("crawl", {})
    import memprobe
    memprobe.lower_priority()
    register_worker(os.getpid(), f"crawl:{a.job}")
    try:
        c = Crawler(a, cfg)
        print(f"crawl {a.job}: slice={a.time_slice or cfg['time_slice_s']}s max_pages={c.opts['max_pages']} depth={c.opts['max_depth']}", flush=True)
        print(json.dumps(c.run()))
    finally:
        unregister_worker(os.getpid())


if __name__ == "__main__":
    main()
