<!-- ORCH-DIGEST:START -->
agent: W5-IMPL-04 | task: T-C5-5 (C5.5) | status: DONE
code_commits: none (orchestrator commits after review) | report_commit: pending
files_changed: 1 (README.md, line 251 only — the stale "require dedicated
  validation" row) + this report; tests/observability/* read in full and verified,
  not modified (the suite was authored by the crashed predecessor session and is
  green as it stands)
contracts_changed: none (no src/ file touched; no frozen surface affected)
tests: added 0, changed 0, removed 0 by me — the suite itself verified end to end:
  185 passed, 0 failed/skipped/xfail (metrics 68, tracing 31, runs 61, logging 25),
  each file also green on its own run; ruff clean on tests/observability; mypy src clean
ownership_violations: none (README line 251 + this report only; line 303 — already
  correct in-tree from the predecessor session — deliberately untouched, and its
  "185" re-verified against a fresh run of my own)
deferred_dependencies: OTLP egress bridge (needs paa[otel] plus a reachable
  collector — impossible on this offline host; owner: solo maintainer, first online
  pass); live-Postgres verification of the run row's UUID shapes (fake-backend
  coverage only; owner: the W9 Docker/Postgres pass or a requires_postgres suite)
out_of_scope_findings: 3 (README STATUS:BEGIN block says "4,386 tests collected"
  vs 4,461 collected in the current tree — stale, regenerate after the wave lands;
  README line 333's layout sketch omits "logging" from observability/; a duplicate
  identical query inside test_the_since_filter — cosmetic, its first assertion
  already covers the inclusive boundary)
temporary_mitigation: no
deep_issue_flag: no (no test found vacuous or asserting nothing; every file read in
  full, every count re-run)
least_confident_about: why the orchestrator's quick run printed 181 where the
  finished tree collects and passes 185 — not reproducible in this tree, recorded
  as the probable mid-crash state of the predecessor's save rather than claimed
  as understood
confidence: high (every number quoted below was produced by a command I ran
  myself in this working tree)
last_comms_line_read: 2026-11-22T00:00:00Z (WAVE-START)
<!-- ORCH-DIGEST:END -->
# Observability validation suite — W5-IMPL-04 — T-C5-5 (C5.5), wave 5, 2026-11-22

Task: spec `docs/plans/2026-11-21-next-spec-2.md` §4 C5.5 — "Observability
validation suite (metrics/tracing/runs are 'built; untested') — golden +
behavioural tests", acceptance "suite green; row flips to Tested". A predecessor
session wrote the suite into the working tree and crashed before verifying or
reporting; this task is the verification, the README reconciliation, and the
report. No commit is made here; the orchestrator commits after review.

## 1. Method and what was actually verified myself

- Read `AGENTS.md` first, then the whole of the four test files
  (`tests/observability/test_metrics.py` 1,141 lines, `test_runs.py` 1,502,
  `test_tracing.py` 661, `test_logging.py` 554) and the whole of the four source
  modules they cover (`src/paa/observability/metrics.py` 563 lines, `runs.py`
  572, `tracing.py` 390, `logging.py` 169), plus the package `__init__.py`.
  Nothing below is inherited from the predecessor session's claims — every
  count, every ruff/mypy result, and the README diff were re-produced by me in
  this tree.
- Confirmed the suite is what C5.5 asks for: it is **not** imports or smoke.
  Golden tests pin published shapes exactly (dict key sets, rendered series
  keys, the RFC §10.2 trace record as a golden dict, the JSON log line's whole
  key set, hand-derived percentile values); behavioural tests drive failure
  paths, concurrency, cancellation boundaries, filter composition, retention
  sweeps and a real ledger join; and there are six hypothesis property tests
  (five in metrics, one in runs) pinning rules rather than examples. Section 2
  is the file-by-file evidence.
- No test was found vacuous or broken, so **no test file was modified**: my
  write-set stayed README line 251 and this report. One cosmetic wart is
  recorded as a finding (§7) rather than fixed, because fixing it is not worth
  a write outside the letter of the brief.
- The runs tests run against a real SQLite `Database` (throwaway file, the
  shared `tests/conftest.py::db` fixture) and a real `LedgerStore` for the
  correction-rate join — not mocks of the SQL.

