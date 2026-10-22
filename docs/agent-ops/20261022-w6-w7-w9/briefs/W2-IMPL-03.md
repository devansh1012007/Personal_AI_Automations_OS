# Brief — W2-IMPL-03 (implementer) · wave 2 · run 20261022-w6-w7-w9

## 1. Role and mission

**Mission:** make `src/paa/agents/` trustworthy by test — and fix the one
place where the orchestrator's own escalation does not work.

**Why this task matters:** the ROADMAP names two properties as "most likely
to regress silently": the **critic-override rule** (a deterministic FAIL can
never be overturned by an LLM PASS) and the **delegation cycle guard**. The
first is well covered; the second is covered only at the graph level, and
neither is covered *through the orchestrator*. And the orchestrator emits a
human-escalation event during hydration and then **carries on and commits the
task anyway** — the escalation is advisory where its name says it is a stop.

**What failure looks like:** a test file that asserts the orchestrator's
*phases* rather than its *events*, or a cycle test written at the default
modality — which asserts the **depth** ceiling and would pass even if the
cycle guard were deleted.

## 2. Required reading

1. `docs/agent-ops/20261022-w6-w7-w9/briefs/W2-CONTEXT.md` — **read first**.
2. `docs/agent-ops/20261022-w6-w7-w9/templates/STANDING-ORDERS.md` — binding.
3. `docs/plans/w6-w7-w9.md` — **§4 (0.2)** (your task, clause by clause), **§0**
   (the evidence), **§6** (**D-4** is yours: the task must **park**).
4. `src/paa/agents/orchestrator.py` — the event emit sites, in order, and the
   `run()` sequence. `src/paa/agents/delegation.py` — `would_cycle`,
   `propose`, the three bounds. `src/paa/agents/policy.py` — the six checks
   and the anti-goal threshold. `src/paa/agents/reasoning.py` — the critic's
   ordering constraint.
5. `src/paa/ledger/events.py` — the idempotency key, and why two critiques of
   the same step collapse into one event.
6. `src/paa/ledger/replay.py` — `_PHASE_MAP`: the map that currently
   **overwrites** the `AWAITING_HUMAN_ATTESTATION` phase.
7. `tests/agents/` — the existing 90 tests, and the fakes in
   `tests/agents/test_orchestrator.py`. `tests/integration/conftest.py` — the
   `ScriptedModel` (it answers by schema shape and records every call) and the
   real `Runtime` it builds.
8. `docs/agent-ops/20261022-w6-w7-w9/templates/report-implementation.md`.

## 3. The task

**T-006.** The exact event sequence and the mediated cycle are unasserted, and
`HARD_STOP_ESCALATE_TO_USER` does not park the task.

### The four ROADMAP clauses

| Clause | State today | What you add |
|---|---|---|
| the orchestrator emits the **exact** ledger event sequence end-to-end | `tests/agents/test_orchestrator.py:78` asserts a **six-of-ten subsequence**; the integration test asserts **membership** only | the **exact** ordered event list for the happy path (ten events), the **failure** path, the **block** path and the **gate** path — and the `CRITIQUE_CONCLUDED` **idempotency trap** (the critic does not pass `attempt`, so a retried step's second critique is *suppressed by the ledger's key*; a naive list assertion is brittle unless the test knows this) |
| delegation refuses a **real** three-node cycle | covered at the **graph** level only (`tests/agents/test_delegation.py:30` drives `DelegationGraph` directly) | a cycle driven **through** `ChiefOrchestrator.mediate_delegation` — three real agents each calling `self.delegate(...)`. **It must run at `MAX` modality**: at STANDARD and COMPLEX the **depth** ceiling fires first, so a test at the default would assert the wrong guard and would pass even if `would_cycle` were deleted |
| policy gates: LOCKDOWN blocks egress, SAFE blocks deletion, anti-goal at **0.82** | LOCKDOWN and SAFE are covered (unit + integration). The **0.82 cosine path has zero coverage** — the only anti-goal test takes the *keyword fallback*, which never reads the threshold | the cosine path with an **injected embedder** (exercisable with no server: the vector store is only checked for `None`); the **boundary** — just below the threshold passes, exactly at it blocks; and the **fail-closed** path (an embedder that raises ⇒ blocked, not passed) |
| the critic's deterministic FAIL cannot be overridden by an LLM PASS | **covered twice** (unit + integration) | the two halves that are not: the model **can** downgrade a deterministic PASS, and an **unparseable** model verdict is coerced to `FAIL_REJECT_RETRY` |

### The fix (D-4, given)

**`HARD_STOP_ESCALATE_TO_USER` must park the task.** Today the orchestrator
emits `AWAITING_HUMAN_ATTESTATION` during hydration and then continues to
`PLAN_COMPILED → POLICY_CLEARED → EXECUTION_STARTED → MUTATION_COMMITTED`,
because `run()` checks the phase only *after* `_check_policy` and the phase
map overwrites the event's phase.

