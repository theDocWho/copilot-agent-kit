# Workspace rules for all agents (agent kit)

This workspace uses a cost-routed agent team (see `.github/agents/`) and a task ledger in `.agents/`.

- **Files, not chat.** Large results (pages, logs, data, digests, reviews) go into files under `.agents/` or the paths a unit brief names. Chat replies stay short: status, paths, one-line findings. Never paste large file contents or command output into chat.
- **Bounded work.** Respect the tool-call budget in your unit brief. When it is nearly used up, checkpoint (notes + follow-up units) instead of pushing on.
- **Memory.** Run heavy commands through `python .agents/tools/memguard.py -- <cmd>`. Crawl with `.agents/tools/crawl.py`, never by fetching dozens of pages in chat. If a tool reports `memory_pause` or the guard blocks a subagent for memory, stop and checkpoint.
- **Questions.** Workers never ask the user directly. They finish their unit with `--status blocked --question "..."`; the Orchestrator batches questions into one prompt.
- **No long blocking commands.** Nothing in the terminal may run longer than ~5 minutes per call; use time slices (`crawl.py` does this) or `memguard.py --timeout`.
- Use `python` for the kit's scripts (or `python3` if `python` is not found).
