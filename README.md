# Copilot Agent Kit: cost-routed, memory-bounded agents for VS Code

Give it any task. **Opus plans and reviews, Sonnet implements, Haiku fetches, crawls and summarizes.** The work runs in small waves tracked in a ledger. All the questions come to you in one batch, together with the expected cost. It watches the machine's memory and stops its own work long before the computer could lock up, and it never runs long enough in one turn to hit the "Continue?" prompt. When the task is done you get **expected vs. actual cost**.

| Agent | Model | Job | API price in/out per 1M tokens* |
|---|---|---|---|
| **Orchestrator** (you talk to this one) | Claude Opus 5.5 | Routes work, asks the questions, runs the waves. It reads only short results | $4 / $20 |
| Planner (subagent) | Claude Opus 5.5 | Splits the task into units sized to the budget, with estimates and questions | $4 / $20 |
| Reviewer (subagent) | Claude Opus 5.5 | Reviews diffs against the acceptance criteria and schedules fix units | $4 / $20 |
| Implementer (subagent) | Claude Sonnet 5.5 | Writes the code and runs tests under a memory cap | $2 / $10 |
| Fetcher (subagent) | Claude Haiku 5.5 | Fetches pages, runs time-sliced crawls, and turns pages into digests (map → reduce) | $0.10 / $0.50 (≤100k-token requests) |
| **Monitor** (you talk to this one) | Claude Haiku 5.5 | Read-only status, cost and memory reports | $0.10 / $0.50 |

\*Since 1 June 2026 Copilot bills AI Credits (1 credit = $0.01) at each model's API rate. The prices live in `.agents/config/pricing.json`. Check them against GitHub's *Models and pricing* page.

---

## Install (about 5 minutes)

