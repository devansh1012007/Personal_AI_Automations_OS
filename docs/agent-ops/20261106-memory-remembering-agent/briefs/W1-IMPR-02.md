# Brief — W1-IMPR-02 (IMPR) · wave 1 docs-reference fix · run 20261106-memory-remembering-agent

## 1. Role and mission
Mission: Fix new gate failure R-W1-DOCS (17 broken `path:line` citations after Wave 1 line shifts) so `tests/conventions/test_docs_references.py` passes without touching unrelated code.
Role: improver (second pass). Root cause: src/paa/runtime.py grew ~33 lines and daemon.py changed; docs citations + operator README.md rewrite (f8c12a6) left 17 refs landing on blank lines.

## 2. Required reading
1. docs/agent-ops/20261106-memory-remembering-agent/templates/STANDING-ORDERS.md (binding)
2. AGENTS.md (docs-references rule: "A corrected reference has to be *read* and not merely re-numbered")
3. docs/agent-ops/20261106-memory-remembering-agent/reports/w1-improve/IMPR-01.md (prior dispositions)
4. docs/agent-ops/20261106-memory-remembering-agent/gates/gate-w1-1.md (baseline)
5. COMMS.md from last WAVE-END (read at start)

## 3. Task
Get the full broken list by running: `.venv\Scripts\python.exe -m pytest tests/conventions/test_docs_references.py::test_every_referenced_line_is_not_blank -q`
Expected broken set (17, from gate run 2026-11-06):
component-interactions.md:226 'runtime.py:431'; overview.md:59 'README.md:94', :96 ':383'/':393', :146 'README.md:114'; data-lifecycle.md:126 'README.md:87'; persistence-and-storage.md:154 'runtime.py:392'/':398'; Plan1.md:119 'daemon.py:179'; daemon.md:60 'daemon.py:163'; sandbox.md:114/:151 'runtime.py:431'; storage.md:85 'runtime.py:398', :86 'runtime.py:431'; validation.md:83 'runtime.py:495'; deployment-and-runtime.md:37 'runtime.py:392'/':398'.
For EACH: read the doc context around the citation, read the claimed target file around the cited line, identify what the sentence claims the line holds (symbol/function), find the current correct line number, and update the citation. If the cited content no longer exists, note it explicitly in the report instead of guessing a nearby line.
Note: README.md:87/:94/:114 were shifted by the operator's own commit f8c12a6 — those still count toward the gate; fix them the same way.

## 4. Ownership
WRITE-SET (may edit only these docs; NO source files):
- docs/architecture/component-interactions.md
- docs/architecture/overview.md
- docs/data/data-lifecycle.md
- docs/data/persistence-and-storage.md
- docs/memory-polish/plans/Plan1.md
- docs/modules/daemon.md
- docs/modules/sandbox.md
- docs/modules/storage.md
- docs/modules/validation.md
- docs/operations/deployment-and-runtime.md
- docs/agent-ops/20261106-memory-remembering-agent/reports/w1-improve/IMPR-02.md
READ-ONLY: everything else. FORBIDDEN: source edits (src/), config, schema, tests (except running them).

## 5. Tests & commands (scoped only)
`.venv\Scripts\python.exe -m pytest tests/conventions/test_docs_references.py -q`
For each fixed citation, the test must pass AND the cited line must be non-blank.
Run the full conventions file, not just the one test.

## 6. Deliverables
Report at docs/agent-ops/20261106-memory-remembering-agent/reports/w1-improve/IMPR-02.md (ORCH-DIGEST first; table of all 17: old ref → new ref → what the line now holds → verified yes/no).
Commits: docs first, then report, trailers `Task: T-001` / `Agent: W1-IMPR-02` / `Wave: 1` / `Report: <path>`. Explicit paths only.
Final message (≤8 lines): STATUS | agent | scope | report path | commit SHAs | open findings.