## 2. Coverage map (module → test file → test classes)

Counts are collected test items per file; "golden" = pins a published shape or
a hand-derived number, "behavioural" = drives the code through a real path.

### `src/paa/observability/metrics.py` → `tests/observability/test_metrics.py` (68 tests)

- **Golden.** `TestPercentileLabels` (5): the default labels are p50/p95/p99;
  a half-way quantile is **rounded not truncated** (99.5 → `p100`, 1.5 → `p2`);
  banker's rounding pinned both ways (98.5 → `p98`); the key names the value
  under it; `DEFAULT_PERCENTILES == (0.50, 0.95, 0.99)`.
  `TestTheHandComputedValues` (8): the percentile *numbers* derived on paper —
  p95 of [1..5] is 4.8, p99 of 1..100 is 99.01, p95 95.05, p50 50.5, the odd
  median 3.0, the even median 2.5, the result is not a sample and lies strictly
  between its neighbours — deliberately *not* the function compared to itself.
  `TestTheSummary` (3): the six numeric fields are present whether or not there
  are samples, hand-valued (count 5, sum 15, mean 3, window 5).
  `TestLabels` (5): the rendered series key `name{k=v}` with sorted labels, the
  snapshot is exactly `{counters, gauges, histograms}` and nothing else, an
  unlabelled series filed under its bare name. `TestTheDeclaredKinds` (8):
  cross-kind updates refused, undeclared refused when `allow_undeclared=False`
  (the `vram_saturaton` typo), accepted by default, `register` end-to-end, and
  the RFC §10.1 `METRIC_SPECS` list pinned by kind with unit and description.
- **Behavioural/property.** `TestTheAlgorithm` (5 hypothesis properties): the
  value sits where `rank = (n-1)·q` puts it (endpoints drawn explicitly), each
  sample sits at its own quantile, the value never leaves the sample range, it
  is monotone in the quantile, the ends are the extremes. `TestPercentileValues`
  (8): empty → 0, single sample, ends, interpolation vs nearest-rank, quantile
  refusal including `nan` and including for an empty series.
  `TestTheRegistryCarriesTheLabels` (1). `TestTheRing` (4): count/sum/min/max
  are lifetime facts while percentiles describe the retained window (window 3,
  window-median 4.0 vs history-median 3.0), recency retention, `capacity=0`
  refused at both the Histogram and the registry layer.
  `TestTheRegistryRefusesNonsense` (7): non-finite observations and counters
  refused, overflow-to-infinity refused (the telemetry column is `json_valid`
  and `json.dumps` would write `Infinity`), the snapshot is JSON end to end.
  `TestCounters` (4) and `TestGauges` (4): monotone counters with the negative
  increment refused without writing, never-incremented reads 0 while a
  never-set gauge reads `None` (the deliberate asymmetry).
  `TestReset` (2): series dropped, declarations survive. `TestTheTimer` (2):
  the block is recorded, including when the block raises.
  `TestTheThreadSafety` (2): 4×250 concurrent increments lose none; a snapshot
  taken while series are being added is never a torn read (500 labelled shards
  written against a reading thread).

### `src/paa/observability/tracing.py` → `tests/observability/test_tracing.py` (31 tests)

- **Golden.** `TestTheSpanRecord` (3): `Span.to_dict`'s whole key set, the event
  entry shape (`name`/`timestamp_ns`/`attributes`), duration rounding,
  JSON-serialisable; an open span reports 0.0; the child names the parent.
  `TestTheIdentity` (1): the trace id is the canonical hyphenated UUID (the
  form both substrates store the same way) and the span id is 16 hex chars.
