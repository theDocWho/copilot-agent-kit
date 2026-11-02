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
  ledger.py peek                                      # one-line state, changes nothing (for scheduled runs)
  ledger.py set-budget 6.00                           # task cost cap in USD (default 1.5 x plan estimate)
  ledger.py edit-unit U03 --json '{"verify": "pytest -q tests/test_x.py"}'
  ledger.py remember "Decision: all services use mTLS (SEC-12)"   # append to .agents/PROJECT.md

Gates (outside the model): `finish --status done` is accepted only if the unit's declared
outputs exist and its `verify` commands pass. Brakes: next-wave stops on the turn budget,
the task's USD cap (BUDGET_STOP), two waves in a row with nothing done (DRY_STOP),
and agents.max_waves_per_task.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import gates  # noqa: E402
from common import (AGENTS, STATE, budget, file_lock, fmt_usd, load_json, memory_snapshot,  # noqa: E402
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
    v = unit.get("verify")
    v = [v] if isinstance(v, str) else (v or [])
    verify_md = (" and these commands pass:\n" + "\n".join(f"- `{c}`" for c in v)) if v else "."
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

## Done gate (checked by the kit, not by you)
`finish --status done` is refused unless every output above exists and is non-empty{verify_md}
Fix the cause and run finish again. Never weaken a test or a verify command to get past the gate.

## Project memory
`.agents/PROJECT.md` holds conventions and decisions from earlier tasks. Skim its headings; read only what applies.

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
        if not led.get("budget_usd"):
            total = sum(x["est_cost"]["usd"] for x in led["units"].values())
            led["budget_usd"] = round(max(0.5, total * budget().get("agents", {}).get("default_budget_factor", 1.5)), 2)
        return led

    led = mutate(fn)
    by_model: dict[str, float] = {}
    for u in led["units"].values():
        m = model_for_agent(u["agent"], pr)
        by_model[m] = by_model.get(m, 0) + u["est_cost"]["usd"]
    total = sum(by_model.values())
    print(f"IMPORTED {len(units)} units. Expected cost {fmt_usd(total)} ({total / pr.get('credit_usd', .01):.0f} credits): "
          + ", ".join(f"{k} {fmt_usd(v)}" for k, v in sorted(by_model.items()))
          + f". Task cost cap {fmt_usd(led['budget_usd'])} (ask the user to confirm or change it; ledger.py set-budget).")
    for w in gates.fake_edges(led["units"]):
        print(w)
    nov = [u["id"] for u in led["units"].values() if u["agent"] == "Implementer" and not u.get("verify")]
    if nov:
        print(f"NO_VERIFY: Implementer units without a verify command: {', '.join(nov)} (add the narrowest test/lint/build).")
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
          f" ({mem['backend']}) RAM free={mem['ram_available_mb']}MB ({mem['ram_available_pct']}%)"
          f" vscode={mem['vscode_mb']}MB (+{mem['vscode_growth_mb']} this task) workers={mem['workers_mb']}MB"
          + (f" | {'; '.join(mem['reasons'])}" if mem["reasons"] else ""))
    import subprocess as _sp
    w = _sp.run([sys.executable, str(Path(__file__).with_name("watchdog.py")), "--ensure"], capture_output=True, text=True)
    print((w.stdout or w.stderr).strip()[:120])
    if mem["state"] == "UNMEASURED":
        print(f"MEMORY_UNMEASURED: no way to read process memory here; using static limits "
              f"({static_limits()['parallel']} subagents per wave). Prefer new chats per phase.")
    if mem["new_chat_recommended"]:
        print("NEW_SESSION_RECOMMENDED: VS Code memory has grown a lot during this task. Finish this wave, then tell the user to open a new chat and run /next-wave.")


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
        # ---- brakes -------------------------------------------------------------
        if len(led["waves"]) >= ab.get("max_waves_per_task", 40):
            return ("WAVE_LIMIT", [])
        finished_waves = [w for w in led["waves"] if len(w.get("results", {})) == len(w["units"])]
        last2 = finished_waves[-2:]
        if len(last2) == 2 and all("done" not in w["results"].values() for w in last2) \
                and not led.get("dry_ack_wave", 0) >= last2[-1]["n"]:
            return ("DRY_STOP", [])
        spent = spent_usd(led)
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
        cap = led.get("budget_usd")
        if picked and cap and spent + sum(u["est_cost"]["usd"] for u in picked) > cap:
            led["budget_hit"] = {"spent": round(spent, 3), "cap": cap, "at": now_iso()}
            return ("BUDGET_STOP", picked)
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
        print(f"Machine memory is low ({'; '.join(mem['reasons'])}). Do not start subagents. End the turn; ask the user to "
              "close heavy apps or open a new chat, then run /next-wave.")
    elif status == "END_TURN":
        print("Turn budget used. Write a 5-line checkpoint and end your turn (the user or /next-wave resumes). Do NOT keep working.")
    elif status in ("NEEDS_ATTENTION", "WAIT_RUNNING"):
        for u in units:
            print(f"  {u['id']} status={u['status']} {u.get('result', {}).get('summary', '')[:160]}")
        if status == "NEEDS_ATTENTION":
            print("blocked -> ask the pending questions (ledger.py questions) in ONE askQuestions call, then `answer`;"
                  " failed -> decide: reset-unit <ID> (retry) or add a smaller replacement unit;"
                  " split -> make sure follow-up units were added, then mark the original skipped via finish --status skipped.")
    elif status == "BUDGET_STOP":
        led = load()
        need = sum(u["est_cost"]["usd"] for u in units)
        print(f"Spent ~{fmt_usd(led['budget_hit']['spent'])} of the {fmt_usd(led['budget_hit']['cap'])} cap; the next wave needs"
              f" ~{fmt_usd(need)}. Ask the user (askQuestions): raise the cap (ledger.py set-budget <usd>), reduce scope, or stop.")
    elif status == "DRY_STOP":
        print("Two waves in a row finished nothing (all failed/blocked/split). Stop and ask the user how to proceed;"
              " after deciding, run `ledger.py set-budget --ack-dry` or reset/replace the stuck units.")
    elif status == "WAVE_LIMIT":
        print("agents.max_waves_per_task reached. Checkpoint and ask the user whether to continue (raise the limit in budget.json).")
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
    if a.status == "done":
        u0 = load()["units"].get(a.uid) or sys.exit(f"No unit {a.uid}")
        ok, report = gates.verify_unit(u0)
        if not ok:
            def bump(led):
                u = led["units"][a.uid]
                u["verify_failures"] = u.get("verify_failures", 0) + 1
                if u["verify_failures"] >= 3:
                    u["status"], u["finished"] = "failed", now_iso()
                    u["result"] = {"summary": "done-gate failed 3 times: " + report[0][:300]}
                    record_wave_result(led, u)
                return u["verify_failures"]
            n = mutate(bump)
            print("\n".join(report))
            print(f"VERIFY_FAILED ({n}/3): unit {a.uid} NOT accepted as done. Fix the cause and run finish again."
                  + ("" if n < 3 else " Third failure: unit marked failed for the orchestrator.")
                  + " Do not weaken tests or verify commands.")
            sys.exit(2)
        print("\n".join(report))

    def fn(led):
        u = led["units"].get(a.uid) or sys.exit(f"No unit {a.uid}")
        u["status"] = a.status
        u["finished"] = now_iso()
        u.setdefault("started", u.get("queued", u["finished"]))
        u["actual"] = {"calls": a.calls, "read_chars": a.read_chars, "wrote_chars": a.wrote_chars}
        u["result"] = {"summary": (a.summary or "")[:400]}
        u["actual_cost"] = actual_for(u, pr)
        record_wave_result(led, u)
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


