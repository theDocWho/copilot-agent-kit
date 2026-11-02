#!/usr/bin/env python3
"""Emergency brake: stop the kit's own processes before the computer runs out of memory.

  watchdog.py            # run in the foreground (VS Code task "Agents: memory watchdog")
  watchdog.py --ensure   # start it in the background if it isn't running (ledger.py turn-start does this)
  watchdog.py --stop

Every few seconds it reads whole-machine memory. If available RAM drops below
watchdog_kill_below_available_mb (or commit charge reaches commit_pause_pct on Windows) it:
  1. kills the kit's worker processes (crawlers, converters, anything run via memguard),
     largest first, until the pressure is gone;
  2. writes .agents/state/memory_stop.json, so next-wave stops dispatching subagents and
     tells the Orchestrator to checkpoint, until RAM recovers.
It also kills any single worker that grows past 1.5 x worker_mb.

It never touches VS Code or anything the kit did not start. It runs at low priority, uses a
few MB, and exits by itself after 3 hours without a turn-start heartbeat.
"""
from __future__ import annotations

import argparse
import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import memprobe  # noqa: E402
from common import STATE, append_jsonl, budget, load_json, now_iso, save_json, unregister_worker  # noqa: E402

PID_FILE = STATE / "watchdog.pid"
HEARTBEAT = STATE / "watchdog.heartbeat"
LOG = STATE / "watchdog.jsonl"


def alive(pid: int) -> bool:
    if pid <= 0:
        return False
    procs = memprobe.list_procs(max_age=0)
    if procs is not None:
        return any(p == pid for p, *_ in procs)
    try:
        os.kill(pid, 0)
        return True
    except Exception:
        return False


def running_pid() -> int | None:
    try:
        pid = int(PID_FILE.read_text().strip())
        return pid if alive(pid) else None
    except Exception:
        return None


def kill(pid: int) -> None:
    try:
        if os.name == "nt":
            subprocess.run(["taskkill", "/F", "/T", "/PID", str(pid)], capture_output=True,
                           creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        else:
            for c in memprobe.descendants(pid):
                try:
                    os.kill(c, signal.SIGKILL)
                except Exception:
                    pass
            os.kill(pid, signal.SIGKILL)
    except Exception:
        pass


def ensure() -> None:
    HEARTBEAT.write_text(str(time.time()))
    if not budget().get("memory", {}).get("watchdog", True):
        print("WATCHDOG disabled in budget.json")
        return
    pid = running_pid()
    if pid:
        print(f"WATCHDOG running (pid {pid})")
        return
    kw: dict = dict(stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, close_fds=True)
    if os.name == "nt":
        kw["creationflags"] = 0x00000008 | 0x00000200 | 0x08000000 | 0x00004000  # DETACHED|NEW_GROUP|NO_WINDOW|BELOW_NORMAL
    else:
        kw["start_new_session"] = True
    p = subprocess.Popen([sys.executable, str(Path(__file__).resolve())], **kw)
    print(f"WATCHDOG started (pid {p.pid})")


def loop() -> None:
    memprobe.lower_priority()
    PID_FILE.write_text(str(os.getpid()))
    if not HEARTBEAT.exists():
        HEARTBEAT.write_text(str(time.time()))
    last_scan = 0.0
    try:
        while True:
            m = budget().get("memory", {})
            interval = m.get("watchdog_interval_s", 3)
            try:
                if time.time() - float(HEARTBEAT.read_text()) > 3 * 3600:
                    break
            except Exception:  # heartbeat gone (state cleared) -> nothing to protect any more
                break
            sysm = memprobe.system_memory()
            reg = load_json(STATE / "workers.json", {}) or {}
            emergency = None
            if sysm:
                if sysm["available_mb"] < m.get("watchdog_kill_below_available_mb", 800):
                    emergency = f"RAM available {sysm['available_mb']} MB"
                elif sysm.get("commit_pct") is not None and sysm["commit_pct"] >= m.get("commit_pause_pct", 92) + 4:
                    emergency = f"commit charge {sysm['commit_pct']}%"
            if emergency and reg:
                sizes = sorted(((memprobe.tree_bytes(int(pid)) or 0, int(pid), meta) for pid, meta in reg.items()), reverse=True)
                for size, pid, meta in [x for x in sizes if alive(x[1])]:
                    kill(pid)
                    unregister_worker(pid)
                    append_jsonl(LOG, {"ts": now_iso(), "action": "kill", "pid": pid, "label": meta.get("label"),
                                       "mb": round(size / 1_048_576), "reason": emergency})
                    time.sleep(1)
                    s2 = memprobe.system_memory()
                    if s2 and s2["available_mb"] >= m.get("pause_below_available_mb", 1500):
                        break
            if emergency:
                save_json(STATE / "memory_stop.json", {"ts": now_iso(), "reason": emergency})
            if time.time() - last_scan > 15 and reg:  # per-worker runaway check
                last_scan = time.time()
                cap = m.get("worker_mb", 2048) * 1.5
                for pid, meta in reg.items():
                    b = memprobe.tree_bytes(int(pid))
                    if b and b / 1_048_576 > cap:
                        kill(int(pid))
                        unregister_worker(int(pid))
                        append_jsonl(LOG, {"ts": now_iso(), "action": "kill", "pid": int(pid), "label": meta.get("label"),
                                           "mb": round(b / 1_048_576), "reason": f"worker over {cap:.0f} MB"})
            time.sleep(interval)
    finally:
        try:
            if PID_FILE.read_text().strip() == str(os.getpid()):
                PID_FILE.unlink()
        except Exception:
            pass


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--ensure", action="store_true")
    p.add_argument("--stop", action="store_true")
    a = p.parse_args()
    if a.ensure:
        ensure()
    elif a.stop:
        pid = running_pid()
        if pid:
            kill(pid)
        PID_FILE.unlink(missing_ok=True)
        print("WATCHDOG stopped" if pid else "WATCHDOG not running")
    else:
        loop()


if __name__ == "__main__":
    main()
