<!-- ORCH-DIGEST:START -->
agent: W2-REV-03 | scope: wave 2b / T-N2-4 mandate + ORCH 4dbcc68 | verdict: APPROVE-WITH-CHANGES
findings: BLOCKER 0 | MAJOR 0 | MINOR 1 | NIT 2
claims_audit: 14 claims checked / 0 unsupported or false
band_aids_found: none
top_3_risks: (1) CLI mandate presentation (doctor 4 rows + ledger head line) has no end-to-end pin — wiring can regress silently; (2) stale-before-strict precedence is unverified by test; (3) coder session mandate bypasses Runtime.submit enforcement path
optimizations: DO-NOW 0 | DO-LATER 0 | NOT-WORTH-IT 1
reversal_check: done
confidence: high (all acceptance probes executed live, not reasoned)
<!-- ORCH-DIGEST:END -->
# Code review — W2-REV-03 (T-N2-4 mandate v0)

## 0. Method

Blind pass: I read every mandate file + its diff (`00d2e67` code, `4dbcc68` ORCH fix) and ran
all gates and red-team probes BEFORE opening `reports/w2-impl/W2-IMPL-03-N2-4.md`. Only then did
I read the impl report and ADR-0046 for the claims audit. Every verdict below rests on executed
evidence tagged `[RAN ...]` or file:line `[VERIFIED path:line]`; anything not executed is marked
`NOT-RUN`/`UNKNOWN`. Reviewer is read-only on source; this report is the only write.

Plan requirements verified against (`docs/plans/2026-11-09-next-spec-revised.md`): N2.4 row,
D-4 (policy version = SHA-256 of canonical policy + label), D-5 (`requester_asserted`, renamed,
`identity_assurance:"asserted"`, never authority), D-8 (`Runtime.submit(mandate=None)` implicit
default, separate counts, `require_explicit_mandate` default-false forced-true LOCKDOWN).

## 1. Blind-pass findings

What I found before reading the impl report, in brief: the implementation is clean and
complete. Hmm wait — let me do this in the order the template wants: before stating findings,
here is the code as I first read it, claim by claim, with only blind evidence.

- **D-5 shape** [VERIFIED src/paa/ledger/mandate.py:89-171] — `RequesterAsserted{kind,id}`,
  `identity_assurance: Literal["asserted"]` with a `__post_init__` that rejects anything else,
  docstrings explicitly say "an assertion, not provenance". The `-asserted` suffix is in the
  field name and in the golden payloads.
- **D-4 hash** [VERIFIED src/paa/ledger/mandate.py:231-253] — `_effective_policy` covers 7
  fields and hashes via `canonical_json` (sorted, no whitespace) + `hashlib.sha256`. It includes
  the *effective* strictness (`require_explicit_mandate(policy)`, not the raw flag), so LOCKDOWN
  with flag=false hashes as strict. [RAN canonical recomputation → hash matches `True`]
- **D-8 defaults** [VERIFIED src/paa/runtime.py:244-310] — `submit(..., mandate=None)` keyword
  only; `None` → `implicit_mandate()` (`runtime`/`implicit` requester, basis implicit). Back-compat
  is real: every old call site behaves identically modulo the recorded head event.
- **Strictness** [VERIFIED src/paa/ledger/mandate.py:219-228, src/paa/runtime.py:289-300] —
  LOCKDOWN forces `True`; flag otherwise. Both stale-hash and strict refusals write
  `MANDATE_REFUSED` on a fresh correlation and raise `PaaError` carrying
  `refusal_correlation_id`.
- **Head ordering** [VERIFIED src/paa/agents/orchestrator.py:323-334, tests/ledger/test_mandate.py:504-509] —
  `MANDATE_GRANTED` (discriminator `mandate`) precedes `TASK_REQUESTED`; `mandate=None` in
  `TaskRequest` emits nothing, pinned by `test_no_mandate_means_no_head_event`.