def record_wave_result(led: dict, u: dict) -> None:
    """Remember how each unit of a wave ended, so retries/resets don't hide a dry wave."""
    for w in led.get("waves", []):
        if w["n"] == u.get("wave") and u["id"] in w["units"]:
            w.setdefault("results", {})[u["id"]] = u["status"]


def spent_usd(led: dict) -> float:
    pr = pricing()
    s = sum((u.get("actual_cost") or {}).get("usd", 0) for u in led["units"].values())
    o = led.get("orch", [])
    if o:
        cpt = budget().get("batching", {}).get("chars_per_token", 4)
        s += unit_cost(model_for_agent("Orchestrator", pr), sum(x["calls"] for x in o),
                       sum(x["read_chars"] + x["wrote_chars"] for x in o) / cpt, sum(x["wrote_chars"] for x in o) / cpt, pr)["usd"]
    return s


def cmd_peek(a):
    """One line, no changes: lets a scheduled run end immediately when there is nothing to do."""
    act = load_json(STATE / "active.json", {}) or {}
    if not act.get("task_id") or not ledger_path(act["task_id"]).exists():
        print("NO_TASK"); return
    led = load()
    if led.get("status") in ("done", "closed"):
        print("IDLE task closed"); return
    units = led["units"]
    if any(q for q in led["questions"] if not q.get("answer")):
        print("WAITING_QUESTIONS " + ", ".join(q["id"] for q in led["questions"] if not q.get("answer"))); return
    mem = memory_snapshot()
    if mem["state"] == "PAUSE":
        print("MEMORY_PAUSE " + "; ".join(mem["reasons"])); return
    if led.get("budget_hit") and spent_usd(led) >= led.get("budget_usd", 0) * 0.99:
        print("BUDGET_STOP"); return
    if units and all(u["status"] in DONE for u in units.values()):
        print("ALL_DONE"); return
    fw = [w for w in led["waves"] if len(w.get("results", {})) == len(w["units"])][-2:]
    if len(fw) == 2 and all("done" not in w["results"].values() for w in fw) and not led.get("dry_ack_wave", 0) >= fw[-1]["n"]:
        print("DRY_STOP needs the user"); return
    ready = [u for u in units.values() if u["status"] == "pending" and deps_done(u, units)]
    running = [u for u in units.values() if u["status"] in ("queued", "running")]
    if ready:
        print(f"WORK_READY {len(ready)} units"); return
    print(f"IDLE {len(running)} running, nothing ready" if running else "STUCK nothing ready (see ledger.py next-wave)")


