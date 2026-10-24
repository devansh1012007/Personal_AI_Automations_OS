# Brief — W1-IMPL-01 (implementer) · wave 1 · run 20261024-memory-polish

## 1. Role and mission
Mission (2 sentences): Add a `paa memory` CLI command group so the operator can read/query memory from the terminal, and add tests proving each subcommand works. Failure looks like: no `paa memory` command exists, or memory facts cannot be listed/searched/drained via the CLI.
Your role: implementer.

## 2. Required reading (in this order)
1. docs/agent-ops/20261024-memory-polish/templates/STANDING-ORDERS.md (binding)
2. AGENTS.md (top-level) + any nested AGENTS.md nearest your files
3. docs/agent-ops/20261024-memory-polish/00-mission.md (commands, hotspots, baseline, assumptions A-001…A-004)
4. Plan: docs/memory-polish/plans/Plan1.md — items P-001 and P-008, quoted verbatim: section E rows C01/C08 and section H rows T01/T03
5. docs/agent-ops/20261024-memory-polish/01-work-ledger.md (task table, wave 1)
6. docs/agent-ops/20261024-memory-polish/templates/report-implementation.md (your report template)
7. docs/agent-ops/20261024-memory-polish/COMMS.md (read from the last WAVE-START line)
8. Source entry points: src/paa/cli/main.py:66-98 (existing `submit`/`status`/`ledger` command shape), src/paa/cli/main.py:289-327 (`serve` command shape), src/paa/memory/creator.py:697-787 (MemoryCreator API), src/paa/memory/facts.py:914-967 (FactRepository.query_facts), src/paa/memory/facts.py:969-1033 (search_facts_by_text)

## 3. Task and acceptance criteria
Task T-001 (P-001 + P-008): Add a `paa memory` Typer command group with subcommands `facts <entity>`, `search <text>`, `signals [--status ...]`, `drain`, `world-model <doc>`. Add tests/cli/test_memory.py proving each subcommand.

Acceptance criteria (measurable):
- `paa memory --help` prints the group with all subcommands, exit code 0
- `paa memory facts <entity>` prints a table of facts for that entity (from `FactRepository.query_facts`), exit 0; prints "no facts" gracefully when none exist
- `paa memory search <text>` prints matching facts (from `FactRepository.search_facts_by_text`), exit 0
- `paa memory drain` runs one `MemoryCreator.run_batch(limit=25)` and prints claimed/processed/ignored/malformed/quarantined/errors/facts_written, exit 0
- `paa memory world-model <doc>` prints the managed-section text of one belief doc (from `WorldModel.read_managed`), exit 0
- Each subcommand has a test that FAILS without the implementation and PASSES with it (reversal check)
- Scoped run: `pytest tests/cli/test_memory.py -q` green; `ruff check src/paa/cli tests/cli` → 0 findings; `mypy src/paa/cli` → 0 errors

## 4. Ownership
WRITE-SET (may edit): `src/paa/cli/main.py`, `tests/cli/test_memory.py` (create the file if the dir does not exist)
READ-ONLY: `src/paa/memory/creator.py`, `src/paa/memory/facts.py`, `src/paa/memory/world_model.py`, `src/paa/runtime.py`
FORBIDDEN (hotspots / other agents' files): `src/paa/api/app.py` (W1-IMPL-02's file), `src/paa/memory/*.py` (read-only — do not modify creator/facts/world_model internals)
Need something outside? Post `NEED` in COMMS.md, finish the safe part, report `DEFERRED-DEPENDENCY`.

## 5. Parallel siblings this wave
| Agent | Task | Owns | Contracts it changes |
|---|---|---|---|
| W1-IMPL-02 | T-002 (P-002 + P-009) | src/paa/api/app.py, tests/api/test_memory.py | New `/memory/*` FastAPI routes |

Known interactions: None — disjoint files, disjoint test dirs, no shared symbols. You cannot see W1-IMPL-02's uncommitted work.

## 6. Skip list and known traps
Already fixed (don't redo): None — this is new surface.
False leads: Do not add API routes (that is W1-IMPL-02's task).
Flaky/pre-existing failures: None in tests/cli (new dir).
Gotchas: Typer subcommands must be registered via `memory_app = typer.Typer()` then `app.add_typer(memory_app, name="memory")`. Follow the existing `register_agent` pattern in cli/coder.py:56. Each command uses `_build_runtime()` + `try/finally rt.close()` (see cli/main.py:81-98).

## 7. Environment and tests
Scoped commands: `pytest tests/cli/test_memory.py -q`; `ruff check src/paa/cli tests/cli`; `mypy src/paa/cli`
Do NOT: run the full suite, restart containers, run shared-DB migrations, bind fixed ports.
Unique test resources: tests write into tmp_path (pytest fixture); no shared DB needed for CLI command tests using an in-memory/sqlite temp DB.

## 8. Deliverables
Report: docs/agent-ops/20261024-memory-polish/reports/w1-impl/W1-IMPL-01-T-001.md (start it early; ORCH-DIGEST block first; append as you work)
Commits: code commit first (trailers: Task: T-001, Agent: W1-IMPL-01, Wave: 1, Report: <path>), then report commit (docs-only, lists the code SHA)
Final message (≤ 8 lines): STATUS | agent | task | report path | commit SHAs | blockers

## 9. Role-specific notes (implementer)
- Fix root causes only; no band-aids (standing orders §7).
- Answer the long-run test: Source / Load / Hostile / Operability / Reversal.
- If a proper fix exceeds your scope, stop and return `ESCALATE` with analysis — do not ship a stopgap.
- Tests must fail without the fix; cover boundaries, malformed input, empty results.
- Create your report file at the start and append as you work.
