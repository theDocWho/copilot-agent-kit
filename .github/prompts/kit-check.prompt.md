---
description: Preflight - verify models, cost tiers and subagents work before a real task (costs a few cents)
agent: Orchestrator
---
Preflight check, not a task. Do not init a ledger.
1. Run `python .agents/tools/doctor.py` (if `python` is not found: `py -3` on Windows) and note any FAIL/WARN.
2. In ONE response start these subagents in parallel with runSubagent, each with the prompt `Preflight check` and no model override: Planner, Fetcher, Implementer, Reviewer.
3. Report a table: agent | expected model | result (OK + model, or the exact error). Expected: Planner and Reviewer = your model, Implementer = Claude Sonnet 5.5, Fetcher = Claude Haiku 4.5.
4. If any failed with a cost-tier error: say which model the chat must be on. If a model name is unknown: say which `model:` line in .github/agents/ to change. End with "ready" or the list of fixes.
