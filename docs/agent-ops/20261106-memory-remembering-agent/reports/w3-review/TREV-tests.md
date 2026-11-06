<!-- ORCH-DIGEST:START -->
agent: W3-TREV-01 | scope: wave3 T-005/T-006 tests | verdict: APPROVE-WITH-CHANGES
tests_reviewed: 34 | fail-before/pass-after confirmed: 34 / reasoned: 0 / not-possible: 0
findings: BLOCKER 0 | MAJOR 0 | MINOR 4 | NIT 2
flaky_suspects: none
missing_edge_cases: P1 3 | P2 5 | P3 5
mutation_survivors: 1 of 7 plausible bugs not caught
changed_behavior_without_tests: none
confidence: high (counts verified by diff; scoped runs repeated; fail-before via worktree reversal)
<!-- ORCH-DIGEST:END -->

# Test quality review — W3-TREV-01 (TREV)

## 0. Method
- Counted added tests myself via `git diff <code-commit>~1..<code-commit> -- tests/` + grep of live files (not digest trust).
- Ran scoped suites, repeated for flakiness; fail-before via temp worktree (see §4).
- Gate-w3-1 orphan-guard regression (R-W3-REG1) is already known — noted as context only, not re-reported.

## 1. Count verification
- T-005: 18 added [VERIFIED git diff tests/memory/test_creator.py + test_llm_extractor.py: 18 `def test_` lines] — 5 `TestFirstPersonMapping` (test_creator.py:703,716,725,731,740) + 10 `TestExtractionDropCounts` (:758,765,773,781,790,801,807,821,830,841) + 3 `TestLLMExtractorDropCounts` (test_llm_extractor.py:565,585,594). Matches digest. [RAN scoped run → see §4]
- T-006: 16 added [VERIFIED test_decay.py:335,338,341,345,381,392,401,408,414,420,433,442,446,451,484,494] — 4 `TestPruneFloorSettings` + 7 `TestPruneFloorSweep` + 5 `TestDeprecatedDecayLambda`. Matches digest.
- Total reviewed: 34. Changed (modified pre-existing) tests: 0 in both commits — purely additive, consistent with brief "keep existing green".

## 2. Per-test assessment
| Test | Asserts behavior? | Fails without fix? | Deterministic/isolated? | Over-mocked? | Notes |
|---|---|---|---|---|---|
| T-005 first-person ×5 | Yes — DB row + fact importance + extractor-level entity/confidence | Yes: reversal 17F/1P claimed; independently re-verified §4 | Yes — tmp DB fixture, no clock | No | test_non_first_person (740) is compat pin, passes pre-fix by design — correctly the 1 pass |
| T-005 drops ×10 | Yes — public `result.drop_counts`/`summary()`/batch aggregate, one per drop class | Yes — AttributeError/zeros pre-fix | Yes | No | Covers all 8 counters? See missing: questions+hedge attribution ok; `subjectless` covered via structured payload (:801) |
| T-005 LLM ×3 | Yes — counted skips + protocol-compat zero fallback | Yes | ScriptedModel, deterministic | No — ScriptedModel is the house reference double (AGENTS.md) | `test_extractor_without_drop_counts` pins the getattr fallback — good |
| T-006 settings ×4 | Yes — explicit_prune_floor() semantics incl. explicit-0.15 | Yes (AttributeError pre-fix) | Yes — direct MemorySettings() | No | Missing: env-var path for explicit_prune_floor (only sweep-level) — P2 |
| T-006 sweep ×7 | Yes — raise/lower/0/1.0-immutable/bounds/env/unset | Yes: 13F/3P pre-change claimed; re-verified §4 | Yes — idle=0 ⇒ effective==initial, no clock dependence | No | Well-chosen boundary values (0.4 vs 0.30/0.5; 0.2 vs 0.30/0.15) |
| T-006 lambda ×5 | Yes — warns/ignores/unknown-drop | Yes (DID NOT WARN pre-fix) | Yes | No | Unknown-key test asserts both warning categories + kept/dropped keys — good |