- **Behavioural.** `TestAFailedSpan` (4): the exception attached (type +
  message, never a traceback), a failed span never marked OK, the caller's
  exception object propagates untouched, a `BaseException` is recorded and the
  span still finished. `TestASuccessfulSpan` (3): OK, finished, attributes,
  `service.name` on every span, retention. `TestNesting` (2): trace/parent
  inheritance and `current_span` following the call structure.
  `TestTheGoverningRule` (3): an exporter that raises does not break the body,
  a metrics registry that raises does not either, a disabled tracer still
  finishes the span but neither retains nor exports it.
  `TestTheBaseExceptionBoundary` (4): an exporter that raises
  `KeyboardInterrupt` propagates (the deliberate hole in `_safely`), the span
  is still finished and retained, an ordinary exporter failure does not
  replace the body's exception, an interruption *does* — the asymmetry stated
  exactly. `TestTheRetainedSpans` (4): the ring keeps the newest, returns a
  copy, `clear` empties it and the tracer keeps working, `spans_for_trace`
  groups by trace id in finish order. `TestContextPropagation` (2): concurrent
  `gather` children of one parent do not adopt each other (asserted *inside*
  the bodies while both are live), and a task created outside a span keeps its
  own trace — the ContextVar's whole reason for being, which no sequential
  nesting test can see. `TestTheMetricsIntegration` (2): the finished span's
  duration observed into `span_duration_ms`, failed spans observed too.
  `TestTheExporter` (2): the exporter receives the finished span; a disabled
  tracer does not export. `TestTheServiceName` (1): from settings, not a
  constant.

### `src/paa/observability/runs.py` → `tests/observability/test_runs.py` (61 tests, real SQLite + real ledger)

- **Golden.** `TestTheTraceRecord` (2): a finished run serialises to the exact
  RFC §10.2 grouped dict (identity/timing/outcome/model/resources/telemetry),
  and an open run records what it knows and nothing else (`IN_FLIGHT`, nulls,
  model group defaults). `TestTheStatsPayload` (1): `SkillStats.to_payload`'s
  rounding contract pinned exactly, with mean latency pinned to 2000.0 from
  fixed timestamps rather than `>= 0.0`.
- **Behavioural.** `TestIdentifiers` (11): canonical trace-id form, a span id
  is not a UUID, the trace id handed back is the one stored, parent-span round
  trip, caller-supplied trace id kept, **normalised** (`uuid4().hex` →
  hyphenated, the cross-substrate join protection), a non-UUID refused as
  `InvalidTraceIdError` (a `PaaError`, and why), the record is JSON, the
  correlation id a UUID, the session kept. `TestTheIdCoercion` (7 + 1
  hypothesis property over every non-identifier shape a column can hand back):
  `as_text` strict refusals. `TestTheRowConversion` (4): the row → model
  conversion exercised on rows the embedded substrate cannot produce — UUID
  objects where the server substrate produces them (the shape that once broke
  a pydantic model) — plus NULL stays `None`, record still JSON.
  `TestFinishRun` (11): every field of the returned row asserted one by one;
  the returned row equals the stored row; IN_FLIGHT until finished; non-zero
  exit → ERROR; duration computed from the stored start, not the caller's
  stopwatch; never negative; telemetry round-trips the JSON column; a partial
  finish preserves what an earlier finish wrote (`COALESCE`, tested by
  finishing twice); an omitted exit code does not erase a recorded one; a
  first finish without an exit code is ERROR, not OK; an unknown trace id
  returns `None` rather than raising. `TestQueryRuns` (11): every filter
  (correlation, session, skill, role, `since` — inclusive boundary —
  `only_finished`), filters compose as AND, the limit cuts the newest rows
  first, a filter that matches nothing returns nothing, and the order is total
  when two runs share a `started_at` (trace id breaks the tie, runs inserted
  in the opposite order so insertion order cannot pass it by accident).
  `TestCount` (1). `TestPurgeOlderThan` (4): finished-only purge, an unfinished
  run never purged however old (the recovery-sweep promise), the strict
  boundary, the return value is the removed count. `TestDurationBoundaries`
  (2): a real zero and the 999 µs → 0 / 1000 µs → 1 truncation.
  `TestSkillStats` (4): no samples → zeroes (an empty sample is not a
  failure), an unfinished run excluded rather than counted as a failure,
  aggregates hand-checked, a silent backend is not a crash.
  `TestTheCorrectionRate` (3): the headline join — the rate is the share of
  *correlations* corrected against real `USER_CORRECTION` ledger events, not
  per-run (the fan-out the two-query design avoids: 2 runs, 1 correction,
  rate 1.0, sample count still 2), and a correction of an unfinished lineage
  is not counted.

### `src/paa/observability/logging.py` → `tests/observability/test_logging.py` (25 tests)

- **Golden.** `TestTheJsonLineShape` (1): a JSON line carries *exactly* the
  documented fields, one record per line, keys sorted (stable bytes for a
  shipper). `TestTheRendering`'s timestamp test: ISO-8601 UTC with the `Z`.
