#!/usr/bin/env python3
"""Expected vs actual cost for the active (or given) task.

  cost.py report                          # table + .agents/state/tasks/<id>/cost-report.md
  cost.py report --usage-csv usage.csv    # also reconcile with GitHub's billing usage export
  cost.py estimate --agent Implementer --calls 15 --ctx-ktok 30 --out-ktok 5   # quick what-if

How "actual" is measured (best available source wins):
  1. --usage-csv: GitHub billing usage report for the task's time window = billed truth.
  2. Hook log (.agents/state/hooks.jsonl): real tool-call counts per unit time window.
  3. Self-reported calls/chars from `ledger.py finish`.
Tokens for 2 and 3 use the same cost model as the estimate (common.unit_cost), so the
delta isolates how far the plan's call counts and context sizes were off.
"""
from __future__ import annotations

import argparse
import csv
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from common import (STATE, budget, fmt_usd, load_json, model_for_agent, parse_iso,  # noqa: E402
                    pricing, read_jsonl, rel, unit_cost)


def hook_calls(units: list[dict], hooks: list[dict]) -> dict[str, float]:
    """Attribute preToolUse events to units by agent name (if present) or time window share."""
    ev = [e for e in hooks if str(e.get("event", "")).lower() == "pretooluse"]
    spans = {u["id"]: (parse_iso(u.get("started") or u.get("queued")), parse_iso(u.get("finished")), u["agent"])
             for u in units if u.get("finished")}
    res = {k: 0.0 for k in spans}
    for e in ev:
        t = e.get("t", 0)
        live = [k for k, (s, f, _) in spans.items() if s and f and s <= t <= f]
        if not live:
            continue
        named = [k for k in live if e.get("agent") and spans[k][2].lower() in str(e["agent"]).lower()]
        tgt = named or live
        share = 1 / (len(tgt) + (0 if named else 1))  # +1: orchestrator also acts in that window
        for k in tgt:
            res[k] += share
    return res


def read_usage_csv(path: str, t0: float, t1: float) -> dict[str, float]:
    """Flexible parser for GitHub usage exports: finds date, model and cost/amount columns."""
    out: dict[str, float] = {}
    with open(path, newline="", encoding="utf-8-sig") as f:
        rd = csv.DictReader(f)
        cols = [c.lower() for c in rd.fieldnames or []]
        def pick(*keys):  # first key (in priority order) that matches any column
            for k in keys:
                for i, c in enumerate(cols):
                    if k in c:
                        return rd.fieldnames[i]
            return None
        c_date, c_model = pick("date", "time"), pick("model", "sku", "product")
        c_cost = pick("net_amount", "net amount", "cost", "amount", "usd")
        if not (c_model and c_cost):
            sys.exit(f"Could not find model/cost columns in {path}: {rd.fieldnames}")
        for row in rd:
            if c_date:
                ts = parse_iso(row[c_date][:19]) or parse_iso(row[c_date][:10])
                if ts and not (t0 - 86400 <= ts <= t1 + 86400):  # exports are often daily
                    continue
            m = row[c_model].lower()
            key = "opus" if "opus" in m else "sonnet" if "sonnet" in m else "haiku" if "haiku" in m else "other"
            try:
                out[key] = out.get(key, 0) + float(str(row[c_cost]).replace("$", "").replace(",", "") or 0)
            except ValueError:
                pass
    return out