def cmd_set_budget(a):
    def fn(led):
        if a.usd is not None:
            led["budget_usd"] = round(a.usd, 2)
            led.pop("budget_hit", None)
        if a.ack_dry:
            led["dry_ack_wave"] = len(led["waves"])
        return led.get("budget_usd")
    cap = mutate(fn)
    print(f"BUDGET {fmt_usd(cap or 0)} spent~{fmt_usd(spent_usd(load()))}" + (" (dry-stop acknowledged)" if a.ack_dry else ""))


def cmd_edit_unit(a):
    allowed = {"verify", "instructions", "est", "inputs", "outputs", "acceptance", "edge_reasons", "depends_on"}
    patch = json.loads(a.json)
    bad = set(patch) - allowed
    if bad:
        sys.exit(f"Cannot edit {sorted(bad)}; allowed: {sorted(allowed)}")
    max_calls = budget().get("agents", {}).get("max_calls_per_unit", 25)

    def fn(led):
        u = led["units"].get(a.uid) or sys.exit(f"No unit {a.uid}")
        if u["status"] in ("running", "done"):
            sys.exit(f"Unit {a.uid} is {u['status']}; edit only pending/failed units (reset-unit first).")
        u.update(patch)
        normalize_unit(u, max_calls)
        u["est_cost"] = est_for(u, pricing())
        u["brief"] = rel(write_brief(task_dir(led["id"]), u, max_calls))
        u.setdefault("edits", []).append({"at": now_iso(), "fields": sorted(patch)})
    mutate(fn)
    print(f"EDITED {a.uid}: {', '.join(sorted(patch))}")


PROJECT_TEMPLATE = """# Project memory

Read by the Planner at the start of every task. Keep it short (under ~2 pages): consolidate
instead of appending forever. Humans can edit it directly; it is committed with the repo.

## Conventions
- (stack, coding conventions, test commands, folders that matter)

## Standards and sources
- (where the internal standards, Confluence spaces and design docs live)

## Decisions
"""


def cmd_remember(a):
    p = AGENTS / "PROJECT.md"
    if not p.exists():
        p.write_text(PROJECT_TEMPLATE, encoding="utf-8")
    tid = (load_json(STATE / "active.json", {}) or {}).get("task_id", "-")
    with open(p, "a", encoding="utf-8") as f:
        f.write(f"- {now_iso()[:10]} [{tid}] {a.text.strip()}\n")
    size = p.stat().st_size
    print(f"REMEMBERED in {rel(p)} ({size} bytes)" + ("  ⚠ over 12 KB: consolidate older decisions" if size > 12000 else ""))


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
    added = mutate(fn)
    print("ADDED " + ", ".join(added))
    for w in gates.fake_edges(load()["units"]):
        if w.split()[1] in added:
            print(w)


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
    print(f"CLOSED {active_id()} status={a.status}. Run cost.py report for the final cost comparison, and save 2-6 durable"
          " decisions/conventions with `ledger.py remember \"...\"` (skip one-off answers).")


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
    if led.get("budget_usd"):
        print(f"cost: spent~{fmt_usd(spent_usd(led))} of cap {fmt_usd(led['budget_usd'])}")
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
    x = s.add_parser("peek"); x.set_defaults(f=cmd_peek)
    x = s.add_parser("set-budget"); x.add_argument("usd", type=float, nargs="?"); x.add_argument("--ack-dry", action="store_true"); x.set_defaults(f=cmd_set_budget)
    x = s.add_parser("edit-unit"); x.add_argument("uid"); x.add_argument("--json", required=True); x.set_defaults(f=cmd_edit_unit)
    x = s.add_parser("remember"); x.add_argument("text"); x.set_defaults(f=cmd_remember)
    x = s.add_parser("show"); x.add_argument("--brief", action="store_true"); x.add_argument("--json", action="store_true"); x.set_defaults(f=cmd_show)
    a = p.parse_args()
    a.f(a)


if __name__ == "__main__":
    main()
