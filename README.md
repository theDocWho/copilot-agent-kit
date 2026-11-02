# Copilot Agent Kit: cost-routed, memory-bounded agents for VS Code

Give it any task. **Opus plans and reviews, Sonnet implements, Haiku fetches, crawls and summarizes.** The work runs in small waves tracked in a ledger. All the questions come to you in one batch, together with the expected cost. The kit stays inside a memory budget you set (4 GB by default) and never runs long enough in one turn to hit the "Continue?" prompt. When the task is done you get **expected vs. actual cost**.

| Agent | Model | Job | API price in/out per 1M tokens* |
|---|---|---|---|
| **Orchestrator** (you talk to this one) | Claude Opus 5.5 | Routes work, asks the questions, runs the waves. It reads only short results | $4 / $20 |
| Planner (subagent) | Claude Opus 5.5 | Splits the task into units sized to the budget, with estimates and questions | $4 / $20 |
| Reviewer (subagent) | Claude Opus 5.5 | Reviews diffs against the acceptance criteria and schedules fix units | $4 / $20 |
| Implementer (subagent) | Claude Sonnet 5.5 | Writes the code and runs tests under a memory cap | $2 / $10 |
| Fetcher (subagent) | Claude Haiku 4.5 | Fetches pages, runs time-sliced crawls, and turns pages into digests (map → reduce) | $1 / $5 |
| **Monitor** (you talk to this one) | Claude Haiku 4.5 | Read-only status, cost and memory reports | $1 / $5 |

\*Since 1 June 2026 Copilot bills AI Credits (1 credit = $0.01) at each model's API rate. The prices live in `.agents/config/pricing.json`. Check them against GitHub's *Models and pricing* page.

---

## Install (about 5 minutes)

1. Copy `.github/`, `.agents/` and `.vscode/tasks.json` into the root of your workspace. **Merge** `.vscode/settings.json` into your existing settings rather than overwriting them.
2. Run `pip install psutil`. It is the only dependency, and memory measurement and caps need it. Playwright is optional and only used by `--render` for JavaScript-heavy sites.
3. Run **Terminal → Run Task → "Agents: doctor (check setup)"**. Every line should be OK.
4. Open the model picker and check the model names. The agents pin `Claude Opus 5.5`, `Claude Sonnet 5.5` and `Claude Haiku 4.5`. If your org shows different labels, edit the `model:` line in each `.github/agents/*.agent.md`.
5. In the chat permissions picker choose **Allow all**, or type `/yolo`. **Don't use Autopilot**: Autopilot answers blocking questions by itself, and this kit is built to bring every question to you.

## Use

| You want to… | Do this |
|---|---|
| Start a task | Type `/run-task` in chat and describe the task, or pick **Orchestrator** in the agent dropdown |
| Answer questions | They arrive as one form. The last question is "Expected cost ≈ $X. Proceed?" |
| Watch progress at no model cost | Run Task → **Agents: watch status**. It refreshes every 15 s and also writes `.agents/STATUS.md` |
| Ask for status in chat | Type `/status`, or switch to the **Monitor** agent (Haiku, about 3 tool calls) |
| Resume after a pause or a memory warning | Type `/next-wave`. It works in a **brand-new chat** because all state is in the ledger |
| See expected vs. actual cost | Type `/cost-report`, or Run Task → **Agents: cost report** (writes `cost-report.md`) |

---

## How each requirement is handled

### 1. Stay inside a 4 GB memory budget
There are three things that make VS Code hang, and each one has a fix:

* **Huge chat context.** Page text never enters the chat. `crawl.py` writes trimmed markdown to disk and prints one JSON line. Haiku digest units read a batch of at most about 30k tokens and write a digest of about 1,200 words. Only `summary.md` reaches the Orchestrator. A guard hook **blocks a 9th `web/fetch` within 10 minutes** and tells the agent to use `crawl.py` instead.
* **Heavy processes.** The crawler keeps its frontier in SQLite, so its memory stays flat whether there are 1k or 100k URLs. Its concurrency adapts: it halves above 80% of `worker_mb` and grows below 50%. Builds and tests run under `memguard.py`, which kills the whole process tree when it goes over the cap or the timeout and returns exit code 137 with the reason. The agent then retries with a smaller batch.
* **Global budget.** `total_mb` (4096) covers *all VS Code processes plus every worker the kit starts*. When the total passes `pause_at_pct` (90%):
  - the crawler pauses, then exits with `memory_pause`. The crawl can be resumed later.
  - the guard hook **refuses to start new subagents**.
  - `next-wave` returns `MEMORY_PAUSE`, and the Orchestrator checkpoints and tells you to resume in a new chat. A new chat frees the renderer memory used by the old one.

  If VS Code alone goes over `vscode_soft_limit_mb` (2560), you get "open a new chat" before anything breaks.
