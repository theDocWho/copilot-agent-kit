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
import memprobe  # noqa: E402
from common import ROOT, budget, deny_reason, memory_snapshot, psutil  # noqa: E402

ok = True


def check(cond: bool, msg: str, fix: str = ""):
    global ok
    print(("  OK   " if cond else "  FAIL ") + msg + ("" if cond or not fix else f"\n         fix: {fix}"))
    ok &= cond


def warn(msg: str):
    print("  WARN " + msg)


print(f"agent kit doctor — workspace {ROOT}")
check(sys.version_info >= (3, 9), f"python {sys.version.split()[0]} (need 3.9+; 3.13 tested)")
be = memprobe.backend()
if be == "none":
    warn("memory NOT measurable (" + "; ".join(memprobe.backend_errors()) + ") -> static limits from budget.json "
         "memory.static_* will be used. Try `pip install psutil`.")
else:
    print(f"  OK   memory measurement via {be}" + ("" if psutil else " (psutil not installed; built-in fallback in use)"))
if sys.platform == "win32":
    try:
        j = memprobe.WinJob(64 * 1_048_576); j.close()
        print("  OK   Windows Job Objects available (memguard hard memory cap)")
    except Exception as e:  # noqa: BLE001
        warn(f"Job Objects unavailable ({e}); memguard will poll and kill instead")
check(shutil.which("git") is not None, "git on PATH (Reviewer uses git diff)")
try:
    import pypdf  # noqa: F401
    print("  OK   pypdf installed (PDF text for convert.py)")
except ImportError:
    print("  WARN pypdf missing -> convert.py cannot read PDFs" + (" (pdftotext found, will use it)" if shutil.which("pdftotext") else " (pip install pypdf)"))
for a in ("orchestrator", "planner", "fetcher", "implementer", "reviewer", "monitor"):
    f = ROOT / ".github" / "agents" / f"{a}.agent.md"
    check(f.exists(), f".github/agents/{a}.agent.md")
check(deny_reason("git push --force origin main") is not None, "memguard safety check blocks a force-push (works without hooks)")
r = subprocess.run([sys.executable, str(Path(__file__).with_name("memguard.py")), "--", sys.executable, "-c", "print('ok')"],
                   capture_output=True, text=True)
check(r.returncode == 0 and "ok" in r.stdout, "memguard runs a command")
sys.path.insert(0, str(Path(__file__).resolve().parent))
from memguard import find_powershell  # noqa: E402
ps = find_powershell()
if ps:
    r = subprocess.run([sys.executable, str(Path(__file__).with_name("memguard.py")), "--timeout", "60", "--pwsh",
                        "$x = 'o' + 'k'; Write-Output $x; cmd /c exit 3" if sys.platform == "win32" else "$x = 'o' + 'k'; Write-Output $x; exit 3"],
                       capture_output=True, text=True)
    check("ok" in r.stdout and r.returncode == 3, f"PowerShell via memguard ({ps}): output + exit code pass through",
          f"got exit {r.returncode}: {(r.stdout or r.stderr).strip()[-200:]}")
    r = subprocess.run([sys.executable, str(Path(__file__).with_name("memguard.py")), "--timeout", "60", "--pwsh",
                        "Get-Item 'C:/definitely/not/here/x' | Out-Null; Write-Output 'should-not-print'"], capture_output=True, text=True)
    check(r.returncode == 1 and "should-not-print" not in r.stdout, "PowerShell errors stop the script (exit 1)")
else:
    warn("PowerShell not found: memguard --pwsh unavailable (cmd/sh via --shell still works)")
print("  INFO hooks are optional. If your org disables them, the guard and memory gate still work through "
      "memguard.py and ledger.py; only the extra per-tool telemetry is lost.")

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
        if s.get("chat.agent.maxRequests") is None:
            warn("chat.agent.maxRequests not set in workspace settings")
    except json.JSONDecodeError:
        warn(".vscode/settings.json could not be parsed (comments?) — check maxRequests manually")
else:
    check(False, ".vscode/settings.json present", "copy the kit's .vscode/settings.json (merge with yours)")

m = b.get("memory", {})
sm = memprobe.system_memory()
if sm:
    print(f"  OK   machine memory readable: {sm['available_mb']} of {sm['total_mb']} MB free"
          + (f", commit charge {sm['commit_pct']}%" if sm.get("commit_pct") is not None else "")
          + f" (pause below {m.get('pause_below_available_mb')} MB / {m.get('pause_below_available_pct')}%)")
else:
    warn("machine memory not readable -> static limits; the watchdog cannot protect this machine")
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
