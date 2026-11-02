#!/usr/bin/env python3
"""Check that the agent kit is wired up: python, psutil, git, agent files, hooks, settings, budgets."""
from __future__ import annotations

import json
import re
import shutil
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from common import ROOT, budget, memory_snapshot, psutil  # noqa: E402

ok = True


def check(cond: bool, msg: str, fix: str = ""):
    global ok
    print(("  OK   " if cond else "  FAIL ") + msg + ("" if cond or not fix else f"\n         fix: {fix}"))
    ok &= cond


def warn(msg: str):
    print("  WARN " + msg)


print(f"agent kit doctor — workspace {ROOT}")
check(sys.version_info >= (3, 9), f"python {sys.version.split()[0]} (need 3.9+)")
check(psutil is not None, "psutil installed (memory measurement + caps)", "pip install psutil")
check(shutil.which("git") is not None, "git on PATH (Reviewer uses git diff)")
for a in ("orchestrator", "planner", "fetcher", "implementer", "reviewer", "monitor"):
    f = ROOT / ".github" / "agents" / f"{a}.agent.md"
    check(f.exists(), f".github/agents/{a}.agent.md")
check((ROOT / ".github" / "hooks" / "agent-kit.json").exists(), ".github/hooks/agent-kit.json")
r = subprocess.run([sys.executable, str(Path(__file__).with_name("hooks.py")), "preToolUse"],
                   input='{"toolName":"runInTerminal","toolArgs":{"command":"git push --force"}}',
                   capture_output=True, text=True)
check('"deny"' in r.stdout, "guard hook blocks a force-push (dry run)")

settings = ROOT / ".vscode" / "settings.json"
b = budget()
if settings.exists():
    txt = re.sub(r"(?m)(^|\s)//\s.*$", "", settings.read_text(encoding="utf-8"))
    try:
        s = json.loads(re.sub(r",(\s*[}\]])", r"\1", txt))
        mr = s.get("chat.agent.maxRequests")
        want = b.get("agents", {}).get("max_requests_setting")
        check(mr == want, f"chat.agent.maxRequests ({mr}) == budget.json agents.max_requests_setting ({want})",
              "make them equal, or the turn budget is computed wrong")
        check(s.get("chat.useHooks") is True, "chat.useHooks enabled")
    except json.JSONDecodeError:
        warn(".vscode/settings.json could not be parsed (comments?) — check maxRequests manually")
else:
    check(False, ".vscode/settings.json present", "copy the kit's .vscode/settings.json (merge with yours)")

m = b.get("memory", {})
check(m.get("vscode_soft_limit_mb", 0) + m.get("worker_mb", 0) <= m.get("total_mb", 0),
      f"memory split: VS Code {m.get('vscode_soft_limit_mb')} + workers {m.get('worker_mb')} <= total {m.get('total_mb')} MB")
import os  # noqa: E402
conf = [v for v in ("CONFLUENCE_URL", "CONFLUENCE_TOKEN") if not os.environ.get(v)]
print("  INFO Confluence credentials: " + ("set" if not conf else f"missing {', '.join(conf)} (only needed for confluence.py)")
      + ("; Cloud mode (CONFLUENCE_EMAIL set)" if os.environ.get("CONFLUENCE_EMAIL") else ""))
snap = memory_snapshot()
print(f"  INFO memory now: {json.dumps(snap)}")
if psutil and snap.get("vscode_mb") == 0:
    warn("no VS Code processes found by name; add your process names to budget.json memory.vscode_process_names")
print("\nAll good." if ok else "\nFix the FAIL lines above.")
sys.exit(0 if ok else 1)