1. Copy `.github/`, `.agents/` and `.vscode/tasks.json` into the root of your workspace. **Merge** `.vscode/settings.json` into your existing settings rather than overwriting them.
2. Optional: `pip install psutil pypdf`. Neither is required. Memory is measured with built-in fallbacks if psutil can't be installed (see *Locked-down environments*), and pypdf is only needed for PDF text. Playwright is optional and only used by `--render` for JavaScript-heavy sites.
3. Run **Terminal → Run Task → "Agents: doctor (check setup)"**. Every line should be OK.
4. Open the model picker and check the model names. The agents pin `Claude Opus 5.5`, `Claude Sonnet 5.5` and `Claude Haiku 5.5`. If your org shows different labels, edit the `model:` line in each `.github/agents/*.agent.md`.
5. Keep the chat's model picker on **Claude Opus 5.5** when you use the Orchestrator, and run **`/kit-check`** once. It starts each subagent with a tiny prompt and tells you if a model name or cost tier is wrong.
6. Permissions: either keep the default level (the kit's scripts are auto-approved by `.vscode/settings.json`, and anything else asks you), or choose **Allow all**. **Don't use Autopilot**: Autopilot answers blocking questions by itself, and this kit is built to bring every question to you.

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

### 1. A task can't spiral and lock up the computer
The kit doesn't try to fit VS Code into a fixed budget. VS Code with language servers and long chats often sits at 10 GB or more, and that's fine as long as it's stable. What locks a machine is the **whole machine** running out of RAM, or on Windows the commit limit (RAM plus page file), while something keeps growing. So the kit protects against that in layers:

* **It watches machine pressure, not a total.** Every wave, `next-wave` checks free RAM and the Windows commit charge.
  - **PAUSE** when free RAM is under 10% or 1.5 GB, or commit charge is at 92% or more. No new subagents start, crawls pause, and the Orchestrator checkpoints and tells you what's wrong.
  - **WARN** under 20% free or at 85% commit.
  - Thresholds are in `budget.json`, under `memory.pause_below_*` and `commit_*`.
* **It watches VS Code's growth, not its size.** The kit remembers VS Code's lowest point during the task. If VS Code grows more than 3 GB above that (`vscode_growth_warn_mb`), you're told to continue in a **new chat**. Long chat sessions are the usual cause, and a new chat releases that memory. The ledger keeps all state, so nothing is lost.
* **Every process the kit starts is capped and polite.**
  - `memguard.py` caps each command at `worker_mb` (2 GB). On Windows that's a Job Object, so Windows itself refuses allocations past the cap.
  - The crawler and converter adapt their concurrency and keep data on disk.
  - Everything the kit starts runs at **below-normal priority**, so the mouse, the desktop and VS Code stay responsive even while a job is busy.
* **There's an emergency brake that doesn't depend on the agent.** `watchdog.py` starts automatically with the first turn: a tiny low-priority background process that checks every 3 s.
  - If free RAM falls under 800 MB, it kills the kit's own workers, largest first, until the pressure is gone, and sets a stop flag that pauses the task until RAM recovers.
  - It also kills any single worker that grows past 1.5 × `worker_mb`.
  - It never touches VS Code or anything the kit didn't start.
* **Chat context stays small.** Page text never enters the chat. `crawl.py` writes trimmed markdown to disk and prints one JSON line. Haiku digest units read batches of about 30k tokens. Only `summary.md` reaches the Orchestrator. The Fetcher has **no web-fetch tool** at all. Subagents render inline (`useRichRendering: false`), and `.agents/cache` is excluded from the file watcher, search and the explorer.

> Honest limit: no extension can cap VS Code's own Electron processes. To see what makes up your 10 GB, use **Help → Open Process Explorer**. Language servers are often the biggest part and have their own caps, for example `java.jdt.ls.vmargs` (`-Xmx2G`) or `typescript.tsserver.maxTsServerMemory`.

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
* Workers run every terminal command through `memguard.py`, which refuses destructive commands (`rm -rf /`, force-push, `git reset --hard`, `curl | sh`, shutdown, printing secrets, dumping the environment) **without needing hooks**. With the default permission level, only the kit's scripts are auto-approved, so a command that skips memguard still asks you.

### 4. Cost optimization, and expected vs. actual
* **Routing.** Mechanical work goes to Haiku and code to Sonnet. Opus only plans and reviews, and it reviews diffs, not whole repos. The Orchestrator has to be Opus because VS Code won't let a subagent run on a more expensive tier than its parent. So the Orchestrator is kept very lean: it never reads pages, digests or code.
* **Expected cost** is calculated per unit when the plan is imported, from `calls`, `ctx_ktok` and `out_ktok` with a shared cost model: agent loops resend the whole context on every call, and 75% of that is cache reads. You see the total before you approve.
* **Actual cost** uses the best source available:
  1. GitHub's usage export: `cost.py report --usage-csv usage.csv` gives billed totals per model for the task's time window.
  2. Tool calls logged by the kit's own scripts (and by hooks, if your org allows them) during each unit's time window.
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

### 6. Gates and brakes that run outside the model
These follow the "harness" and "loop" levels of building with agents: the model never decides alone that it's finished, or that it should keep spending.

* **Done gate.** `ledger.py finish --status done` is accepted only if every declared output exists and isn't empty, and every command in the unit's `verify` list passes (run through memguard). `VERIFY_FAILED` sends the worker back to fix the cause, and the third failure marks the unit failed for the Orchestrator. It needs no hooks. The Planner gives every Implementer unit a `verify` command (the narrowest test, lint or build), and `import-plan` flags any it missed. For documents, `check_md.py` checks required sections, word count, citations, a Mermaid diagram and leftover TODOs.
* **Cost cap.** Each task gets a cap: by default 1.5 × the plan's estimate, confirmed or changed by you in the first question form. `next-wave` returns `BUDGET_STOP` before a wave that would cross it, and only you can raise it (`ledger.py set-budget`).
* **No-progress stop.** If two waves in a row finish nothing (everything failed, blocked or split), `next-wave` returns `DRY_STOP` and the Orchestrator asks you instead of retrying forever. There's also a hard limit on waves per task (`agents.max_waves_per_task`).
* **Only real dependencies.** A unit should wait for another only if it reads that unit's output. `import-plan` warns `FAKE_EDGE?` otherwise, because a needless dependency makes work wait for nothing. It caught one in the threat-model example: the system model was waiting on the standards catalog it never reads.
* **Skeptic step.** For findings that will drive decisions, the Planner adds a cheap Haiku step that tries to disprove each claim against its source before it's used. Research jobs get this with `batch.py --skeptic`, and downstream units read `summary.verified.md`. The threat model's evidence and verdict units already work this way.
* **Project memory.** `.agents/PROJECT.md` (committed) holds conventions, where the standards live, and decisions. The Planner reads it at the start of every task, so you aren't asked the same things twice. At the end of a task the Orchestrator adds 2–6 lasting decisions with `ledger.py remember`. You can edit the file directly.

### 7. Unattended runs (optional, untested)
VS Code has scheduled **Automations** since 1.137 (Agents window, setting `chat.automations.enabled`, on by default from 1.138). One can run **`/auto-next-wave`** every 30–60 minutes, so work continues without you typing `/next-wave`.
* Each run starts with `ledger.py peek`, which changes nothing. If there's no work (questions waiting, memory paused, cost cap or no-progress stop, everything done), it replies with that one line and stops, so an idle run costs a few cents.
* It never waits on a question. Questions are recorded and listed for you to answer when you're back.
* I haven't been able to test whether Automations accept custom agents and the Local harness, or what your org's policy allows. Try it once on a small task before relying on it.

## Locked-down environments

| Constraint | What the kit does |
|---|---|
| **Hooks disabled by policy** | Nothing depends on them. `memguard.py` is the safety check (every non-kit command goes through it), `ledger.py next-wave` is the memory gate, and the kit logs its own tool calls to `.agents/state/calls.jsonl` for status and cost. `.github/hooks/` only adds extra telemetry where hooks are allowed. |
| **psutil can't be installed** | `memprobe.py` measures memory with what is already there. On Windows it uses ctypes (process snapshot plus working-set size, no admin rights, because VS Code runs as you) and falls back to `tasklist`. On Linux it reads `/proc`, and on macOS it runs `ps`. memguard's hard cap on Windows uses a **Job Object**, which needs no extra package. |
| **Memory can't be read at all** | State `UNMEASURED`: the kit switches to static limits (`memory.static_max_parallel_subagents` = 2, crawl concurrency 2), memguard keeps its timeout and low priority, and turn-start tells the Orchestrator to prefer a new chat per phase. `doctor` shows which method is in use and why the others failed. Free RAM is read separately (`GlobalMemoryStatusEx` on Windows), so the pressure checks and the watchdog usually still work when per-process numbers don't. |
| **A subagent's model can't cost more than the calling chat's** | Planner and Reviewer have **no pinned model**, so they inherit the chat's model and can never exceed it. Implementer (Sonnet) and Fetcher (Haiku) are cheaper than Opus. Keep the chat on Opus when you run the Orchestrator. **Economy mode:** run the chat on Sonnet and set `pricing.json` `chat_model` to `sonnet`; Planner and Reviewer then run on Sonnet. `/kit-check` catches tier and model-name problems before a real task. |
| **Subagents can't ask questions** | By design, they never need to. The Planner puts every decision into the plan's questions, which you get in one form. A worker that hits a new decision finishes `blocked --question`, and the Orchestrator batches those into one form per wave. |
| **Subagents can't start subagents** | By design, only the Orchestrator dispatches. A worker that finds more work adds **ledger units** (`ledger.py add`, `batch.py --add-units`), and the next wave picks them up. Nested subagents are never used. |
| **Python 3.13** | Tested on 3.13. Standard library only, with optional extras. On Windows, if `python` isn't on PATH, the agents use `py -3`, and the auto-approve rule covers `python`, `python3`, `py -3` and `py -3.13`. |

**VS Code version.** Newer versions are fine; the kit doesn't depend on a particular release. Two things to watch:
* From 1.140 on, VS Code also ships the **Copilot harness** (Copilot SDK in the Agent Host) and may select it by default. This kit's agent files target the **Local** harness (`runSubagent`, `askQuestions`, handoffs, VS Code tool names), so pick **Local** in the harness / session-target picker. If your org sets a permissions or sandbox policy, 1.142+ requires the Copilot harness for full chat sessions. In that case the scripts and ledger still work, but the agent files would need adapting to Copilot-format custom agents.
* None of the three subagent limits goes away in newer versions. Nesting can be switched on in the Local harness with `chat.subagents.allowInvocationsFromSubagents`, but the kit doesn't need it. Leave it off.

## Using your own skills (e.g. threat modeling)

Put your skill where VS Code finds it, e.g. `.github/skills/threat-model/SKILL.md`, and start the task as usual with `/run-task`. The Planner first runs `skills.py --match "<your request>"`:

* **Skill = method.** If a skill matches, the Planner reads its outline and only the sections it needs. Each unit's brief then points the worker at the exact section ("follow `.github/skills/threat-model/SKILL.md`, section *Credibility rules*"), so methodology, rating scales, templates and report format come from your skill.
* **Playbook = execution.** `.agents/playbooks/` holds unit layouts for recurring task types. `threat-model.md` covers your flow: gather (code inventory, Confluence, documents, standards catalog, diagrams) → system model → threats per group → credibility against internal standards → report. It also says which agent does each step and gives budget hints. When a skill and a playbook both match, the playbook gives the units and the skill gives the method.
* **The kit's rules override how a skill says to execute.** "Read all Confluence pages" becomes `confluence.py` plus Haiku digests, "read the codebase" becomes search-based inventory units, and "ask the user" becomes questions in the one up-front form.
* Units can add units at run time. For example, the system-model unit creates one threat, evidence and verdict unit per component group, and dependencies like `"C-JUDGE-*"` wait for all of them.
* `.agents/playbooks/threat-model.plan.example.json` is a complete example plan (one repo, one Confluence space, a docs folder, a standards space). It imports cleanly; the expected cost before digest units is about $2.19.

Credibility is where routing pays off most: Haiku (`C-EVID`) searches the standards catalog and the code for evidence and writes facts only, and Opus (`C-JUDGE`) reads just those short evidence files to give the verdict: Credible, Mitigated (std ID + evidence), Gap, Not applicable, or Needs info.

Add more playbooks the same way (a markdown file with `name` and `description` front matter). Run `python .agents/tools/skills.py` to see what the Planner can find.

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

### Documents: design docs, infrastructure docs, standards, diagrams
```
python .agents/tools/convert.py --job kb-hld --inputs docs/hld/ "specs/**/*.docx" infra/network.pdf
python .agents/tools/convert.py --job kb-arch-att --inputs .agents/cache/kb-arch/attachments   # Confluence attachments
```
| Format | What you get |
|---|---|
| `.docx` | Paragraphs in order, tables as `\| a \| b \|` rows, headers and footers |
| `.pptx` | `## Slide N`, slide text and speaker notes |
| `.xlsx` | `## Sheet: name`, one row per line (up to 2,000 rows per sheet) |
| `.pdf` | `## Page N` text (needs `pip install pypdf` or the `pdftotext` CLI; scanned PDFs need OCR) |
| `.drawio` (plain or compressed), `.vsdx`, `.svg` | Containers/zones, elements and connections, e.g. `API Gateway -> payments-db : JDBC/TLS` |
| `.html`, `.md`, `.txt`, `.json`, `.yaml`, `.tf`, … | Text as is |
| Images (`.png`, `.jpg`, …) | Not converted. They're listed in `.agents/cache/<job>/images.json` for the diagram step (your diagram-to-dsl skill, or a model that can see images) |
| Legacy `.doc/.ppt/.xls/.vsd` | Reported as failed with "save as .docx/.pptx/…" |

Only the standard library is used, except for PDFs. Files are processed one at a time, long documents are split into parts (never truncated), and re-runs skip unchanged files, so you can point it at a growing folder.

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
               │   BUDGET_STOP / DRY_STOP ─► ask you
               │   worker finish ─► done gate: outputs exist + verify commands pass
               └─ ALL_DONE ─► final review ─► cost.py report ─► ledger.py close ─► remember decisions
memguard.py (every non-kit command): safety check + memory cap · next-wave: memory gate · no hooks required
```

## Tuning (`.agents/config/budget.json`)

| Key | Default | Meaning |
|---|---|---|
| `memory.pause_below_available_pct` / `_mb` | 10 / 1500 | Pause the task when the machine's free RAM is this low |
| `memory.commit_pause_pct` / `commit_warn_pct` | 92 / 85 | Windows commit charge (RAM + page file) thresholds |
| `memory.vscode_growth_warn_mb` | 3072 | VS Code growth during a task before "open a new chat" |
| `memory.worker_mb` | 2048 | Hard cap per process the kit starts |
| `memory.watchdog` / `watchdog_kill_below_available_mb` | true / 800 | Emergency brake: kill the kit's workers below this much free RAM |
| `crawl.time_slice_s` | 240 | Longest a single crawl call runs |
| `crawl.start_concurrency` / `max_concurrency` | 4 / 8 | Adaptive range |
| `crawl.per_host_delay_s` | 0.5 | Politeness delay between requests to one host |
| `batching.max_tokens_per_batch` / `max_files_per_batch` | 30000 / 12 | Size of a Haiku digest unit |
| `agents.max_requests_setting` | 150 | **Keep equal to** `chat.agent.maxRequests` |
| `agents.turn_budget_pct` | 60 | Share of the request limit one turn may use |
| `agents.max_parallel_subagents` | 3 | Units per wave. Lower it to save memory |
| `agents.max_calls_per_unit` | 25 | Hard limit on unit size |
| `agents.default_budget_factor` | 1.5 | Default task cost cap = this × the plan's estimate |
| `agents.max_waves_per_task` | 40 | Hard stop on waves per task |
| `agents.verify_timeout_s` | 600 | Time limit for each verify command |

## Files

```
.github/agents/*.agent.md     the 6 agents (model pinned per agent)
.github/prompts/*.prompt.md   /run-task, /next-wave, /status, /cost-report, /kit-check, /auto-next-wave
.github/hooks/agent-kit.json  optional hooks (extra guard + telemetry) -> .agents/tools/hooks.py
.github/copilot-instructions.md   shared rules (files not chat, budgets, questions)
.agents/PROJECT.md            project memory: conventions, standards locations, decisions (committed)
.agents/config/               budget.json, pricing.json
.agents/playbooks/            task playbooks (threat-model.md + example plan)
.agents/tools/                ledger, gates, check_md, skills, crawl, confluence, convert, batch, memguard, memprobe, watchdog, status, cost, hooks, doctor (stdlib only; psutil optional)
.agents/state/  (runtime)     active task, ledgers, unit briefs, notes, reviews, call log, progress
.agents/cache/  (runtime)     crawled pages, batches, digests, summary.md
```

## Things to check in your environment
* **Model labels** in your org's picker (see Install, step 4).
* **Hooks** are optional (see *Locked-down environments*).
* **`chat.agent.maxRequests`** may be capped by your org or by an experiment. If so, set `agents.max_requests_setting` to the real value.
* **Billing mode.** If your org is still on legacy premium requests, set `legacy_premium_requests.enabled` in `pricing.json` and fill in the multipliers. The report will then show premium requests as well.
* **Confluence**: a 401/403 means the token can't see that content. memguard refuses commands that print tokens or dump environment variables, so secrets stay out of chat history.
* `vscode_process_names`: if `doctor` says it finds no VS Code processes while VS Code is open, add your process names to this list.
