#!/usr/bin/env python3
"""Copilot hook handler (wired in .github/hooks/agent-kit.json). OPTIONAL: if your org disables
hooks, the same deny list is enforced by memguard.py and memory gating by ledger.py next-wave.

  hooks.py preToolUse     -> guard: block destructive commands, block new subagents when
                             memory is over budget, block bulk web/fetch (use crawl.py), log
  hooks.py <other event>  -> log only (sessionStart, postToolUse, subagentStart/Stop, agentStop, sessionEnd)

Writes one trimmed line per event to .agents/state/hooks.jsonl; status.py and cost.py
use it to count real tool calls per unit. Must never crash: on any error it prints {}.
"""
from __future__ import annotations

import json
import re
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

BULK_FETCH_LIMIT = 8          # web/fetch calls allowed per 10 minutes before forcing crawl.py
BULK_FETCH_WINDOW = 600


def out(obj: dict):
    sys.stdout.write(json.dumps(obj))
    sys.stdout.flush()


def deny(reason: str):
    out({"permissionDecision": "deny", "permissionDecisionReason": reason})


def main():
    event = sys.argv[1] if len(sys.argv) > 1 else "unknown"
    try:
        raw = sys.stdin.read() if not sys.stdin.isatty() else ""
        data = json.loads(raw) if raw.strip() else {}
    except Exception:
        data = {}
    from common import STATE, append_jsonl, deny_reason, load_json, memory_snapshot, now_iso, read_jsonl, save_json

    tool = data.get("toolName") or data.get("tool_name") or ""
    args = data.get("toolArgs", data.get("tool_input", ""))
    if isinstance(args, str):
        try:
            args = json.loads(args)
        except Exception:
            pass
    if isinstance(args, dict) and isinstance(args.get("command"), str):
        cmd_s = args["command"]
    else:
        cmd_s = args if isinstance(args, str) else json.dumps(args, ensure_ascii=False)
    args_s = args if isinstance(args, str) else json.dumps(args, ensure_ascii=False)
    log = STATE / "hooks.jsonl"
    append_jsonl(log, {"ts": now_iso(), "t": time.time(), "event": event, "tool": tool, "args": args_s[:200],
                       "session": data.get("sessionId") or data.get("session_id"),
                       "agent": data.get("agentName") or data.get("agent_name") or data.get("subagentName")})

    if event.lower() != "pretooluse":
        out({})
        return

    tl = tool.lower()
    # 1) destructive terminal commands (we run with "Allow all", so this is the safety net)
    if any(k in tl for k in ("terminal", "bash", "shell", "execute", "powershell")):
        why = deny_reason(cmd_s)
        if why:
            deny(f"Blocked by agent-kit guard: {why}. Ask the user to run it manually if it is really intended.")
            return

    # 2) memory gate for new subagents
    if "subagent" in tl or tl in ("agent", "task", "runsubagent"):
        cache = STATE / "mem_cache.json"
        snap = load_json(cache, {}) or {}
        if time.time() - snap.get("_t", 0) > 5:
            snap = memory_snapshot()
            snap["_t"] = time.time()
            save_json(cache, snap)
        if snap.get("state") == "PAUSE":
            deny(f"Machine memory is low ({'; '.join(snap.get('reasons', []))}). Do not start subagents. Checkpoint with ledger.py, end the turn and ask "
                 "the user to open a NEW chat and run /next-wave.")
            return

    # 3) bulk web fetching belongs in crawl.py, not in chat context
    if tl.endswith("fetch") or tl in ("web", "webfetch", "fetch_webpage"):
        now = time.time()
        recent = [e for e in read_jsonl(log, tail=600)
                  if e.get("event", "").lower() == "pretooluse" and str(e.get("tool", "")).lower().endswith("fetch")
                  and now - e.get("t", 0) < BULK_FETCH_WINDOW]
        if len(recent) > BULK_FETCH_LIMIT:
            deny(f"More than {BULK_FETCH_LIMIT} page fetches in 10 min. Write the URLs to a file and use "
                 "`python .agents/tools/crawl.py --job <name> --seeds-file <file> --max-depth 0` instead; it is "
                 "memory-capped and keeps page text out of the chat.")
            return
    out({})


if __name__ == "__main__":
    try:
        main()
    except Exception:
        out({})
