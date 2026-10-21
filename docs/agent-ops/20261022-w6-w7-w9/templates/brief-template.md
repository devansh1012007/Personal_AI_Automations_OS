# Brief — <AGENT-ID> (<ROLE>) · wave <N> · run <run-id>

## 1. Role and mission
Mission (2 sentences): …  Why this task matters / what failure looks like: …
Your role: <investigator | implementer | deep-dive | code reviewer | test reviewer | improver | final reviewer>

## 2. Required reading (in this order)
1. docs/agent-ops/<run-id>/templates/STANDING-ORDERS.md (binding)
2. AGENTS.md (+ nested AGENTS.md nearest your files), CLAUDE.md/CONTRIBUTING.md if present
3. docs/agent-ops/<run-id>/00-mission.md (commands, hotspots, baseline, assumptions)
4. Plan: <path> — items <P-xxx>, quoted verbatim: "<short excerpt>"
5. Investigation report: docs/agent-ops/<run-id>/reports/w0-investigation/<file> (sections <…>)
6. Template for your report: docs/agent-ops/<run-id>/templates/<file>
7. COMMS.md from the last WAVE-START
8. <other: ADRs, architecture docs, related code entry points with path:line>

## 3. Task and acceptance criteria
Task <T-xxx>: <statement>. Root cause (verified in investigation): <…>. Recommended approach: <…> (you may deviate with evidence).
Acceptance criteria (measurable):
- …
Tests that must exist and fail without the fix: …
Commands that must pass (scoped): <exact commands>

## 4. Ownership
WRITE-SET (may edit): <exact paths>
READ-ONLY: <paths>
FORBIDDEN (hotspots / other agents' files): <paths>
Need something outside? Don't edit; post NEED; report DEFERRED-DEPENDENCY with the exact change.

## 5. Parallel siblings this wave
| Agent | Task | Owns | Contracts it changes |
Known interactions: <…>. You cannot see their uncommitted work.

## 6. Skip list and known traps
Already fixed (don't redo): … · False leads: … · Flaky/pre-existing failures: … · Gotchas: …

## 7. Environment and tests
Scoped commands: … · Unique test resources to use: … · Do NOT: full suite, container restarts, shared-DB migrations, fixed ports.

## 8. Deliverables
Report: docs/agent-ops/<run-id>/reports/<…>.md (start early; ORCH-DIGEST first)
Commits: code first, then report (docs-only), trailers: Task / Agent / Wave / Report
Final message (≤ 8 lines): STATUS | agent | task | report path | commit SHAs | blockers

## 9. Role-specific notes
- Investigator: read-only; no source edits; verify existence, root cause, blast radius, design, classification, sets, test plan.
- Implementer: root cause only; tests first-class; long-run test answered.
- Deep-dive: design record before code; full report with addendum (templates/report-deepdive-addendum.md); you may take the time you need; ESCALATE if too large.
- Code/test reviewer: READ-ONLY on source. Blind pass first, then claims audit. Diff: `git diff <WAVE_BASE_SHA>..HEAD -- <paths>`. Implementer reports: <paths> (read after your blind pass).
- Improver: address every BLOCKER/MAJOR (FIXED/REJECTED-with-evidence/DEFERRED-with-reason); improve code AND tests; reviewer findings: <paths>.
- Final reviewer: whole-branch diff `BASE_SHA..HEAD` excluding docs/agent-ops; look for cross-wave interactions.
