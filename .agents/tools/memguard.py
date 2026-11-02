#!/usr/bin/env python3
"""Run any heavy command under a memory cap, or report memory use.

  memguard.py --report                         # VS Code + workers vs the 4 GB budget
  memguard.py -- python my_script.py           # cap = budget.json worker_mb
  memguard.py --mb 512 --timeout 600 -- npm test

The command's whole process tree is polled every second. Over the cap -> the tree is
killed and exit code 137 is returned with a one-line reason, so the agent can retry
with smaller batches instead of freezing the machine. Output is tail-truncated so a
noisy build cannot flood the chat context.
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from collections import deque
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from common import budget, memory_snapshot, psutil, register_worker, unregister_worker  # noqa: E402


def kill_tree(pid: int):
    if psutil:
        try:
            parent = psutil.Process(pid)
            for c in parent.children(recursive=True):
                c.kill()
            parent.kill()
            return
        except Exception:
            pass
    try:
        if os.name == "nt":
            subprocess.run(["taskkill", "/F", "/T", "/PID", str(pid)], capture_output=True)
        else:
            os.kill(pid, 9)
    except Exception:
        pass


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--report", action="store_true")
    p.add_argument("--mb", type=int, help="cap in MB (default budget.json memory.worker_mb)")
    p.add_argument("--timeout", type=int, default=0, help="seconds, 0 = none")
    p.add_argument("--tail", type=int, default=40, help="output lines to keep")
    p.add_argument("cmd", nargs=argparse.REMAINDER)
    a = p.parse_args()

    if a.report or not a.cmd:
        print(json.dumps(memory_snapshot()))
        return
    cmd = a.cmd[1:] if a.cmd[0] == "--" else a.cmd
    cap = a.mb or budget()["memory"].get("worker_mb", 1024)
    if not psutil:
        print("WARN: psutil missing -> cannot enforce the memory cap (pip install psutil). Running uncapped.", file=sys.stderr)

    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, errors="replace",
                            bufsize=1)
    register_worker(proc.pid, "memguard:" + " ".join(cmd)[:60])
    tail: deque[str] = deque(maxlen=a.tail)
    peak, reason, start = 0.0, None, time.time()

    import threading

    def reader():
        for line in proc.stdout:  # type: ignore[union-attr]
            tail.append(line.rstrip("\n")[:400])
    t = threading.Thread(target=reader, daemon=True)
    t.start()
    try:
        while proc.poll() is None:
            if psutil:
                try:
                    pr = psutil.Process(proc.pid)
                    rss = sum((c.memory_info().rss for c in [pr] + pr.children(recursive=True)), 0) / 1_048_576
                    peak = max(peak, rss)
                    if rss > cap:
                        reason = f"memory {rss:.0f}MB > cap {cap}MB"
                except Exception:
                    pass
            if a.timeout and time.time() - start > a.timeout:
                reason = f"timeout {a.timeout}s"
            if reason:
                kill_tree(proc.pid)
                break
            time.sleep(1)
        proc.wait()
        t.join(timeout=2)
    finally:
        unregister_worker(proc.pid)
    print("\n".join(tail))
    code = 137 if reason else proc.returncode
    print(f"[memguard] exit={code} peak={peak:.0f}MB cap={cap}MB seconds={time.time() - start:.0f}"
          + (f" KILLED: {reason} -> retry with a smaller batch/--mb" if reason else ""))
    sys.exit(code)


if __name__ == "__main__":
    main()
