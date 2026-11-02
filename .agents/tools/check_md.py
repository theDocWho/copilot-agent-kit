#!/usr/bin/env python3
"""Deterministic done-gate for document outputs (use it in a unit's `verify`).

  check_md.py threat-model/system-model.md --sections "Components,Data flows,Trust boundaries" --mermaid
  check_md.py threat-model/THREAT_MODEL.md --min-words 800 --citations 10

Fails (exit 1) when: the file is missing; a required heading is absent (case-insensitive,
any level); fewer words than --min-words; fewer source citations than --citations
(URLs, file:line refs or [source] tags); --mermaid and no ```mermaid block; or leftover
placeholders (TODO, TBD, <fill>, lorem ipsum).
"""
import argparse
import re
import sys
from pathlib import Path


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("file")
    p.add_argument("--sections", default="")
    p.add_argument("--min-words", type=int, default=0)
    p.add_argument("--citations", type=int, default=0)
    p.add_argument("--mermaid", action="store_true")
    a = p.parse_args()
    f = Path(a.file)
    if not f.is_file():
        sys.exit(f"FAIL missing {a.file}")
    t = f.read_text(encoding="utf-8", errors="replace")
    heads = [h.strip().lower() for h in re.findall(r"(?m)^#{1,6}\s+(.+)$", t)]
    problems = []
    for s in [x.strip().lower() for x in a.sections.split(",") if x.strip()]:
        if not any(s in h for h in heads):
            problems.append(f"missing section '{s}'")
    words = len(re.findall(r"\w+", t))
    if words < a.min_words:
        problems.append(f"only {words} words (< {a.min_words})")
    cites = len(re.findall(r"https?://\S+|[\w./-]+\.\w+:\d+|\[source[^\]]*\]", t))
    if cites < a.citations:
        problems.append(f"only {cites} citations (< {a.citations})")
    if a.mermaid and "```mermaid" not in t:
        problems.append("no ```mermaid diagram")
    ph = re.findall(r"\b(TODO|TBD|FIXME)\b|<fill[^>]*>|lorem ipsum", t, re.I)
    if ph:
        problems.append(f"{len(ph)} placeholder(s) left (TODO/TBD/<fill>)")
    if problems:
        print("FAIL " + a.file + ": " + "; ".join(problems))
        sys.exit(1)
    print(f"OK {a.file}: {words} words, {cites} citations, {len(heads)} headings")


if __name__ == "__main__":
    main()