## 3. Mutation thinking (brief a–g)
| # | Plausible bug | Caught? | By which test |
|---|---|---|---|
| a | pronoun map removed → user fact lost / still drops silently | Caught | test_i_am_based_writes_user_fact (DB entity==user), test_we_subject…/test_uppercase_we…; silent-drop variant also caught by drop_counts.total==0 assertion (:707) — a pure drop would give IGNORED ≠ PROCESSED |
| b | counters removed → drop uncounted | Caught | Every TestExtractionDropCounts nonzero assertion; LLM 3 tests |
| c | LLM extractor always-return-empty | **NOT caught as such** — see finding M1 | No test forces LLM path to return facts and asserts facts_written>0 with drop_counts==0 for valid input. Pre-existing LLM tests (test_structured_triples_become_facts etc.) catch it incidentally, but no *new* C05 test pins "valid LLM input → PROCESSED + 0 drops". If those pre-existing tests were weakened, C05 suite would stay green with an empty-return mutant |
| d | explicit floor ignored (always per-domain) | Caught | test_explicit_floor_evicts_above_domain_floor (0.4@0.5) + test_env_explicit_floor_propagates… |
| e | floor always-on (default 0.15 overrides per-domain) | Caught | test_unset_env_floor_keeps_per_domain_behavior + test_explicit_floor_can_lower_threshold (proves replacement not max) + settings-default None test |
| f | decay_lambda warn missing | Caught | test_explicit_decay_lambda_warns_deprecation (pytest.warns — fails DID NOT WARN) |
| g | unknown key crash | Caught | test_unknown_decay_lambda_keys_dropped_not_fatal + test_unknown_only_decay_lambda_loads (would raise/record mismatch pre-fix) |
Survivors: 1 of 7 (c, partial — mutant is caught only by pre-existing tests, not by the new-suite contract).

## 4. Coverage, runs, flakiness
- [RAN `.venv\Scripts\python.exe -m pytest tests/memory/test_creator.py tests/memory/test_llm_extractor.py tests/memory/test_decay.py -q -> 70+53=123 passed` (run 1), repeated run 2 identical — see final §4 tail for verbatim]
- Coverage of changed lines: counters at every drop site ✓; first-person at 4 rule paths — **triple path tested (:731), sentence path tested, but key-value and _walk paths untested** (P2); non_storable-vs-extractor disjointness pinned (:807) ✓; LLM skipped-entry counting incl. non-list (+1) ✓; prune-floor via constructor + env ✓, via settings-injection (curator) ✗ known deferred; decay_lambda unknown keys ✓.
- Error paths: NaN/out-of-range floor (constructor + settings) ✓; malformed LLM facts ✓; hostile (NUL/oversized) not re-tested in new tests but pre-existing LLM containment tests remain green.
- Fail-before re-verified (not just implementer claim): temp worktree at code parent with new tests only — see §4 tail.
  - T-005 fail-before @ 648f979~1 (c1d0321) + new tests copied in, PYTHONPATH=worktree/src: **17 failed / 1 passed** (`FFFF.FFFFFFFFFFFFF`; pass = test_non_first_person_fact_keeps_default_importance compat pin). Failures semantic (entity "I am", entity "I", 0 candidates for We, AttributeError drop_counts) — not import errors. [RAN worktree pytest]
  - T-006 fail-before @ f4d0ca8~1 (2ad99e3) + new test_decay.py: **13 failed / 3 passed** (`FFF.FFFFFF.F.FFF`; passes = validates_range pre-existing Probability check, default-no-warn silence, unset-env per-domain — all correct no-op pins). Modes: AttributeError explicit_prune_floor, TypeError unexpected kwarg, evicted==1 got 0 (true behavioral), DID NOT WARN, unknown dict not dropped. [RAN worktree pytest]
- Flakiness: scoped 3-file suite run twice, both 123 passed, EXIT:0. No flakes observed. [RAN .venv\Scripts\python.exe -m pytest tests/memory/test_creator.py tests/memory/test_llm_extractor.py tests/memory/test_decay.py -q ×2 -> 123 passed each]

