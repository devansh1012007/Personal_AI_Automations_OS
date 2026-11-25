<!-- ORCH-DIGEST:START -->
agent: W6-ORCH | task: C6.1 (P1.3) | status: DONE
code_commits: (this commit) | report_commit: (this commit)
files_changed: 4 (src/paa/coder/pipeline.py new, src/paa/coder/loop.py, tests/pipelines/test_coder_pipeline.py new, docs citations inline)
contracts_changed: none (CoderLoopFacade.use_skill gains a keyword-only call_id with a default — protocol-compatible with the frozen facade)
tests: added 23; scoped runs all green
ownership_violations: none
deferred_dependencies: none
out_of_scope_findings: 1
temporary_mitigation: no
deep_issue_flag: no
least_confident_about: the think-path CallJournal fingerprint covers the rendered window digest, not the raw window — replay (C6.3) must rebuild the window to verify it
confidence: high (equivalence proven over 10 scenarios + journal row identity + the spec's own done-when re-run live)
last_comms_line_read: n/a
<!-- ORCH-DIGEST:END -->
# Implementation report — W6-ORCH — C6.1 (P1.3) the coder tool loop on the facade

## 1. Task and acceptance criteria (restated)

SPEC 2 §5 C6.1 / pipeline spec P1.3: re-express the coder tool loop as
`coder.tool-loop` on the facade; `ctx.use_skill` maps to the existing
ToolJournal (ADR-0051's decision). Done when: `tests/coder/` pass
unchanged; the S1 dry-run still passes — the spec's own done-when.

## 2-4. Problem, root cause, approach

The mind/hands split the pipeline spec draws, made true for the second
reference loop: `CoderToolLoopPipeline` owns control flow (steps, budget,
final detection) and touches nothing but the facade; `CoderLoopFacade`
(host-minted per run) owns the effects — `think` (the model call,
journalled through the C6.2 CallJournal) and `use_skill` (the gated tool
call, journalled through the existing coder ToolJournal, never a second
vocabulary). `AgentLoop` stays the classic in-process path, untouched
behaviour-wise (`_isolation_for` became a shared module function
`isolation_level_of` — the one refactor, so the two paths can never
attest containment differently).

## 5. What changed

- `src/paa/coder/pipeline.py` (new, ~650 lines): `CoderLoopFacade`
  (run scope, `think`/`use_skill` with the full safety ordering:
  PRE hook → PROPOSED → permission → ASK → execute → outcome → POST;
  refused capabilities in the protocol's exact signatures; `now`/
  `random`/`progress` determinism aids; `project()` refuses honestly —
  the coder loop drives no task lineage) and `CoderToolLoopPipeline`
  (`coder.tool-loop` v4.1.0, grants = {think, use_skill}).
- `src/paa/coder/loop.py`: `_isolation_for` delegates to the new shared
  `isolation_level_of` (behaviour-identical; docstring preserved).
- `tests/pipelines/test_coder_pipeline.py` (new, 23 tests): equivalence
  over 10 scenarios (final, tool+final, denied, ASK-declined, hook-block,
  tool error, no-such-tool, max-steps, token-budget, multi-call), journal
  row identity (kinds, model call ids, args hashes, isolation levels,
  outcomes — the rows the two paths write are the same rows), facade
  contract (grant set, schema refusal, foreign-facade refusal, seeded
  random), CallJournal integration (PIPELINE_CALL_* rows land, the
  recorded result carries the model's turn), params discipline
  (max_steps/max_tokens from the task; `True` is not 1).

## 7. Tests — the equivalence method

The same scripted `ScriptedModel` turns and `RecordingTool`s drive both
paths; the assertion compares stopped_reason/steps/tool_calls/denied_
calls/tokens, the transcript shape, and (with a recording ToolJournal)
the TOOL_CALL_* rows verbatim. Three faithfulness bugs were caught and
fixed by these tests before they could land: `Session.turns` is a
property, not a method; the refusal methods must carry the protocol's
exact signatures or the facade is not structurally a CapabilityFacade;
and the model's `call_id` must reach the journal (the classic path
attests it) — `use_skill` gained a keyword-only `call_id` (default keeps
the frozen protocol satisfied), and the mind passes it from the turn.

## 11. Commands run (verbatim summaries)

- `pytest tests/pipelines/test_coder_pipeline.py -q` → `23 passed`
- `pytest tests/coder -q` → `484 passed, 1 skipped` (unchanged — the
  spec's own done-when, first half)
- `python scripts/drill_kill_resume.py --dry-run` → `dry-run: all checks
  held` (the S1 dry-run — the spec's own done-when, second half)
- `pytest tests/pipelines tests/chat tests/ledger tests/conventions
  tests/runtime -q` → `403 passed`
- `mypy src/paa` → `Success: no issues found in 175 source files`
- `ruff check src tests` → `All checks passed!`

## 13. Out-of-scope findings

1. **Sandbox pollution caught by the S1 drill.** The drill initially
   failed its "tree clean after commit" check with two untracked
   16-hex-char files in the workspace. Root cause (audit-hook traced):
   `ddtrace` 4.2.2 — preinstalled in this sandbox's venv as monitoring
   tooling, NOT a paa dependency — registers a pytest 11 entry-point
   plugin whose `Config()` touches an IPC scratch file
   (`ddtrace/internal/ipc.py:106`) into TMPDIR, and the paa sandbox
   points the child's TMPDIR at the workspace. The failure reproduced
   identically on the clean committed tree (stashed), proving it
   environmental. Resolution: `pip uninstall ddtrace` from the venv
   (the venv is rebuilt from `pyproject.toml` on recycle; ddtrace is
   not among the declared dependencies) → the drill passes all checks.
   Recorded here because the next sandbox rebuild may need the same
   uninstall before the drill is trusted.

## 14. Known limitations

The `think` CallJournal fingerprint covers the rendered window's digest
plus role and budget — not the raw window bytes. Replay (C6.3) must
re-render the window from the session transcript to verify a recorded
call; the equivalence this change proves is at the metrics/transcript/
journal-row level, which is the level the spec's done-when names.
