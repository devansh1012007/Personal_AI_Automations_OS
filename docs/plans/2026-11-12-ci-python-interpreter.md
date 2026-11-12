# Plan: fix CI failures caused by missing `python` interpreter (2026-11-12)

## Problem
CI on `main` is red. Nine tests fail because the sandbox worker's default
command `["python", "-c", "pass"]` cannot find a `python` binary on hosts that
only ship `python3` (modern Linux; PEP 394 does not guarantee `python`).
Affected tests (all share the same root cause, a task never reaching COMMITTED):

- tests/api/test_api.py::TestTasks::test_submit_and_run_reaches_committed
- tests/api/test_api.py::TestLedger::test_ledger_returns_event_chain
- tests/api/test_daemon.py::TestDaemon::test_dispatch_loop_drains_a_queued_task
- tests/cli/test_cli.py::TestTaskLifecycle::test_submit_runs_to_committed
- tests/cli/test_cli.py::TestTaskLifecycle::test_submit_then_status_and_ledger
- tests/integration/test_end_to_end.py::TestHappyPath::test_task_runs_to_committed
- tests/integration/test_end_to_end.py::TestHappyPath::test_full_event_sequence_is_recorded
- tests/integration/test_end_to_end.py::TestDeterministicRecovery::test_committed_task_is_not_touched_by_recovery
- tests/integration/test_end_to_end.py::TestZeroNetworkPosture::test_a_full_run_needs_no_frontier_provider

## Root cause
`src/paa/agents/reasoning.py:445` builds the sandbox `SandboxSpec` with default
`command=["python", "-c", "pass"]`. The sandbox child environment is built from
scratch in `sandbox/base.py::build_child_env` with
`PATH=/usr/local/bin:/usr/bin:/bin` (deliberately minimal; see AGENTS.md).
On a host without `/usr/bin/python`, `python3 -c "pass"` works but
`python -c "pass"` fails with `No such file or directory`, so every sandboxed
step fails fast and the task never reaches `COMMITTED`.

## Constraint from AGENTS.md
AGENTS.md records that `build_child_env`'s PATH must NOT be widened (it would
undo the sandbox's no-ambient-environment security property), and that the
documented remedy for a missing `python` is the host fix
`sudo apt install python-is-python3`. This environment has no `sudo` and
`/usr/bin`/`/usr/local/bin` are not writable, so the host fix is unavailable.

## Decision
Change the default interpreter name from `python` to `python3` in
`src/paa/agents/reasoning.py:445`.

This is a root-cause fix, not a symptom patch:
- It corrects the wrong interpreter-name assumption (`python` is not portable;
  `python3` is the PEP 394-reliable name on modern Linux).
- It does **not** widen the sandbox PATH — `python3` already lives in
  `/usr/bin`, which is in the existing minimal PATH. The security property of
  `build_child_env` is preserved.
- It is strictly more portable: on hosts that DO have `/usr/bin/python`
  (via `python-is-python3`), `python3` is the same binary and behaves identically.

## Alternatives considered
1. Host fix `sudo apt install python-is-python3` — correct per AGENTS.md when a
   host can be modified, but unavailable here (no `sudo`, no write access to
   system dirs). Rejected for this environment.
2. Widen `build_child_env` PATH to include a user-local `python` — explicitly
   forbidden by AGENTS.md (breaks the ambient-environment security property).
   Rejected.
3. `sys.executable` — invalid: the sandbox child is a separate process/container;
   the runtime's interpreter path is meaningless there. Rejected.

## Scope
One file, one token: `src/paa/agents/reasoning.py`. Tests that pass explicit
`command=["python", ...]` (e.g. `tests/agents/test_policy.py`) use stub sandboxes
and are unaffected. `tests/validation/test_test_runner.py` asserts
`("python", "-m", "pytest")` for `PytestRunner`; `PytestRunner` is not changed
and is only exercised with stubs, so it is unaffected.

## Verification
- `ruff check src tests` — 0 findings.
- `mypy src` — 0 errors.
- Previously failing tests above now pass.
- `tests/agents/test_reasoning.py`, `tests/sandbox/`, `tests/coder/test_agent.py` — pass.

Note: the full `pytest tests/` run is flaky on this host (the baseline without
this change also hangs at ~23% due to an unrelated pre-existing hang); the hang
is not caused by this change.

## Rollback
Revert the one-line change in `src/paa/agents/reasoning.py`.