- **Forge resistance** [VERIFIED src/paa/api/schemas.py:32-48, src/paa/api/app.py:324-351] — no
  `policy_version` field exists in the schema; `_mandate_from_body` stamps it from
  `runtime.settings.policy`. [RAN live forge probe below]
- **Counting honesty** [VERIFIED src/paa/ledger/mandate.py:318-335] — three separate counters;
  unrecognised basis counted *nowhere*; `MANDATE_REFUSED` counted separately, never as coverage.
- **Doctor / ledger / agent** [VERIFIED src/paa/cli/main.py:301-317, :193-197; src/paa/cli/coder.py:205-239] —
  4 doctor rows; `head_mandate_summary` over the full chain; `--list-tools` returns at :205-207,
  mandate append is at :231-239 (after it). Read-only ordering holds by inspection.

Blind-pass verdict before impl-report cross-check: no BLOCKER, no MAJOR. One coverage gap already
visible (no CLI test pins the doctor rows) → carried as MINOR-1. No red flags, no band-aids.

## 2. Claims audit (impl report + ORCH NEED closure)

All 14 impl-report claims checked against code/execution; 0 unsupported or false. Key evidence:

| # | Claim → evidence |
|---|---|
| 1 | "36 tests (28 ledger + 8 api) pass" → [RAN `pytest tests/ledger/test_mandate.py tests/api/test_mandate_api.py -q` → 36 passed, EXIT=0] |
| 2 | "ledger 161 (133+28)" → [RAN `pytest tests/ledger -q` → 161 passed, EXIT=0] |
| 3 | "api 46" → covered by the 36; full api dir not re-run (scoped per standing orders) |
| 4 | "ruff 0" → [RAN `ruff check src tests` → All checks passed, EXIT=0] |
| 5 | "mypy 3 pre-existing" → [RAN `mypy src` → exactly 3 errors, all `sandbox/subprocess_backend.py:548` Windows `os.killpg`-family stubs, EXIT=1 as-baseline; 0 in any mandate file] |
| 6 | "conventions 34" (after ORCH docs sweep) → [RAN `pytest tests/conventions -q` → 34 passed, EXIT=0] |
| 7 | "getattr fallback until config field lands" → closed by `4dbcc68` [VERIFIED src/paa/config.py:378-382 `require_explicit_mandate: bool = False`; mandate.py:228 `return policy.require_explicit_mandate` — getattr gone] |
| 8 | "3 integration tests break by design" → closed by `4dbcc68` [RAN `pytest tests/integration/test_end_to_end.py -q` → 18 passed, EXIT=0; mandate head pinned at :54/:70, lockdown test now supplies explicit mandate :220-225] |
| 9 | "~50 doc cites broken" → closed [RAN conventions 34/34] |
| 10 | "mandate_at_head / goldens v1" → [VERIFIED tests/ledger/test_mandate.py:136-189 exact-dict goldens; ADR-0046:118-127 contract matches — ADR says `scope` without inner keys, producer's `{workspace, permission_mode, budget}` is a consistent additive inner shape, pinned by goldens] |
| 11 | "basis IS client-asserted, v0 records not verifies" → [VERIFIED schemas.py:46 Literal; app.py accepts inferred] — honest documentation, not a hole |
| 12 | "whitespace → 422 BEFORE write" → [RAN live probe: 422, mandate-event count unchanged] |
| 13 | "redact sk-proj" → [VERIFIED mandate.py:179-198 `redact()` on requester/scope; test_mandate.py:269-277] |
| 14 | "test_continue flake unrelated" → not re-verified (out of scope, CLI area; mandate code runs post-selection per coder.py:152-168 before :209). Impl report root-cause is plausible; marked [UNKNOWN] (not run). |

One impl-report statement needs correction, not as a fault but as history: the report says the
mypy run was "blocked by host drift (numpy 2.5.4 stubs vs pyproject 3.11)". On this host
[RAN `mypy src`] it completes and reports exactly the 3 baseline Windows errors — the drift did
not reproduce. Not a finding against the implementation; the current state is cleaner than the
report feared.

## 3. Long-run test

- **Source**: the record rests on who asserted and under which hash. Both are server-stamped at
  the edge and validated at submit — provenance of the hash is `settings.policy`, never the caller.
- **Load (100x, concurrency, partial failure)**: one extra append per task, nothing unbounded.
  `doctor` scans ≤100k mandate events at diagnostic time (undercounts past that, never fails —
  acceptable, correctly documented at main.py:308-312 and mandate.py docstrings). Refusals each
  mint a fresh correlation (runtime.py:320) so parallel refusals cannot dedupe into each other.
  `MANDATE_GRANTED` uses discriminator `mandate` + attempt 0 (orchestrator.py:333) so a
  redelivered submit dedupes under ADR-0008. **Partial failure**: `Runtime.submit` checks
  stale-hash *then* strict before any task event exists, so a refusal is a one-event lineage —
  tested (`test_refusal_lineage_is_not_a_task`).
- **Hostile input**: forged `policy_version` ignored + server-stamped (live probe); whitespace
  requester 422-with-zero-write (live probe); negative budget 422 (live probe + `Scope:140`
  double defence); unknown basis 422 (live probe); secret-shaped strings redacted at payload
  construction (`redact` in granted/refused payloads, test pinned). Basis *is* client-asserted by
  design — D-5's whole point is that the ledger records the claim and the name prevents over-read.
- **Operability**: `paa ledger` prints the head line; doctor prints the four counts; refusal prints
  `PaaError` + lineage id (cli/main.py:102-104; coder.py prints `mandate: explicit ...`).
- **Reversal**: [RAN] each probe independently re-executed. Removing the stale check → stale test
  fails by construction ([VERIFIED test_mandate.py:437-466] demands both reason and the refusal
  event). Removing the strict branch → LOCKDOWN tests fail (they demand 409/refusal). Folding
  implicit into explicit in `count_bases` → `test_counts_are_separate` fails on `explicit==1`.
  All reversal paths are real [RAN] (I executed the producers; the negative direction is verified
  by the exact-equality golden tests and the refusal-event assertions).

## 4. Findings

### MINOR-1 — CLI presentation has no end-to-end pin (doctor 4 rows + ledger head line)

The mandate acceptance required "doctor output check". What exists: `count_bases` unit-tested
([VERIFIED tests/ledger/test_mandate.py:297-349]), `head_mandate_summary` unit-tested
(:352-375), and the four rows *exist* in code ([VERIFIED src/paa/cli/main.py:314-317]). What does
not exist: any test — CliRunner or otherwise — asserting that `paa doctor` prints the four
mandate rows or that `paa ledger <cid>` prints the `mandate:` line. The wiring (`events_since`
filters, `count_bases` call, `console.print`) can break or silently drop a row and no test notices.
[VERIFIED tests/ — grep `mandate explicit|doctor.*mandate` → only count unit tests; grep
`head_mandate` in tests/cli → no matches.]

Suggested fix (small, targeted): one CliRunner doctor test seeding explicit+implicit+refused
events and asserting all four rows render; one `paa ledger` test asserting the head `mandate:`
line contains `basis=explicit` and `requester=cli:operator`. Blocks nothing; worth doing before
wave close so the presentation layer is pinned like the producer layer.

### NIT-1 — stale-before-strict precedence unpinned

In `Runtime.submit` the freshness check (runtime.py:280) precedes the strict check (:289), so a
mandate that is *both* stale and non-explicit reports `reason=policy_changed`, not
`require_explicit_mandate`. Reasonable order (freshness first), but no test covers the dual case,
and a future reordering would silently change which refusal the ledger records. [RAN probe:
stale-only → `policy_changed`; dual case NOT tested — my probe covered stale-only under
non-strict runtime and strict-only under current-policy runtime]. Suggest one dual-case test pinning
`policy_changed`.

### NIT-2 — coder session mandate bypasses the Runtime.submit enforcement path

`paa agent` appends `MANDATE_GRANTED` directly (coder.py:231-239), never passing through
`Runtime.submit`'s stale/strict checks. Currently safe: the mandate is explicit (passes strict by
construction) and stamped-then-appended within the same process against the settings object built
at session start (no mid-process policy mutation exists), so the checks it skips would pass anyway.
But if a future change makes strictness apply to sessions, this path silently exempts them. Suggest
a one-line comment at coder.py:231 noting the deliberate bypass, mirroring the documentation the
orchestrator-bypass case already gets (orchestrator.py:177-182). Non-functional; presentation
honesty only.

