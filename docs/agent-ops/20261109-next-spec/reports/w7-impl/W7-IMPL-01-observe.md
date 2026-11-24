<!-- ORCH-DIGEST:START -->
agent: W7-ORCH | task: C7.1 (H3.2 / P1.4) | status: DONE
code_commits: (this commit) | report_commit: (this commit)
files_changed: 19 (src/paa/observe/{__init__,observer,bundle,pipeline}.py, src/paa/cli/observe.py, src/paa/cli/main.py, src/paa/cli/pipeline.py, src/paa/core/types.py, src/paa/pipelines/{journal,registry,slots,conformance}.py, tests/observe/{test_bundle,test_observer,test_pipeline,test_cli}.py, tests/pipelines/{test_conformance,test_registry,test_overlay}.py, tests/cli/test_pipeline.py, tests/conventions/test_import_conventions.py, docs/interfaces/observe-bundle.md, docs citations x125)
contracts_changed: none (PIPELINE_RUN_STARTED gains optional resume/evidence_tier keys — additive, v=1 payload golden keys unchanged for in-process pipelines)
tests: added 61 (tests/observe 57 + conformance 1 + registry 2 + cli inspect 1); full suite green, exit 0
ownership_violations: none
deferred_dependencies: none
out_of_scope_findings: 1
temporary_mitigation: no
deep_issue_flag: no
least_confident_about: none of the real-subprocess chaos rows are flaky on this box; the memory-kill classification row is unit-level only (the subprocess backend degrades memory to no-measurement here, so RESOURCE_KILLED is pinned via fabricated SandboxResult)
confidence: high (gates green: full pytest exit 0, mypy 186 files clean after tui-extra reinstall, ruff clean; conformance 4 pipelines GREEN)
last_comms_line_read: n/a
<!-- ORCH-DIGEST:END -->
# Implementation report — W7-ORCH — C7.1 (H3.2 / P1.4) `paa observe`, pipeline #4

## 1. Task and acceptance criteria (restated)
SPEC 2 §6 C7.1 / pipeline spec §6 + P1.4: `paa observe <cmd>` — the T1
observer mode. Run any CLI in a workspace; record the manifest diff, git
changes, transcript; classify the state after a crash; register it as
pipeline #4 on the same facade contract, `resume = "none"`,
`evidence_tier = "T1"`. Done when: *a scripted run of a fake harness
produces a complete T1 bundle; crash classification correct* (SPEC 2's
own row), and the campaign gates (full pytest, mypy, ruff) hold.

## 2-4. Problem, root cause, approach
The spec's evidence ladder had no T1 artifact: the cheapest honest
evidence about what an external harness did did not exist, while the
registry's rule-of-three waited on a fourth pipeline to prove the facade
contract generalises. Approach: an observer core that sequences
skeleton → before-snapshots → sandboxed child → after-snapshots →
classification; a bundle format (`paa-observe-bundle/1`) whose two-phase
write makes "complete or says it is not" a property of the file itself;
an adapter expressed as `builtin.observe` with an empty capability ask
journalled as one `observe.run` call whose COMPLETED result carries the
bundle's SHA-256, never its text.

## 5. What changed
- `src/paa/observe/bundle.py` (new): the format — FileFacts/manifest
  walk (`.git` skipped, symlinks as link facts), ManifestDiff by
  content identity, capped streams (codepoint-safe cap, honest
  `truncated` marker), atomic two-phase write, `read_bundle` with
  schema-tag refusal, `sha256()` attestation.
- `src/paa/observe/observer.py` (new): `ObserverCore` (the sequencing;
  git before/after asked *through the same sandbox*) and
  `classify_outcome` — the five-state crash vocabulary
  (clean/error/signalled/timed_out/resource_killed) with the sandbox's
  `killed_reason` outranking the exit code and negative POSIX exits
  decoded to signal numbers.
