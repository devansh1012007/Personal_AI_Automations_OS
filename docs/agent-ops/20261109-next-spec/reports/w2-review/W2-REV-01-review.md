<!-- ORCH-DIGEST:START -->
agent: W2-REV-01 | scope: wave 2a / T-N2-1 (N2.1) | verdict: APPROVE-WITH-CHANGES
findings: BLOCKER 0 | MAJOR 0 | MINOR 4 | NIT 3
claims_audit: 22 claims checked / 5 unsupported or false
band_aids_found: none
top_3_risks: boot-sweep requeue of coder-only lineages until N2.3 (documented); tier-fold fact_id leakage with no structural barrier; discriminator collision on colon/empty call ids once N2.2 wires dedupe
optimizations: DO-NOW 1 | DO-LATER 2 | NOT-WORTH-IT 1
reversal_check: reasoned-only (read-only reviewer; impl's executed mutation corroborates)
confidence: high (full scoped gates re-run + red-team scripts; Windows host, scoped suite only)
<!-- ORCH-DIGEST:END -->
# Code review — W2-REV-01 (T-N2-1 / N2.1)

## 0. Method

Blind pass first, then claims audit. Read, in order: the plan N2.1 row + §C/§G
(`docs/plans/2026-11-09-next-spec-revised.md:76,26-36`), ADR-0046
(`docs/adr/0046-coder-event-forward-compatibility.md`), the 51a8c61 diff
(6 files, +901/-1), and the current form of `src/paa/core/types.py`,
`src/paa/ledger/replay.py`, `src/paa/coder/loop.py`,
`tests/ledger/test_coder_events.py`, `tests/ledger/test_replay.py`,
`src/paa/ledger/events.py` (`compute_idempotency_key`),
`src/paa/ledger/store.py` (`_row_to_event`, head projection),
`src/paa/ledger/recovery.py` (sweep), `src/paa/coder/agent.py` (journal slot).
W2-IMPL-01's report was opened only after the blind findings were settled.
Two red-team scripts were run (interleaved/hostile replay ≈ 80 combos;
protocol surface probes). No source was edited; the only writes are this
report and scratch probes under `%TEMP%\opencode\w2rev01\`.

## 1. Blind-pass findings

Per plan verification list (all verified):

- **All 8 in `_NO_PHASE_CHANGE`, not `_PHASE_MAP`, not `TERMINAL_EVENTS`**
  — [VERIFIED] `src/paa/core/types.py:252-259` (8 members),
  `src/paa/ledger/replay.py:407-427` (`_NO_PHASE_CHANGE` 9→17),
  `src/paa/ledger/replay.py:150-168` (`_PHASE_MAP` untouched at 17),
  `src/paa/core/types.py:273-282` (`TERMINAL_EVENTS` untouched at 6).
  Independent partition check [RAN redteam2 → total EventType 34,
  `_PHASE_MAP` 17, `_NO_PHASE_CHANGE` 17, overlap ∅, uncovered ∅].
- **Every payload carries v=1** — [VERIFIED] golden dicts
  `tests/ledger/test_coder_events.py:54-125` all carry `"v": 1`, and each
  matches ADR-0046's field table field-for-field (including nullable intent
  in PROPOSED and `result_sha256: null` in FAILED). Caveat: this pins
  test-local literals, not producer output — there is no producer and no
  src-side schema validation; see MINOR-2/claims audit.
- **Replay never raises over any ordering incl. new types** — [VERIFIED]
  [RAN `pytest tests/ledger/test_coder_events.py tests/ledger/test_replay.py`
  → 56 passed; hypothesised totals: 60 examples coder-orderings, 150
  extended totality]. Red-teamed independently ( §5): phase/commit
  outcome invariant under hostile interleavings.
- **Persisted intent pinned in PROPOSED** — [VERIFIED]
  `tests/ledger/test_coder_events.py:187-197` (one vacuous half — NIT-3).
- **Idempotency discriminator fits `(correlation_id, event_type, attempt,
  discriminator)`** — [VERIFIED] `src/paa/ledger/events.py:68-102` key
  shape accepts any string discriminator, `attempt` defaults 0, payload
  excluded; `session:call_id:kind` is well within that contract. Collision
  class exists for adversarial ids — MINOR-4.
- **ToolJournal frozen: before/denied/after async, ToolCtx 9 fields,
  frozen** — [VERIFIED] `src/paa/coder/loop.py:305-372`: `@dataclass(
  frozen=True, slots=True)`, 9 fields, 3 `async def`s. Probes confirm
  frozen (`FrozenInstanceError` on assignment), hashable, 3 defaults `None`.
  One defect in the frozen artifact: annotation unresolvable at runtime —
  MINOR-1.
- **No producer yet; journal impl (N2.2), mandate (N2.4), recovery (N2.3)
  untouched** — [VERIFIED] `git show --stat 51a8c61` = 6 files, none in
  `ledger/recovery.py` / `ledger/mandate.py` / `sandbox/`; [RAN
  `Test-Path`] `src/paa/coder/journal.py` and `src/paa/ledger/mandate.py`
  both absent; grep over `src/paa` finds the 8 types only in
  `core/types.py`, `ledger/replay.py`, and `coder/loop.py` comments/protocol.

Blind findings settled before reading the impl report: MINOR-1 (TYPE_CHECKING
`get_type_hints` trap), MINOR-2 (tier fold has no barrier against
`fact_id`), MINOR-4 (discriminator ambiguity), NIT-1 (`__all__`/package
exports), NIT-3 (vacuous nullable test). The impl report independently
disclosed the sweep gap, stale `agent.py` message, and one-way door; the
"no field moves" overstatement (MINOR-3) and the false TYPE_CHECKING
justification (part of MINOR-1) were not disclosed.

## 2. Claims audit

22 checkable claims from W2-IMPL-01-N2-1.md; 17 verified, 5 false or
unsupported (details in §4). Summary:

| # | Claim | Verdict |
|---|---|---|
| 1 | Code commit 51a8c61, 6 files | [VERIFIED] `git show --stat` |
| 2 | 8 EventTypes present, TERMINAL untouched (6) | [VERIFIED] types.py:252-282 |
| 3 | `_PHASE_MAP` 17, `_NO_PHASE_CHANGE` 9→17, total 34=17+17 | [VERIFIED] redteam2 partition |
| 4 | `_row_to_event` raises on unknown strings (one-way door) | [VERIFIED] store.py:478-496 `EventType(row[...])`; round-trip test covers the new members |
| 5 | Fold records unknowns, never raises (running binary's enum) | [VERIFIED] replay.py:189-197 |
| 6 | Pre-commit loop had 6 `__all__` exports, no ToolJournal/ToolCtx | [VERIFIED] 51a8c61 diff is append-only in loop.py |
| 7 | Agent journal slot fails fast (NotImplementedError) | [VERIFIED] agent.py:182-186 |
| 8 | `journal.py` absent (N2.2 target) | [VERIFIED] Test-Path False |
| 9 | IsolationLevel IntEnum NONE..VM; shell convention = name string | [VERIFIED] sandbox/base.py:58-94; ADR:84-89 vs shell_tools; golden payloads use `"PROCESS"` |
| 10 | 35 new tests; 56 two-file; 133 ledger dir | [VERIFIED] [RAN … → 56 passed; ledger dir 133 via junit `tests=133 failures=0`] |
| 11 | ruff 0; mypy 3 at subprocess_backend.py:548 | [VERIFIED] [RAN full `ruff check src tests` → All checks passed; `mypy src` → 3 errors, all at that line, pre-existing Windows attrs] |
| 12 | Conventions 33/34 at impl time (NEED-1) | [VERIFIED historically] `git show fb14fac` adds the classification; now [RAN → junit `tests=34 failures=0`] |
| 13 | Mutation reversal: removing `_NO_PHASE_CHANGE` entries fails TestPhaseInertness (6 tests) | Corroborated by code path (replay.py:191 → unknown_events non-empty breaks `== []` asserts) + impl's disclosed mutation run; reviewer could not re-run (read-only). Counted verified-by-reasoning. |
| 14 | Digest: "ToolCtx/ToolJournal exports" | **FALSE as stated** — neither name is in loop `__all__` (loop.py:41-48) nor `paa.coder.__init__` (coder/__init__.py:47). Body §12.4 admits it. NIT-1. |
| 15 | "No TaskProjection field moves" (replay.py:295-297, ADR-0046:55-59) | **FALSE strictly** — common preamble (replay.py:176-184) stamps `session_id`/`state_version`/`updated_at`/`modality`/`created_at` from *any* event incl. coder ones [VERIFIED redteam2]. MINOR-3. |
| 16 | TYPE_CHECKING justification: "runtime import would pull the sandbox tree into every importer" | **FALSE** — importing `paa.coder.loop` already loads `paa.sandbox.base` + all backends via `paa.coder.__init__` [VERIFIED redteam3]. Class E itself proves converting breaks no import. MINOR-1. |
| 17 | `apply_tier_event` is a no-op for coder events / "needs no change" | True for golden payloads, **UNSUPPORTED as invariant** — any `fact_id` key fabricates a T2 tier [VERIFIED redteam2 LEAK]. MINOR-2. |
| 18 | Explicit arm "mirrors the TIER/ATTENTION precedent" | **UNSUPPORTED** — TIER/ATTENTION rely on the generic `case _: pass` (no explicit arm anywhere for them). The arm is fine; the precedent claim is not. |
| 19 | Payload `v` rule enforced by golden tests as tripwire | True only for literals; no producer/validator exists. Restated accurately in §4 MINOR-2; the reader rule for future `v` is ADR text with no reader code (impl discloses this — [ASSUMED] it lands in N2.2/N2.3). |
| 20 | Docs-references lock green despite line shifts (NEED-3 semantic sweep) | [VERIFIED] conventions 34/34 incl. docs lock + ORCH re-point commit 55ea344 |
| 21 | No producer/ledger row carries the 8 types yet; mixed-version crash cannot occur until N2.2 | [VERIFIED] grep + journal.py absent |
| 22 | "133 store/replay/tier tests pass incl. verify_chain" | [VERIFIED] ledger junit 133 |

## 3. Long-run test (independent answers)

- **Source**: the freeze is the source. `v=1` + persisted intent
  (pre/post/patch in PROPOSED) is the contract N2.3 classification will
  read. Roots are correct: `_NO_PHASE_CHANGE` membership (not sweep
  exemption) is the primary classification, exactly per plan B-5; the
  sweep exemption is genuinely separate (see Operability). Any N2.2 change
  to payload keys/discriminator breaks golden tests — the intended
  tripwire.
- **Load (100x, concurrency, partial failure)**: no hot-path change.
  One extra frozenset lookup per event; the match arm only fires for the
  8. Store dedupe is untouched and covered (`test_duplicate_proposed_
  append_returns_existing` hits the real savepoint path). Cost model is
  unchanged from pre-existing events (2 writes/tool-call once N2.2
  exists). No new queues/loops/unbounded state.
- **Hostile input**: red-teamed ( §5). Spoofed keys
  (`workspace_path`, `applied_patch_sha256`, `step_index`,
  `tokens_consumed`) in coder payloads are ignored by the fold — the arm
  reads no payload keys at all. Two hostile paths remain open *by
  contract only*: `fact_id` injection into the tier fold (MINOR-2) and
  separator/empty `call_id` discriminator collision (MINOR-4). Both are
  unreachable today (no producer) and must be closed in N2.2, not here.
- **Operability (observe, roll back, migrate)**: rollback before the
  first N2.2 write is a clean revert — verified: the only
  behavioural surface is vocabulary + fold inertness; there is no
  migration (TEXT column) and no stored coder rows anywhere. After the
  first journal write to a shared `PAA_HOME`, the one-way door is real:
  pre-N2.1 binaries raise `ValueError` in `_row_to_event`. Critically,
  do **not** run `boot_sweep` against a home containing coder-only
  lineages until N2.3: they project to initial phase `CREATED`
  (resumable), head `is_terminal=0`, so `recovery.py:236-282` will offer
  them as resumable work — this is N2.3's, already ADR-documented.
- **Reversal (which test fails if the fix is reverted?)**: reasoned-only
  (read-only; no source mutation was run by this reviewer, impl's
  executed mutation is disclosed and consistent). Reverting the
  `_NO_PHASE_CHANGE` entries makes every phase-inertness test fail via
  replay.py:191 (coder types land in `unknown_events`, breaking the
  `== []` asserts); reverting the enum breaks `test_coder_events.py` at
  import and the extended Hypothesis sampled enum; renaming a golden
  field fails exact-dict equality; re-pointing a type into `_PHASE_MAP`
  fails `test_all_eight_are_in_no_phase_change` / `test_none_is_terminal`.

## 4. Findings

**MINOR-1 — `ToolCtx.isolation_level` annotation is unresolvable at
runtime; the `TYPE_CHECKING` guard buys nothing** —
`src/paa/coder/loop.py:347,382-383`. `typing.get_type_hints(ToolCtx)`
raises `NameError: name 'IsolationLevel' is not defined` [VERIFIED
redteam1]. ADR-0046's premise is also false in this tree: any import of
`paa.coder.loop` already loads `paa.sandbox.base` (and every backend)
through `paa.coder/__init__.py` → `agent.py` → `runtime` [VERIFIED
redteam3]. The conventions lock classifies the block E — mechanically
proving a runtime import breaks no import [VERIFIED fb14fac +
test_import_conventions.py:152]. Fix (bundle with the NEED-3 docs sweep
to absorb the line-shift it causes at the top of the file, or in N2.2
when loop.py is next touched): runtime import `from paa.sandbox.base
import IsolationLevel`; downgrade the classification entry/remove it;
add a `get_type_hints(ToolCtx)` assertion so the freeze cannot silently
regress. No current consumer calls `get_type_hints(ToolCtx)` (construction
and awaiting work), so this is not a MAJOR — but a frozen protocol whose
annotation cannot be read at runtime is the wrong thing to freeze.

**MINOR-2 — Tier fold has no structural barrier against `fact_id` in
coder payloads; the "no-op" proof covers only golden literals** —
`src/paa/ledger/replay.py:313-384` (`apply_tier_event`), contract
`tests/ledger/test_coder_events.py:161-164,323-329`. [VERIFIED redteam2]
A coder event carrying `{"fact_id": "fact-evil", "to_tier": "T4"}` folds
to `{"fact-evil": "T2"}`. The fold's "any other event naming a fact
defaults to T2" is pre-existing and deliberate, but the claim that N2.1
leaves the tier fold safely untouched rests entirely on `fact_id` never
appearing in a producer payload that does not exist yet. A single
N2.2 producer bug (e.g. a tool result that names a fact) would then
silently fabricate tier state in every rebuild. DO-LATER (N2.2, needs
an ADR note if the fold changes): either assert-and-refuse `fact_id`
in the journal constructor, or hard-exclude the 8 coder types (and any
future coder vocabulary) from `apply_tier_event`'s defaulting branch.

**MINOR-3 — "No projection field moves" overstates the fold; coder
events do stamp the common preamble** — `src/paa/ledger/replay.py:
176-184` (preamble) vs `285-298` (arm comment), `src/paa/core/types.py:
230-236`, ADR-0046:53-59. [VERIFIED redteam2] A `TOOL_CALL_PROPOSED`
event sets `TaskProjection.session_id` (first-seen wins), `state_version`,
`updated_at`, `modality`, `created_at`. This is pre-existing behaviour
for all `_NO_PHASE_CHANGE` events (tier/allocator do the same) and is
harmless on a coder-only correlation, but the comments assert the strong
form ("no field moves", "the fold carries nothing"). If N2.2 ever shares
a correlation between a task and coder events, the first coder envelope
`session_id` would capture `TaskProjection.session_id`. Recommend
softening the comments to "no arm-specific field moves; the common
preamble (version/session/routing) is shared by every event", and
N2.2 keep coder events on coder correlations.

**MINOR-4 — Frozen discriminator `session:call_id:kind` collides for
adversarial ids; empty-`call_id` dedupe hazard documented but
unenforced** — `src/paa/core/types.py:238-243`,
`docs/adr/0046-coder-event-forward-compatibility.md:129-144`,
`src/paa/coder/loop.py:316-321`. Two distinct calls
(`session="s:1", call="c"`) vs (`session="s", call="1:c"`) produce the
identical discriminator `s:1:c:PROPOSED`, so the second PROPOSED would
dedupe away and its tool would execute unjournalled. Empty `call_id`
(all distinct calls collapsing to one discriminator per session) is the
same failure in its most likely form — `ToolCall.call_id` defaults to
`""` (loop.py:59) and `Session.resume`/`Session(session_id=…)` accept
arbitrary strings (session.py:89,174). Default `session_id` is
uuid-hex, so this needs hostile/model-shaped ids, and there is no
producer today — hence MINOR, not MAJOR. DO-LATER (N2.2 + ADR
amendment): the journal must reject `:`-bearing and empty `session_id`/
`call_id` before constructing the discriminator (or a separator is
chosen that needs no rejection); add collision-pair tests.

**NIT-1 — Frozen public names are not exported** —
`src/paa/coder/loop.py:41-48` (`__all__` unchanged),
`src/paa/coder/__init__.py:47`. `ToolCtx`/`ToolJournal` are importable
only by full path (`from paa.coder.loop import …`), which is how the
tests reach them. Digest called them "exports". Add both to `__all__`
(and consider the package namespace) in N2.2, deliberately — NOT by
editing here, since adding to `__all__` at the top of loop.py shifts
docs-cited lines.

**NIT-2 — `CoderAgent.build` error message is now stale** —
`src/paa/coder/agent.py:182-186` says "no ToolJournal protocol exists
yet", false since 51a8c61 landed the protocol in loop.py. The fail-fast
behaviour is correct and unchanged; only the message lies. N2.2 owns
`agent.py`.

**NIT-3 — `test_intent_fields_are_nullable_per_call_kind` is vacuous** —
`tests/ledger/test_coder_events.py:192-197` copies the golden dict, sets
the three fields to `None` itself, then asserts they are `None`. It
cannot fail against the system; it only restates its own setup. Replace
with a producer-side or schema-side assertion in N2.2; for N2.1 it pins
nothing.

No BLOCKER, no MAJOR. No band-aids (no swallowed errors, no
skip/weaken/suppression, no flag behind the failing path anywhere in
the diff [VERIFIED by full diff read]).

## 5. Attempts to break it

All run by this reviewer, on current tree:

- **Interleaved ordering (task × coder)** [RAN redteam2]: full
  TASK_REQUESTED→MUTATION_COMMITTED history with each of the 8 coder
  types inserted mid-stream, × 10 payload variants (empty, `fact_id`
  injection, missing `v`, `v=2`, `v="1"`, 1000-element nested, all-null
  intent, workspace/patch/step/token spoof keys, session spoof). 80/80
  replays raised nothing; phase, attempts, tokens, completed steps,
  workspace, applied patch all equal the coder-free replay. The arm
  reads zero payload keys — totality is structural, not sampled.
- **Tier leakage** [RAN redteam2]: `fact_id`/`fact_id`+`to_tier` in a
  `TOOL_CALL_PROPOSED` payload → fabricated `T2` tier entry. Confirmed
  leakage path (MINOR-2). Empty/missing-`v`/spoofed payloads: no leak.
- **Protocol surface** [RAN redteam1]: `get_type_hints(ToolCtx)` →
  `NameError` (MINOR-1); `ToolJournal.before` hints resolve fine (only
  `ToolCtx`'s field hurts); frozen mutation raises; empty `ToolCtx`
  constructs without complaint (feeds MINOR-4); `__all__`/package lack
  both names (NIT-1).
- **Import cost premise** [RAN redteam3]: `paa.sandbox.base` (+ all
  backends) already in `sys.modules` after importing `paa.coder.loop`.
- **Phase partition audit** [RAN redteam2]: 34 = 17 + 17, disjoint,
  covering — a future 9th-type mistake of the pre-existing kind
  (neither map) would still be caught by `test_unknown_events == []`
  only if a test feeds it; there is no exhaustive
  `set(EventType) == _PHASE_MAP | _NO_PHASE_CHANGE` lock test. Noted
  as residual risk below; suggest one in N2.2.
- **`filterwarnings`**: [RAN `pytest tests/ledger/test_coder_events.py
  -q -W error::DeprecationWarning` → 35 passed] — no DeprecationWarning
  from `paa.*` (repo config `error::DeprecationWarning:paa.*` also
  green across all runs).
- **Envelope spoof**: a coder event's `session_id` lands in the
  projection (documented here as MINOR-3) but never moves phase or
  counters — consistent with tier precedent; no integrity impact found.

Things that held: hash chain / `verify_chain` over all 8 round-trip
(store test, re-run green); duplicate-PROPOSED dedupe against the real
store; snapshot fallback unaffected (`tests/ledger` 133 green);
conventions locks (docs references, markdown links, import sweep,
classification, annotation-readability for the *relational* family)
34/34.

## 6. Dimension checklist

- Functional correctness: PASS (partition, inertness, totality,
  round-trip, dedupe all re-verified).
- Security/privacy: PASS for N2.1 scope (DENIED = proof of non-execution
  by contract; `args_preview_redacted` post-hook; canary N2.5 deferred
  by design). MINOR-2/4 are integrity-not-confidentiality gaps.
- Error handling: PASS (no new raise paths; fold total; store path unchanged).
- Idempotency: PASS mathematically; MINOR-4 adversarial-id gap.
- Replay purity/totality: PASS (arm is pure `pass`; stored-data paths unaffected).
- Observability: PASS (no log surface added; unknown_events stays empty —
  the absence itself is the observable).
- Docs/ADR accuracy: GAP — MINOR-1 rationale false, MINOR-3 wording,
  digest "exports" phrasing (claim 14).
- Test quality: PASS overall (35 focused tests, store+hypothesis, real
  mutation evidence) with NIT-3 vacuous test and hypothesis payload
  coverage thinner than ideal (types-only draws; benign here since the
  arm ignores payloads — my §5 covered what the test doesn't).
- Performance: PASS (one frozenset probe per event; negligible).
- Compatibility/migration: PASS (TEXT column, no migration; one-way door
  documented; rollback clean pre-N2.2).

Implementation-level notes: the explicit no-op arm (replay.py:285-298)
duplicates the generic `case _: pass`, but the tier precedent is
comment-text, not an arm; keeping the arm is the right call (the next
reader finds the contract in the fold). Not flagged as a defect.

## 7. Optimization and simplification opportunities

- **DO-NOW (1)**: MINOR-1 fix — runtime import of `IsolationLevel` in
  loop.py, bundled with the ORCH NEED-3 line-shift sweep (which already
  re-points `loop.py:75/85/116`-adjacent citations), plus a
  `get_type_hints(ToolCtx)` test and removal/reclassification of the
  E entry. One line; class-E proof makes it risk-free.
- **DO-LATER (2)**: (a) MINOR-2 structural tier exclusion or
  producer-side `fact_id` refusal; (b) MINOR-4 discriminator
  validation + collision tests + ADR amendment. Also recommended: an
  exhaustive `set(EventType) == keys(_PHASE_MAP) | _NO_PHASE_CHANGE`
  lock test so a future type cannot silently fall into `unknown_events`.
- **NOT-WORTH-IT (1)**: removing the explicit no-op match arm to
  "simplify" back to `case _: pass` — the arm is 13 lines of
  documentation with zero runtime cost; reverting it would make the
  contract invisible again.

## 8. Cross-agent interactions

- NEED-1 (classify `paa.coder.loop`) → closed by ORCH fb14fac (E +
  note); NEED-2 (ADR index row) and NEED-3 (path:line re-point sweep)
  → closed by ORCH 55ea344. My `DO-NOW` above deliberately piggybacks
  NEED-3 rather than introducing a second loop.py line-shift.
- Hotspot serial: `coder/loop.py` is N2.2's next file — MINOR-1's fix
  and NIT-1/NIT-2 should land in N2.2's edit to the same files, not as
  a parallel loop.py touch.
- N2.3 owns the sweep exemption; until then no boot sweep over coder
  homes. N2.2 owns journal immplementation and must inherit MINOR-2/4
  guards and re-point golden payload tests at real producer output
  (today's goldens pin literals only).
- COMMS: one DONE line appended (verdict + counts + evidence link), per
  the COMMS protocol; no NEED/CONFLICT — all findings are either owned
  by N2.2/N2.3 or by the NEED-3 sweep bundle (see §7).

## 9. What I could not verify

- **Full suite** — not run (standing order 16 restricts sub-agents to
  scoped checks). Scoped evidence: tests/ledger 133, conventions 34,
  ruff full, mypy full — all green at the baseline.
- **Linux/CI** — reviewer host is Windows; mypy's 3 pre-existing Windows
  errors at subprocess_backend.py:548 are environmental, and the Linux
  gate was not exercised here. `EventType` round-trip parity on
  PostgreSQL/S3 is [ASSUMED] implicit (TEXT column, substrate-agnostic),
  matching the impl report's disclosure.
- **Pre-51a8c61 tree state** beyond the commit diff (e.g. "zero hits
  repo-wide" pre-state) — verified through the diff, not through a
  checkout.
- **Reversal** — mutation re-run not executed (read-only); verdict rests
  on the code path + impl's disclosed mutation result.
- **N2.2 producer behaviour** — does not exist; all producer-facing
  claims (v=1 on every emitted row, post-hook shas, discriminator
  construction) are contract-only and unverifiable until N2.2.