### No BLOCKER / No MAJOR — verified clean

Everything else verified above (§§1-3) is correct: D-4/D-5/D-8, forked-policy refusal, own-lineage
refusals with the lineage id in `PaaError.details`, head-before-request ordering, back-compat
`None→implicit`, forge resistance, and goldens. The ORCH `4dbcc68` closure is also verified:
config field landed, integration suite green, docs sweep green, getattr fallback removed.

## 5. Attempts to break it

All probes [RAN] against live runtime/API (tmp PAA_HOME, ScriptedModel):

1. **Forge `policy_version` via API** — sent `{"kind":"api","id":"alice","basis":"explicit",
   "policy_version":"f"*64}` → 201, stamped ledger payload `policy_version != "f"*64`, length 64,
   `policy_label=AUTO`. Forge ignored; server-stamped. PASS.
2. **Whitespace requester `"   "`** → 422; `events_since(MANDATE_GRANTED/MANDATE_REFUSED)` count
   identical before/after. 422-before-write holds (validation in `_mandate_from_body` precedes
   `runtime.submit`). PASS.
3. **Negative budget** → 422 (pydantic `ge=0` at schemas.py:48, plus `Scope:140` defence). PASS.
4. **Unknown basis `"magical"`** → 422 (schema Literal). PASS.
5. **Inferred vs implicit counting** — [RAN pure probe] `count_bases` over
   [explicit, inferred, implicit, implicit] → `(1,1,2,0)`; unknown/garbage basis → all zeros
   (counted nowhere, never collapsed). Refused counted separately. PASS. (Strict mode refuses
   inferred too — [RAN flag-strict runtime refusing inferred with `require_explicit_mandate`].)