* Other settings help too: subagents render inline (`useRichRendering: false`), and `.agents/cache` is excluded from the file watcher, search and the explorer. Thousands of crawled files would otherwise bloat VS Code itself.

> Honest limit: no extension can hard-cap the memory of VS Code's own Electron processes. The kit *measures* VS Code, caps everything it launches, stops adding load when the total is high, and moves work to a fresh chat to free memory.

### 2. No "Continue?" prompts and no endless sessions
* `chat.agent.maxRequests` = 150. The ledger gives each chat turn a **budget of 60% of that** (90 tool calls), and the budget counts the Orchestrator plus every subagent it starts. `next-wave` only schedules units whose estimates fit what's left of the budget. When the budget is used up it returns `END_TURN`, and the Orchestrator checkpoints and stops cleanly. It never gets cut off in the middle of a unit.
* Every unit is limited to ≤ `max_calls_per_unit` (25) tool calls. The Planner can't import a unit that's bigger. Each worker has a hard budget in its brief. When a worker runs out, it writes notes, adds follow-up units and finishes as `split`.
* Long jobs are **time-sliced**. One `crawl.py` call runs for at most `time_slice_s` (240 s) and then returns; running it again resumes the crawl. `memguard.py --timeout` does the same for builds. No tool call blocks for long.
* Subagents can't start subagents (`allowInvocationsFromSubagents: false`), and fix loops are capped at 2 rounds.
* All state is in `.agents/state/tasks/<id>/ledger.json`, so `/next-wave` continues anywhere, including a new chat on another day.

### 3. Questions reach you even under "Allow all"
Subagents can't ask questions in VS Code (by design). Instead:
* The Planner lists every decision up front: scope, ambiguities, irreversible actions, credentials, which sites to crawl. The Orchestrator asks them **all in one `askQuestions` form**, together with the expected cost, *before* any work starts.
* If a worker hits a real decision later, it finishes its unit as `blocked --question`. Other units keep running, and at the end of the wave the Orchestrator asks all the new questions in one form.
* Allow all is safe enough because the guard hook still denies destructive commands: `rm -rf /`, force-push, `git reset --hard`, `curl | sh`, shutdown and similar.

### 4. Cost optimization, and expected vs. actual
* **Routing.** Mechanical work goes to Haiku and code to Sonnet. Opus only plans and reviews, and it reviews diffs, not whole repos. The Orchestrator has to be Opus because VS Code won't let a subagent run on a more expensive tier than its parent. So the Orchestrator is kept very lean: it never reads pages, digests or code.
* **Expected cost** is calculated per unit when the plan is imported, from `calls`, `ctx_ktok` and `out_ktok` with a shared cost model: agent loops resend the whole context on every call, and 75% of that is cache reads. You see the total before you approve.
* **Actual cost** uses the best source available:
  1. GitHub's usage export: `cost.py report --usage-csv usage.csv` gives billed totals per model for the task's time window.
  2. Tool calls counted by the hooks during each unit's time window.
  3. What the worker reported in `ledger.py finish`.
* The report also shows **"same work with every agent on Opus"**, so you can see what the routing saved. If the actual numbers keep differing from the estimates, adjust `assumptions` in `pricing.json`.