- **Behavioural.** `TestTheCorrelationIdIsAField` (3): the id is a field of
  the record, not a substring of the message; a line logged outside a binding
  carries no id (absent, not empty); the trace id stamped the same way.
  `TestTheCallSiteWins` (3): an explicit `correlation_id`/`trace_id` keyword
  wins over the ambient binding (the recovery-sweep case), and a
  `structlog.contextvars` binding wins too — the processor *order*
  (`merge_contextvars` before the injector) is part of the contract.
  `TestTheBindingIsRestored` (6): nesting restores the previous value for both
  ids, an inner binding that omits the trace leaves the ambient one alone, a
  UUID is accepted and becomes its canonical text, `clear_correlation` drops
  both, `set_trace_id` is independent. `TestTheRendering` (5): JSON is opt-in
  with console the default, the level filters at write time, the second
  configuration takes effect (no logger cache). `TestIdempotence` (2): a
  second call does not duplicate the processor chain (JSON and console paths).
  `TestTheDefaultLevel` (1): `settings=None` means INFO, DEBUG absent.
  `TestTheNameCollision` (4): the module is not and does not shadow the stdlib
  `logging`, resolves the same object, and the trap is demonstrated in a child
  interpreter (a shadowed `sys.modules["logging"]` breaks the import with an
  `AttributeError`) plus the fresh-interpreter half.

`__init__.py` (re-exports and the wiring docstring) is exercised implicitly by
the imports above; there is no behaviour of its own to test beyond them.

## 3. Residual uncovered surfaces (stated, not buried)

1. **The OTLP egress bridge (`Tracer._otel`)** — not exercised: the import
   attempt, the `_otel_attempted` give-up-once semantics, the
   `tracing.otel_unavailable` warning, and the bridge that forwards spans to
   an `OTLPSpanExporter`. This host is offline and `opentelemetry` is not
   installed; the bridge cannot be built here, and faking the whole SDK
   import chain would test the fake. The README row (line 303) already says
   exactly this, and that honesty is kept. Owner: solo maintainer, at the
   first pass on a machine with `paa[otel]` and a reachable collector.
2. **The live PostgreSQL substrate for the run row** — the row → model
   coercion is tested against a `_ServerShapedBackend` that returns rows with
   real `uuid.UUID` objects (the shape the server produces), but no test here
   runs against Postgres itself. The storage layer's cross-substrate parity is
   the W9 Docker/Postgres pass's business; the fake covers the coercion the
   module owns.
3. **Small, listed for completeness**: `MetricsRegistry.__repr__` (trivial);
   of `METRIC_SPECS`' 18 names, 6 are individually kind-pinned and all 18 are
   checked for unit+description — the other 12 gauges' kinds are not
   individually asserted; `_supports_colour`'s isatty colour branch (console
   output asserted non-JSON, colours not asserted); `_begin`'s defensive
   fallback and `_otel`'s setup-failure backstop are `# pragma: no cover` by
   design, matching the repo's convention for unreachable defensive code.

## 4. The README fix (line 251 only)

The two observability rows contradicted each other: line 303 (build status)
had been flipped by the predecessor session to "Tested — golden + behavioural
(tests/observability) | 185; OTLP egress bridge not exercised", while line 251
(known limitations) still said the suite did not exist. Line 251 now states
the truth and keeps the same honest residual:

- Before: `| Observability | Metrics and tracing are built but require
  dedicated validation. |`
- After: `| Observability | Metrics, tracing, runs, and logging now have a
  dedicated suite (tests/observability, 185); the OTLP egress bridge is not
  exercised offline, and the server substrate's UUID row shapes are covered
  by a fake backend rather than a live Postgres. |`

Line 303 was **not** touched (my brief reserves it as already correct), and
its "185" was re-verified against my own run before leaving it alone. The
README diff in the tree is exactly these two rows: the predecessor's line-303
flip and my line-251 reconciliation — nothing else. No other README line was
edited by me.

## 5. The 181 → 185 count

