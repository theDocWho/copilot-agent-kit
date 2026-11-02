#!/usr/bin/env python3
"""Split crawled pages (or digests) into token-bounded batches for cheap Haiku
digest units, then add those units to the ledger (map -> reduce).

  batch.py --job docs --plan-only                         # show batch count / tokens, change nothing
  batch.py --job docs --add-units --question "What auth methods does the API support?"
  batch.py --job docs --source digests --round 2 --add-units   # reduce round when digests are still too big

Each batch fits budget.json batching.max_tokens_per_batch and max_files_per_batch, so a
Fetcher (Haiku) subagent reads it in one bounded context and writes one digest file.
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from common import CACHE, ROOT, budget, rel, save_json  # noqa: E402


def sources(job: str, source: str, rnd: int) -> list[tuple[str, int]]:
    jd = CACHE / job
    if source == "pages":
        db = jd / "crawl.sqlite"
        if not db.exists():
            sys.exit(f"No crawl db for job {job}. Run crawl.py first.")
        con = sqlite3.connect(db)
        rows = con.execute("SELECT file, chars FROM urls WHERE status='done' AND file IS NOT NULL AND chars > 40 ORDER BY depth, rowid").fetchall()
        return [(f, c) for f, c in rows if (ROOT / f).exists()]
    d = jd / "digests" / f"r{rnd - 1}"
    files = sorted(d.glob("*.md")) if d.exists() else []
    if not files:
        sys.exit(f"No digests in {rel(d)}")
    return [(rel(f), f.stat().st_size) for f in files]


def make_batches(items, max_tokens, max_files, cpt):
    batches, cur, cur_chars = [], [], 0
    for f, chars in items:
        chars = min(chars, max_tokens * cpt)  # single huge file still gets its own batch
        if cur and (len(cur) >= max_files or (cur_chars + chars) / cpt > max_tokens):
            batches.append((cur, cur_chars))
            cur, cur_chars = [], 0
        cur.append(f)
        cur_chars += chars
    if cur:
        batches.append((cur, cur_chars))
    return batches


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--job", required=True)
    p.add_argument("--source", choices=["pages", "digests"], default="pages")
    p.add_argument("--round", type=int, default=1, help="1 = pages; 2+ = digests of previous round")
    p.add_argument("--question", default="", help="what to extract (defaults to the task goal)")
    p.add_argument("--plan-only", action="store_true")
    p.add_argument("--add-units", action="store_true")
    a = p.parse_args()
    if a.source == "digests" and a.round < 2:
        a.round = 2
    bc = budget().get("batching", {})
    cpt, max_tok, max_files = bc.get("chars_per_token", 4), bc.get("max_tokens_per_batch", 30000), bc.get("max_files_per_batch", 12)
    items = sources(a.job, a.source, a.round)
    total_tok = sum(c for _, c in items) // cpt
    batches = make_batches(items, max_tok, max_files, cpt)
    print(f"{a.job} round {a.round}: {len(items)} files, ~{total_tok:,} tokens -> {len(batches)} batches "
          f"(<= {max_tok:,} tok / {max_files} files each)")
    if total_tok <= bc.get("reduce_when_digest_tokens_over", 40000) and a.source == "digests":
        print("SMALL_ENOUGH: write the final synthesis directly from these digests (no more rounds).")
    if a.plan_only:
        return

    bdir = CACHE / a.job / "batches" / f"r{a.round}"
    ddir = CACHE / a.job / "digests" / f"r{a.round}"
    bdir.mkdir(parents=True, exist_ok=True)
    ddir.mkdir(parents=True, exist_ok=True)
    units = []
    q = a.question or "the task goal stated in the ledger (ledger.py show --brief)"
    for i, (files, chars) in enumerate(batches, 1):
        bf = bdir / f"batch-{i:03d}.json"
        save_json(bf, {"job": a.job, "round": a.round, "files": files, "est_tokens": chars // cpt})
        out = ddir / f"batch-{i:03d}.md"
        units.append({
            "id": f"D-{a.job}-r{a.round}-{i:03d}", "agent": "Fetcher",
            "title": f"Digest {a.job} round {a.round} batch {i}/{len(batches)}",
            "instructions": (f"Read the file list in {rel(bf)}, then read each listed file once. Extract only facts relevant to: {q}. "
                             f"Write {rel(out)}: one '## <source url or file>' section per useful source with terse bullet facts, "
                             "numbers, names and short quotes (<15 words). Skip irrelevant sources entirely. Max ~1,200 words total. "
                             "Do not use web/fetch here; everything is already on disk."),
            "inputs": [rel(bf)], "outputs": [rel(out)],
            "acceptance": "Digest exists, every bullet traceable to a listed source, no raw page dumps.",
            "est": {"calls": len(files) + 3, "ctx_ktok": round(chars / cpt / 1000) + 3, "out_ktok": 2},
        })
    reduce_id = f"R-{a.job}-r{a.round}"
    final = CACHE / a.job / "summary.md"
    units.append({
        "id": reduce_id, "agent": "Fetcher", "depends_on": [u["id"] for u in units],
        "title": f"Reduce {a.job} round {a.round}",
        "instructions": (f"Run `python .agents/tools/batch.py --job {a.job} --source digests --round {a.round + 1} --plan-only`. "
                         f"If it prints SMALL_ENOUGH: read every file in {rel(ddir)} and write {rel(final)} (structured findings, "
                         "sources list, open gaps; max ~1,500 words). Otherwise run the same command with --add-units instead of "
                         "--plan-only (that schedules the next round) and finish with status done and summary 'next round scheduled'."),
        "inputs": [rel(ddir)], "outputs": [rel(final)],
        "acceptance": "summary.md written OR next reduce round scheduled.",
        "est": {"calls": min(len(batches) + 4, 20), "ctx_ktok": min(len(batches) * 2 + 4, 40), "out_ktok": 3},
    })
    out_file = bdir / "units.json"
    save_json(out_file, units)
    if a.add_units:
        r = subprocess.run([sys.executable, str(Path(__file__).with_name("ledger.py")), "add", "--file", str(out_file)],
                           capture_output=True, text=True)
        print((r.stdout or r.stderr).strip()[:600])
        if r.returncode:
            sys.exit(r.returncode)
    else:
        print(f"units written to {rel(out_file)} (use --add-units to put them in the ledger)")


if __name__ == "__main__":
    main()
