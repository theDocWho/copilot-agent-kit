#!/usr/bin/env python3
"""Task ledger: the single source of truth for a multi-agent task.

Every agent talks to the ledger through this CLI, so chat context stays tiny
and any new chat session can resume exactly where the last one stopped.

  ledger.py init "Research X and implement Y"        # new task, becomes active
  ledger.py import-plan .agents/state/tasks/<id>/plan.json
  ledger.py questions                                 # unanswered questions
  ledger.py answer Q1 "use option B"
  ledger.py turn-start                                # orchestrator: start of every chat turn
  ledger.py next-wave                                 # units to dispatch now (or END_TURN)
  ledger.py start U03 / finish U03 --status done --calls 9 --read-chars 40000 --wrote-chars 3000 --summary "..."
  ledger.py add --json '{"id":"U09","title":"...","agent":"Implementer",...}'
  ledger.py orch --calls 5 --read-chars 6000 --wrote-chars 1500
  ledger.py show [--brief|--json]   ledger.py reset-unit U03   ledger.py close
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from common import (STATE, budget, file_lock, fmt_usd, load_json, memory_snapshot,  # noqa: E402
                    model_for_agent, now_iso, pricing, rel, save_json, static_limits, turn_budget_calls, unit_cost)

AGENTS_OK = {"Fetcher", "Implementer", "Reviewer", "Planner"}
DONE = {"done", "skipped"}


def deps_done(u: dict, units: dict) -> bool:
    """All dependencies finished. 'PREFIX-*' = every unit whose id starts with PREFIX-
    (units added later included) must be done, and at least one must exist."""
    for d in u.get("depends_on", []):
        if d.endswith("*"):
            match = [x for k, x in units.items() if k.startswith(d[:-1]) and k != u["id"]]
            if not match or any(x["status"] not in DONE for x in match):
                return False
        elif units.get(d, {}).get("status") not in DONE:
            return False
    return True


# ------------------------------------------------------------------ storage
def active_id() -> str:
    a = load_json(STATE / "active.json", {}) or {}
    if not a.get("task_id"):
        sys.exit("No active task. Run: ledger.py init \"<task>\"")
    return a["task_id"]


def task_dir(tid: str | None = None) -> Path:
    return STATE / "tasks" / (tid or active_id())


def ledger_path(tid: str | None = None) -> Path:
    return task_dir(tid) / "ledger.json"


def load(tid: str | None = None) -> dict:
    led = load_json(ledger_path(tid))
    if led is None:
        sys.exit(f"Ledger missing for task {tid or active_id()}")
    return led


def mutate(fn, tid: str | None = None):
    p = ledger_path(tid)
    with file_lock(p):
        led = load(tid)
        out = fn(led)
        led["updated"] = now_iso()
        save_json(p, led)
    return out


# ------------------------------------------------------------------ helpers
def est_for(unit: dict, pr: dict) -> dict:
    e = unit.get("est", {})
    return unit_cost(model_for_agent(unit["agent"], pr), e.get("calls", 10),
                     e.get("ctx_ktok", 20) * 1000, e.get("out_ktok", 3) * 1000, pr)


def actual_for(unit: dict, pr: dict) -> dict | None:
    a = unit.get("actual")
    if not a or not a.get("calls"):
        return None
    cpt = budget().get("batching", {}).get("chars_per_token", 4)
    work = (a.get("read_chars", 0) + a.get("wrote_chars", 0)) / cpt
    return unit_cost(model_for_agent(unit["agent"], pr), a["calls"], work, a.get("wrote_chars", 0) / cpt, pr)


def write_brief(tdir: Path, unit: dict, max_calls: int) -> Path:
    bdir = tdir / "units"
    bdir.mkdir(parents=True, exist_ok=True)
    p = bdir / f"{unit['id']}.md"
    model = model_for_agent(unit["agent"])
    calls = unit.get("est", {}).get("calls", 10)
    lst = lambda xs: "\n".join(f"- {x}" for x in xs) if xs else "- (none)"  # noqa: E731
    answers = tdir / "answers.md"
    refs = unit.get("skill") or []
    refs = refs if isinstance(refs, list) else [refs]
    skill_md = ""
    if refs:
        lines = []
        for r in refs:
            r = {"path": r} if isinstance(r, str) else r
            lines.append(f"- `{r['path']}`" + (f" — section **{r['section']}**" if r.get("section") else ""))
        skill_md = ("\n## Method (authoritative for how to do the work)\n" + "\n".join(lines) +
                    "\nRead only the named section (find its heading with a text search, then read that range)."
                    " Follow its method, rating scales and output format. This brief's budget, inputs and outputs still win.\n")
    p.write_text(f"""# {unit['id']} — {unit['title']}

