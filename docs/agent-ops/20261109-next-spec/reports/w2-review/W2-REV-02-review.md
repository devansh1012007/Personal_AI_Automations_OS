<!-- ORCH-DIGEST:START -->
agent: W2-REV-02 | scope: wave 2b / T-N2-2 (N2.2) | verdict: APPROVE-WITH-CHANGES
findings: BLOCKER 0 | MAJOR 0 | MINOR 4 | NIT 4
claims_audit: 18 claims checked / 2 unsupported or false
band_aids_found: none
top_3_risks: N2.3 sweep exemption still open (coder-only lineages resumable until N2.3); journal _hash_file reads unbounded prior before tool's stat gate; journal ValueError aborts whole run (fail-closed but not per-call)
optimizations: DO-NOW 0 | DO-LATER 3 | NOT-WORTH-IT 1
reversal_check: reasoned-only (read-only reviewer; impl's executed mutation corroborates + independent probes)
confidence: high (full scoped gates re-run: 69 + 34 + ruff 0 + mypy 3 pre-existing; red-team probes executed)
<!-- ORCH-DIGEST:END -->
# Code review — W2-REV-02 (T-N2-2 / N2.2)

## 0. Method

Blind pass first, then claims audit. Read, in order: the brief's verification
list, ADR-0046 (`docs/adr/0046-coder-event-forward-compatibility.md`), the
`9a4a519` diff (5 files, +1265/-44), and the current form of
`src/paa/coder/journal.py`, `src/paa/coder/loop.py`,
`src/paa/coder/agent.py`, `tests/coder/test_journal.py`,
`tests/ledger/test_coder_events.py`, `src/paa/coder/shell_tools.py`
(`preflight`), `src/paa/coder/fs_write_tools.py` (text model),
`src/paa/ledger/replay.py` (tier fold + arm), `src/paa/runtime.py`
(ledger presence), `tests/conftest.py` (ledger fixture), and
`tests/conventions/test_import_conventions.py`. `4dbcc68` was confirmed by
stat to touch only docs + `src/paa/config.py` + `src/paa/ledger/mandate.py` +
integration tests — no journal/loop/agent/`test_journal.py` files — so the
config/mandate half is out of this review's scope, as the brief directs.
W2-IMPL-02's report was opened only after the blind findings were settled.
W2-REV-01's review was then read to disposition MINOR-1..4. Seven independent
red-team probes were executed (opaque canary, colon/empty rejection,
COMPLETED-without-hash, escaped path, producer key set, hook-block path,
colon-call-id bubbling). No source was edited; the only write is this
report (plus scratch reads). Host is Windows (`.venv/Scripts/python`,
CPython 3.12.12).

## 1. Blind-pass findings

Per brief verification list (all verified):

- **Post-hook PROPOSED (preview shape summaries, `args_sha256` of post-hook
  args)** — [VERIFIED] `src/paa/coder/loop.py:234-255`: `_build_journal_ctx`
  is built from `ctx.mutable_arguments` (the PRE hook's dict) *before* the
  block branch, so even a blocked call's DENIED attests redacted args; the
  permission check, the tool, and `before()` all receive that same dict.
  [VERIFIED] `src/paa/coder/journal.py:151-220`: `args_preview_redacted`
  renders every string as `<str len=N sha256=xxxxxxxx>` and containers
  recursively; secret-named keys render `key=***`. [RAN opaque-canary probe
  → `bash(api_key=***, command=<str len=38 sha256=69b7f491>)`, leak False].
  [VERIFIED] `src/paa/coder/journal.py:141-148`: `args_sha256_of` hashes
  `canonical_json` (sorted keys, no whitespace) of the post-hook args.
- **Persisted intent = actual jailed file hashes** — [VERIFIED]
  `src/paa/coder/journal.py:223-303`: `compute_persisted_intent` joins
  `workspace.root / Path(raw)` then `assert_inside_workspace` (the write
  tools' own jail shape), hashes prior bytes via `_hash_file`, predicts
  `post` via `_predict_edit` (UTF-8/LF-normalised/BOM re-attached mirror of
  `fs_write_tools.py:283-295`), `apply_patch` yields only
  `compute_patch_sha256`. [RAN probe → write/edit hashes match
  `sha256(prior bytes)`/`sha256(predicted text)`; escape → `(None,None,None)`].
- **`session:call_id:kind` idempotency (duplicate returns existing, 4
  keys/call)** — [VERIFIED] `src/paa/coder/journal.py:109-117,413-423`:
  `discriminator_for` is `f"{session_id}:{call_id}:{kind}"`, `attempt=0`
  on all four emitters, so the store's dedupe-then-seal transaction (one
  transaction, returns existing on duplicate key) does the deduping — the
  journal adds no second cache. [RAN probe → four discriminators distinct;
  `test_duplicate_proposed_append_returns_existing` hits the real store and
  verifies the chain].
- **Denied never executes** — [VERIFIED] `src/paa/coder/loop.py:242-248`
  (hook block), `:258-263` (permission deny), `:266-272` (ASK decline):
  each returns `False` before `tool.run`. [RAN hook-block probe →
  ledger types `['TOOL_CALL_DENIED']` only, `tool.calls == []`,
  `denied_calls == 1`]. Suite pins deny + ASK-decline; hook-block path
  itself is unpinned (R2-MINOR-1) but the code provably holds.
- **Empty/colon id rejection** — [VERIFIED] `src/paa/coder/journal.py:120-138`
  `_validate_ids` raises `ValueError` before any append; the loop mints
  `uuid4().hex` for legacy `""` (`loop.py:331`) so the rejection only fires
  for deliberately-supplied ambiguous ids. [RAN probe → `('s:1','c')`,
  `('s','1:c')`, `('', 'c')`, `('s','')` all raised, nothing appended].
- **Isolation = actual (preflight/NONE, never requested)** — [VERIFIED]
  `src/paa/coder/loop.py:341-365` `_isolation_for`: `isolation_level`
  attribute (must be an `IsolationLevel` instance) → no-arg `preflight()`
  mapping's `"isolation_level"` member name (validated against
  `IsolationLevel.__members__`) → `IsolationLevel.NONE`. [VERIFIED]
  `src/paa/coder/shell_tools.py:314-349`: `BashTool.preflight()` takes
  keyword-only args *with defaults*, so `preflight()` with zero args
  succeeds and returns the backend's actual `isolation.name`. No loop-wide
  configured level is attested.
- **Journal default-on in agent build** — [VERIFIED]
  `src/paa/coder/agent.py:214-221,243`: `journal=None` constructs
  `LedgerToolJournal(runtime.ledger, correlation_id or new_correlation_id())`
  and passes it to `AgentLoop`. `Runtime.ledger` is `LedgerStore`
  (non-optional `[VERIFIED] src/paa/runtime.py:181`). No test asserts the
  wiring (R2-MINOR-2).
- **No producer drift (v1 fields exact)** — [VERIFIED] `journal.py:400-411,
  428-434, 463-471, 485-500` vs `tests/ledger/test_coder_events.py:54-98`
  goldens and ADR-0046:91-111: PROPOSED 10 keys, DENIED 5, COMPLETED 7,
  FAILED 8 — [RAN producer probe → key sets equal goldens on all four].
  Every payload carries `"v": 1`. `_assert_no_fact_id` is called on all
  four emitters (`journal.py:412,435,472,501`).
- **Loop behaviour when `journal=None`** — [VERIFIED] `9a4a519` diff of
  `loop.py`: every new `_handle_call` statement is guarded by
  `if self._journal is not None`; `_build_journal_ctx`/`_isolation_for`
  are only reached under that guard, so no file read or `preflight()` runs
  on the None path. The sole unguarded move is `arguments =
  ctx.mutable_arguments` from after to before the block check — no side
  effect (the block branch didn't use `arguments`). Source is not
  byte-identical; behaviour on the None path is equivalent (see claim 17).

Blind findings before reading the impl report: R2-MINOR-1 (hook-block
untested), R2-MINOR-2 (default-on unpinned), R2-MINOR-3 (journal exception
aborts run), R2-MINOR-4 (unbounded prior read), plus NITs on wording,
generic FAILED reason, broad `"key"` mask, and untested tripwire. The impl
report independently disclosed the unbounded read (S14), generic FAILED
reason (S12.4), hook-block construction coverage (S9), and the raise-
through-loop gap (S9); the default-on test gap and wording overstatement
were not disclosed.

## 2. Claims audit

18 checkable claims from W2-IMPL-02-N2-2.md; 16 verified, 2 false or
unsupported (details in §4). Summary:

| # | Claim | Verdict |
|---|---|---|
| 1 | Code commit `9a4a519`, 5 files | [VERIFIED] `git show --stat 9a4a519` |
| 2 | `journal.py` is the producer; protocol satisfied structurally | [VERIFIED] journal.py:369-510; [RAN `get_type_hints(ToolCtx)` → `IsolationLevel`; protocol methods all async] |
| 3 | Post-hook `args_sha256`, redaction order proven | [VERIFIED] loop.py:234-255; [RAN full pytest → green incl. `test_post_hook_order_proven_by_redaction`] |
| 4 | Persisted intent from actual jailed hashes; escape→nulls | [VERIFIED] journal.py:223-303; [RAN probe → escape/write/edit/non-file all match] |
| 5 | Duplicate PROPOSED → one row; 4 kinds → 4 keys | [VERIFIED] [RAN pytest → green incl. both idempotency tests + chain verify] |
| 6 | Empty/colon rejection incl. collision pair | [VERIFIED] [RAN probe → 4/4 raised, 0 appended] |
| 7 | `get_type_hints(ToolCtx)` resolves (MINOR-1) | [VERIFIED] [RAN probe → `hints['isolation_level'] is IsolationLevel`] |
| 8 | Ruff full `All checks passed!` | [VERIFIED] [RAN `.venv/Scripts/python -m ruff check src tests` → All checks passed, exit 0] |
| 9 | Scoped mypy clean (3.12, 3 files); full `mypy src` numpy-blocked | Partially stale: [RAN full `mypy src` → 3 errors, all pre-existing `subprocess_backend.py:548` (killpg/getpgid/SIGKILL), exit 1; **no numpy drift on this host** — the drift is host-dependent, not a blocker here] |
| 10 | `pytest test_journal + test_coder_events` → 68 passed | **Off by one**: [RAN → 69 tests, 0 failures (junit `tests=69`); collect: journal 34, coder_events 35] — count drift, not a passing-suite claim failure |
| 11 | `100` and `225` scoped combos pass | Plausible; [RAN journal+coder_events (69) + conventions (34) → all green]. Full 100/225 combos not re-run (read-only reviewer, scoped rule) — counted verified-by-subset + impl's disclosed runs |
| 12 | Loop/agent/integration suites pass with default journal on (115) | Plausible; [RAN journal+coder_events → green; agent/loop files untouched since by 4dbcc68] — consistent, not independently re-run as a 115-block |
| 13 | Conventions 33/34 at impl time (docs blank cites), NEED filed | [VERIFIED historically] `git show 9a4a519` shifts `loop.py`/`agent.py` line numbers; **now closed**: [RAN `pytest tests/conventions` → 34 passed (5+21+8) after ORCH `4dbcc68` re-point sweep] |
| 14 | MINOR-2 producer tripwire; structural half deferred (replay.py read-only) | [VERIFIED] journal.py:352-366 + 4 call sites; replay.py untouched in `9a4a519` |
| 15 | MINOR-4 fix is the collision-pair test | [VERIFIED] test_journal.py:241-250 |
| 16 | Preview shape-summary fix after N2.5 canary; `redact()` blanket removed | [VERIFIED] journal.py:190-220; [RAN opaque-canary probe → shape only] |
| 17 | "With `journal=None` behaviour is byte-identical to pre-N2.2" | **FALSE strictly** — source differs (guards + one moved line); **true behaviourally** on the None path (each new statement guarded). Wording overstates (R2-NIT-1). |
| 18 | `4dbcc68` / NIT-1 package half / SESSION events: deferrals disclosed | [VERIFIED] `9a4a519` stat vs `4dbcc68` stat (journal files only in former); `paa.coder.__init__` untouched; no STARTED/ENDED emission in journal/loop/agent |

## 3. Long-run test (independent answers)

- **Source**: ADR-0046 v1 contracts + the store's dedupe are the source.
  The journal writes exactly those keys (`v=1` everywhere, `attempt=0`);
  N2.3 will read `pre/post/patch` from PROPOSED to classify crashes.
  Roots are correct: post-hook attestation (ledger-before-side-effect),
  jail-then-hash intent, discriminator-ambiguity rejection. Any key or
  discriminator change breaks the exact-dict goldens in `test_journal.py`
  and `test_coder_events.py` — the intended tripwire.
- **Load (100x, concurrency, partial failure)**: 2 appends per tool call,
  each a single dedupe-then-seal transaction (store path unchanged). No
  caches, queues, or unbounded state. Cost is O(args) + one file read per
  file-tool call for the pre-hash. Partial failure: crash between PROPOSED
  and outcome leaves exactly the PROPOSED row N2.3 looks for; redelivery
  dedupes by key. The new failure mode is that journal/ledger errors now
  sit on the turn's critical path — an agent turn that previously
  succeeded without a journal can now raise on ledger I/O failure. That is
  the point of the task, not a regression, but it is a real operability
  change for `CoderAgent.build` callers (they journal always, with no
  opt-out flag; see §6/§8).
- **Hostile input**: red-teamed (§5). Opaque canary in a free-form arg →
  shape summary only (holds by construction, not by pattern). Colon /
  empty ids → pre-append `ValueError`, nothing appended. Escaped path →
  explicit nulls, never a hash of an outside file. Crypto hashes use
  `canonical_json` (sorted keys) so key order can't fork the digest.
  Residuals: unbounded prior read (R2-MINOR-4), journal exception aborts
  the run (R2-MINOR-3), tier fold's structural half still open (§8).
- **Operability (observe, roll back, migrate)**: observe via
  `read_correlation` (tool, actual isolation, intent, redacted preview
  carry enough to triage without raw secrets). Roll back by reverting
  `9a4a519` before the first journal write to a shared `PAA_HOME`; after
  that, the ADR-0046 one-way door applies (pre-N2.1 binaries raise
  `ValueError` in `_row_to_event`). No migration (TEXT column), no config
  flag — the journal is the path, not a feature toggle. Until N2.3,
  do **not** run `boot_sweep` against a home containing coder lineages
  (W2-REV-01 carry-forward, still true — §8).
- **Reversal (which test fails if the fix is reverted?)**: reasoned-only
  (read-only reviewer; no source mutation was executed, impl's disclosed
  probe corroborates). Reverting `_validate_ids` fails
  `TestDiscriminatorValidation` (collision pair + empty parametrisation);
  building ctx from `call.arguments` instead of post-hook args fails
  `test_post_hook_order_proven_by_redaction` (hash equality + secret-absent
  asserts); removing the COMPLETED result-hash requirement fails
  `test_completed_without_result_sha_raises_and_appends_nothing`; reverting
  the runtime import fails `test_get_type_hints_resolves_tool_ctx` with the
  original `NameError`; reverting the default journal in `agent.py` fails
  **no** test (R2-MINOR-2).

## 4. Findings

**W2-REV-01 MINOR dispositions (carried):** MINOR-1 runtime import — CLOSED
[VERIFIED] `loop.py:44` runtime import, TYPE_CHECKING block gone, E entry
deleted, `get_type_hints` test present. MINOR-2 tier fold — producer half
CLOSED (tripwire on all 4 emitters); structural half (exclude coder types
in `apply_tier_event`) DEFERRED to fold owner, correctly (replay.py is
read-only for N2.2; a producer-only change is the option W2-REV-01 allowed).
MINOR-3 wording ("no projection field moves", `replay.py:295-297`) — STILL
OPEN, correctly deferred: replay.py was read-only for N2.2, so N2.2 made no
wording change there; N2.2 did keep coder events on coder correlations by
constructing a fresh correlation when none is supplied. MINOR-4
discriminator — CLOSED [VERIFIED] `_validate_ids` + collision-pair test.
NIT-1 package half (`paa.coder.__init__` exports) — still open, disclosed.
NIT-2 stale agent message — CLOSED (guard deleted). NIT-3 vacuous
nullable test — still true in `test_coder_events.py:192-197` (pins
literals); N2.2's producer-side exact-dict tests are the real pin.

**R2-MINOR-1 — Hook-block journal path has no test pin** —
`src/paa/coder/loop.py:242-248`, `tests/coder/test_journal.py` (absent).
The code is correct (DENIED alone, no PROPOSED, tool never runs —
[VERIFIED] by independent probe), but no suite test covers a PRE-hook
block through a journalled loop. A future refactor that moved `before()`
above the block branch (attesting PROPOSED for a call that never executed)
would pass the suite. The impl discloses this as "covered by construction"
(S9). Add a loop-level test: blocking hook + journalled loop → ledger is
exactly `[TOOL_CALL_DENIED]`, tool uncalled. Not MAJOR: the branch is
co-located with the pinned deny/ASK branches and the ordering is enforced
by code position, not by convention alone.

**R2-MINOR-2 — Agent default-on journal wiring is unpinned** —
`src/paa/coder/agent.py:214-221`, `tests/coder/test_agent.py` (no journal
assertion). Code constructs `LedgerToolJournal` when `journal=None`
[VERIFIED], and the 115-test legacy suite passes with it on, but reverting
the default back to `journal=None` (silent unjournalled execution) fails
**no** test. Pin it: build an agent with a scripted model + a tool call,
assert the run's ledger contains `TOOL_CALL_PROPOSED` on the supplied
`correlation_id` (or at least that `agent.loop._journal` is a
`LedgerToolJournal`). Not MAJOR: production code is correct; this is the
highest-value missing pin in the delivery.

**R2-MINOR-3 — Journal refusal aborts the whole run, not just the call** —
`src/paa/coder/loop.py:254-255,285-287`. `before()` (and `denied()` /
`after()` on bad ids) raises `ValueError`, which `_handle_call` does not
catch, so it bubbles through `run()` and aborts the turn with a traceback.
Fail-closed holds (tool never executes — [RAN probe → `tool.calls == []`,
0 rows]), but the blast radius is the whole session turn. Production
reachability is low: the real adapter never sets `call_id`
(`model_adapter.py:242` always mints `""` → loop mints uuid hex), and the
default `session_id` is uuid-hex, so the trigger is a programmer-supplied
`session_id`/`call_id` containing `:` — a caller bug, arguably correctly a
crash. Still, the more robust shape is to catch the journal's `ValueError`
at the `_handle_call` boundary and return `False` with an
`error: journal refused …` tool result (fail-closed, per-call), matching
how unknown tools are handled (`loop.py:222-224`). If the crash is
deliberate, pin it with a loop-level test asserting the raise-through-loop
(impl S9 notes this test does not exist). Not MAJOR because the
fail-closed property — the security property — holds either way.

**R2-MINOR-4 — `_hash_file` reads an unbounded prior before the tool's size
gate** — `src/paa/coder/journal.py:306-313`. `path.read_bytes()` loads the
whole prior file; the tools stat-check first (`fs_write_tools.py:405-415`,
256 KiB cap). A hostile multi-GB prior in the workspace is read whole by
intent hashing before the tool can refuse it. Disclosed (S14), but worth
recording as the one real resource-exhaustion exposure in the delivery.
Fix (DO-LATER, N2.3-adjacent): stat-size pre-check mirroring
`MAX_CONTENT_BYTES` (return `None` intent over the limit — still never a
gate, still never blocks the tool's own refusal). Same shape applies to
`_predict_edit`'s whole-file reads. Not MAJOR: requires a hostile file
already in the workspace of a local-first agent; the sweep still classifies
safely (`unknown`, never auto-repeat).

**R2-NIT-1 — "Byte-identical" overstates the None-path guarantee** —
`src/paa/coder/loop.py` (impl §5 / brief). Source changed (guards + moved
`arguments` line + imports); behaviour on `journal=None` is equivalent.
Reword to "behaviourally identical on the None path" in follow-up docs.
No code change needed.

**R2-NIT-2 — FAILED reason is generic (`"tool failed"`)** —
`src/paa/coder/journal.py:496`. The frozen `after(ctx, ok, result_sha)`
signature carries no reason, so exception text is lost for triage; N2.3's
own sweep writes named INTERRUPTION reasons (different emitter). Recorded
correctly in code; a protocol change would be needed to fix it. Accept as
frozen-contract consequence.

**R2-NIT-3 — `"key"` substring over-masks** — `src/paa/coder/journal.py:106`.
Any arg key containing `key` (e.g. `monkey`, `keyboard`, `keynote`) renders
`***`. Security-safe (over-redaction) but noisy. Consider word-boundary or
tokenise matching (split on `_`/`-`/case) in a later pass. No change
required now.

**R2-NIT-4 — `_assert_no_fact_id` tripwire is untested** —
`src/paa/coder/journal.py:352-366`. Correctly fails loudly if a future
payload edit adds `fact_id`, but no test exercises it (the journal API
cannot receive a `fact_id`, so the only exerciser is a direct
`_assert_no_fact_id` unit test). Add a 3-line unit test so a future
removal of the tripwire goes red.

No BLOCKER, no MAJOR. No band-aids (no swallowed errors — intent
nulls are by contract, not catch-all; no `any` casts, no `type: ignore`,
no `# noqa`; the pre-existing `except Exception` on tool execution is
preserved, now with a FAILED row; no sleeps/retries/flag-disabled paths
[VERIFIED] by full diff read).

## 5. Attempts to break it

All run by this reviewer, on current tree:

- **Opaque canary in free-form arg** [RAN `python -c args_preview_redacted`
  → `bash(api_key=***, command=<str len=38 sha256=69b7f491>)`, canary
  absent, shape present]. Holds by construction (§1), including the
  N2.5 opaque form with no secret pattern and no secret-named key.
- **Colon collision pair** [RAN dummy-store probe → `('s:1','c')`,
  `('s','1:c')`, `('','c')`, `('s','')` all `ValueError`, 0 appended].
  The pair that W2-REV-01 named cannot collide because neither reaches the
  store.
- **Hook-block DENIED without PROPOSED** [RAN journalled-loop probe with
  blocking PRE hook → ledger exactly `[TOOL_CALL_DENIED]`, tool uncalled,
  `denied_calls == 1`]. Correct; unpinned by suite (R2-MINOR-1).
- **COMPLETED without hash raises** [RAN probe → `ValueError`, nothing
  appended; FAILED with a hash forced to `None`]. Holds at journal level.
- **Escaped path nulls** [RAN probe → `('../outside.txt') → (None,None,None)`;
  write/edit on real files hash actual bytes]. Holds.
- **Producer key-set audit** [RAN real-store probe → PROPOSED/DENIED/
  COMPLETED/FAILED key sets equal ADR-0046 goldens; all `v=1`; no
  `fact_id`]. No drift (§1).
- **Colon `call_id` through the loop** [RAN journalled-loop probe with
  `call_id='bad:id'` → `ValueError` bubbled out of `run()`, 0 rows, tool
  uncalled]. Fail-closed holds; run aborts (R2-MINOR-3).
- **Gates** [RAN `.venv/Scripts/python -m pytest
  tests/coder/test_journal.py tests/ledger/test_coder_events.py -q →
  69 passed, exit 0 (junit `tests=69 failures=0`); pytest tests/conventions
  -q → 34 passed, exit 0; ruff check src tests → All checks passed, exit
  0; mypy src → 3 errors, all pre-existing
  `subprocess_backend.py:548`, exit 1]. No DeprationWarning failure
  observed in the scoped runs.

Things that held: post-hook attestation under redaction (suite + probe);
duplicate-PROPOSED dedupe against the real store with chain verify;
4-kind/4-key distinctness; `preflight()`-actual isolation (declared attr,
shell `preflight()` shape, NONE fallback — suite + code read); default-
on wiring exists and is unconditional (`is not None` checks, no falsy
traps — `agent.py:214-220`).

## 6. Dimension checklist

- Functional correctness: PASS (post-hook order, intent-from-disk,
  idempotency, denied-never-executes, isolation-actual, v1 exactness all
  re-verified with probes + suite).
- Security/privacy: PASS for N2.2 scope (preview cannot disclose content
  by construction; journal refusal is fail-closed; jail-then-hash;
  `fact_id` tripwire on the producer). R2-MINOR-4 is a resource, not a
  disclosure, exposure.
- Error handling: PASS with R2-MINOR-3 (journal errors propagate as
  turn-aborting exceptions rather than per-call denials — fail-closed but
  coarse). No swallowed errors anywhere else.
- Idempotency: PASS (store dedupe, `attempt=0`, 4 kinds; collision class
  closed by rejection).
- Replay purity/totality: PASS (no replay.py change in `9a4a519`;
  `_NO_PHASE_CHANGE` still holds; tier tripwire only guards the producer).
- Observability: PASS (rows carry tool/isolation/intent/redaction shape;
  FAILED reason genericity is the one blind spot — R2-NIT-2).
- Docs/ADR accuracy: PASS except wording (R2-NIT-1) and the pre-existing
  MINOR-3 comment still in replay.py (out of N2.2's scope).
- Test quality: PASS on the pinned paths (33–34 producer tests hit a real
  store + real loop + real files; mutation evidence disclosed), with
  R2-MINOR-1/2 as the gaps. The golden literals in `test_coder_events.py`
  remain literal pins; N2.2's exact-dict producer tests are the bridge to
  real output (impl S7, agreed).
- Performance: PASS (2 ledger txns/call; O(args) preview/intent work;
  one prior-file read per file call). `preflight()` per sandbox call is a
  dict build — negligible.
- Compatibility/migration: PASS (additive EventTypes, TEXT column, no
  migration; one-way door documented; callback-catch on journal errors is
  the one behavioural tightening that future callers should know).

## 7. Optimization and simplification opportunities

- **DO-NOW (0)**: none required before acceptance. The four MINORs are
  test pins and a disclosed hardening, not correctness defects.
- **DO-LATER (3)**: (a) R2-MINOR-1 + R2-MINOR-2 tests (hook-block pin +
  default-on pin — both ~20 lines in `test_journal.py`/`test_agent.py`);
  (b) R2-MINOR-4 stat-gate in `_hash_file`/`_predict_edit` (mirror
  `MAX_CONTENT_BYTES`, return `None` over-limit); (c) R2-MINOR-3 decision:
  either catch journal `ValueError` per-call in `_handle_call` or pin the
  raise-through-loop with a test + docstring, so the failure mode is a
  documented contract rather than an accident of missing `try`.
- **NOT-WORTH-IT (1)**: removing the `_assert_no_fact_id` tripwire as
  "dead code" — it is 10 lines, zero cost when idle, and the exact kind
  of cheap guard that prevents a silent tier-fold fabrication on a future
  payload edit. Keep (and add its unit test, R2-NIT-4).

## 8. Cross-agent interactions

- **N2.3 (recovery sweep) owns the sweep exemption** — still open and
  still blocking `boot_sweep` over coder homes (W2-REV-01 §9 carry-
  forward). N2.2's DENIED-alone (hook-block) semantics must be honoured
  there: DENIED without PROPOSED is proof of non-execution, not an
  interrupted call to classify (impl S14, agreed — N2.3 must special-case
  it). N2.2's `post` fidelity degrades to `unknown` for CRLF priors /
  write normalisation — safe for N2.3 (never auto-repeat) but a known
  `applied`-rate cost; only a tool-reported intent closes it fully.
- **N2.4 (mandate) owns SESSION_STARTED/ENDED emission** — not emitted by
  the journal or `ask()`; see impl S12.5 (deferred, flagged for the wave
  reviewer; agreed as a clean deferral — the protocol has no method for
  them and BOLTING them onto `ask()` would have been out-of-brief).
- **W2-IMPL-04 (N2.5 canary)** — its opaque-canary NEED is closed by this
  delivery: shape-summary preview [VERIFIED] + suite's opaque test
  [green]. W2-IMPL-04's sibling suite (`test_secret_hygiene.py`) corroborates
  (impl S11; reviewer did not re-run it — scoped check only).
- **ORCH `4dbcc68`** — closes the conventions docs-blank NEED from N2.2
  (34/34 now green [RAN]). The `require_explicit_mandate` config + mandate
  head tests in that commit are outside this review. The two NITs the
  impl flagged as ORCH-owned (docs re-point) are done.
- **MINOR-3 wording + tier-fold structural half** remain with their owners
  (replay.py). N2.2 correctly did not touch either under a read-only
  mandate; no action required from N2.2.
- No NEED/CONFLICT from this review. COMMS untouched by this reviewer
  (read-only role; the orchestrator owns the append on my behalf, per the
  wave protocol — [ASSUMED] it will be recorded from this report).

## 9. What I could not verify

- **Full suite** — not run (scoped rule). Scoped evidence: journal +
  coder_events 69, conventions 34, ruff full, mypy full — all green at the
  baseline (mypy 3 pre-existing).
- **Linux/CI** — reviewer host is Windows; the documented `python-is-
  python3` host dependency for the sandbox tests was not exercised here
  (no sandbox `COMMITTED` assertions run in this review).
- **Mutation re-run** — not executed (read-only). Reversal rests on the
  impl's disclosed probe (validation-disabled → 2 failures for the right
  reason) plus code-path reasoning corroborated by independent probes.
- **PostgreSQL/S3 live paths** — CI-only, not run here (SQLite store path
  exercised; TEXT-column assumptions shared with W2-REV-01's
  `[ASSUMED]`).
- **CRLF `post` fidelity end-to-end (journal → tool → N2.3 `unknown`)** —
  verified at the prediction level (mirror of `fs_write_tools` text model);
  the sweep side does not exist yet, so the classification claim remains
  [ASSUMED] until N2.3.
- **`101`-scale concurrency (duplicate delivery races on the same
  discriminator)** — deficit covered structurally by the store's
  dedupe-then-seal transaction (single-transaction path shared with all
  events) and by the existing store dedupe test; no concurrent-probe run
  was performed here.