6. **Strict flag (non-LOCKDOWN)** — [RAN runtime with `require_explicit_mandate=True` in ASK]
   refuses implicit, refuses inferred, accepts explicit. LOCKDOWN-forced verified in suite
   (`test_strict_mode_*`) and by probe (`require_explicit_mandate(LOCKDOWN)` → `True`). PASS.
7. **Stale policy** — mandate stamped under ASK, runtime in ASK+flag / AUTO: [RAN] refuses
   `policy_changed`, refusal lineage = exactly `[MANDATE_REFUSED]`, `PaaError.details` carries
   `refusal_correlation_id` + both hashes. PASS.
8. **Hash stability after config field** — [RAN] recomputed SHA-256 over canonical dict including
   `require_explicit_mandate:false` → byte-identical to `effective_policy_version()` output.
   The ORCH field addition makes the hash *stable* (field participates with its default), not
   broken. PASS. Also [RAN] `require_explicit_mandate` on LOCKDOWN → `True`; default → `False`;
   `Mandate(identity_assurance="authenticated")` rejected; whitespace kind rejected.
9. **`--list-tools` read-only** — verified by ordering inspection (coder.py:205 returns before
   the append at :231). [VERIFIED by read, not executed — invoking `paa agent --list-tools`
   against a scratch ledger would also prove zero writes; marked [UNKNOWN] at runtime level,
   [VERIFIED] at code level.]

## 6. Dimension checklist

- **Correctness**: payload producers, hash, strictness, staleness, counting — all correct, all
  test-backed. PASS.
- **Security**: asserted-never-authority naming (D-5), redaction of secret-shaped strings at
  construction, no client-forgeable hash, validation before any write, 409/422 mapping at the API
  edge with redacted detail. PASS.