Agent: **{unit['agent']}** ({model}) · Tool-call budget: **{min(calls + 3, max_calls)}** (hard stop) · Depends on: {', '.join(unit.get('depends_on', [])) or 'nothing'}

## Instructions
{unit.get('instructions', unit['title'])}
{skill_md}
## Inputs (read only what you need)
{lst(unit.get('inputs', []))}

## Outputs (write exactly these; create folders if needed)
{lst(unit.get('outputs', []))}

## Acceptance
{unit.get('acceptance', 'Outputs exist and satisfy the instructions.')}

## User decisions
{'See ' + rel(answers) if answers.exists() else 'None recorded yet.'}

## Finish protocol (mandatory, last tool call)
```
python .agents/tools/ledger.py finish {unit['id']} --status done --calls <tool calls you used> --read-chars <approx chars read> --wrote-chars <approx chars written> --summary "<= 200 chars, no raw content>"
```
- Out of budget / too big: `--status split` and add follow-up units with `ledger.py add --json '{{...}}'` (ids {unit['id']}a, {unit['id']}b ...).
- Need a human decision: `--status blocked --question "<one clear question with options>"`. Do not guess on irreversible choices.
- Failed: `--status failed --summary "<why>"`.
Reply to the orchestrator with at most 8 lines: status, output paths, one-line findings. Never paste file contents.
""", encoding="utf-8")
    return p


def normalize_unit(u: dict, max_calls: int) -> dict:
    for k in ("id", "title", "agent"):
        if not u.get(k):
            sys.exit(f"Unit missing '{k}': {json.dumps(u)[:200]}")
    if u["agent"] not in AGENTS_OK:
        sys.exit(f"Unit {u['id']}: agent must be one of {sorted(AGENTS_OK)}")
    e = u.setdefault("est", {})
    e.setdefault("calls", 10)
    e.setdefault("ctx_ktok", 20)
    e.setdefault("out_ktok", 3)
    if e["calls"] > max_calls:
        sys.exit(f"Unit {u['id']} estimates {e['calls']} tool calls > max_calls_per_unit={max_calls}. Split it into smaller units.")
    u.setdefault("depends_on", [])
    u.setdefault("status", "pending")
    u.setdefault("inputs", [])
    u.setdefault("outputs", [])
    return u


# ------------------------------------------------------------------ commands
def cmd_init(a):
    slug = re.sub(r"[^a-z0-9]+", "-", a.task.lower()).strip("-")[:40] or "task"
    tid = a.id or f"{now_iso()[:10].replace('-', '')}-{slug}"
    tdir = task_dir(tid)
    if ledger_path(tid).exists() and not a.force:
        sys.exit(f"Task {tid} exists. Use --force or a different --id.")
    tdir.mkdir(parents=True, exist_ok=True)
    save_json(ledger_path(tid), {
        "id": tid, "title": a.task, "created": now_iso(), "updated": now_iso(), "status": "planning",
        "questions": [], "units": {}, "waves": [], "orch": [],
        "turn": {"n": 0, "budget": turn_budget_calls(), "used": 0},
    })
    save_json(STATE / "active.json", {"task_id": tid, "since": now_iso()})
    print(f"ACTIVE_TASK {tid}\nplan file to write: {rel(tdir / 'plan.json')}")


def cmd_import_plan(a):
    plan = load_json(Path(a.plan))
    if not plan:
        sys.exit("Plan not found or empty")
    pr, b = pricing(), budget()
    max_calls = b.get("agents", {}).get("max_calls_per_unit", 25)
    units = [normalize_unit(u, max_calls) for u in plan.get("units", [])]
    ids = {u["id"] for u in units}
    for u in units:
        missing = [d for d in u["depends_on"] if d not in ids and not d.endswith("*")]
        if missing:
            sys.exit(f"Unit {u['id']} depends on unknown {missing}")

    def fn(led):
        tdir = task_dir(led["id"])
        for u in units:
            u["est_cost"] = est_for(u, pr)
            u["brief"] = rel(write_brief(tdir, u, max_calls))
            led["units"][u["id"]] = u
        existing = {q["id"] for q in led["questions"]}
        for q in plan.get("questions", []):
            if q.get("id") and q["id"] not in existing:
                q.setdefault("blocking", True)
                q.setdefault("answer", None)
                led["questions"].append(q)
        if plan.get("summary"):
            led["plan_summary"] = plan["summary"]
        led["status"] = "running"
        return led

    led = mutate(fn)
    by_model: dict[str, float] = {}
    for u in led["units"].values():
        m = model_for_agent(u["agent"], pr)
        by_model[m] = by_model.get(m, 0) + u["est_cost"]["usd"]
    total = sum(by_model.values())
    print(f"IMPORTED {len(units)} units. Expected cost {fmt_usd(total)} ({total / pr.get('credit_usd', .01):.0f} credits): "
          + ", ".join(f"{k} {fmt_usd(v)}" for k, v in sorted(by_model.items())))
    pend = [q for q in led["questions"] if not q.get("answer")]
    if pend:
        print("ASK_USER_NOW (one askQuestions call, all at once):")
        for q in pend:
            print(f"  {q['id']}{' [blocking]' if q.get('blocking') else ''}: {q['question']}"
                  + (f" | options: {', '.join(q['options'])}" if q.get("options") else "")
                  + (f" | default: {q['default']}" if q.get("default") else ""))


def cmd_questions(a):
    led = load()
    pend = [q for q in led["questions"] if not q.get("answer")]
    if not pend:
        print("NO_PENDING_QUESTIONS")
    for q in pend:
        print(f"{q['id']}{' [blocking]' if q.get('blocking') else ''}"
              f"{' (unit ' + q['unit'] + ')' if q.get('unit') else ''}: {q['question']}"
              + (f" | options: {', '.join(q['options'])}" if q.get("options") else ""))


def cmd_answer(a):
    def fn(led):
        for q in led["questions"]:
            if q["id"] == a.qid:
                q["answer"], q["answered"] = a.text, now_iso()
                if q.get("unit") and led["units"].get(q["unit"], {}).get("status") == "blocked":
                    led["units"][q["unit"]]["status"] = "pending"
                break
        else:
            sys.exit(f"No question {a.qid}")
        lines = ["# User decisions", ""] + [f"- **{q['id']}** {q['question']} → **{q['answer']}**"
                                             for q in led["questions"] if q.get("answer")]
        (task_dir(led["id"]) / "answers.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    mutate(fn)
    print(f"ANSWERED {a.qid}")


def cmd_turn_start(a):
    def fn(led):
        t = led.setdefault("turn", {})
        t.update({"n": t.get("n", 0) + 1, "budget": turn_budget_calls(), "used": 0, "started": now_iso()})
        return t
    t = mutate(fn)
    mem = memory_snapshot()
    print(f"TURN {t['n']} budget={t['budget']} tool calls (orchestrator + subagents). memory={mem['state']}"
          f" ({mem['backend']}) vscode={mem['vscode_mb']}MB workers={mem['workers_mb']}MB used={mem['used_pct']}%")
    if mem["state"] == "UNMEASURED":
        print(f"MEMORY_UNMEASURED: no way to read process memory here; using static limits "
              f"({static_limits()['parallel']} subagents per wave). Prefer new chats per phase.")
    if mem["vscode_over_soft_limit"]:
        print("NEW_SESSION_RECOMMENDED: VS Code is over its soft memory limit. Finish this wave, then tell the user to open a new chat and run /next-wave.")


def cmd_next_wave(a):
    b = budget()
    ab = b.get("agents", {})
    max_par = a.max_parallel or ab.get("max_parallel_subagents", 3)
    orch_cost = ab.get("orchestrator_calls_per_wave", 6)
    mem = memory_snapshot()
    if mem["state"] == "UNMEASURED":
        max_par = min(max_par, static_limits()["parallel"])

    def fn(led):
        units = led["units"]
        blocking = [q for q in led["questions"] if q.get("blocking") and not q.get("answer") and not q.get("unit")]
        if blocking:
            return ("BLOCKED_ON_QUESTIONS", [])
        if mem["state"] == "PAUSE":
            return ("MEMORY_PAUSE", [])
        t = led.setdefault("turn", {"budget": turn_budget_calls(), "used": 0})
        remaining = t["budget"] - t["used"] - orch_cost
        ready = [u for u in units.values() if u["status"] == "pending" and deps_done(u, units)]
        ready.sort(key=lambda u: (len(u["depends_on"]), u["id"]))
        picked, spend = [], 0
        for u in ready:
            c = u["est"]["calls"] + 3
            if len(picked) >= max_par or spend + c > remaining:
                break
            picked.append(u)
            spend += c
        if not picked:
            active = [u for u in units.values() if u["status"] in ("queued", "running")]
            if active:
                return ("WAIT_RUNNING", active)
            if all(u["status"] in DONE for u in units.values()) and units:
                return ("ALL_DONE", [])
            blocked = [u for u in units.values() if u["status"] in ("blocked", "failed", "split")]
            if blocked:
                return ("NEEDS_ATTENTION", blocked)
            if ready:
                return ("END_TURN", [])
            return ("NOTHING_READY", [u for u in units.values() if u["status"] == "pending"])
        wave = len(led["waves"]) + 1
        for u in picked:
            u["status"], u["wave"], u["queued"] = "queued", wave, now_iso()
        led["waves"].append({"n": wave, "units": [u["id"] for u in picked], "at": now_iso(), "turn": t.get("n")})
        t["used"] += spend + orch_cost
        return ("DISPATCH", picked)

    status, units = mutate(fn)
    print(status)
    if status == "DISPATCH":
        for u in units:
            print(f"  {u['id']} | agent={u['agent']} | brief={u['brief']} | est_calls={u['est']['calls']}")
        print("Dispatch each with runSubagent using the named custom agent and prompt:"
              " 'Execute unit <ID>. Read <brief> first and follow it exactly.' Start them in the same response so they run in parallel.")
    elif status == "BLOCKED_ON_QUESTIONS":
        print("Run `ledger.py questions`, ask the user ALL of them in one askQuestions call, record with `ledger.py answer`.")
    elif status == "MEMORY_PAUSE":
        print(f"Memory {mem['used_pct']}% of budget. Do not start subagents. End the turn; ask the user to open a new chat and run /next-wave.")
    elif status == "END_TURN":
        print("Turn budget used. Write a 5-line checkpoint and end your turn (the user or /next-wave resumes). Do NOT keep working.")
    elif status in ("NEEDS_ATTENTION", "WAIT_RUNNING"):
        for u in units:
            print(f"  {u['id']} status={u['status']} {u.get('result', {}).get('summary', '')[:160]}")
        if status == "NEEDS_ATTENTION":
            print("blocked -> ask the pending questions (ledger.py questions) in ONE askQuestions call, then `answer`;"
                  " failed -> decide: reset-unit <ID> (retry) or add a smaller replacement unit;"
                  " split -> make sure follow-up units were added, then mark the original skipped via finish --status skipped.")
    elif status == "NOTHING_READY":
        for u in units[:8]:
            print(f"  {u['id']} waits on {', '.join(u['depends_on'])}")
        print("These wait on units that do not exist or will never finish. Check `ledger.py show`; add the missing"
              " units (ledger.py add) or ask the user.")
    elif status == "ALL_DONE":
        print("All units done. Run the final Reviewer unit if not yet run, then `cost.py report` and `ledger.py close`.")


def cmd_start(a):
    def fn(led):
        u = led["units"].get(a.uid) or sys.exit(f"No unit {a.uid}")
        u["status"], u["started"] = "running", now_iso()
    mutate(fn)
    print(f"STARTED {a.uid}")


def cmd_finish(a):
    pr = pricing()

    def fn(led):
        u = led["units"].get(a.uid) or sys.exit(f"No unit {a.uid}")
        u["status"] = a.status
        u["finished"] = now_iso()
        u.setdefault("started", u.get("queued", u["finished"]))
        u["actual"] = {"calls": a.calls, "read_chars": a.read_chars, "wrote_chars": a.wrote_chars}
        u["result"] = {"summary": (a.summary or "")[:400]}
        u["actual_cost"] = actual_for(u, pr)
        if a.status == "blocked":
            qid = f"Q{len(led['questions']) + 1}"
            led["questions"].append({"id": qid, "unit": a.uid, "question": a.question or a.summary or "?",
                                     "blocking": True, "answer": None, "asked_by": u["agent"]})
        # correct the turn spend with reality
        t = led.get("turn", {})
        if t and u.get("wave") and any(w["n"] == u["wave"] and w.get("turn") == t.get("n") for w in led["waves"]):
            t["used"] += max(0, a.calls - (u["est"]["calls"] + 3))
        return u
    u = mutate(fn)
    c = u.get("actual_cost") or {}
    print(f"FINISHED {a.uid} status={a.status} est={fmt_usd(u['est_cost']['usd'])} actual~{fmt_usd(c.get('usd', 0))}")


def cmd_add(a):
    raw = Path(a.file).read_text(encoding="utf-8") if a.file else a.json
    data = json.loads(raw)
    items = data if isinstance(data, list) else [data]
    pr, max_calls = pricing(), budget().get("agents", {}).get("max_calls_per_unit", 25)

    def fn(led):
        added = []
        for u in items:
            u = normalize_unit(u, max_calls)
            if u["id"] in led["units"]:
                sys.exit(f"Unit {u['id']} already exists")
            u["est_cost"] = est_for(u, pr)
            u["brief"] = rel(write_brief(task_dir(led["id"]), u, max_calls))
            led["units"][u["id"]] = u
            added.append(u["id"])
        return added
    print("ADDED " + ", ".join(mutate(fn)))


def cmd_orch(a):
    def fn(led):
        led["orch"].append({"at": now_iso(), "calls": a.calls, "read_chars": a.read_chars,
                            "wrote_chars": a.wrote_chars, "note": a.note})
    mutate(fn)
    print("ORCH_LOGGED")


def cmd_reset(a):
    def fn(led):
        u = led["units"].get(a.uid) or sys.exit(f"No unit {a.uid}")
        u["status"] = "pending"
        for k in ("wave", "queued", "started", "finished"):
            u.pop(k, None)
    mutate(fn)
    print(f"RESET {a.uid}")


def cmd_close(a):
    def fn(led):
        led["status"], led["closed"] = a.status, now_iso()
    mutate(fn)
    print(f"CLOSED {active_id()} status={a.status}. Run cost.py report for the final cost comparison.")


def cmd_show(a):
    led = load()
    if a.json:
        print(json.dumps(led, indent=2))
        return
    counts: dict[str, int] = {}
    for u in led["units"].values():
        counts[u["status"]] = counts.get(u["status"], 0) + 1
    print(f"TASK {led['id']} [{led['status']}] {led['title'][:80]}")
    print("units: " + (", ".join(f"{k}={v}" for k, v in sorted(counts.items())) or "none")
          + f" | waves={len(led['waves'])} | turn {led.get('turn', {}).get('n')} used {led.get('turn', {}).get('used')}/{led.get('turn', {}).get('budget')}")
    pend = [q for q in led["questions"] if not q.get("answer")]
    if pend:
        print(f"pending questions: {', '.join(q['id'] for q in pend)}")
    if a.brief:
        return
    for u in sorted(led["units"].values(), key=lambda x: x["id"]):
        print(f"  {u['id']:<6} {u['status']:<8} {u['agent']:<11} {u['title'][:60]}"
              + (f"  — {u['result']['summary'][:80]}" if u.get("result") else ""))


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    s = p.add_subparsers(dest="cmd", required=True)
    x = s.add_parser("init"); x.add_argument("task"); x.add_argument("--id"); x.add_argument("--force", action="store_true"); x.set_defaults(f=cmd_init)
    x = s.add_parser("import-plan"); x.add_argument("plan"); x.set_defaults(f=cmd_import_plan)
    x = s.add_parser("questions"); x.set_defaults(f=cmd_questions)
    x = s.add_parser("answer"); x.add_argument("qid"); x.add_argument("text"); x.set_defaults(f=cmd_answer)
    x = s.add_parser("turn-start"); x.set_defaults(f=cmd_turn_start)
    x = s.add_parser("next-wave"); x.add_argument("--max-parallel", type=int); x.set_defaults(f=cmd_next_wave)
    x = s.add_parser("start"); x.add_argument("uid"); x.set_defaults(f=cmd_start)
    x = s.add_parser("finish"); x.add_argument("uid")
    x.add_argument("--status", default="done", choices=["done", "failed", "blocked", "split", "skipped"])
    x.add_argument("--calls", type=int, default=0); x.add_argument("--read-chars", type=int, default=0)
    x.add_argument("--wrote-chars", type=int, default=0); x.add_argument("--summary", default="")
    x.add_argument("--question"); x.set_defaults(f=cmd_finish)
    x = s.add_parser("add"); g = x.add_mutually_exclusive_group(required=True); g.add_argument("--json"); g.add_argument("--file"); x.set_defaults(f=cmd_add)
    x = s.add_parser("orch"); x.add_argument("--calls", type=int, default=0); x.add_argument("--read-chars", type=int, default=0)
    x.add_argument("--wrote-chars", type=int, default=0); x.add_argument("--note", default=""); x.set_defaults(f=cmd_orch)
    x = s.add_parser("reset-unit"); x.add_argument("uid"); x.set_defaults(f=cmd_reset)
    x = s.add_parser("close"); x.add_argument("--status", default="done"); x.set_defaults(f=cmd_close)
    x = s.add_parser("show"); x.add_argument("--brief", action="store_true"); x.add_argument("--json", action="store_true"); x.set_defaults(f=cmd_show)
    a = p.parse_args()
    a.f(a)


if __name__ == "__main__":
    main()
