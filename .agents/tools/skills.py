#!/usr/bin/env python3
"""Find skills and playbooks that match a task, cheaply (no file is read in full).

  skills.py                                  # list every skill/playbook: name, path, description
  skills.py --match "threat model the payments service"   # ranked matches
  skills.py --outline threat-model           # headings of one skill/playbook (to cite sections)

Looks in: .github/skills/*/SKILL.md, .claude/skills/*/SKILL.md, .agents/playbooks/*.md,
~/.copilot/skills/*/SKILL.md, ~/.claude/skills/*/SKILL.md
"""
from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from common import ROOT, rel  # noqa: E402

STOP = set("a an and the of to for in on with by from is are be this that it as at or into using use your my our".split())


def discover() -> list[dict]:
    pats = [ROOT / ".github/skills", ROOT / ".claude/skills", Path.home() / ".copilot/skills", Path.home() / ".claude/skills"]
    found = []
    for base in pats:
        if base.is_dir():
            for f in sorted(base.glob("*/SKILL.md")):
                found.append(parse(f, "skill"))
    pb = ROOT / ".agents/playbooks"
    if pb.is_dir():
        for f in sorted(pb.glob("*.md")):
            found.append(parse(f, "playbook"))
    return found


def parse(f: Path, kind: str) -> dict:
    head = f.read_text(encoding="utf-8", errors="replace")[:4000]
    fm = re.match(r"---\s*\n(.*?)\n---", head, re.S)
    meta = {}
    if fm:
        for line in fm.group(1).splitlines():
            m = re.match(r"(\w[\w-]*):\s*(.*)", line)
            if m:
                meta[m.group(1)] = m.group(2).strip().strip("'\"")
    name = meta.get("name") or (f.parent.name if f.name == "SKILL.md" else f.stem)
    desc = meta.get("description") or next((ln.strip("# ").strip() for ln in head.splitlines()[1:] if ln.strip()), "")
    return {"kind": kind, "name": name, "path": rel(f), "description": desc[:300]}


def words(s: str) -> set[str]:
    return {w for w in re.findall(r"[a-z0-9]+", s.lower()) if w not in STOP and len(w) > 2}


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--match")
    p.add_argument("--outline")
    a = p.parse_args()
    items = discover()
    if a.outline:
        it = next((i for i in items if i["name"] == a.outline or a.outline in i["path"]), None)
        if not it:
            sys.exit(f"No skill/playbook named {a.outline}")
        print(f"{it['kind']} {it['name']}: {it['path']}")
        for n, line in enumerate((ROOT / it["path"]).read_text(encoding="utf-8", errors="replace").splitlines(), 1):
            if re.match(r"#{1,4} ", line):
                print(f"  L{n:<5} {line}")
        return
    if a.match:
        q = words(a.match)
        scored = []
        for i in items:
            w = words(i["name"].replace("-", " ") + " " + i["description"])
            score = len(q & w) + 2 * len(q & words(i["name"].replace("-", " ")))
            if score:
                scored.append((score, i))
        scored.sort(key=lambda x: -x[0])
        if not scored:
            print("NO_MATCH: plan from the request alone.")
        for s, i in scored[:5]:
            print(f"{s:>3}  {i['kind']:<8} {i['name']:<24} {i['path']}\n     {i['description'][:200]}")
        return
    if not items:
        print("No skills or playbooks found.")
    for i in items:
        print(f"{i['kind']:<8} {i['name']:<24} {i['path']}\n         {i['description'][:200]}")


if __name__ == "__main__":
    main()