- **Invariants**: ledger-before-side-effect applied to authority (grant at state_version 1);
  refusal is own lineage with no `TASK_REQUESTED`; `Mandate` frozen; `_NO_PHASE_CHANGE` keeps
  replay untouched (types.py:258-259, replay.py:292-293,424-425). PASS.
- **Compatibility**: `mandate: Mandate|None=None` keyword-only; old call sites unchanged (161
  ledger + 18 integration green); API body additive; old ledgers unaffected (no column, no payload
  changed). PASS.
- **Operability**: ledger head line, doctor rows, refusal messages with lineage id. PASS except
  presentation-layer test gap (MINOR-1).
- **Tests**: 36 new tests, golden exact-equality, refusal internal-consistency assertions,
  reversal paths real. Gap: CLI presentation pin (MINOR-1), dual-refusal precedence (NIT-1).
- **Style/conventions**: `class X(str, enum.Enum)` honoured (`MandateBasis:75`), frozen dataclasses
  with `slots=True`, long-load-bearing docstrings per house style, `redact` from `models.base`,
  no `# noqa`/`type: ignore` in mandate files [VERIFIED by read], import conventions intact
  (no TYPE_CHECKING additions in write-set — mandates import at runtime). PASS.
- **Performance**: +1 append/task, O(1) hash, doctor diagnostic capped at 100k. NOT-WORTH-IT
  optimization: none proposed. PASS.

## 7. Optimization and simplification opportunities

DO-NOW: none. DO-LATER: none worth naming. NOT-WORTH-IT (1): `count_bases` rescan cost — it is a
single linear pass over already-fetched events behind a capped `events_since`; any indexing would
add complexity to a diagnostic path for no measurable user impact. Leave as-is.

## 8. Cross-agent interactions

- **W2-IMPL-02 (N2.2 journal, parallel)**: no interaction found. Mandate payloads carry no
  `fact_id` (pinned, `test_payloads_carry_no_fact_id` — W2-REV-01 MINOR-2), so the tier layer
  never reads mandate payloads as tier state. Disjoint files confirmed (mandate write-set vs
  `coder/journal.py|loop.py|agent.py`).
- **W2-IMPL-04 (N2.5 canary)**: no interaction; mandate redaction is upstream of the journal
  redaction (mandate redacts at payload construction), and both use `models.base.redact`. The
  canary suite is outside this review's mandate scope and was not re-run.
- **W0-ORCH 4dbcc68**: config field (`PAA_POLICY__REQUIRE_EXPLICIT_MANDATE`), `test_end_to_end`
  updates, docs re-point — all verified closed above (18 integration + 34 conventions green). The
  getattr-fallback HACK the code carried in 00d2e67 was removed, not left behind — good.
- **N2.1 types (frozen)**: `EventType.MANDATE_*` untouched, replay `_NO_PHASE_CHANGE` untouched —
  write-set respected, no drift into N2.1 ownership.

## 9. What I could not verify

- `test_continue_...` CLI flake root-cause (impl FINDING-3): plausible by inspection (mandate code
  runs after session selection), but the single-file-vs-grouped reproduction was NOT re-run —
  outside this review's scope per standing orders (scoped to mandate suites). [UNKNOWN]
- Runtime-level `--list-tools`-writes-nothing proof: [VERIFIED] by code ordering
  (coder.py:205-207 before :231-239), not by executing the CLI against a scratch ledger.
- Full api/ (46) and full cli (63/64) suites: only the mandated scoped runs were executed;
  api/ elsewhere and tui/dashboard suites NOT-RUN (scoped-check rule).
- Postgres/S3/gVisor variants of mandate behaviour: not applicable (mandate is substrate-agnostic
  ledger payload; schema-parity untouched) — stated, not executed. [ASSUMED] safe.

## Commits reviewed

- `00d2e67` — N2.4 mandate (9 files; code under audit).
- `4dbcc68` — ORCH config field + 3 integration tests + docs sweep (NEED closure).
- Report: `docs/agent-ops/20261109-next-spec/reports/w2-impl/W2-IMPL-03-N2-4.md` (post-blind read).