Example (from the kit's own test run):
```
Expected (whole plan): $2.24 ≈ 224 AI credits
Actual~ for finished work: $0.93 ≈ 93 credits vs expected $1.77 (-47%)
Same work with every agent on Opus ≈ $4.06 → routing saved ≈ $1.81 (45%)
```

### 5. Monitoring
* `status.py` shows % done, running units and how long they've run, questions waiting for you, failed or blocked units, crawl progress (pages/s), memory against the budget, cost so far and an ETA. It flags **STALLED?** when units are running but no agent has called a tool for 10 minutes. That usually means a question or a Continue prompt is waiting in the Orchestrator chat.
* The **Monitor** agent relays the same information in chat for a few cents of Haiku.

---

## Internal sources

Everything below writes into the same job store as the web crawler, so `batch.py --job <job> --add-units` turns any source into Haiku digest units the same way. Secrets are always passed as the **name** of an environment variable. Their values are read at run time, sent only to the source's own host (and stripped if a redirect leaves it), and never written to disk, printed, or put in chat.

### Confluence (REST API, Cloud or Data Center)
Set the credentials in the terminal VS Code runs in (or your OS user environment), not in any file in the repo:

```
CONFLUENCE_URL    Cloud: https://<site>.atlassian.net/wiki    Data Center: https://confluence.corp[/context]
CONFLUENCE_TOKEN  Cloud: API token                            Data Center: personal access token
CONFLUENCE_EMAIL  Cloud only (Basic auth). Leave unset for Data Center (Bearer token).
```

```
python .agents/tools/confluence.py --check                                 # test URL + credentials
python .agents/tools/confluence.py --job kb-sec  --space SEC --label standard
python .agents/tools/confluence.py --job kb-arch --ancestor 123456 --attachments   # a page tree + its diagrams/docs
python .agents/tools/confluence.py --job kb-x    --cql 'space = OPS and title ~ "runbook"'
```
It uses the CQL search API with `_links.next` paging. Each page becomes markdown with page id, space, breadcrumb, version, modified date and URL in its front matter, so findings can cite the exact page. Confluence code blocks are kept. Long pages are split into parts, not truncated. `--attachments` downloads images and documents attached to the pages. The crawl is time-sliced and resumable like `crawl.py`, and it backs off on HTTP 429. It refuses to run without a filter, so it never dumps a whole site by accident.

### Other internal sites (intranet, SharePoint pages, wikis)
```
python .agents/tools/crawl.py --job infra --seeds https://intranet/infra/ --auth-env INTRANET_TOKEN
python .agents/tools/crawl.py --job portal --seeds https://portal/docs/ --cookie-env PORTAL_COOKIE
```
`--auth-env` takes a token (`Bearer x`, `user:token` for Basic, or a raw token, sent as Bearer). `--cookie-env` reuses a browser session cookie for SSO-only sites: copy the `Cookie` request header from your browser's dev tools into the variable.

---

## Flow

```
/run-task ──► Orchestrator (Opus)
               │ ledger.py init
               ├─► Planner (Opus) ── plan.json ──► ledger.py import-plan  (estimates, size checks)
               │ askQuestions: all questions + "Expected $X, proceed?"
               │ loop: ledger.py next-wave
               │   DISPATCH ─► Fetcher (Haiku)     crawl.py slices ─► batch.py ─► digest units ─► summary.md
               │           ─► Implementer (Sonnet) edit + memguard tests ─► notes/<unit>.md
               │           ─► Reviewer (Opus)      git diff + tests ─► reviews/<unit>.md (+ fix units)
               │   BLOCKED ─► one askQuestions batch
               │   END_TURN / MEMORY_PAUSE ─► checkpoint, "run /next-wave"
               └─ ALL_DONE ─► final review ─► cost.py report ─► ledger.py close
guard hook (every tool call): blocks destructive commands, bulk fetching, and new subagents when memory is over budget
```

## Tuning (`.agents/config/budget.json`)

| Key | Default | Meaning |
|---|---|---|
| `memory.total_mb` | 4096 | VS Code plus all workers |
| `memory.vscode_soft_limit_mb` / `worker_mb` | 2560 / 1024 | Split of the total |
| `memory.pause_at_pct` / `resume_at_pct` | 90 / 75 | Backpressure thresholds |
| `crawl.time_slice_s` | 240 | Longest a single crawl call runs |
| `crawl.start_concurrency` / `max_concurrency` | 4 / 8 | Adaptive range |
| `crawl.per_host_delay_s` | 0.5 | Politeness delay between requests to one host |
| `batching.max_tokens_per_batch` / `max_files_per_batch` | 30000 / 12 | Size of a Haiku digest unit |
| `agents.max_requests_setting` | 150 | **Keep equal to** `chat.agent.maxRequests` |
| `agents.turn_budget_pct` | 60 | Share of the request limit one turn may use |
| `agents.max_parallel_subagents` | 3 | Units per wave. Lower it to save memory |
| `agents.max_calls_per_unit` | 25 | Hard limit on unit size |

## Files

```
.github/agents/*.agent.md     the 6 agents (model pinned per agent)
.github/prompts/*.prompt.md   /run-task, /next-wave, /status, /cost-report
.github/hooks/agent-kit.json  guard + telemetry hooks -> .agents/tools/hooks.py
.github/copilot-instructions.md   shared rules (files not chat, budgets, questions)
.agents/config/               budget.json, pricing.json
.agents/tools/                ledger, crawl, confluence, batch, memguard, status, cost, hooks, doctor (stdlib + psutil)
.agents/state/  (runtime)     active task, ledgers, unit briefs, notes, reviews, hook log, progress
.agents/cache/  (runtime)     crawled pages, batches, digests, summary.md
```

## Things to check in your environment
* **Model labels** in your org's picker (see Install, step 4).
* **Hooks** need `chat.useHooks` and a trusted workspace. On Windows the hook runs `python`, and on macOS/Linux it runs `python3`. If hooks don't fire, everything else still works, but you lose the guard, the memory gate and the hook-based call counts.
* **`chat.agent.maxRequests`** may be capped by your org or by an experiment. If so, set `agents.max_requests_setting` to the real value.
* **Billing mode.** If your org is still on legacy premium requests, set `legacy_premium_requests.enabled` in `pricing.json` and fill in the multipliers. The report will then show premium requests as well.
* **Confluence**: a 401/403 means the token can't see that content. The guard hook blocks commands that print tokens or dump environment variables, so secrets stay out of chat history.
* `vscode_process_names`: if `doctor` says it finds no VS Code processes while VS Code is open, add your process names to this list.
