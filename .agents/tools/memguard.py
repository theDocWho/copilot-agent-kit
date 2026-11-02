#!/usr/bin/env python3
"""Run any command under a memory cap and a safety check, or report memory use.
This is the kit's hook-free guard: agents run every non-kit command through it.

  memguard.py --report                               # VS Code + workers vs the 4 GB budget
  memguard.py -- python my_script.py                 # cap = budget.json worker_mb
  memguard.py --mb 512 --timeout 600 -- npm test
  memguard.py --shell "pytest -q tests/unit && ruff check ."   # compound command via the OS shell
  memguard.py --pwsh 'Get-ChildItem -Recurse *.tf | Measure-Object | Select -Expand Count'
  memguard.py --pwsh-file .agents/state/scripts/U03-inventory.ps1 -- arg1 arg2

PowerShell: pwsh 7 if installed, otherwise Windows PowerShell 5.1. Code is sent as an encoded
command, so the calling terminal can't mangle quotes or $variables and execution policy (which
only blocks .ps1 files) doesn't apply. Errors stop the script with exit 1, a failing native
program's exit code is passed through. For anything longer than a one-liner, write the
script to a file and use --pwsh-file. In a PowerShell terminal, wrap --pwsh code in single quotes.

1. Safety: the command line is checked against the deny list in common.py (destructive
   deletes, force-push, reset --hard, curl|sh, printing secrets, dumping env). Denied
   commands are not run (exit 126) and the reason is printed.
2. Memory cap, best mechanism available:
   - Windows: a Job Object hard-limits the whole process tree; Windows itself stops it at the cap.
   - Otherwise: the tree is polled every second (psutil, /proc, ps, tasklist) and killed when over.
   - If memory cannot be read at all: runs with the timeout only and says so.
   Killed -> exit 137 with the reason, so the agent retries with a smaller batch.
3. Output is tail-truncated so a noisy build cannot flood the chat context.
"""
from __future__ import annotations

import argparse
import base64
import json
import os
import shutil
import tempfile
import signal
import subprocess
import sys
import threading
import time
from collections import deque
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import memprobe  # noqa: E402
from common import STATE, budget, deny_reason, log_call, memory_snapshot, register_worker, unregister_worker  # noqa: E402


def find_powershell() -> str | None:
    """pwsh (PowerShell 7) first, then Windows PowerShell 5.1."""
    for name in ("pwsh", "powershell"):
        p = shutil.which(name)
        if p:
            return p
    if os.name == "nt":
        p = Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32" / "WindowsPowerShell" / "v1.0" / "powershell.exe"
        if p.exists():
            return str(p)
    return None


def build_powershell(code: str, args: list[str]) -> tuple[list[str], str | None]:
    """Run PowerShell code robustly:
    - sent as -EncodedCommand (base64 UTF-16LE): no quoting problems from the calling terminal, and
      execution policy does not apply to commands (it only restricts .ps1 files);
    - UTF-8 output, no progress bars, errors stop the script and become exit code 1;
    - a native program's non-zero $LASTEXITCODE becomes the exit code; `exit N` still works;
    - very long scripts (Windows 32k command-line limit) fall back to a temp .ps1 with -File."""
    exe = find_powershell()
    if not exe:
        raise FileNotFoundError("PowerShell not found (looked for pwsh and powershell)")
    quoted = " ".join("'" + x.replace("'", "''") + "'" for x in args)
    wrapped = ("$ProgressPreference='SilentlyContinue'\n"
               "try { [Console]::OutputEncoding = [System.Text.Encoding]::UTF8 } catch {}\n"
               "$ErrorActionPreference='Stop'\n"
               "$global:LASTEXITCODE = 0\n"
               "try {\n& {\n" + code + "\n} " + quoted + "\n"
               "if ($global:LASTEXITCODE) { exit $global:LASTEXITCODE }\nexit 0\n"
               "} catch {\nWrite-Output ('ERROR: ' + $_.Exception.Message)\n"
               "if ($_.InvocationInfo.PositionMessage) { Write-Output $_.InvocationInfo.PositionMessage }\nexit 1\n}\n")
    base = [exe, "-NoProfile", "-NonInteractive"]
    enc = base64.b64encode(wrapped.encode("utf-16-le")).decode("ascii")
    if len(enc) < 30000:
        return base + ["-EncodedCommand", enc], None
    fd, path = tempfile.mkstemp(suffix=".ps1", prefix="kit-", dir=str(STATE))
    with os.fdopen(fd, "w", encoding="utf-8-sig") as f:
        f.write(wrapped)
    return base + ["-ExecutionPolicy", "Bypass", "-File", path], path


