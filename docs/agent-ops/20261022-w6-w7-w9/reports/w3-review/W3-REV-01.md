# Review report — W3-REV-01 · wave 3 · run 20261022-w6-w7-w9

Independent review of the wave-3 diff `git diff 445a6ec..HEAD -- src/ tests/ docs/ .github/`.
I did not write any of the wave-3 code. I am not the orchestrator and not W3-IMPL-01..04.
I do not edit code; I report findings and the orchestrator decides.

Branch: `ops/20261022-w6-w7-w9` (verified). Commits under review: dad08e4, 07baff0 (T-009);
3c500de, a949bbf (T-010); 01b5adb, efd18f6, e214428 (T-011); eb849ff, f54e7cf (T-012).

Gate runs (all from this checkout, `.venv` at repo root, Python 3.12.3):

```
$ .venv/bin/python -m ruff check src tests
All checks passed!                                       (exit 0, 0 findings)
$ .venv/bin/python -m mypy src
Success: no issues found in 138 source files             (exit 0, 0 errors)
$ .venv/bin/python -m pytest tests/conventions tests/marketplace -q
...(all green)...                                        (exit 0)
$ .venv/bin/python -m pytest tests/ -o addopts="--strict-markers --strict-config" -q
2907 passed, 235 skipped, 4 xfailed, 1 warning in 126.55s (exit 0)
```

The 1 warning is a pre-existing Starlette/httpx deprecation warning in `tests/api`, not a
gate failure and not introduced by wave 3.

---

## VERDICT

**WAVE 3 IS SAFE TO CLOSE.** No BLOCKER or MAJOR findings. One NIT on T-011 (reason
misclassification on an oversized single chunk); no code change is required for it because
both outcomes are safe `RegistryError` refusals. All per-tasks CLOSED.

---

## PER-TASK

### T-009 — CI workflow (W3-IMPL-01) — CLOSED

Evidence:
- `.github/workflows/ci.yml` parses with `yaml.safe_load` (verified). Jobs: one `gate`
  job, matrix over `install` shapes `minimal`/`dev`/`all` with extras `""`/`[dev]`/`[all]`,
  `mypy_exclude` `src/paa/api`/`""`/`""`. `fail-fast: false`, `timeout-minutes: 45`,
  `permissions: contents: read`.
- The gate is exactly the repo's gate, in order:
  `ruff check src tests` · `mypy src` · `pytest tests/ -o addopts="--strict-markers
  --strict-config" -q`. No `-k`, no `--ignore`, no marker exclusion.
- Install shape is `.[dev]` for the dev job (the documented setup). `pyproject.toml`
  `dev = [...]` self-references `paa[api]` and `paa[s3]`; confirmed in the file.
