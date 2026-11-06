# Brief — DOCS-AUDIT-01 (AUDIT) · docs tree cleanup audit · run 20261106-memory-remembering-agent

## 1. Role and mission
Mission: Audit /docs tree (all subfolders) for useless/generated items + tidy .gitignore so they are not committed, WITHOUT touching files owned by the parallel FINAL-IMPR-01 agent (see §4).
Role: auditor. Read-only on docs; may edit ONLY .gitignore.

## 2. Required reading (in order)
1. docs/agent-ops/20261106-memory-remembering-agent/templates/STANDING-ORDERS.md (binding, esp. ownership + COMMS)
2. AGENTS.md (docs conventions; path:line lock)
3. docs/agent-ops/20261106-memory-remembering-agent/00-mission.md (run branch/commands)
4. COMMS.md (read at start)

## 3. Task
- Enumerate the /docs tree (all files, incl. subfolders) using glob + `git ls-files` evidence (distinguish tracked vs untracked; run output is evidence).
- Classify each file: (a) live docs (keep), (b) run-trail docs/agent-ops (keep — this run's memory), (c) dated/historical proposals (keep but note exclusion from conventions lock), (d) useless/generated (pycache, .pyc, .tmp/.swp/.bak, editor artifacts, generated indexes, empty placeholders, duplicated checkpoints).
- For (d): recommend .gitignore additions ONLY (do NOT delete committed files — irreversible, needs operator approval). Write .gitignore additions for truly generated patterns already present/observed in /docs (evidence: paths + counts). If a tracked file is useless, list it as a DELETE-CANDIDATE for the operator; do NOT delete or untrack it.
- Update docs? Only if you find a docs file that is a near-certain autocorrect/junk (exclude list below); otherwise do NOT edit docs content (the parallel agent owns that and any line shift breaks the conventions lock).
- Report: docs/agent-ops/20261106-memory-remembering-agent/reports/final/DOCS-AUDIT-01.md (ORCH-DIGEST first; table of useless items + recommended ignores with evidence).

## 4. Ownership
WRITE-SET (may edit ONLY these):
- .gitignore
- docs/agent-ops/20261106-memory-remembering-agent/reports/final/DOCS-AUDIT-01.md
READ-ONLY: everything else, ESPECIALLY the parallel agent's files (DO NOT EDIT, DO NOT REWRITE):
- docs/modules/daemon.md
- docs/modules/memory.md
- docs/operations/deployment-and-runtime.md
- docs/deployment/docker.md
- ALL files under docs/agent-ops/ (except your own report above)
FORBIDDEN: deleting files, `git rm`, moving/renaming, editing docs content, editing src/, config, schema.
Need something outside write-set? Post NEED; DEFERRED-DEPENDENCY.

## 5. Siblings (parallel, disjoint write-sets)
| Agent | Owns (this wave) | Contracts changed |
| FINAL-IMPR-01 | src/, tests/, 4 operator docs (above) | daemon/curator/creator behavior + operator docs |
| DOCS-AUDIT-01 | .gitignore + own report only | none |

## 6. Commands (scoped only)
`git ls-files docs/` · `git status --porcelain` for docs/ untracked evidence. Do NOT run pytest/ruff/mypy (not your lane). If you add .gitignore patterns, verify with `git check-ignore` / `git status --porcelain` evidence.

## 7. Commits & deliverables
Commits: code (.gitignore) first, then report, trailers `Task: DOCS-AUDIT` / `Agent: DOCS-AUDIT-01` / `Wave: FINAL` / `Report: <path>`. Explicit paths only. Stay on `ops/20261106-memory-remembering-agent`.
Final ≤8 lines.