* The task must stop at the escalation and wait for a human — the way the
  gate path already does (`AWAITING_HUMAN_ATTESTATION` from `_check_policy`).
* The **existing** human-gate machinery is the model: `clear_human_gate(approved=…)`
  releases a parked task, and *nothing in the automated path can clear its own
  gate* — that is what makes the event mean something. **Reuse it; do not
  invent a second mechanism.**
* This is a **behaviour change on a live path**, so it gets **its own ADR**:
  `docs/adr/0034-*.md` — what changed, why, what it costs, and how it rolls
  back.
* Watch the **ledger's phase map** (`ledger/replay.py::_PHASE_MAP`): the
  phase the projection lands in is what the caller sees, and it is the
  thing that currently overwrites the escalation. The fix must make the
  projection agree with the intent.

## 4. Acceptance criteria (measurable)

* The **exact** event list is asserted for four paths — happy, failure, block,
  gate — and the assertion is a **list equality**, not a subsequence.
* The idempotency trap is asserted, and the test states why the list is what
  it is.
* A **mediated** three-node cycle is refused **at `MAX` modality** with the
  cycle path in the error, and the test asserts **the cycle guard** fired
  (the error's `cycle` field), not merely that an error was raised.
* The 0.82 threshold is asserted on **both** sides of the boundary, with an
  injected embedder, and the fail-closed path is asserted.
* The critic's two uncovered halves are asserted.
* `HARD_STOP_ESCALATE_TO_USER` **parks**: the task's phase is
  `AWAITING_HUMAN`, **no** `EXECUTION_STARTED` and **no**
  `MUTATION_COMMITTED` are emitted, and the task resumes only after
  `clear_human_gate`. **ADR-0034** records it.
* **Commands that must pass (scoped):**
  ```bash
  .venv/bin/python -m pytest tests/agents tests/integration -q
  .venv/bin/python -m ruff check src/paa/agents tests/agents
  .venv/bin/python -m mypy src/paa/agents
  ```
* The wave-1 gate must not regress: ruff 0, mypy 0 across `src`.

## 5. Ownership

**WRITE-SET (you may edit exactly these):**

```
src/paa/agents/orchestrator.py
tests/agents/__init__.py
tests/agents/test_orchestrator.py
tests/agents/test_delegation.py
tests/agents/test_policy.py
tests/agents/test_reasoning.py
tests/integration/test_end_to_end.py
docs/adr/0034-*.md                  (new)
```

**READ-ONLY:** `src/paa/agents/{base,delegation,policy,context_agents,reasoning}.py`
— **read them; do not edit them** (wave 1 converted their imports; they are
not yours). If a test proves a defect in them, report it as a `FINDING` with
the reproduction, and mark the test `xfail(strict=True)` with the reason.

**FORBIDDEN:** `src/paa/**` outside `agents/orchestrator.py`,
`src/paa/ledger/**` (the phase map is **read-only** for you — if the fix
needs a change there, post a `NEED` with the exact change and report
`DEFERRED-DEPENDENCY`; the orchestrator will decide), `tests/**` outside
`tests/agents/` and `tests/integration/test_end_to_end.py`, `pyproject.toml`,
`deploy/**`, `docs/**` except your ADR, `docs/adr/README.md`, `docs/plans/**`,
`AGENTS.md`.

**A note on the phase map:** if the fix turns out to need
`ledger/replay.py`, **do not edit it** — post a `NEED` and say exactly what
must change and why. The alternative is a fix in the orchestrator that leaves
the projection lying.

## 6. Environment

* Scoped checks only. **Never** the full suite, never a container, never a
  port, never a migration against the shared database.
* The `ledger` fixture in `tests/conftest.py` is a **real** `LedgerStore` over
  a real SQLite file in `tmp_path` — use it, and read the events back with
  `read_correlation`.
* `PolicyRiskAgent()` with **no** `settings=` reads the **global** settings
  (the repo's `.env` and `~/.paa`) — so a threshold assertion must pass an
  explicit `settings=` or it is environment-dependent. This is a real trap.
* Kill any process you start.

## 7. Deliverables

* **Report:** `docs/agent-ops/20261022-w6-w7-w9/reports/w2-impl/W2-IMPL-03-T-006.md`
  — `templates/report-implementation.md`, ORCH-DIGEST first, appended as you
  work.
* **Commits:** code first, then the report in a separate docs-only commit.
  Trailers: `Task: T-006`, `Agent: W2-IMPL-03`, `Wave: 2`, `Report: <path>`.
  Explicit paths only:
  ```bash
  test "$(git branch --show-current)" = "ops/20261022-w6-w7-w9" || { echo "wrong branch"; exit 1; }
  git add -- src/paa/agents/orchestrator.py tests/agents/ tests/integration/test_end_to_end.py
  git commit -m "<msg>" -- src/paa/agents/orchestrator.py tests/agents/ tests/integration/test_end_to_end.py
  ```
* **Final message (≤ 8 lines):** `STATUS | agent | task | report path | commit SHAs | blockers`.
  **It must not be empty.**
