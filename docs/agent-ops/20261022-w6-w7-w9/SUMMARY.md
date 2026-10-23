# SUMMARY — run 20261022-w6-w7-w9, branch `ops/20261022-w6-w7-w9`

**Merge base:** `02f3636` (origin/main) · **tip:** `27a744d` · **commits on the branch:** 56
**Gate at close:** ruff 0 findings · mypy 0 errors across 138 source files · **2907 passed / 235 skipped / 4 xfailed** (baseline: 1915 passed / 233 skipped)

## 1. What changed and why (root cause, not symptom)

The substrate track's W6–W8 and ROADMAP Phase 0 (0.1–0.5), as scoped in `docs/plans/w6-w7-w9.md`:

**W6 — the suites.**
* T-001 (blob error vocabulary, wave 1): the cold lake translated nothing, so a driver's exception crossed the boundary and the contract was silent about failure. New `BlobError` vocabulary (six members) + `translate_blob_error`; six raw botocore escape sites closed; `BlobStore` got its error clause. ADR-0032 (amended in place by W2-IMPR-05: `upload_fileobj` propagates the raw `ClientError` — the *outcome* holds, the *record* was wrong).
* T-002 (live S3 suite): 20 lifecycle/failure-mode bodies × two independent servers (SeaweedFS, RustFS), test-owned buckets per run, `PAA_TEST_S3_*` convention, `requires_s3` marker, `paa[s3]` added to the dev extra. **UNVERIFIED against real servers here** (no `PAA_TEST_S3_ENDPOINT` configured) — verified by the agents' own runs, not by this gate.
* T-003 (fetch-images↔compose parity, wave 1): merged compose stack; repeatable `--compose <path>` merged in file order, later wins; `PAA_COMPOSE_FILE` kept; loud refusals for missing/invalid compose files. ADR-0030 amended.

