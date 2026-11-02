#!/usr/bin/env python3
"""Zero-LLM-cost status dashboard for the active task.

  status.py                 # full status, also written to .agents/STATUS.md
  status.py --brief         # <= 12 lines (what the Monitor agent relays)
  status.py --watch 15      # refresh every 15 s in a terminal (VS Code task "Agents: watch status")
"""
from __future__ import annotations

import argparse
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from common import (AGENTS, STATE, fmt_usd, load_json, memory_snapshot, parse_iso,  # noqa: E402
                    read_jsonl)


def ago(ts: float | None) -> str:
    if not ts:
        return "-"
    s = int(time.time() - ts)
    return f"{s}s" if s < 90 else (f"{s // 60}m" if s < 5400 else f"{s // 3600}h{(s % 3600) // 60:02d}m")


def build(brief: bool) -> list[str]:
    a = load_json(STATE / "active.json", {}) or {}
    L: list[str] = []
    mem = memory_snapshot()
    hooks = read_jsonl(STATE / "hooks.jsonl", tail=2000)
    now = time.time()
    calls10 = sum(1 for e in hooks if str(e.get("event", "")).lower() == "pretooluse" and now - e.get("t", 0) < 600)
    last_evt = max((e.get("t", 0) for e in hooks), default=0)

    if not a.get("task_id"):
        L.append("No active task. Start one with /run-task.")
    else:
        led = load_json(STATE / "tasks" / a["task_id"] / "ledger.json", {}) or {}
        units = list(led.get("units", {}).values())
        counts: dict[str, int] = {}
        for u in units:
            counts[u["status"]] = counts.get(u["status"], 0) + 1
        done = [u for u in units if u["status"] in ("done", "skipped")]
        pct = round(100 * len(done) / len(units)) if units else 0
        L.append(f"TASK {led.get('id')} [{led.get('status')}] {pct}% ({len(done)}/{len(units)} units) — {led.get('title', '')[:70]}")
        L.append("units: " + (", ".join(f"{k}={v}" for k, v in sorted(counts.items())) or "none")
                 + f" | wave {len(led.get('waves', []))} | turn {led.get('turn', {}).get('n')} "
                 f"calls {led.get('turn', {}).get('used')}/{led.get('turn', {}).get('budget')}")
        running = [u for u in units if u["status"] in ("running", "queued")]
        for u in running[: (3 if brief else 20)]:
            L.append(f"  ▶ {u['id']} {u['agent']} {u['status']} {ago(parse_iso(u.get('started') or u.get('queued')))} — {u['title'][:60]}")
        if running and last_evt and now - last_evt > 600:
            L.append(f"  ⚠ STALLED? no agent tool activity for {ago(last_evt)} while units are running "
                     "(chat may be waiting on 'Continue' or a question)")
        pend = [q for q in led.get("questions", []) if not q.get("answer")]
        if pend:
            L.append(f"❓ {len(pend)} question(s) waiting for you: " + "; ".join(f"{q['id']}: {q['question'][:70]}" for q in pend[:3]))
        bad = [u for u in units if u["status"] in ("failed", "blocked", "split")]
        for u in bad[:5]:
            L.append(f"  ✖ {u['id']} {u['status']}: {u.get('result', {}).get('summary', '')[:90]}")
        exp_all = sum(u.get("est_cost", {}).get("usd", 0) for u in units)
        exp_done = sum(u.get("est_cost", {}).get("usd", 0) for u in done)
        act_done = sum((u.get("actual_cost") or {}).get("usd", 0) for u in done)
        delta = f"{(act_done - exp_done) / exp_done * 100:+.0f}%" if exp_done else "n/a"
        L.append(f"cost (units, excl. orchestrator): expected total {fmt_usd(exp_all)} | done units expected {fmt_usd(exp_done)} vs actual~ {fmt_usd(act_done)} ({delta})")
        durs = [parse_iso(u["finished"]) - parse_iso(u["started"]) for u in done
                if u.get("finished") and u.get("started") and parse_iso(u["finished"]) and parse_iso(u["started"])]
        left = len([u for u in units if u["status"] in ("pending", "queued", "running")])
        if durs and left:
            par = max(1, load_json(AGENTS / "config" / "budget.json", {}).get("agents", {}).get("max_parallel_subagents", 3))
            L.append(f"eta: ~{ago(now - (sum(durs) / len(durs)) * left / par)} of agent time left ({left} units)")
        if not brief:
            L.append("")
            L.append("| unit | agent | status | est $ | actual~ $ | summary |")
            L.append("|---|---|---|---|---|---|")
            for u in sorted(units, key=lambda x: x["id"]):
                L.append(f"| {u['id']} | {u['agent']} | {u['status']} | {u.get('est_cost', {}).get('usd', 0):.3f} | "
                         f"{(u.get('actual_cost') or {}).get('usd', 0):.3f} | {u.get('result', {}).get('summary', '')[:70]} |")

    for pf in sorted((STATE / "progress").glob("crawl-*.jsonl")) if (STATE / "progress").exists() else []:
        ev = read_jsonl(pf, tail=3)
        if not ev:
            continue
        e = ev[-1]
        last = parse_iso(e.get("ts"))
        live = last and now - last < 30 and e.get("event") != "slice_end"
        L.append(f"crawl {pf.stem[6:]}: {'RUNNING' if live else e.get('status', 'idle')} done={e.get('done')} "
                 f"queued={e.get('queued')} failed={e.get('failed')} rate={e.get('rate_pps', '-')}/s ({ago(last)} ago)")
    L.append(f"memory: {mem['state']} — VS Code {mem['vscode_mb']}MB + workers {mem['workers_mb']}MB = "
             f"{mem['used_pct']}% of {mem['total_budget_mb']}MB" + ("  ⚠ open a NEW chat to free VS Code memory" if mem["vscode_over_soft_limit"] else "")
             + ("" if mem["psutil"] else "  (pip install psutil to measure)"))
    L.append(f"activity: {calls10} tool calls in last 10 min, last event {ago(last_evt)} ago")
    return L


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--brief", action="store_true")
    p.add_argument("--watch", type=int, default=0)
    a = p.parse_args()
    while True:
        lines = build(a.brief)
        (AGENTS / "STATUS.md").write_text("# Agent status\n\n```\n" + "\n".join(build(False)) + "\n```\n", encoding="utf-8")
        if a.watch:
            os.system("cls" if os.name == "nt" else "clear")
        print("\n".join(lines[:12] if a.brief else lines))
        if not a.watch:
            break
        time.sleep(a.watch)


if __name__ == "__main__":
    main()
