---
description: For a scheduled automation - continue the active task only if there is work, never wait for a human
agent: Orchestrator
---
Scheduled run. Nobody is watching this chat.
1. Run `python .agents/tools/ledger.py peek` (if `python` is not found: `py -3` on Windows). This is cheap and changes nothing.
2. If its first word is NOT `WORK_READY`, reply with that one line and stop. Make no other tool calls.
3. Otherwise follow your turn protocol from step 1 with two differences: do not call askQuestions (if questions come up, record them, finish the wave and end with the list for the user), and stop at the first END_TURN, MEMORY_PAUSE, BUDGET_STOP, DRY_STOP or WAVE_LIMIT.