def kill_tree(proc: subprocess.Popen) -> None:
    pids = memprobe.descendants(proc.pid)
    try:
        if os.name == "nt":
            subprocess.run(["taskkill", "/F", "/T", "/PID", str(proc.pid)], capture_output=True,
                           creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        else:
            try:
                os.killpg(proc.pid, signal.SIGKILL)  # we started it in its own group
            except Exception:
                proc.kill()
            for p in pids:
                try:
                    os.kill(p, signal.SIGKILL)
                except Exception:
                    pass
    except Exception:
        proc.kill()


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--report", action="store_true")
    p.add_argument("--mb", type=int, help="cap in MB (default budget.json memory.worker_mb)")
    p.add_argument("--timeout", type=int, default=0, help="seconds, 0 = none")
    p.add_argument("--tail", type=int, default=40, help="output lines to keep")
    p.add_argument("--shell", help="run this command string through the OS shell (cmd.exe on Windows, sh elsewhere)")
    p.add_argument("--pwsh", help="run this PowerShell code (pwsh 7 if installed, else Windows PowerShell 5.1)")
    p.add_argument("--pwsh-file", help="run a .ps1 file's code (sent as an encoded command: not blocked by execution policy)")
    p.add_argument("cmd", nargs=argparse.REMAINDER)
    a = p.parse_args()

    if a.report or not (a.cmd or a.shell or a.pwsh or a.pwsh_file):
        snap = memory_snapshot()
        snap["backend_errors"] = memprobe.backend_errors()
        snap["powershell"] = find_powershell()
        print(json.dumps(snap))
        return
    cmd = a.cmd[1:] if a.cmd and a.cmd[0] == "--" else a.cmd
    tmp_ps1 = None
    if a.pwsh or a.pwsh_file:
        code = a.pwsh if a.pwsh else Path(a.pwsh_file).read_text(encoding="utf-8-sig")
        line = code if a.pwsh else f"[{a.pwsh_file}] " + code
        why = deny_reason(code)
        if not why:
            try:
                cmd, tmp_ps1 = build_powershell(code, cmd)
            except FileNotFoundError as e:
                print(f"[memguard] NOT RUN: {e}")
                sys.exit(127)
        a.shell = None
    else:
        line = a.shell or subprocess.list2cmdline(cmd) if os.name == "nt" else (a.shell or " ".join(cmd))
        why = deny_reason(line)
    log_call("memguard", ("DENIED " if why else "") + line)
    if why:
        print(f"[memguard] NOT RUN: blocked by the kit's safety check ({why}). "
              "If this is really intended, ask the user to run it themselves.")
        sys.exit(126)
    cap = a.mb or budget()["memory"].get("worker_mb", 1024)

    kw = dict(stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, errors="replace", bufsize=1)
    if os.name == "nt":  # below-normal priority keeps the desktop responsive
        kw["creationflags"] = getattr(subprocess, "CREATE_NO_WINDOW", 0) | 0x00004000
    else:
        kw["start_new_session"] = True
        kw["preexec_fn"] = lambda: os.nice(10)
    proc = subprocess.Popen(a.shell if a.shell else cmd, shell=bool(a.shell), **kw)

    job, mode = None, "poll"
    if os.name == "nt":
        try:
            job = memprobe.WinJob(cap * 1_048_576)
            job.assign(proc.pid)
            mode = "job-object"
        except Exception as e:  # nested-job or policy restrictions -> fall back to polling
            job, mode = None, f"poll (job object unavailable: {str(e)[:60]})"
    if mode.startswith("poll") and memprobe.tree_bytes(proc.pid) is None and memprobe.backend() == "none":
        mode = "timeout-only (memory not measurable on this machine)"
    register_worker(proc.pid, "memguard:" + line[:60])

    tail: deque[str] = deque(maxlen=a.tail)

    def reader():
        for ln in proc.stdout:  # type: ignore[union-attr]
            tail.append(ln.rstrip("\n")[:400])
    t = threading.Thread(target=reader, daemon=True)
    t.start()
    peak, reason, start = 0.0, None, time.time()
    try:
        while proc.poll() is None:
            if mode.startswith("poll"):
                b = memprobe.tree_bytes(proc.pid)
                if b is not None:
                    rss = b / 1_048_576
                    peak = max(peak, rss)
                    if rss > cap:
                        reason = f"memory {rss:.0f}MB > cap {cap}MB"
            if a.timeout and time.time() - start > a.timeout:
                reason = f"timeout {a.timeout}s"
            if reason:
                kill_tree(proc)
                break
            time.sleep(1)
        proc.wait()
        t.join(timeout=2)
        if job:
            peak = job.peak_bytes() / 1_048_576
            if proc.returncode != 0 and peak >= cap * 0.97:
                reason = f"memory hit the {cap}MB job limit (Windows stopped the allocation)"
    finally:
        if job:
            job.close()
        unregister_worker(proc.pid)
        if tmp_ps1:
            Path(tmp_ps1).unlink(missing_ok=True)
    print("\n".join(tail))
    code = 137 if reason else proc.returncode
    print(f"[memguard] exit={code} peak={peak:.0f}MB cap={cap}MB mode={mode} seconds={time.time() - start:.0f}"
          + (f" KILLED: {reason} -> retry with a smaller batch/--mb" if reason else ""))
    sys.exit(code)


if __name__ == "__main__":
    main()