def report(a):
    tid = a.task or (load_json(STATE / "active.json", {}) or {}).get("task_id")
    if not tid:
        sys.exit("No active task")
    tdir = STATE / "tasks" / tid
    led = load_json(tdir / "ledger.json") or sys.exit("no ledger")
    pr, b = pricing(), budget()
    cpt = b.get("batching", {}).get("chars_per_token", 4)
    units = list(led["units"].values())
    hk = hook_calls(units, read_jsonl(STATE / "hooks.jsonl"))
    rows, by_model = [], {}
    for u in sorted(units, key=lambda x: x["id"]):
        mk = model_for_agent(u["agent"], pr)
        est = u.get("est_cost") or unit_cost(mk, u["est"]["calls"], u["est"]["ctx_ktok"] * 1000, u["est"]["out_ktok"] * 1000, pr)
        act, src, calls = None, "-", 0
        if u.get("actual"):
            ac = u["actual"]
            calls, src = ac.get("calls", 0), "self"
            h = round(hk.get(u["id"], 0), 1)
            if h > calls:  # conservative: trust whichever source saw more tool calls
                calls, src = h, "hooks"
            elif h:
                src = f"self; hooks {h}"
            work = (ac.get("read_chars", 0) + ac.get("wrote_chars", 0)) / cpt
            act = unit_cost(mk, calls, work, ac.get("wrote_chars", 0) / cpt, pr)
        rows.append((u, mk, est, act, calls, src))
        bm = by_model.setdefault(mk, {"est": 0.0, "act": 0.0, "est_done": 0.0})
        bm["est"] += est["usd"]
        if act:
            bm["act"] += act["usd"]
            bm["est_done"] += est["usd"]

    # orchestrator overhead (expected: per wave; actual: logged turns)
    waves = len(led.get("waves", []))
    ocw = b.get("agents", {}).get("orchestrator_calls_per_wave", 6)
    o_est = unit_cost("opus", max(1, waves) * ocw, 8000, 1500 * max(1, waves), pr)
    o = led.get("orch", [])
    o_act = unit_cost("opus", sum(x["calls"] for x in o), sum(x["read_chars"] + x["wrote_chars"] for x in o) / cpt,
                      sum(x["wrote_chars"] for x in o) / cpt, pr) if o else None
    bm = by_model.setdefault("opus", {"est": 0.0, "act": 0.0, "est_done": 0.0})
    bm["est"] += o_est["usd"]
    if o_act:
        bm["act"] += o_act["usd"]
        bm["est_done"] += o_est["usd"]

    est_total = sum(v["est"] for v in by_model.values())
    act_total = sum(v["act"] for v in by_model.values())
    est_done = sum(v["est_done"] for v in by_model.values())
    # counterfactual: same work with every agent on Opus
    all_opus = sum(unit_cost("opus", (r[4] or r[0]["est"]["calls"]),
                             r[0]["est"]["ctx_ktok"] * 1000, r[0]["est"]["out_ktok"] * 1000, pr)["usd"] for r in rows) + o_est["usd"]

    L = [f"# Cost report — {led['id']}", "", f"Task: {led['title']}", f"Status: {led['status']} · waves: {waves} · units: {len(units)}", "",
         "| unit | agent | model | est calls | act calls (src) | expected | actual~ | Δ |", "|---|---|---|---|---|---|---|---|"]
    for u, mk, est, act, calls, src in rows:
        d = f"{(act['usd'] - est['usd']) / est['usd'] * 100:+.0f}%" if act and est["usd"] else ""
        L.append(f"| {u['id']} | {u['agent']} | {mk} | {u['est']['calls']} | {calls if act else '-'} ({src}) | "
                 f"{fmt_usd(est['usd'])} | {fmt_usd(act['usd']) if act else '-'} | {d} |")
    L.append(f"| orchestrator | Orchestrator | opus | {max(1, waves) * ocw} | {sum(x['calls'] for x in o) if o else '-'} (self) | "
             f"{fmt_usd(o_est['usd'])} | {fmt_usd(o_act['usd']) if o_act else '-'} | |")
    L += ["", "| model | expected (all) | expected (finished) | actual~ (finished) |", "|---|---|---|---|"]
    for mk, v in sorted(by_model.items()):
        L.append(f"| {mk} | {fmt_usd(v['est'])} | {fmt_usd(v['est_done'])} | {fmt_usd(v['act'])} |")
    credit = pr.get("credit_usd", 0.01)
    delta = f"{(act_total - est_done) / est_done * 100:+.0f}%" if est_done else "n/a"
    L += ["", f"**Expected (whole plan): {fmt_usd(est_total)} ≈ {est_total / credit:.0f} AI credits**",
          f"**Actual~ for finished work: {fmt_usd(act_total)} ≈ {act_total / credit:.0f} credits vs expected {fmt_usd(est_done)} ({delta})**",
          f"Same work with every agent on Opus ≈ {fmt_usd(all_opus)} → routing saved ≈ {fmt_usd(max(0, all_opus - est_total))} "
          f"({(1 - est_total / all_opus) * 100 if all_opus else 0:.0f}%)."]
    if a.usage_csv:
        t0 = parse_iso(led["created"]) or 0
        t1 = parse_iso(led.get("closed")) or time.time()
        billed = read_usage_csv(a.usage_csv, t0, t1)
        tot = sum(billed.values())
        L += ["", "## Billed (GitHub usage export, task window ±1 day)", "", "| model | billed | actual~ model | diff |", "|---|---|---|---|"]
        for mk in sorted(set(billed) | set(by_model)):
            L.append(f"| {mk} | {fmt_usd(billed.get(mk, 0))} | {fmt_usd(by_model.get(mk, {}).get('act', 0))} | "
                     f"{fmt_usd(billed.get(mk, 0) - by_model.get(mk, {}).get('act', 0))} |")
        L.append(f"\nBilled total {fmt_usd(tot)}. If other Copilot work happened in the same window it is included; "
                 "use the ratio billed/actual~ to recalibrate pricing.json assumptions.")
    L += ["", "Model: input = calls × (base context + ½ work tokens), "
          f"{int(pr['assumptions']['cache_hit_ratio'] * 100)}% cache reads; prices from pricing.json."]
    out = tdir / "cost-report.md"
    out.write_text("\n".join(L) + "\n", encoding="utf-8")
    print("\n".join(L[-8:] if a.short else L))
    print(f"\nreport: {rel(out)}")


def estimate(a):
    pr = pricing()
    mk = model_for_agent(a.agent, pr) if a.agent else a.model
    r = unit_cost(mk, a.calls, a.ctx_ktok * 1000, a.out_ktok * 1000, pr)
    print(f"{a.agent or mk} ({mk}): ~{r['in_tokens']:,} in / {r['out_tokens']:,} out tokens → {fmt_usd(r['usd'])} ({r['credits']} credits)")


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    s = p.add_subparsers(dest="cmd", required=True)
    x = s.add_parser("report"); x.add_argument("--task"); x.add_argument("--usage-csv"); x.add_argument("--short", action="store_true"); x.set_defaults(f=report)
    x = s.add_parser("estimate"); x.add_argument("--agent"); x.add_argument("--model", default="sonnet")
    x.add_argument("--calls", type=float, default=10); x.add_argument("--ctx-ktok", type=float, default=20)
    x.add_argument("--out-ktok", type=float, default=3); x.set_defaults(f=estimate)
    a = p.parse_args()
    a.f(a)


if __name__ == "__main__":
    main()
