<!-- ORCH-DIGEST:START -->
agent: W6-ORCH | task: C6.3-C6.6 + W6-CLOSE | status: DONE
code_commits: f535071 (C6.3), b87234e (C6.4), b5390ea (C6.5), 215cb25 (C6.6), this commit (close)
files_changed: 14 new (2 src, 5 test, 1 CLI, 6 docs) + 7 modified
contracts_changed: Scope.pipelines added (mandate payload golden v1 preserved for unconstrained mandates); ChatTurnFacade/ChatService gain an optional context_provider (default-None, protocol-compatible); coder metrics gain served_calls
tests: added 97 (22 replay+chaos, 31 overlay, 29 registry/CLI, 3 conformance, 12 CLI-driven)
ownership_violations: none
deferred_dependencies: none
out_of_scope_findings: 2
temporary_mitigation: no
deep_issue_flag: no
least_confident_about: the divergence check in conformance drives a served think then a changed window — the strongest generic divergence vector, but a facade-level mutating CallJournal method (P3) will make the unknown-outcome row integration-reachable rather than fold-driven
confidence: high (full gate 4,368/244/1x/0 exit 0; mypy 181 files clean; ruff clean; every task's done-when re-run live)
last_comms_line_read: n/a
<!-- ORCH-DIGEST:END -->

> **SUPERSESSION (2026-11-25, W6-CLOSE truth pass).** This report is retained as
> the historical record of the upstream run. Two statements below describe
> patch `0006` as first applied and are superseded for readers comparing it
> with the current tree:
>
> - §2's "Coder: all six real" (now line 102). In the current tree five coder checks
>   are real passes and `unknown-outcome-never-auto-repeats` is an honest,
>   pinned `n/a` (`src/paa/pipelines/conformance.py:300-315`). No registered
>   in-process pipeline journals a mutating call through `CallJournal`, so a
>   green there would prove the fold in isolation and nothing about the
>   pipeline. The behaviour is pinned at
>   `tests/pipelines/test_replay.py::TestDecisionTable::test_interrupted_mutating_call_never_auto_repeats`
>   and in the kill-9 sweep `tests/pipelines/test_replay_chaos.py:214-224`.
> - §5's "a synthesised mutating PROPOSED row" (now lines 138-140). That row was
>   removed by the remediation; `tests/pipelines/test_conformance.py:100` now
>   asserts the `n/a` explicitly so a future fake pass fails the gate.
>
> All three pipelines remain GREEN, with `n/a` rows visible rather than hidden.

# Implementation report — W6-ORCH — C6.3-C6.6 + the Wave 6 close

## 1. Tasks and acceptance criteria (restated)

SPEC 2 §5, the four remaining Wave 6 rows, plus the close:

- **C6.3 (P2.2-P2.3)** — replay/divergence logic + kill -9 chaos for chat and
  coder. Done when: chaos suite green for both loops.
- **C6.4 (P1.5)** — slot-addressable stages + overlay loader with the
  tighten-only check at load. Done when: an overlay swapping chat's context
  provider changes behaviour in a test; widening attempts fail loudly.
- **C6.5 (P4-lite)** — registry v0 + `paa pipeline list/inspect/run/trace`.
  Done when: same task, two pipelines, two recoverable runs; a mandate that
  doesn't list a pipeline refuses it.
- **C6.6** — conformance v0 for all registered pipelines. Done when:
  conformance green for chat + coder + orchestrator built-ins.
- **W6-CLOSE** — G-LOOPS gate evidence, truth pass, deliverables.

## 2. What landed

**C6.3 (f535071).** `src/paa/pipelines/replay.py`: the decision table
(`ReplayView.check` — EXECUTE/SERVE/DIVERGED/OUTCOME_UNKNOWN/FAIL, the sweep's
`interrupted` close marker read back, `think` as the one pure method) and
`ServedToolResults` (position-indexed, order-checked: a swapped tool order is
`ToolSequenceDiverged`, not a forged history). `CoderLoopFacade` grows
`replay`/`served_tools`: a recorded call is answered from the journal (no
model invocation, no row re-appended — the discriminators already exist), a
fingerprint mismatch halts the run with `PIPELINE_RUN_DIVERGED`, an
interrupted pure call re-executes once, an interrupted mutating call reports
`-32008` and is never auto-repeated. The chaos matrix: coder (kill at all
five call boundaries → sweep run **twice** → resume completes with the same
answer and zero duplicate tool effects; model arithmetic 3+1/3), chat (a
killed turn sweeps to `FAILED(interrupted)` and recovers by redelivery; the
mid-remember kill pinned as episodic-not-duplicated). The five broken WIP
integration tests were repaired to honest assertions — the strongest finding:
a *full* replay serves every call including the final one, so the model is
invoked zero times (the ADR's N-cheap-round-trips cost model, made visible).

**C6.4 (b87234e).** `slots.py` states the three in-process pipelines'
manifest-shaped declarations; `overlay.py` is the tighten-only loader —
every widening attempt rejected at load by name (undeclared param/slot,
out-of-range value, wrong type, malformed ref, capability outside the ask,
false→true, grown tool/role list, limit above the declared, unknown limits
key/section, broken TOML). Chat's context-provider slot: the facade gained
`build_context()`, the loop asks the facade, the service threads a resolved
provider. The behaviour proof, as a test: an overlay filling the slot with a
terse provider makes turn 2's reply lose the `(from memory: ...)` note.

**C6.5 (b5390ea).** `registry.py`: builtin + local sources with trust
levels; local manifests validated against the closed contract at load,
off-contract ones **named**, never hidden, never run (out-of-process
execution is P3's RPC transport — the refusal says so). Selection explicit →
mandate scope → default, never scored; a multi-pipeline scope without a name
refuses. The mandate grows `Scope.pipelines` (golden v1 payload preserved
byte-for-byte for unconstrained mandates — the key is absent, not null).
`paa pipeline list/inspect/run/trace`: the task pipeline through the
runtime's own submit→run; chat as a one-shot offline turn with the *host*
writing the `PIPELINE_RUN_*` rows on the fresh session lineage; the coder
loop on `CoderAgent.build`'s own wiring driven by `CoderToolLoopPipeline`
with a `CallJournal`; trace reads a lineage's pipeline-shaped rows and says
which vocabulary it is reading (the fallback for task lineages names the
task vocabulary honestly).

**C6.6 (215cb25).** `conformance.py`: the six-check standard (double-run
call-sequence equality, divergence halt, unknown-outcome,
cancel-within-grace, budget exhaustion, no-secrets-in-journal) run against
every registered pipeline **in its own vocabulary**. Coder: all six real.
Chat/builtin: the expressible checks real, the rest honest `n/a` rows naming
where the behaviour IS pinned. All three GREEN; the tests pin the order,
the GREEN/RED rendering, and that the coder row is strong, not ceremonial.

## 3. Verification (re-run 2026-11-24, this sandbox, tree at the close commit)

- `python -m pytest tests/` → **4,368 passed / 244 env-gated skipped /
  1 xfailed, 0 failures, exit 0** (W5 close was 4,237 → +131 tests).
- `mypy src/paa` → no issues in 181 source files.
- `ruff check src tests` (the CI command) → all checks passed.
- G-LOOPS: three pipelines (builtin, chat, coder.tool-loop) on one facade;
  conformance + chaos + overlay green; protocol still **draft** (freezing
  waits for P3's RPC run, per the spec's own rule).

## 4. Truth pass (the close commit)

- 26 docs citations repointed: bare `replay.py:NNN` references became
  `ledger/replay.py:NNN` (the new `pipelines/replay.py` made them ambiguous
  — the house reference gate caught it); shifted `cli/main.py` /
  `ledger/mandate.py` line citations repointed at their moved anchors.
  Archived `docs/agent-ops/**` reports were deliberately left untouched:
  they are historical records.
- The SPEC-2 plan's status line now states W6 landed with the gate met.
- The board's Known-left section flips P1.3/P1.5/P2/P4-lite/conformance to
  done; the rule of three is met.

## 5. Honest boundaries (what this wave did NOT do)

- The builtin task pipeline still records its runs in the task vocabulary —
  no `PIPELINE_RUN_*` rows on task lineages (the orchestrator's exact event
  sequences are a pinned contract; changing them is P3's transport work).
- Chat's `PIPELINE_RUN_*` rows are written by the one-shot driver (CLI /
  conformance), not by `ChatService` in multi-turn sessions — a session is a
  session; a pipeline-shaped invocation carries the wrapper.
- Local (out-of-process) manifests are listed and inspected, never executed.
- The unknown-outcome conformance check for coder drives the real fold but a
  synthesised mutating PROPOSED row (no facade method journals `commit`
  through the `CallJournal` yet — P3).
- P5 distribution stays gated on a first outside author; C5.1/G1 stays
  operator-blocked (no model key in this sandbox).

## 6. Out-of-scope findings (for the register)

1. `tests/cli/test_cli.py`'s `_isolated_home` fixture and the new
   `tests/cli/test_pipeline.py`'s copy are near-duplicates — a `tests/cli`
   conftest would own it; left alone to keep the wave's diff scoped.
2. `paa agent` (the classic loop) and `paa pipeline run coder.tool-loop`
   now both build `CoderAgent` stacks; a shared builder would prevent drift
   when the wiring changes next.