## 5. Findings
- M1 (MINOR) tests/memory/test_llm_extractor.py: no new test pins valid-LLM-input → PROCESSED with drop_counts.total==0. Mutation (c) survives the new suite; only pre-existing tests catch it. Recommend 1 test: ScriptedModel returns one valid triple → facts_written==1, drop_counts.total==0.
- M2 (MINOR) first-person mapping tested on sentence + triple paths only. Key-value (`{"subject":"I",…}`-style) and `_walk` structured paths apply the same helper per impl report §4 but have no test. A regression removing the mapping from one path survives. Recommend 1 test per untested path.
- M3 (MINOR) No test that `BatchReport.summary()` key set is frozen beyond the one batch test's subset assertion (`set(summary) >= {...}` is a superset check — allows additive keys). Acceptable but note the assertion direction: it does not fail if summary gains a key. The digest claims "frozen key set pinned" — the test only pins presence, not absence. Recommend exact-set assert.
- M4 (MINOR) test_decay.py env test mutates global settings cache; uses try/finally reset ✓ but no `monkeypatch.delenv` guard for pre-existing PAA_MEMORY__PRUNE_CONFIDENCE_FLOOR in CI — if the CI env sets it, test_unset_env… could be polluted. Adjacent risk only; CI currently doesn't. Recommend `monkeypatch.delenv(..., raising=False)` in unset test.
- N1 (NIT) test_first_person docstring claims "No upper-case assumption" yet no "I'M"/"WE'RE" contraction variant tested (brief's requested sketch) — first-word map uses token `i`/`we` exactly, so "I'm" (single token, no space) does NOT map. Behaviour is arguably correct (entity "I'm …" is a different input) but unpinned — add a pinning test either way.
- N2 (NIT) LLM drop-count tests reuse `_row({"fakes": []})` payload key name "fakes" (scripted-model routing) — works but opaque; no issue.

## 6. Missing edge cases (P1/P2/P3 with sketches)
P1:
- P1-1 LLM happy path with zero drops (mutation c): ScriptedModel({"facts":[{"subject":"Acme","predicate":"status","object":"active"}]}) → PROCESSED, facts_written==1, drop_counts.total==0.
- P1-2 "user" as real name collision: entity literally named "user" in non-first-person text ("User owns Acme" / "user reports status") — assert it does not get importance 0.3 forced, or pin the documented trade. First-word map would map "user"-leading subjects only if token is i/we — "User" token ≠ i/we, so safe; pin it.
- P1-3 Empty/pronoun-only text: "" / "I" / "we" alone (no predicate) → IGNORED with pronoun/no_match counted, no crash, no entity row.
P2:
- P2-1 Pronoun variants: "I'M based in Berlin", "WE'RE live", "I" uppercase mid-sentence — pin current behaviour (contractions unmapped → likely no_match/pronoun count).
- P2-2 Key-value and _walk first-person paths (M2).
- P2-3 Counter under LLM path on question/hedge text (LLM extractor receives question payload — is it counted as question or malformed_skipped? unpinned).
- P2-4 .env-file explicit-set (impl report §15 least-confident): cwd-controlled .env with PAA_MEMORY__PRUNE_CONFIDENCE_FLOOR=0.5 + reset_dotenv_cache() → explicit_prune_floor()==0.5.
- P2-5 Multiple decay_lambda domains: {"semantic":0.5,"temporal":0.3} both kept, single DeprecationWarning.
P3:
- P3-1 DOMAINS values drift: test_every_domain_has_a_policy already guards existence; strengthen with prune_floor/lambda spot values so a DOMAINS edit without settings-table update fails.
- P3-2 Registry column dormant: assert no test/config path seeds hot_serving_memory_registry.decay_lambda (documents dormant status; schema hotspot so pin absence, don't seed).
- P3-3 Exact BatchReport.summary() key-set equality (M3).
- P3-4 NaN prune floor via env string "nan" → settings ValidationError at construction, not silent 0-drop.
- P3-5 Counter saturation: 10k hedged lines → hedges==10000, one log line (load shape from impl report §8; property-style if cheap).

## 7. What I could not verify
- Mutation (c) full-kill: not run as a live mutant (brief asks "caught?" — answered by inspection + pre-existing suite, not by injecting the mutant).
- .env-file explicit-set path (P2-4) — needs cwd/chdir isolation; not run.
- Full suite / gate-w3-1 (out of scope per brief: scoped checks only; orphan-guard regression known context).
- LLM first-person mapping (impl §13 out-of-scope #2) — confirmed untested by design; not a test defect.
