<!-- ORCH-DIGEST:START -->
agent: REV-W1b | scope: wave-1 fix diff 228fcf1..HEAD (R-W1-001/002/003/004 + TREV P1-a/P2-a/P2-b) | verdict: APPROVE-WITH-CHANGES
findings: BLOCKER 0 | MAJOR 0 | MINOR 3 | NIT 2
claims_audit: 14 claims checked / 0 unsupported or false
band_aids_found: none
top_3_risks: hostile payload scalars in the fallback float() still crash _semantic (pre-existing class, now reachable from 2 branches) | P2-c dedup-pinning test still absent (mutation-class gap from TREV) | ~200 non-broken citations to shifted files remain off-by-one where the shifted line is non-blank (lock-blind, out of fix scope)
optimizations: DO-NOW 0 | DO-LATER 2 | NOT-WORTH-IT 1
reversal_check: reasoned-only (read-only reviewer; reversal logic checked against test bodies + fix-report evidence)
confidence: high (blind pass on full fix diff before fix reports; scoped ruff/mypy-pytest re-run incl. full mypy src; docs re-points spot-verified semantically)
last_comms_line_read: 2026-11-07T14:20:00Z W1-FIX-DOCS -> ORCH | DONE | W1-FIX-DOCS
<!-- ORCH-DIGEST:END -->

# Code re-review — REV-W1b (independent, read-only; did not write wave-1 code or fixes)

## 0. Method

