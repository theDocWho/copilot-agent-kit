---
name: threat-model
description: Playbook for threat modeling a system from its codebase, Confluence pages, infrastructure and high-level design documents and architecture diagrams, then judging each threat's credibility against internal security standards and processes. Defines phases, units, routing and budgets; the user's threat-model skill (if installed) defines the method.
---
# Threat-model playbook

This playbook says **how the work is split and routed**. If a threat-model skill is installed (`skills.py --match "threat model"`), that skill is **authoritative for method**: methodology (STRIDE, LINDDUN, attack trees…), rating scales, templates, credibility rules and report format. Reference its sections from each unit with the `skill` field. Without a skill, use STRIDE per element and a 3×3 likelihood × impact scale.

Output folder for the deliverable: `threat-model/` in the workspace (or what the user chooses in Q-OUT).
Knowledge-base jobs: `kb-code`, `kb-conf-<space>`, `kb-docs`, `kb-standards`, `kb-<x>-att`.

## Questions (ask all up front)
- **Q-SCOPE** Which repos/folders and which services are in scope? What is explicitly out of scope?
- **Q-CONF** Which Confluence spaces, labels or page trees (ancestor page ids) describe this system? Include attachments (diagrams)?
- **Q-DOCS** Which folders/files hold infrastructure docs and high-level design documents?
- **Q-STD** Where are the internal security standards and processes (Confluence space/label, folder)? Which ones apply?
- **Q-METHOD** Methodology and rating scale (default: your skill's, else STRIDE + 3×3).
- **Q-OUT** Output location and format (default `threat-model/THREAT_MODEL.md`).
- **Q-CRED** Are Confluence credentials set in the VS Code terminal environment? (needed before any Confluence unit)
Blocking: Q-SCOPE, Q-STD, Q-CRED (only if Confluence is used). Others have defaults.

## Phase 1 — Gather (all units independent → same wave)
| Unit | Agent | What | Output |
|---|---|---|---|
| G-CODE-<repo> | Fetcher | Inventory one repo with text search and targeted reads only (no full-tree reads): components/services, entry points (HTTP routes, gRPC, queues, schedulers, CLIs), data stores, external calls, authentication and authorization code, secrets handling, crypto/TLS settings, IaC/Docker/K8s/CI files, dependency manifests. Each item cites `file:line`. One unit per repo or top-level service; ≤ 20 calls. | `threat-model/inventory/code-<repo>.md` |
| G-CONF-<space> | Fetcher | `confluence.py --job kb-conf-<space> --space <KEY> [--label …] [--ancestor …] --attachments`, re-run while time_slice, then the printed `convert.py` (attachments) and `batch.py --add-units --question "architecture, components, data flows, trust boundaries, data classification, security controls, dependencies"` | digest units → `.agents/cache/kb-conf-<space>/summary.md` |
| G-DOCS | Fetcher | `convert.py --job kb-docs --inputs <Q-DOCS paths>` then `batch.py --job kb-docs --add-units --question` (same question) | `.agents/cache/kb-docs/summary.md` |
| G-STD | Fetcher | Pull the standards (Confluence or files) into job `kb-standards`, then `batch.py --job kb-standards --add-units --question "list every requirement as: ID | requirement (short) | applies to | how compliance is evidenced"` | `.agents/cache/kb-standards/summary.md` = **standards catalog** |
| G-IMG | Fetcher | For each image in the `images.json` files: if the diagram-to-dsl skill is installed, run its parser to get a model plus C4/Mermaid; list the images it cannot parse. | `threat-model/inventory/diagrams.md` (+ DSL files) |

Images that no parser can read: add one Implementer unit only if your Copilot models accept image input; otherwise add a question asking the user for a text description or the source `.drawio`.

## Phase 2 — System model
Depends on every Phase 1 unit **and** on `R-<job>-*` for each knowledge-base job (the digest/reduce units the gather units add at run time), e.g. `["G-CODE-api", "G-CONF-ARCH", "R-kb-conf-ARCH-*", "G-DOCS", "R-kb-docs-*", "G-IMG"]`. Not on G-STD: the model never reads the standards, so that edge would only make it wait. The standards catalog is needed in Phase 4, so C-EVID units depend on `R-kb-standards-*`.
| Unit | Agent | What | Output |
|---|---|---|---|
| M-MODEL | Implementer | (verify: `check_md.py threat-model/system-model.md --sections "Components,Data flows,Trust boundaries" --mermaid --citations 5`) Build the system model from the inventories and summaries only: components, data stores, external entities, data flows (Mermaid DFD), trust boundaries, data classification, assets. Every element cites its sources. **List conflicts** between docs and code ("HLD says mTLS, code uses plain HTTP") — these are findings. | `threat-model/system-model.md` |
| M-REVIEW | Reviewer | Check the model against the sources it cites (spot-check 3–5), missing boundaries, wrong flows. PASS or fix units. Small input → cheap Opus. | review file |

## Phase 3 — Threats (parallel per group, depends on M-REVIEW)
Groups are only known once the model exists, so **M-MODEL creates the Phase 3–4 units**: it splits the model into groups of ≤ 5 elements or one trust boundary each and adds `T-<group>`, `C-EVID-<group>` (depends on `T-<group>` and `R-kb-standards-*`) and `C-JUDGE-<group>` (depends on `C-EVID-<group>`) with `ledger.py add`, then mentions the group list in its notes. T units also depend on M-REVIEW.
| Unit | Agent | What | Output |
|---|---|---|---|
| T-<group> | Implementer | Enumerate threats per element and flow with the skill's method. Table: ID, element/flow, category, threat, attack path, preconditions, impact, controls observed (with citation), initial likelihood × impact. No credibility verdicts yet. ≤ 18 calls. | `threat-model/threats/<group>.md` |

## Phase 4 — Credibility against internal standards (per group)
| Unit | Agent | What | Output |
|---|---|---|---|
| C-EVID-<group> | Fetcher | For each threat: find the matching requirements in the standards catalog (search by keywords and category), plus evidence that the control exists or is missing (search the inventories, code `file:line`, config, Confluence summaries). **Facts only, no verdicts.** | `threat-model/credibility/<group>-evidence.md` |
| C-JUDGE-<group> | Reviewer | Verdict per threat, using the skill's credibility rules: **Credible** / **Mitigated** (control required by <std ID> and evidenced at <citation>) / **Gap** (required by <std ID>, not evidenced) / **Not applicable** (why) / **Needs info** (question). Adjust the risk rating. Reads only the threats and evidence files. | `threat-model/credibility/<group>.md` |

This split is where the cost saving comes from: Haiku does the searching, and Opus reads only short evidence files.

## Phase 5 — Report
| Unit | Agent | What | Output |
|---|---|---|---|
| R-REPORT (depends on `"C-JUDGE-*"`; verify: `check_md.py threat-model/THREAT_MODEL.md --sections "Scope,Threats,Gaps,Sources" --min-words 800 --citations 10`) | Implementer | Assemble the report in the skill's template: scope, assumptions, system model + DFD, threats sorted by residual risk with verdicts, gaps against standards (std ID → threat → recommended control), doc/code conflicts, open questions, sources (Confluence URLs with version, file paths). | `threat-model/THREAT_MODEL.md` |
| FINAL-REVIEW | Reviewer | Check against the original request and the skill's quality bar. | review file |

## Budget hints (estimates for the plan)
- Fetcher inventory per repo: calls 15–20, ctx_ktok 40, out_ktok 4.
- Crawl/convert units: calls 4–10 (time slices), ctx_ktok 5, out_ktok 1. Digest units are added automatically.
- M-MODEL: calls 14, ctx_ktok 45, out_ktok 8. T-<group>: calls 12, ctx_ktok 30, out_ktok 6.
- C-EVID: calls 15, ctx_ktok 35, out_ktok 4. C-JUDGE: calls 8, ctx_ktok 20, out_ktok 4.
- R-REPORT: calls 12, ctx_ktok 40, out_ktok 10.