- `python-is-python3` is installed via apt with the retry convention, followed by
  `test -x /usr/bin/python`. Rationale is present and correct in the workflow header and
  in the `install python-is-python3` step comment: `src/paa/sandbox/base.py::build_child_env`
  sets `PATH=/usr/local/bin:/usr/bin:/bin` (verified at `src/paa/sandbox/base.py:588`), and
  `src/paa/agents/reasoning.py:445` defaults the plan-step command to `["python", "-c",
  "pass"]`. Without `/usr/bin/python` the step exits 127 and six COMMITTED assertions fail
  for a host reason, not a code reason (matches AGENTS.md's account of the wave-1/2 host fix).
- Service containers with health checks: `postgres:16` (pg_isready), `seaweedfs:4.48`
  (curl on 9333), `rustfs:latest` (curl on 9000). Env exported:
  `PAA_TEST_POSTGRES_DSN=postgresql://paa:paa@127.0.0.1:5432/postgres`,
  `PAA_TEST_S3_SEAWEEDFS_*` and `PAA_TEST_S3_RUSTFS_*` (labelled, two servers). These names
  match `tests/storage/test_postgres_migrations.py:74` and `tests/storage/test_s3_live.py`.
  Convention: DSN/endpoints name a server; each suite makes its own resource and drops it.
- Install uses the network-fragility convention: `PIP_RETRIES=10`, `PIP_TIMEOUT=120`,
  `PIP_DISABLE_PIP_VERSION_CHECK=1`, `PIP_NO_CACHE_DIR=1`, plus a shell `retry()` loop
  (5 attempts, growing backoff) wrapping `apt update`, `apt install`, and `pip install -e`.
  Gate tooling is read from `pyproject.toml`'s `[project.optional-dependencies.dev]`
  (skipping self-referential `paa[...]` entries), via a `tomllib` one-liner — one place,
  not a hand-maintained list.
- `src/` changes for T-009: none (only `.github/workflows/ci.yml` and docs/reports).

UNVERIFIED (cannot be run here; no GitHub, no Docker): `actions/checkout`/`actions/setup-python`
action SHA ↔ version correspondence; image digest pins (`postgres:16@sha256:...`,
`chrislusf/seaweedfs:4.48@sha256:...`, `rustfs/rustfs:latest@sha256:...`); SeaweedFS/RustFS
health-check `curl` assumptions; live S3 + PostgreSQL suites against service containers;
end-to-end workflow on `ubuntu-latest`. These are conventional pins, and the env-var names,
command texts, and gate shape are all statically verified. Minimal-venv build (`pip install -e "."`)
could not be attempted: the host has no pip/`ensurepip` in a fresh venv; the static import
scan over `src/paa` (all heavy deps imported inside functions/methods, except the graph
package's own `kuzu` module) is the proxy. The report correctly marks these UNVERIFIED.

### T-010 — docs sweep + docs-reference lock (W3-IMPL-02) — CLOSED

Evidence:
- `pytest tests/conventions/test_docs_references.py -q` → 5 passed (part of the full green
  suite; also run standalone as part of `tests/conventions`).
- The lock has teeth: I broke one reference in a scratch copy (`docs/README.md:46`,
  `src/paa/cli/main.py:73` → `:99999`) and the suite went red with
  `AssertionError: citations past the end of their file: ... src/paa/cli/main.py has 468 lines`.
  The real tree was never modified; the scratch copy was discarded.
- Scope: every markdown under `docs/`, plus `README.md` and `AGENTS.md`. Excluded, with
  reasons stated in the file: `docs/agent-ops/` (run trail, allowed to rot) and `docs/plans/`
  (dated proposals, discuss references as findings). Both are whole directories with stated
  reasons; no exclusion without a reason. This matches AGENTS.md's own rule text.
- Citation sample (sentence vs cited line), all agree:
  - `src/paa/cli/main.py:73` → `app = typer.Typer(` (docs/modules/cli.md:10)
  - `src/paa/cli/main.py:118` → `def status(...)` (docs/backend/overview.md:15)
  - `src/paa/cli/main.py:144` → `return existing` inside the idempotency-key branch
    (docs/modules/ledger.md:56)
  - `src/paa/cli/main.py:224` → `def doctor() -> None:` (report §3)
  - `src/paa/cli/main.py:289` → `serve` (docs/architecture/repository-map.md:119)
  - `src/paa/cli/main.py:352` → `tui` (docs/architecture/repository-map.md:125)
  - `src/paa/models/base.py:906` → `async def complete_structured(` (report §3)
  - `src/paa/runtime.py:251` → `async def build(` = `Runtime.build()` (docs/backend/overview.md:23)
  - `src/paa/ledger/store.py:144` → `return existing` (idempotency) (docs/modules/ledger.md:56)
  - `src/paa/ledger/store.py:450` → `async def save_snapshot(` (docs/modules/ledger.md:75)
  - `src/paa/ledger/recovery.py:250` → `except Exception as exc:  # one bad lineage...`
    (docs/flows/workflow-03-crash-recovery.md:147)
  - `src/paa/config.py:628` → `def _enforce_loopback(self)` (docs/modules/cli.md:50)
- The lock's blind spot (wrong without being broken) is acknowledged in the file itself;
  the hand-checked sample above covers it for the cited lines I read.
- README's three test counts: NOT A FINDING. AGENTS.md documents them as approximate/stale
  ("the total line is stale"); the sweep deliberately did not reconcile them, and they are
  outside T-010's write-set. The `path:line` references inside README.md pass the checker.
- `src/` changes for T-010: none (38 markdown docs + the new checker).

### T-011 — marketplace's untrusted HTTP body (W3-IMPL-03) — CLOSED (1 NIT)

Evidence: I read every body-reading path in `src/paa/marketplace/registry_client.py` and
`src/paa/marketplace/package.py`.

- Every body-reading path raises the project's own error type, on every malformed shape:
  - `search`: `_exchange` (bounded read) → `_require_success` (2xx only; 302 and 500 both fail)
    → `_require_body` (truncated/empty) → `_json` (not-JSON) → `_entries` (dict, results list)
    → `_entry` (typed fields). All failures → `RegistryError` (`PaaError`). No
    `json.JSONDecodeError`, `AttributeError`, `TypeError`, or `httpx.HTTPStatusError` can
    escape: JSON/decode failures are caught inside `_parse_json` and replaced with a sentinel;
    `raise_for_status` is not used; the status is checked in-project vocabulary.
  - `fetch`: `_exchange` → `_require_success` → `_require_body` → `SkillPackage.from_bytes(body)`;
    `PackageError` is caught and re-raised as `RegistryError(reason="malformed_package")` with
    the `PackageError` kept as `__cause__` (its message carries a bounded, quoted fragment —
    compliant with the message rule). Zip errors (`BadZipFile`, `LargeZipFile`, `RuntimeError`,
    `NotImplementedError`, `EOFError`, `OSError`, `zlib.error`) are one `PackageError`.
  - `publish`: `_exchange` → `_require_success`; no body to parse on success; an error body is
    read for its excerpt and refused.
  - Malformed shapes covered: not-JSON, JSON-not-an-object, `results` not a list, missing/wrongly
    typed entry keys, empty body, enormous body (1 MiB index / 16 MiB package caps), 500 with
    valid JSON body, 302, unreachable host, non-zip, non-JSON/non-object/wrongly-typed manifest,
    64 MiB unpacked-ceiling (decompression bomb), zip-slip member names.
- Server's text is bounded, with concrete maxima in code: `_MAX_INDEX_BYTES=1 MiB`,
  `_MAX_PACKAGE_BYTES=16 MiB`, `_MAX_ERROR_BODY_BYTES=4 KiB`, `_MAX_EXCERPT_CHARS=200`.
  The excerpt is computed from the first 800 bytes and truncated to 200 chars; cost is
  independent of body size.
- Server's text never reaches a log line unescaped: `_reject` logs only facts
  (`operation`, `reason`, `status_code`, `body_bytes`, `excerpt_chars`). `tests/marketplace/
  test_registry_client.py::TestTheServersTextNeverReachesALog` pins the exact key set
  `{"event","log_level","operation","reason","status_code","body_bytes","excerpt_chars"}` and
  asserts the server's words are in no log record, no message, no `details`. The excerpt
  (`RegistryError.remote_excerpt`) is bounded (≤200 chars), `unicode_escape`-escaped to a
  single ASCII line, quoted, and stripped of the client's own bearer token before anything
  else. It is an attribute, not a detail, so it cannot reach a ledger payload by accident.
- Closed vocabulary of messages: `_MESSAGES` (7 fixed strings, no remote interpolation).
- Callers of the old exception types: grep over `src/` and `tests/` for `HttpRegistry`,
  `MarketplaceRegistry`, `RegistryError` outside `marketplace/registry_client.py` and
  `marketplace/__init__.py` → zero hits. `HTTPStatusError` → 3 docstring hits only; nothing
  catches it. `except httpx…` → 3 hits, all in `models/_http.py` (a different client) plus
  the new `registry_client.py:496`. The contract change has no caller to break; this is stated,
  not assumed. `installer.py` (wave 1's file, read-only here) imports `SkillPackage` as a type
  and never calls `from_bytes` on remote bytes.
- `src/` changes for T-011: only `src/paa/marketplace/` — `__init__.py` (2 lines: export
  `RegistryError`), `package.py` (manifest type/shape/field checks; zip-error family → one
  `PackageError`; 1 MiB manifest / 64 MiB unpacked ceilings), `registry_client.py` (the fix).
  Nothing else in `src/` changed (verified via `git diff --stat -- src/`).
- Tests: 33 new, all over `httpx.MockTransport` (no network). Reversal measured: pre-fix
  behaviour + seam/name only → 29/33 new tests fail; full reversal → module does not import.
  15/15 single-rule mutations caught. Deterministic (no clock/sleep/socket), run 6× identical.
- `ruff` 0 / `mypy` 0 on the marketplace files / import-conventions lock 21 passed.

NIT (T-011, `src/paa/marketplace/registry_client.py:489-495`): in `_exchange`, if a single
HTTP response chunk is larger than the body cap, `total > limit` trips on the first chunk
before it is appended, so `chunks` stays empty and `body = b""`. The refusal is then logged
and raised as `empty_body` instead of `body_too_large`. Both outcomes are safe `RegistryError`
refusals (no body parsed, no remote text logged, excerpt empty), so there is no security or
correctness defect — only the refusal reason is misclassified. httpx's default chunk size is
64 KiB, so a single >1 MiB chunk is unlikely. Suggested fix: append the chunk before the cap
check (or track `truncated` separately from `body`), so an oversized single chunk reports
`body_too_large`. Not a blocker; no code change required for wave closure.

### T-012 — records (W3-IMPL-04) — CLOSED

Evidence: four files changed — `docs/MCP_AND_PLUGINS.md`, `docs/ROADMAP.md`, `AGENTS.md`,
`docs/adr/README.md`. No source, no tests, no other docs.

- Claims checked against the tree:
  - ADR index rows 0032–0035 present and correct in `docs/adr/README.md` (0032 blob error
    vocabulary, 0033 S3 live server suite, 0034 hard-stop escalation, 0035 unqualified-delete
    rule; all class **Correctness**). Index has the body-vs-index-only distinction
    (bodies on disk: 0008, 0010, 0015, 0019–0026, 0028–0035; the rest index-only).
  - `docs/adr/README.md` class taxonomy completed (fourth class `Honesty` added; rows for
    0016, 0017, 0018 added — the numbers the tree cites via `SPEC DEVIATION` citations).
  - `docs/MCP_AND_PLUGINS.md`: W6/W7 status lines to past tense; W8 corrected (plan records
    W8 as done); header status line corrected; five stale `path:line` references fixed, all
    sixteen rewritten to full repository-relative paths (verified against the code; e.g.
    `docker_backend.py:1369` → `DEFAULT_IMAGE = "paa/base_worker:v4.1"`,
    `sandbox/base.py:550-604` → `build_child_env`).
  - `AGENTS.md`: new section "The rules this tree holds itself to" at the end (line-stable:
    nothing cites past line 322; verified `git show HEAD:AGENTS.md` vs working tree — cited
    lines unchanged, differing lines ≤322 are [18,19,20,74,75] none of which are cited);
    `dev`-extra paragraph now names `paa[s3]`; "no CI config" bullet corrected; known-stale
    ADR bullet corrected. Line-number stability rationale written into the file.
  - ROADMAP Phase 0: State column added; per-item "what covers / what does not" table;
    coverage figures honest and not overstated (validation 94%, agents 89% with context_agents
    70% named as thin, models 85% with anthropic/ollama providers 43%/44% named as needing
    live endpoints, observability 90% with tracing 84%/metrics 85% named); three strict xfails
    named (scanner RecursionError, engine empty-artifact PASS, patch trailing newline).
- Open items visible as open, not closed:
  - ROADMAP 0.2 row: "Two operator decisions are open" — the unreachable declared-permission
    half of `_check_lockdown` and the four pattern-semantics findings.
  - `AGENTS.md` §"Two policy decisions are open" (line 493): same two items, same wording,
    carried to a durable location.
  - `docs/agent-ops/20261022-w6-w7-w9/01-work-ledger.md` §"Open items (carried, not closed)":
    item 2 (unreachable egress check) and item 4 (four pattern-semantics findings:
    `git push -f`, `rm` flag spellings, `DELETE FROM public.users`, `TRUNCATE` without `TABLE`),
    each with "Why it is open" and a pointer to W2-FIX-03's report. Neither item is patched.
  - ADR-0035 closed the fifth (position dependence of the delete rule); the other four remain
    open as policy decisions. Matches AGENTS.md's standing-orders text.
- AGENTS.md line-stability claim verified: of 52 external `AGENTS.md:NN` citations, none
  points at changed content; the five changed lines (≤322) are uncited.

---

## FINDINGS

| Severity | file:line | What is wrong | Evidence | Suggested fix |
|----------|-----------|---------------|----------|---------------|
| NIT | `src/paa/marketplace/registry_client.py:489-495` | In `_exchange`, a single response chunk larger than the body cap is not appended before the `break`, so `body` is `b""` and the refusal is classified `empty_body` instead of `body_too_large`. Both outcomes are safe `RegistryError` refusals — no body parsed, no remote text logged — so this is a reason misclassification only. | Code read; httpx default chunk 64 KiB makes a >1 MiB single chunk unlikely. | Append the chunk before the cap check (or track `truncated` independently of `body`) so an oversized single chunk reports `body_too_large`. Not required for wave closure. |

No BLOCKER, no MAJOR findings.

---

## CLAIMS CHECKED (with actual output)

| Claim | Source | Check | Actual output |
|-------|--------|-------|---------------|
| `ruff check src tests` = 0 | T-009/T-010/T-011/T-012 reports | Ran | `All checks passed!` (exit 0) |
| `mypy src` = 0 | T-009/T-010/T-011 reports | Ran | `Success: no issues found in 138 source files` (exit 0) |
| `pytest tests/conventions tests/marketplace` green | T-010/T-011 reports | Ran | exit 0 (all collected tests passed; 1 skip = pre-existing signing skip in marketplace) |
| `pytest tests/ --strict-markers --strict-config` green | Gate | Ran | `2907 passed, 235 skipped, 4 xfailed, 1 warning in 126.55s` (exit 0) |
| CI YAML parses | T-009 report | Ran `yaml.safe_load` | Parsed OK; jobs: gate; installs minimal/dev/all |
| Lock has teeth | T-010 report | Broke a ref in scratch copy | `FAILED ... citations past the end of their file: ... src/paa/cli/main.py has 468 lines` |
| Citations match code | T-010 report | Read 12 cited lines | All 12 agree (symbol/behaviour at cited line matches the sentence) |
| Zero callers of HttpRegistry outside marketplace | T-011 report | Grep `src/` `tests/` | Zero hits outside `registry_client.py`/`__init__.py` |
| Only marketplace/ changed in src | T-011 report | `git diff --stat -- src/` | 3 files, all under `src/paa/marketplace/` |
| Open items visible as open | T-012 report | Read ROADMAP 0.2, AGENTS.md:493, work ledger | Both items listed as open in all three places; neither patched |
| ADR index 0032–0035 | T-012 report | Read `docs/adr/README.md` | All four rows present, class Correctness; body/index-only distinction stated |
| AGENTS.md line stability | T-012 report | `git show HEAD:AGENTS.md` vs working | Cited lines unchanged; 5 changed lines (≤322) uncited |
| python-is-python3 rationale | T-009 report | Read `sandbox/base.py:588`, `reasoning.py:445` | PATH = `/usr/local/bin:/usr/bin:/bin`; default command `["python","-c","pass"]` |

---

## VERIFIED

- All four gate commands (ruff, mypy, conventions+marketplace, full strict pytest) — actual output above.
- T-009: YAML parse; gate command texts; `.[dev]` shape; python-is-python3 rationale and code basis; service containers + env-var names; retry convention; extras matrix; zero `src/` changes.
- T-010: lock passes; lock has teeth (demonstrated); exclusions justified with reasons; 12-citation sample matches code; README counts acknowledged as approximate (not a finding).
- T-011: every body-reading path raises `RegistryError`; no raw `AttributeError`/`TypeError`/`JSONDecodeError`/`HTTPStatusError` escape; server text bounded with concrete maxima; server text never reaches log (exact key set pinned, excerpt escaped/quoted/redacted); zero callers of old exception types; only `marketplace/` changed in `src/`; tests comprehensive (33 new, reversal + mutations measured).
- T-012: ADR index 0032–0035 correct; coverage claims honest with named gaps; open items visible as open in ROADMAP/AGENTS.md/work ledger; AGENTS.md line-stability verified; MCP_AND_PLUGINS reference fixes verified against code.

## UNVERIFIED

- GitHub Actions cannot run here: action SHAs (`actions/checkout@3d3c42e…`, `actions/setup-python@6d4fd22b…`), image digest pins, Docker health-check `curl` assumptions, live S3/PostgreSQL suites against service containers, end-to-end `ubuntu-latest` run. Marked UNVERIFIED in T-009's report; env-var names, command texts, and gate shape are statically verified.
- Minimal-venv build (`pip install -e "."` with no pip/ensurepip on the host) — static import scan used as proxy.

## NOT A FINDING

- README's three approximate/stale test counts: documented as such in AGENTS.md; outside T-010's write-set; deliberately not reconciled.
- T-011 NIT (oversized single chunk → `empty_body` instead of `body_too_large`): both outcomes are safe refusals; no code change required.
- `LocalDirectoryRegistry.fetch` bare `FileNotFoundError` (pre-existing, out of scope for T-011).
- `mypy tests/marketplace/test_marketplace.py` pre-existing type errors (out of scope; gate is `mypy src`).
- Wave-2 artefacts (CI workflow, docs checker) were in flight when T-012 was written; T-012 verified them against the working tree and reported them UNVERIFIED-against-final; both are now committed and green.

---

## Report location

`docs/agent-ops/20261022-w6-w7-w9/reports/w3-review/W3-REV-01.md`

Commit: docs-only, explicit path, on branch `ops/20261022-w6-w7-w9`.
