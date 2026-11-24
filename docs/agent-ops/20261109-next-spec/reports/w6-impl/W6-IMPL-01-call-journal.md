<!-- ORCH-DIGEST:START -->
agent: W6-ORCH | task: C6.2 (P2.1) | status: DONE
code_commits: (this commit) | report_commit: (this commit)
files_changed: 8 (src/paa/pipelines/journal.py, src/paa/core/types.py, src/paa/ledger/replay.py, src/paa/ledger/recovery.py, src/paa/pipelines/__init__.py, tests/pipelines/test_journal.py, tests/ledger/test_replay.py, docs citations x9)
contracts_changed: none (new event vocabulary is additive; all seven types are _NO_PHASE_CHANGE, none terminal)
tests: added 23, changed 1 strategy list; scoped run: pass
ownership_violations: none
deferred_dependencies: none
out_of_scope_findings: 2
temporary_mitigation: no
deep_issue_flag: no
least_confident_about: bare intra-doc line citations in workflow-03 were drifted before this change; repointed at real anchors
confidence: high (gates + scoped suites green; property tests over all orderings)
last_comms_line_read: n/a
<!-- ORCH-DIGEST:END -->
# Implementation report — W6-ORCH — C6.2 (P2.1) the generalised CallJournal

## 1. Task and acceptance criteria (restated)
SPEC 2 §5 C6.2 / pipeline spec P2.1: generalise the coder journal into a
`CallJournal` (seq, fingerprint, outcome); add `PIPELINE_RUN_STARTED/COMPLETED/
FAILED/DIVERGED` and `PIPELINE_CALL_PROPOSED/COMPLETED/FAILED` per ADR-0046.
Done when: replay property test over all orderings including the new types.

## 2-4. Problem, root cause, approach
The spike (P0.4) proved replay semantics over a throwaway JSONL journal; the
production vocabulary did not exist. Approach: mirror the coder/chat journal
discipline (v=1 payloads, ADR-0008 discriminators, hashes-and-shapes-only
preview, N2.6 blob plumbing) at capability-call granularity, keyed by
`seq` with `fingerprint = sha256(canonical_json({method, params}))`.

## 5. What changed
- `core/types.py`: seven PIPELINE_* event types with the full payload
  contract documented on the enum block.
- `ledger/replay.py`: all seven in `_NO_PHASE_CHANGE` + an informational
  match-case (no projection field moves; run state folds in
  `pipelines.journal`, never in the task fold).
- `ledger/recovery.py`: `_PIPELINE_EVENT_TYPES`, `SKIPPED_PIPELINE_LINEAGE`
  outcome, `find_/close_interrupted_pipeline_calls` (golden v=1 FAILED close
  shape, `call:<seq>:FAILED` discriminator), sweep branch before the ordinary
  path; task lineages carrying PIPELINE rows still recover normally.
- `pipelines/journal.py` (new, 500 lines): `CallJournal` (run lifecycle rows +
  call rows; refuses non-positive seq before append; refuses unserialisable
  results before append; blobs oversized payloads), `CallRecord`,
  `fold_call_journal` (total, seq-keyed, order-independent by outcome
  precedence: COMPLETED/FAILED beat PROPOSED, FAILED wins contradictions),
  `read_call_journal`, `fingerprint`, `params_shape`.
- `tests/pipelines/test_journal.py` (new, 23 tests): golden v1 shapes,
  canonicality, secret-shape previews, dedupe-on-redelivery, blob round-trip
  (ContentAddressedStore), fold totality (hypothesis, 200 examples incl.
  foreign types), outcome-beats-proposal property, recovery-sweep exemptions
  (skip-not-requeue, interrupted close golden shape, second-sweep noop,
  mixed task lineage stays a task).
- `tests/ledger/test_replay.py`: the seven types joined the house
  totality strategy list (the "all orderings including the new types"
  requirement, house style).

## 7. Tests
The ordering property failed first against a last-write-wins fold (permuted
COMPLETED-before-PROPOSED read PROPOSED) — fixed by precedence, which is the
semantics the property actually demands, then green.

## 11. Commands run (verbatim summaries)
- `pytest tests/pipelines/test_journal.py -q` → `23 passed`
- `pytest tests/ledger tests/chat tests/conventions tests/pipelines -q` →
  conventions failed on 10 blank-line citations (docs cited recovery.py/types.py
  line numbers my edits shifted) → repointed at verified anchors → `34 passed`
  for conventions; all green after.
- `pytest tests/observability tests/mcp_server tests/pipelines tests/ledger
  tests/chat -q` → all passed (716 tests).
- `mypy src/paa` → `Success: no issues found in 174 source files`
- `ruff check src tests` → `All checks passed!`

## 13. Out-of-scope findings
1. workflow-03's bare `(:NNN)` citations were already drifted pre-change;
   now repointed at real anchors (912/948/1035/1042/1051/1123/1161).
2. `docs/agent-ops` historical reports intentionally not repointed
   (as-written records).

## 14. Known limitations
`result_json` on COMPLETED is the one deliberate content-bearing payload
field (replay must serve values back); it blobs past the inline limit and is
shape-audited by the conformance suite's no-secrets check (C6.6).