- Diff range: `228fcf1..HEAD` (`10989e1` code fix + `789f501` docs re-point + 2 report commits `493034b`, `32b3138`). Files in diff: `src/paa/agents/context_agents.py`, `tests/agents/test_semantic_derivation_p002.py`, 28 docs files, 2 fix reports.
- Files read in context: full fix diff for `context_agents.py` + tests + ADR-0036; surrounding code `context_agents.py:_hot_facts` (:196-241), `_semantic` (:243-338), `_touch_recalled` (:490-500), `cli/main.py:629-674`, `runtime.py:462-496`, `creator.py:1485-1514`, `models/base.py` imports; REV-W1 §§4-5 + TREV-W1 §6 (prior findings); the two fix reports (read AFTER blind pass).
- Commands run (scoped, Windows venv):
  - [RAN ruff check on context_agents.py + test file -> All checks passed]
  - [RAN mypy on context_agents.py -> Success: no issues found in 1 source file]
  - [RAN mypy src -> Found 3 errors in 1 file, all subprocess_backend.py:548 (killpg/getpgid/SIGKILL, Windows-host attr-defined), matching the fix report's "mypy total 3" claim and baseline]
  - [RAN pytest tests/agents/test_semantic_derivation_p002.py tests/conventions/test_docs_references.py -q -> 19 passed (14 semantic + 5 docs-refs)]
  - [RAN pytest tests/agents/test_semantic_derivation_p002.py tests/agents/test_context_agents.py tests/memory/test_decay.py -q -> 91 passed (72+19 dots), matching W1-FIX-CODE's 91-passed claim]
  - [RAN .venv\Scripts\python probe printing ~35 cited lines at HEAD + git show 294fb31 baseline contents for config.py/context_agents.py comparison -> see §1]
- Order followed: blind pass on the diff + surrounding code + prior findings BEFORE reading W1-FIX-CODE.md / W1-FIX-DOCS.md; claims audit (§2) done after.
- NOT run: full test suite (standing order 16), live Qdrant/Postgres/S3 (no DSN/server), reversal executions (read-only — reasoned only), Docker/CI, import-conventions subprocess sweep.

## 1. Blind-pass findings (before reading implementer reports)

1. **R-W1-002 closed on reading.** Hydration batch log is now `context.hydration_failed` with `error=redact(str(exc))` [VERIFIED context_agents.py:285]; the vector-path `context.vector_search_failed` at :252 is untouched (correct — that is the real vector path). Module-level `from paa.models.base import redact` at :56; `models/base.py` imports only stdlib + `paa.core.*` (lines 67-78) — no cycle with `agents/`. The removed function-local `_touch_recalled` import is still satisfied by the module-level import [VERIFIED line 498 uses `redact`, resolves to :56]. No new unredacted line in the fix diff.
2. **R-W1-006 closed.** Per-hit `try/except (ValueError, TypeError)` wraps the whole hydrated derivation (:294-322); corrupt row falls back per-hit with redacted warning + `fact_id` (:315-319). Inner `KeyError` unknown-domain passthrough preserved (:297-308). A `KeyError` from a missing SELECT key would still propagate (outer except excludes `KeyError`) — but rows always carry the SELECT keys, and the fix report discloses this as "exactly as pre-fix". Honest, not a regression.
3. **R-W1-007 closed.** Hydration SELECT now `AND superseded_by IS NULL` (:279-280), mirroring `_hot_facts` :201. A superseded hit therefore takes the payload-fallback branch — the intended semantics.
4. **New tests are behavior-focused and discriminating (read before reports).** Degrade test now secret-shaped (`sk-ABCDEF1234567890`, 16 chars ≥ `sk-[A-Za-z0-9_-]{12,}` at models/base.py:244) and asserts marker + raw-token absence + new event + old event absent. Corrupt-row test drives the exact R-W1-006 crash (`last_queried_at='garbage-not-a-date'` → `from_iso` ValueError) and asserts per-hit fallback + healthy sibling still derives. Superseded test asserts payload-exact (0.42/0.33) with a discriminating derived value (aged 300 days). P1-a (mixed live/deleted hits), P2-a (`db=None`), P2-b (zero hits → zero `fetch_all`) each pin the exact branches TREV marked absent. All deterministic (injected `now`, `_FakeVectors` pattern), no mocks of the unit under test.
5. **Docs lock restored on reading.** `tests/conventions/test_docs_references.py` passes 5/5 [RAN] — 63→0 broken. Spot-verified ~28 re-pointed citations against HEAD line contents: every one lands on a def line or the line the sentence claims (e.g. `cli/main.py:76 def submit(`, `:292 def serve(`, `:227 def doctor(`, `config.py:896 def dotenv_value`, `:886 def ensure_directories`, `:188/:192 token_ceiling fields`, `context_agents.py:143 _hot_facts`, `:243 _semantic`, `:340 _graph_neighbours`, `:376/:427 handle() defs`, `creator.py:1497 def drain_pending_embeddings`). Two baseline-wrong citations were actually improved to the symbol def (`models.md: config.py:491→:611 escalate_at_or_above`, `context.md: :133/:137→:188/:192 token ceilings`), which is better than blind +N. One true wrong-without-broken added fix: `agents.md :264→:340` (trio completion). No re-pointed citation found landing on a blank or semantically unrelated line.
6. **ADR-0036 §P01 paragraph is accurate.** Bound = rendered console text at `cli/main.py:635` (`_MAX_DRAIN_ERROR_CHARS`), marker inside 200 [VERIFIED cli/main.py:666-668 slices to `200 - len(marker)`]; runtime site `runtime.py:482` redact-only unbounded, mirroring `:474` [VERIFIED]. `creator.py:1497` in both spots [VERIFIED def line; :1490 is body line `indexed = ...`]. R-W1-004 closed.

## 2. Claims audit

| Implementer claim | Verified? | Evidence / finding |
|---|---|---|
| FIX-CODE: R-W1-002 fixed (redacted + renamed, module-level import) | **Yes** | [VERIFIED context_agents.py:56,285]; tests green [RAN 91 passed] |
| FIX-CODE: R-W1-006 fixed (per-hit fallback) | **Yes** | [VERIFIED :294-322]; corrupt test exercises the exact crash [RAN] |
| FIX-CODE: R-W1-007 fixed (superseded filter) | **Yes** | [VERIFIED :279-280]; superseded test [RAN] |
| FIX-CODE: TREV P1-a/P2-a/P2-b tests added | **Yes** | 5 new tests present (9→14) [RAN 14 passed]; P2-c dedup test not added (see R-W1b-002) |
| FIX-CODE: "91 passed; ruff 0; mypy total 3 pre-existing" | **Yes** | [RAN 91 passed across the 3 claimed files; ruff scoped clean; mypy src = exactly the 3 subprocess_backend.py:548 errors] |
| FIX-CODE: reversal 3 fail / 11 pass on pre-fix code | Reasoned-only | Logic consistent with test bodies (corrupt errors ValueError out of `_semantic`; superseded derives 0.5488≠0.42; degrade event-name mismatch). Not re-executed (read-only). The "11 pass" includes 3 pinning tests + 8 others — plausible, not independently proven. |
| FIX-CODE: "missing row key KeyError propagates exactly as pre-fix" | **Yes** | Outer except is `(ValueError, TypeError)` only [VERIFIED :311]. Disclosure is accurate. |
| FIX-CODE: "pre-existing unredacted lines :137/:252/:367 are FINDING, not fixed" | **Yes, honest** | Vector path :252 indeed still `error=str(exc)` [VERIFIED]; brief-mandated FINDING, not a fix-diff defect. |
| FIX-DOCS: "63 broken at start, 0 at end; lock 5 passed" | **Yes** | [RAN test_docs_references.py -> 5 passed]; consistent with REV-W1's reproduced 63 |
| FIX-DOCS: "never blind +N; each re-point read from citing sentence + base" | **Yes (sampled)** | ~28 spot-checks, all land on claim-holding lines; 2 baseline-wrong cites improved to defs; 1 wrong-without-broken (:264→:340) fixed beyond the broken list |
| FIX-DOCS: ADR-0043/0038/0041/0042 citation scan clean | Spot-verified | [VERIFIED creator.py:1497, cli/main.py:635, runtime.py:482/474; ADR-0042 docker-compose.yml:67 / pyproject.toml:42 / graph/base.py:68 cited correctly in FIX-DOCS report; I did not re-check every ADR-0038/0041 line] |
| FIX-DOCS: "no src/tests edits; docs-only diff" | **Yes** | [VERIFIED git diff --stat: docs-only + 2 report files in the docs commit range] |
| FIX-DOCS: residual FINDINGs (off-by-one walk, bare cli.md lines, wrong-without-broken rows) | **Yes, honest** | Disclosure is accurate and consistent with the lock's documented blind spot; none introduced by this diff. `config.py:490` for `get_settings()` is one instance (line 490 is `host_path_map` field) — pre-existing, disclosed class. |
| FIX-CODE: "code commit message lacks trailers; cannot amend" | **Yes** | Honest process disclosure; no code impact. NIT only. |
| FIX-CODE: "10989e1 (2 files)" | **Yes** | [VERIFIED git log --oneline + diff --stat for the fix range] |

0 unsupported or false claims.

## 3. Long-run test (independent answers)

- **Source:** ranking truth unchanged (`effective_confidence` on hydrated rows, T02 approx pinned); degradation truth is now per-hit payload snapshot, not just batch. Docs citations verified against target-line content, not arithmetic.
- **Load (100x, concurrency):** unchanged — one batched IN query per search, bounded by `limit`; read-only derivation; no new allocation growth. The added `AND superseded_by IS NULL` is a cheap indexed predicate, no perf regression.
- **Hostile input:** corrupt `last_queried_at`/confidence/importance degrades per-hit (new). Residual: hostile *payload* scalars (`confidence: "abc"` in `hit.payload`) still raise `ValueError` in both fallback branches — pre-existing class (REV-W1 attempt #7), now reachable from the corrupt-row branch too, but not introduced by the fix. See R-W1b-001 (accepted residual, not a blocker for this re-review).
- **Operability:** `context.hydration_failed` (DB/hydration) vs `context.vector_search_failed` (vector) are now distinct — operators grepping vector outages no longer misread DB outages. Rollback = revert 10989e1 (no migration). Docs rollback = revert 789f501 (lock re-breaks, by design).
- **Reversal:** removing `redact()` fails the degrade redaction assertions; removing the per-hit try errors the corrupt test; removing the superseded filter fails the superseded test (all reasoned against test bodies + fix-report reversal evidence). Re-pointing any citation back to its old number re-breaks the lock (old lines are blank), except `:264→:340` which is reasoned from the tier-trio sentence.

## 4. Findings

### R-W1b-001 [MINOR] Hostile payload scalars in fallback `float()` still crash `_semantic`
- Location: `src/paa/agents/context_agents.py:320-322, 324-326` · Evidence: the fallback `float(hit.payload.get("confidence", 0.8))` raises `ValueError`/`TypeError` on hostile payload types (`"abc"`, `None`), escaping both the hydrated-corrupt and missing-row branches. Pre-existing class (REV-W1 attempt #7; fix report discloses it as out of scope), but the corrupt-row branch now reaches this line from a *live-row* path where the old code crashed earlier at the row. Why it matters: the "never empty the tier" invariant is one hostile payload away from failing; payload comes from the vector store (JSON from an external system). Recommended fix (separate task): coerce with `try/except (ValueError, TypeError)` defaulting to 0.8/0.5, or validate at enqueue. Not a blocker: untracked pre-existing scope, honestly disclosed, outside the fix brief's write-set intent.

### R-W1b-002 [MINOR] TREV P2-c (duplicate hit ids deduped before IN clause) still untested
- Location: `tests/agents/test_semantic_derivation_p002.py` (absence); branch at `context_agents.py:275` · Evidence: the `dict.fromkeys` dedup claim in code has no pinning test; TREV-W1 §6 P2-c ("should add") was not in the W1-FIX-CODE brief's P1-a/P2-a/P2-b set, and no subsequent task added it. Why it matters: a refactor dropping the dedup inflates placeholders (correctness-neutral today at limit=30, but the IN-size contract is unpinned). Recommended fix: the ~15-line TREV sketch (two `_Hit`s, same id → 1 query). Not a blocker: marked P2 ("should add") by TREV, and REV-W1 did not gate on it.

### R-W1b-003 [MINOR] ~200 non-broken citations to shifted files remain off-by-one (lock-blind, disclosed)
- Location: docs corpus-wide · Evidence: W1-FIX-DOCS FINDING correctly identifies that only the 63 citations landing on blank lines were re-pointed; citations shifted by +1/+3/+137 landing on non-blank lines may now point one line early (e.g. `config.py:490` for `get_settings()` lands on the `host_path_map` field; `config.py:281` for the gVisor row lands on the extractor comment). Why it matters: the lock is green but the corpus still contains wrong-without-broken references — the exact class REV-W1 §8 warned about. Recommended fix: a scheduled full semantic re-check as the FINDING proposes (not this wave's scope; do not hold W1 sign-off for it). File as accepted residual with an owner, not as a re-review blocker — the fix diff introduces no new instance of this class.

### R-W1b-004 [NIT] Per-hit fallback duplicates the payload block (acknowledged decision)
- Location: `context_agents.py:320-322` vs `:324-326` · Evidence: three scalar lines duplicated in two branches, with an explicit DECISION note in the report ("mirror the existing shape"). Why it matters: only drift risk if one branch changes and the other doesn't. Recommended fix: none required; a helper extraction would be a refactor for 3 lines. Leave as-is.

### R-W1b-005 [NIT] Fix-code commit message lacks required trailers
- Location: commit `10989e1` · Evidence: disclosed by W1-FIX-CODE itself; `--amend` is forbidden, so it cannot be corrected post-hoc. Why it matters: wave-trail hygiene only; the SHA is recorded in both fix reports so traceability holds. No action; lesson for future waves (compose the message with trailers before committing).

## 5. Attempts to break it

1. **Secret-shaped DB error through the new event**: degrade test drives `sk-` token through `_semantic`'s hydration except; asserts `***REDACTED***` present and raw token absent [RAN green]. Cannot leak via this path (redact precedes log). PASS.
2. **Corrupt `last_queried_at` mid-batch**: corrupt test drives garbage string with a healthy sibling; asserts both elements, payload-exact + derived respectively [RAN green]. Crash is per-hit, not batch-wide. PASS.
3. **Superseded fact with surviving vector point**: superseded test asserts payload-exact, derived differs [RAN green]. PASS.
4. **Missing-row partial hydration / `db=None` / empty hits**: P1-a/P2-a/P2-b tests [RAN green]. PASS.
5. **Token straddling, boundary 200/201, markup attack** (P-001 sites): untouched by this diff; tests still green in the earlier 47-test set's scope (not re-run here — unchanged code). Reasoned PASS.
6. **`_touch_recalled` after local-import removal**: full context_agents suite green in the 91-pass batch [RAN]; `redact` resolves to module-level import. PASS.
7. **Hostile payload scalar** (`confidence: "abc"`): still crashes — R-W1b-001 (pre-existing, out of fix scope). Confirmed by reading, not executed.
8. **Docs reversal (any re-point reverted)**: lock fails again for the blank-line set; `:264→:340` rests on the trio sentence (reasoned). PASS.

## 6. Dimension checklist

| Dimension | Verdict | Notes |
|---|---|---|
| Correctness | PASS | R-W1-002/006/007 all closed at root cause; no new defect introduced |
| Root cause vs symptom | PASS | Per-hit fallback fixes the tier-emptying at the derivation site, not at the caller |
| Regression / compat | PASS (scoped) | 91-test scoped batch green [RAN]; signature unchanged; payload/ContextElement unchanged |
| Concurrency / idempotency | PASS | Read-only; one query/search; no state mutated |
| Error handling / degrade | PASS | Batch + per-hit + superseded + missing-row + db=None all degrade; R-W1b-001 is a pre-existing payload-scalar residual |
| Security / abuse | PASS | New log line redacted; event name correct; ANSI residual (R-W1-005) and other unredacted lines remain pre-existing FINDINGs, none newly introduced |
| Performance / scalability | PASS | One indexed predicate added; no N+1; no new allocations |
| Migration / rollback | PASS | No schema change; revert = 2 commits |
| Observability | PASS | `context.hydration_failed` vs `vector_search_failed` now distinguishable; per-hit log carries `fact_id` |
| Maintainability / conventions | PASS | Module-level import aligns with the file's existing practice; no new TYPE_CHECKING block; no noqa/type-ignore in the fix diff [RAN ruff/mypy] |
| Docs impact | PASS (with residual) | Lock 63→0 [RAN 5 passed]; all re-pointed citations semantically correct; R-W1b-003 documents the lock-blind remainder as accepted residual |

## 7. Optimization and simplification opportunities

| # | Location | Evidence | Impact | Effort | Risk | Verdict |
|---|---|---|---|---|---|---|
| 1 | Payload-scalar coercion in fallbacks (R-W1b-001) | `float(payload.get(...))` can still raise | Would complete "never empty the tier" for hostile payloads | Small | Low | DO LATER (separate hardening task; pre-existing) |
| 2 | P2-c dedup pinning test (R-W1b-002) | `dict.fromkeys` unpinned | Pins IN-size contract | Trivial | None | DO LATER (with #1) |
| 3 | Full semantic citation sweep (R-W1b-003) | ~200 citations, lock-blind class | Corpus-wide correctness | Medium (docs-only) | Low | DO LATER (scheduled wave-gate item, not W1 sign-off) |
| 4 | Dedupe payload fallback into helper (R-W1b-004) | 3 lines ×2 | Negligible | Small | Low | NOT WORTH IT alone |

## 8. Cross-agent / cross-task interactions observed

- **FIX-CODE / FIX-DOCS interleaving was handled correctly.** W1-FIX-DOCS finalised `context_agents.py` citations after `10989e1` landed (per its report §Context), so the code fix's +2 lines (@56, ~+15) — which would have re-broken any context citations fixed against `228fcf1` — do not leave residual broken citations. Verified: lock green [RAN].
- **No write-set collisions.** Code fix touched only `context_agents.py` + its test; docs fix touched only `docs/**` (28 files, no `src/**`/`tests/**`). No COMMS conflicts posted.
- **REV-W1 §8 lesson partially applied.** The re-point task caught one wrong-without-broken (`:264→:340`) beyond the broken list, showing the "read the sentence" method works. The remaining off-by-one class (R-W1b-003) is disclosed rather than silently carried — the correct posture for wave-gate accounting.

## 9. What I could not verify

- Full test suite, live Qdrant/Postgres/S3, Docker/CI — not run (standing order 16; no servers/DSN).
- Reversal executions (fix report's 3-fail/11-pass) — read-only constraint; reasoned against test bodies, not re-run.
- ADR-0038/0041 citation lines beyond the report's scan claim — spot-verified ADR-0036/0042 + creator/runtime/cli only.
- Whether non-broken citations beyond my ~28-line sample are all semantically correct — my sample found no failure, but a systematic sweep was out of scope (see R-W1b-003).
- Linux behaviour (CLI rendering etc.) — this diff touches no rendering code; Windows host only.