**W7 — the cleanup.**
* T-004 (`TYPE_CHECKING`, wave 1): all nine modules that mentioned `RelationalBackend` now bind it at runtime; the import lock (`tests/conventions/test_import_conventions.py`) enforces it as a subprocess sweep; the `storage/relational/__init__.py` `get_type_hints` crash fixed. Wave 2 fulfilled the eighth deferral (`observability/runs`) and removed the now-empty deferral facility (W2-FIX-01).
* T-010 (stale docs + docs-reference checker): ~26 wrong `path:line` references swept and fixed; `tests/conventions/test_docs_references.py` is a lock (file exists, line exists, line not blank) with stated, narrow exclusions (`docs/agent-ops` run trail, `docs/plans` proposals).
* T-011 (marketplace's untrusted body): `HttpRegistry` let the server choose the local exception type (`AttributeError`/`TypeError`). One project error (`RegistryError`) for every body-reading path, server text bounded (`_MAX_EXCERPT_CHARS=200` etc.), never interpolated into a log message unescaped. Zero in-tree callers of the old exception types.

**W9 — ROADMAP Phase 0 (0.1–0.5).**
* 0.1 validation: 417 tests; a malformed keyword value (`minLength: "abc"`) is a **schema error** — reported, never a `TypeError`, never silently ignored — consistent across **all three** JSON-Schema validators (validation module, `models/base.py`, `skills/contracts.py`) via one shared keyword table in `src/paa/core/json_schema.py`.
* 0.2 agents: the exact ten-event list asserted for the happy/failure/block/gate paths (list equality, not subsequence; the `CRITIQUE_CONCLUDED` idempotency trap accounted for); a mediated three-node delegation cycle refused **at `MAX` modality** (at STANDARD the depth ceiling fires first); the anti-goal 0.82 cosine path on both sides of the boundary plus fail-closed; the six untested irreversible patterns now each have a case that fails when removed; `HARD_STOP_ESCALATE_TO_USER` now **parks** the task until a human rules — a gate request is a question with an identity (`gate:<role>:<ordinal>`), stable across redelivery, distinct across questions, releasable only by `clear_human_gate`. ADR-0034 records the behaviour change and its cost.
* 0.3 models: the API key no longer reaches a log line, a ledger payload, an exception's message/details, **or the exception's chain** — the router's raised errors carry no chain at all (built in a helper, raised outside the handler with `from None`); `ModelUnavailableError`'s details scrubbed like its sibling's message; the LOCKDOWN spy asserts zero frontier calls; the semaphore asserts `max_in_flight == 2` (not `<=`); the escalation discriminator is the event's own uuid4, so a restarted router no longer suppresses the second `MODEL_ESCALATED` record; the router uses the mode it was constructed with, failing closed otherwise.
* 0.4 observability: percentile **values** pinned against hand-computed numbers (p95 of `[1..5]` = 4.8) plus a hypothesis property over `rank = (n-1)·q`; `finish_run`'s returned row asserted field by field; `COALESCE` non-destructive partial finish; all six `query_runs` filters; `purge_older_than` respects "unfinished runs never purged"; `logging.py`'s five claims tested; six source defects fixed at the root cause (the `finish_run` COALESCE for `exit_code`, the `LIMIT` cut's missing total order, the empty summary's missing `window`, `json_valid` rejecting `Infinity`, quantile validation for empty series, `capacity=0` `IndexError`).
* 0.5 CI: `.github/workflows/ci.yml` runs `ruff check src tests`, `mypy src`, and the full strict pytest suite on every push and PR; installs `.[dev]` (not `.`); installs `python-is-python3` (the landmine — the sandbox's child `PATH` hides `actions/setup-python`'s interpreter); PostgreSQL service container with `PAA_TEST_POSTGRES_DSN`; SeaweedFS + RustFS service containers with `PAA_TEST_S3_*`; extras matrix; install retry convention. **UNVERIFIED as a workflow run** (no GitHub/Docker here) — steps verified locally.

**T-012 (records):** `AGENTS.md` gained the new house rules (import lock without deferral, live S3 convention, docs-reference checker, CI commands); `docs/MCP_AND_PLUGINS.md` W6/W7 status lines now past tense and name what covers them; `docs/ROADMAP.md` Phase 0 has a State column per item with what covers and what does not; `docs/adr/README.md` indexed 0032–0035.

**Close-out fixes (reviews' findings):** all five MAJORs from the wave-2 reviews closed and each reversal confirmed red by the delta reviewer himself (13 mutations, all caught): the second human-gate request swallowed by the idempotency key (F-1); the malformed-keyword crash on `models/base.py`'s validator (F-2); its sibling `ModelUnavailableError` carrying details unscrubbed (F-3); the key surviving on the exception's `__cause__`/traceback (R-W2-203); six of nine irreversible patterns uncovered (R-W2-201); the ADR-0032 record's false wrapper claim (R-W2-202).

**Two operator decisions deliberately left open** (recorded in `01-work-ledger.md` §"Open items", visible as open in `ROADMAP.md`/`AGENTS.md`, not patched): (a) the unreachable egress check in `_check_lockdown` — reorder vs delete is a judgement about what the policy should *say*; (b) four pattern-semantics findings (`git push -f`, `rm` flag spellings, `DELETE FROM public.users`, `TRUNCATE` without `TABLE`) — each widens a security rule over prose.

## 2. Verified (commands run, actual results)

| Gate | Base `02f3636` | Wave-2 close | Wave-3 close |
|---|---|---|---|
| `ruff check src tests` | 0 findings | 0 findings | 0 findings |
| `mypy src` | 0 errors (136 files) | 0 errors (138 files) | 0 errors (138 files) |
| `pytest tests/ --strict-markers --strict-config` | 1915 passed / 233 skipped | 2678 / 235 / 3 xfailed | **2907 / 235 / 4 xfailed** |

Reviews (all independent, none by an implementer): W2-REV-01 (code), W2-REV-02 (tests), W2-REV-03 (delta — re-ran reversals itself, 13 mutations all caught), W3-REV-01, W4-REV-FINAL (whole-branch: **merge-ready as-is**). All reversals confirmed; every test that was claimed to fail without its fix was shown to fail.

## 3. Unverified or assumed

* The live S3 suite against SeaweedFS + RustFS — skipped here (no `PAA_TEST_S3_ENDPOINT`); proven by the agents' own container runs, not by this gate.
* The CI workflow actually executing on GitHub Actions — YAML parse-checked and every step verified locally; workflow run UNVERIFIED.
* The `requires_postgres` tests — skipped here (no `PAA_TEST_POSTGRES_DSN`), as in every baseline.
* `actions/setup-python` interpreter visibility issue on the runner — the `python-is-python3` line's necessity was verified against `AGENTS.md`'s account and the sandbox's child-`PATH` design; not re-proven on a runner.

## 4. Known limitations, follow-ups, and decisions for the user

* **Push decision:** the branch is **not pushed**; `origin/main` remains at `02f3636`. Push only on your word.
* **The two operator decisions** above (egress check reorder vs delete; pattern-semantics widenings) — neither was patched; each carries a recommended resolution in `01-work-ledger.md` §"Open items" and W2-FIX-03's report §6.
* Strict xfail markers remain: four defects found in files outside their write-set (each honestly escalated, each marked `xfail(strict=True)`).
* Next runs should not re-derive this trail: `docs/agent-ops/20261022-w6-w7-w9/` holds the mission, the ownership tables, every brief, every report, and every gate record.

Never claim tests passed without running them — every number above was produced by the commands listed, on this branch, in this session.