The orchestrator's quick run reported `181 passed`; every run I made against
the finished tree reports **185** (collected items 25 + 68 + 61 + 31 = 185;
`185 passed in 16.19s`; per-file 68/31/61/25 all green). The 4-test delta is
consistent with the predecessor session crashing mid-write — its last saves
may not have been on disk at quick-run time — but that is a reconstruction,
not a verified fact: 181 is not reproducible in the current tree. Because the
README row's figure is the count *this* tree produces, the row's "185" was
kept, not "corrected" to the stale 181. (Note for anyone re-running the exact
command from the task: `python -m pytest tests/observability -q
-p no:cacheprovider` prints dots only — `pyproject.toml`'s `addopts` already
carry `-q`, so the extra `-q` makes verbosity −2, which suppresses the summary
line. The count is in the dots; `185 passed` appears when the duplicate `-q`
is not added.)

## 6. Commands run and results (verbatim)

| Command | Result |
|---|---|
| `python -m pytest tests/observability -p no:cacheprovider` | `185 passed in 16.19s` (exit 0) |
| `python -m pytest tests/observability -q -p no:cacheprovider` | dots only, `[100%]`, exit 0 (summary suppressed by double `-q`; see §5) |
| `python -m pytest tests/observability -p no:cacheprovider -o addopts="--strict-markers --strict-config"` | `============================= 185 passed in 8.54s ==============================` |
| `python -m pytest tests/observability/test_metrics.py` | `68 passed in 3.56s` |
| `python -m pytest tests/observability/test_tracing.py` | `31 passed in 2.23s` |
| `python -m pytest tests/observability/test_runs.py` | `61 passed in 8.50s` |
| `python -m pytest tests/observability/test_logging.py` | `25 passed in 5.76s` |
| `python -m pytest tests/observability --collect-only -q -o addopts=""` | test_logging 25, test_metrics 68, test_runs 61, test_tracing 31 |
| `python -m ruff check tests/observability` | `All checks passed!` |
| `python -m mypy src/paa/observability` | `Success: no issues found in 5 source files` |
| `python -m mypy src` | `Success: no issues found in 173 source files` |
| `python -m pytest tests --collect-only -q -o addopts=""` | `4461 tests collected in 24.42s` (see §7.1) |

## 7. Out-of-scope findings (not fixed here, per brief)

1. The README `<!-- STATUS:BEGIN -->` block says "**4,386 tests collected**";
   the current tree collects **4,461** (the wave-5 uncommitted test additions,
   observability's +49 among them). The block is script-generated
   (`scripts/status.py`) and belongs to whoever lands the wave — I did not
   edit it.
2. README line 333, the layout sketch, reads "`observability/ metrics,
   tracing, execution runs`" — it omits `logging`. One-word fix, but it is
   outside my one-line write-set (line 251), so it is listed here instead.
3. `test_runs.py::TestQueryRuns::test_the_since_filter` executes the same
   query twice (`rows` and `boundary` with identical arguments); the first
   assertion already covers the inclusive boundary (gamma starts exactly at
   the cutoff), so the second block is redundant, not wrong. Cosmetic; left
   alone to keep my write-set clean.
4. The README "Testing" section shows the Windows venv path while AGENTS.md
   shows the Linux one — pre-existing and consistent with each other's
   platform notes; not an observability claim, noting only that I checked
   README claims about observability and found none stale besides line 251.

## 8. Deviations

- None on scope or write-set. README.md line 251 only, plus this report; no
  test file was modified because no broken or vacuous test was found; line
  303 left as the predecessor wrote it after re-verifying its count myself.
- The task permitted correcting the README row's "185" if the true count
  differed; the true count is 185, so the row stands — the number that
  *was* wrong (181) was the quick-run figure, not the row's.

## 9. Checklist

- [x] Suite verified as genuinely golden + behavioural (coverage map §2;
      every file read in full, every claim backed by a green run I executed).
- [x] `python -m pytest tests/observability -q -p no:cacheprovider` run
      myself; true count 185 quoted verbatim (§5, §6).
- [x] `python -m ruff check tests/observability` — `All checks passed!`.
- [x] README line 251 reconciled with reality (before → after in §4);
      one line, table style kept; no other README line edited.
- [x] Residual uncovered surfaces stated honestly with owners (§3).
- [x] No `git add`/`git commit`/`git push`/`git stash`/`git checkout`
      performed; the tree's uncommitted sibling work untouched.
