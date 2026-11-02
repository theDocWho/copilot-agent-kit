# Workspace rules for all agents (agent kit)

This workspace uses a cost-routed agent team (see `.github/agents/`) and a task ledger in `.agents/`.

- **Files, not chat.** Large results (pages, logs, data, digests, reviews) go into files under `.agents/` or the paths a unit brief names. Chat replies stay short: status, paths, one-line findings. Never paste large file contents or command output into chat.
- **Done gates.** A unit is done only when `ledger.py finish --status done` accepts it (outputs exist, verify commands pass). Never weaken a test or verify command to get through.
- **Project memory.** `.agents/PROJECT.md` holds conventions and past decisions; check it before asking the user.
- **Bounded work.** Respect the tool-call budget in your unit brief. When it is nearly used up, checkpoint (notes + follow-up units) instead of pushing on.
- **Commands.** Every terminal command other than the kit's own `.agents/tools/*.py` scripts runs through `python .agents/tools/memguard.py --shell "<command>"`. It is both the safety check (works without hooks) and the memory cap. Exit 126 means refused; never route around it.
- **PowerShell.** `memguard.py --pwsh '<code>'` for one-liners, or write a `.ps1` under `.agents/state/scripts/` and use `memguard.py --pwsh-file <file>`. Same safety check and memory cap as `--shell`.
- **Crash-safe progress.** Workers record `ledger.py checkpoint <ID> "<done; next>"` after each meaningful step; an interrupted unit resumes from its checkpoints and partial outputs.
- **Memory.** Crawl with `.agents/tools/crawl.py`, never by fetching dozens of pages in chat. If a tool reports `memory_pause` or the guard blocks a subagent for memory, stop and checkpoint.
- **Questions.** Workers never ask the user directly. They finish their unit with `--status blocked --question "..."`; the Orchestrator batches questions into one prompt.
- **No long blocking commands.** Nothing in the terminal may run longer than ~5 minutes per call; use time slices (`crawl.py` does this) or `memguard.py --timeout`.
- Use `python` for the kit's scripts (if `python` is not found: `py -3` on Windows, `python3` elsewhere).