- `src/paa/observe/pipeline.py` (new): `ObserveFacade` — the full
  `CapabilityFacade` surface with an **empty grant set** (every
  capability door present and closed, the coder's own refusal shape),
  one real operation `observe_command` journalling PROPOSED →
  COMPLETED/FAILED; `ObservePipeline` — goal-as-command-line (shlex;
  quoting survives the round trip), clean exit → completed, everything
  else → failed with the classification riding the metrics.
- `src/paa/cli/observe.py` (new) + `cli/main.py` registration:
  `paa observe -- <cmd>` with `--workspace/--timeout-s/--memory-mb/
  --transcript-cap-kb/--env/--json`; exit code is the subject's fate.
  `run_observe_pipeline` is the shared driver — `paa pipeline run
  builtin.observe` comes through the same code, so both doors journal
  identically.
- `pipelines/journal.py`: `run_started` gains optional `resume` +
  `evidence_tier` payload keys (spec §6 facts a reader needs on the
  row; omitted for in-process pipelines — the three keys stay exactly
  what they were).
- `pipelines/slots.py`: `builtin.observe` declaration (empty
  capabilities, three int knobs, `resume="none"`, `evidence_tier="T1"`);
  `PipelineDeclaration` carries the two fields with in-process defaults.
- `pipelines/registry.py`: local manifests may declare
  `[evidence] declares_tier = T1|T2|T3`; off-ladder tiers and
  off-contract `[evidence]` keys are refused at load, by name.
- `pipelines/conformance.py`: `observe_conformance` — four real rows
  (double-run sequence equality on reseeded identical workspaces;
  SIGKILLed child → exactly one COMPLETED call, state `signalled`,
  never re-run; 1 s wall clock → `timed_out` with terminal rows; stdout
  secret rides the bundle, never the lineage payloads) and two honest
  n/a rows (no replay to diverge — `resume="none"`; child budgets are
  the sandbox's own contract).
- `docs/interfaces/observe-bundle.md` (new): the format documented for
  consumers, mcp-server.md's style.
- Tests: `tests/observe/` — 57 tests across bundle mechanics, the
  classification matrix (fabricated results for all five states
  including resource_killed), real scripted runs on the real subprocess
  backend (the done-when test: fake harness → complete T1 bundle;
  kill -9 chaos: self-killed child → complete bundle, `signal=9`,
  half-work visible; timeout; deletion; transcript cap; the
  started-skeleton crash marker; git head-to-head/commit/dirty/
  no-commits cases), pipeline #4 (row order = ledger-before-effect,
  run rows carry the two facts, observer crash → FAILED row, foreign
  facade refused by name, unparseable/empty goal refused, bool-is-not-
  an-int, recovery sweep SKIPPED_PIPELINE_LINEAGE + interrupted close),
  and the CLI front door (6 command tests incl. `pipeline trace` and
  `pipeline run builtin.observe`).
- Conventions: 125 docs citations re-anchored after the line shifts
  (difflib-mapped, prefix-preserving; one bracket-style `[FACT ...]`
  citation repointed by hand); the new `TYPE_CHECKING` block in
  `cli/observe.py` classified E with a note.

## 7. Tests and gates
- `tests/observe` + touched pipelines/cli suites: green.
- Full suite: **4,429 passed / 244 env-gated skipped / 1 xfailed, 0
  failures, exit 0** (W6 close: 4,368 passed → +61, exactly the new
  tests; 4,674 collected).
- mypy `src/paa`: 0 issues in 186 files (after reinstalling the `tui`
  extra — the sandbox recycle had dropped textual, which alone
  produced 19 phantom "has type Any" errors in `tui/chat/*`).
- ruff `src tests`: clean (the first pass flagged RUF022 `__all__`
  order, S311 on `random.random()` — replaced with the coder's seeded
  LCG — and ASYNC221 on fixture git calls — moved behind a sync
  `_git()` helper, the coder suite's own pattern).
- Conformance: **4 pipelines GREEN** (builtin, chat, coder.tool-loop,
  builtin.observe).

## 13. Out-of-scope findings
- `paa inspect`-time honesty: the registry's `runnable` flag still
  means "has an in-process run path"; a local manifest declaring
  `evidence.declares_tier` cannot run (P3), and the CLI says so — but
  the row does not yet surface the tier in `paa pipeline list`'s table
  (only in `inspect`). Left for C7.2's bundle work to avoid churning
  the list format twice.
