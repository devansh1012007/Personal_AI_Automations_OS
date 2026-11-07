<!-- ORCH-DIGEST:START -->
agent: TREV-W3b | scope: wave-3 fix tests 6464eac..HEAD (037a8d6+f3baf89+efe4207) | verdict: APPROVE-WITH-CHANGES
tests_reviewed: 15 (phantom 5, panels 2, tier P2 5, dual-vault run_weekly 2, limit 1)
fail-before/pass-after confirmed: 0 / reasoned: 13 / not-possible: 2 (limit pin + override pin are contract pins, pre-existing code)
findings: BLOCKER 0 | MAJOR 0 | MINOR 3 | NIT 2
flaky_suspects: none
missing_edge_cases: P1 0 | P2 2 | P3 2
mutation_survivors: 2 of 12 plausible bugs not caught (CID-equality half of phantom guard; high clamp 1000->50)
changed_behavior_without_tests: none for the 3 src changes (recovery/reflection have behavioral tests; runner redact is log-only, correctly grep-verified)
confidence: high (all 5 files executed by me, fast files x3, counts match claims exactly)
<!-- ORCH-DIGEST:END -->

# Test quality review — TREV-W3b (fresh independent, read-only; did not write code or tests)

Prior TREV-W3 verdict: APPROVE-WITH-CHANGES — P1 panel-builder happy-path gap (mutation survivor) + cheap P2s. This re-review audits only the fix diff `6464eac..HEAD`.

## 0. Method

Blind pass first: read `git diff 6464eac..HEAD` (src: recovery.py, reflection.py, runner.py; tests: 2 new files + 3 additive files) and all 5 fix test files before auditing W3-FIX-01 claims. Then claims audit + own runs:

- [RAN `.venv/Scripts/python.exe -m pytest tests/ledger/test_recovery_tier_phantom_p009.py tests/tui/test_runner_panels.py tests/memory/test_tier_sweeper_p009.py -q` → 33 passed (5+2+26), repeated ×3, all green]
- [RAN `.venv/Scripts/python.exe -m pytest tests/memory/test_dual_vault_p010.py tests/coder/test_runtime_tools.py -q` → 107 passed (37+70)]
- [RAN `.venv/Scripts/python.exe -m ruff check src tests` → All checks passed]
- [RAN `.venv/Scripts/python.exe -m mypy src` → 3 errors, all pre-existing `sandbox/subprocess_backend.py:548` (Windows host attr-defined), matches W3-FIX-01 claim]
- No temp worktree (read-only); mutation assessment by construction against source. Real `LedgerStore`/`Database` tmp fixtures throughout — `tests/conftest.py` `db`/`ledger`/`requeuer` function-scoped.

## 1. Per test/suite assessment

| Test/suite | Asserts behavior? | Fails without fix? | Deterministic/isolated? | Over-mocked? | Notes |
|---|---|---|---|---|---|
| phantom `test_tier_only_head_is_skipped_not_resumed` | Yes — outcome counts, no requeue, ledger count frozen, still CREATED in open_correlations | Yes — revert exemption → RESUMED+requeue+audit event, 4 asserts fail | Yes, tmp ledger | No | Strongest test in the fix. The "still open" pin documents FINDING-1 honestly |
| phantom `test_sweep_is_idempotent...` | Yes — 2 sweeps, count frozen | Yes | Yes | No | Pins the "audit event would grow ledger per boot" rationale |
| phantom `test_mixed_tier_event_types...` | Yes — DEMOTED+ARCHIVED both exempt | Yes (both-type check mutant) | Yes | No | — |
| phantom `test_real_task_alongside...` | Yes — exact-match control, requeue payload names real CID | Yes for CID-pattern mutants | Yes | No | Does NOT catch CID-half removal (see §3 survivor 1) |
| phantom `test_non_tier_event_on_the_sweeper_cid...` | Yes — id-reuse not exempted | Yes (CID-only mutant) | Yes | No | The other half of the conjunction — good |
| panels `test_two_open_lineages...` | Yes — decision+reason by CID (exact dict eq), awaiting_ids exact list, recent_roles set+len, counts zeros | Yes — empty-forever builders fail len/gates/roles by construction | Yes, real ledger | `SimpleNamespace` rt — sanctioned seam (`rt: Any`), NOT a mock of the unit | **P1 closed.** Real `LedgerStore` + real events (stronger than brief's fake-heads sketch). Exact-equality on tracker entry, not substring |
| panels `test_a_lineage_without_a_policy_decision...` | Yes — POLICY-less excluded, empty panels | Yes | Yes | No | Proves empty is data-driven, not stub-shaped. Nice second test |
| dual-vault `test_run_weekly_survives_in_block_edit` | Yes — completes, human stands, `rules_written==[]`, 2 conflict logs with domain set, path in error | Yes — revert try → `VaultConflictError` propagates | Yes, tmp_path vault, tmp ledger | No | Real in-block edit, no monkeypatch. Proves per-domain isolation via 2 logs |
| dual-vault `test_run_weekly_continues...` | Yes — docker lands, deploy skipped, `rules_written` exact | Yes — loop-level catch/abort mutants fail the lands-half | Yes | `monkeypatch` on `engine.apply_to_playbook` (instance method) — failure injection on the collaborator, not the unit under test. Legit | Correctly explains why landing needs injection (one shared playbooks.md) |
| tier `TestCuratorSweepFailure` | Yes — errors==1, DECAY uncompleted, PRUNE_EDGES+ANALYZE completed, no raise | Yes — isolation removal propagates | Yes | `_ExplodingSweeper` is a deliberate seam double with the real signature (`dry_run` attr + `sweep(now, dry_run)`). Legit | Pins "phase error, not crash" |
| tier `TestDryRunOverride` | Yes — moved==1, 0 events, row byte-identical, then no-override control moves | Yes — ignoring per-call flag writes the row | Yes | No | Control half makes it a real pin, not a tautology |
| tier `TestInvalidTier` (3) | Yes — schema refusal + count 0; resolve TX==T2; `_due_move` tuple; not-due None | Yes — resolve drift fails tuple assert | Yes | `_due_move` private-unit pin — acceptable for the "unreachable row" truth | Two halves + fresh/due pair. Correctly does not pretend a TX row can be inserted |
| coder `test_vector_limit_one_truncates_when_two_facts_exist` | Yes — limit=1→1, limit=50→2 | Yes — limit-propagation-dropped mutant returns 2 at limit=1 | Yes, same store/embedder, distinct entity avoids quarantine | No | Discriminates propagation; does NOT discriminate the high clamp (see §3 survivor 2) |

## 2. Coverage of changed lines/branches, error paths, boundaries

- **recovery.py `_is_system_tier_lineage`**: exact-CID miss (control), TIER-only hit, mixed hit, CID-hit+non-TIER miss — both halves of the conjunction exercised. Empty-events branch (`bool(events)`) unreachable via `boot_sweep` (no heads → not scanned) — correctly untested.
- **reflection.py run_weekly**: vault-conflict skip, success append, `vault is None` append (pre-existing test), non-conflict propagation (NOT tested — broad-catch mutant survives; minor, code-review scope).
- **runner.py**: 7 redact sites — log-only, no behavioral test possible/needed; grep-verified identical shape to coder's `redact(str(exc))`. Correctly not pinned by a brittle log-content test.
- **tier.py sweep override**: `dry_run=True` on live sweeper covered; `dry_run=False` on dry sweeper (preview→live crossover) not covered — P2 §6.2.
- **limit**: propagation covered; high bound (1000→50) still vacuous — survivor 2.

## 3. Mutation thinking

| Plausible bug | Caught? | By which test |
|---|---|---|
| Phantom exemption removed / inverted / audit event re-added | Yes | phantom tests 1–3 (RESUMED/requeue/count) |
| CID-only exemption (drop TIER-only half) | Yes | `test_non_tier_event_on_the_sweeper_cid...` |
| **TIER-only-only exemption (drop CID-equality half)** | **NO — survives** | No test seeds a TIER-only lineage on a *random* CID. §5 R-W3b-001 |
| Panel builders empty-forever (`[]`/`0`/`[]`) | Yes | `test_two_open_lineages...` (len 2 / gates 1 / roles 2) |
| Awaiting = executing lineage / swapped decisions | Yes | exact `awaiting_ids` list + exact tracker dict eq |
| POLICY-less lineage emitted with nulls | Yes | `test_a_lineage_without...` |
| run_weekly guard removed / loop abort after first | Yes | both run_weekly tests (raise / second-lands) |
| Skipped rule still listed in `rules_written` | Yes | `rules_written == []` / exact docker-only |
| Sweep per-call `dry_run` ignored (constructor wins) | Yes | `TestDryRunOverride` (ledger 0 + row identical) |
| Curator phase isolation removed | Yes | `TestCuratorSweepFailure` (raise escapes) |
| `resolve("TX")` → T0/raises | Yes | tuple assert `("T2","T3",TIER_DEMOTED)` |
| Limit dropped in `_vector_fallback` propagation | Yes | two-fact limit=1 |
| **High clamp removed (`high=50`→∞)** | **NO — survives** | Old test seeds 1 fact (`count<=50` vacuous); new test maxes at 50. §5 R-W3b-002 |
| Broad `except Exception` in run_weekly (swallow OSError) | No | No non-conflict-failure test. Minor — §5 R-W3b-003 |

Survivors: **2 of 12** (both minor — the surviving mutants are in untested-but-narrow branches, and the CID-half survivor requires an implausible rewrite of the helper, but the conjunction *is* the safety property).

## 4. Flakiness evidence

- [RAN fast files ×3 → 33 passed each time]. No sleeps, no wall-clock (tier tests EPOCH-relative), no shared DB/ports. `uuid4` fact ids are fine (uniqueness only). Structlog `capture_logs` is process-local. `random` CIDs in panel/phantom tests — assertions are relational (by_id/move sets), not value-pinned. No flaky suspects.

## 5. Fail-before/pass-after audit (W3-FIX-01 claims)

- **Counts** — all confirmed by my runs: phantom 5, panels 2, dual-vault 37 (35+2), tier 26 (21+5), runtime_tools 70. Ruff 0 [RAN], mypy 3 pre-existing [RAN]. Claim "no existing test deleted/weakened, additive only" [VERIFIED via diff: only `+` hunks in the 3 test files].
- **Phantom fail-before**: new enum member `SKIPPED_SYSTEM_LINEAGE` + helper are new symbols → pre-fix failure is `AttributeError`/`ImportError` at collection, not a behavioral failure. Acceptable for a new-behavior test, but W3-FIX-01's §3 "revert recovery.py → all 5 fail (RESUMED/requeue/audit)" overstates slightly: reverting *only* the exemption block (keeping the enum) would indeed fail behaviorally — the claim holds for the exemption body, not for a full-file revert. Noted, not a defect.
- **run_weekly fail-before** — behavioral and construction-necessary (`VaultConflictError` pre-exists; no try → raise). Sound.
- **Panels fail-before** — builders pre-exist; reverting a body to empty fails by construction ([VERIFIED builders iterate real heads in diff context]). Sound — this is the TREV-W3 P1, now genuinely closed.
- **Limit / override / curator / invalid-tier** — these are *pins* of pre-existing working code (W3-FIX-01 changed no tier.py/coder code); "fails without the fix" is N/A by design and the report correctly frames them as pins. The two-fact limit test is strictly stronger than the vacuous original. Good.
- **W3-FIX-01 §4 claim "notes P1 panel happy-path tests (2 lineages, real ledger) green"** [RAN ✓]. **"115 scoped tests green"** — consistent with my 33+107=140 across a wider file set; the 115 subset isn't independently reproducible from the report's commands, but every file count it implies matches mine. Accepted.

## 6. Findings

- **R-W3b-001 (MINOR)** `tests/ledger/test_recovery_tier_phantom_p009.py`: the CID-equality half of `_is_system_tier_lineage` has no negative test — a TIER-only lineage on a random CID would be silently exempted by a CID-less mutant and no test notices. One 6-line test closes it: seed `TIER_DEMOTED` on `new_correlation_id()`, assert RESUMED + requeue. The control test only proves non-TIER lineages aren't exempted, which is the other half.
- **R-W3b-002 (MINOR)** `tests/coder/test_runtime_tools.py`: R-W3-001 is only half-fixed. The two-fact test discriminates *propagation*; the *high clamp* (1000→50, the original finding's subject) still survives — old test has 1 fact (`<=50` vacuous), new test never exceeds 50. A discriminating test needs >50 vector hits at `limit=1000` (seed 55 facts, assert count==50) — heavier but decisive; or assert the clamped value reaching the store via a counting store. Cheap partial: `limit=0`→1 is already covered by the old test.
- **R-W3b-003 (MINOR)** `tests/memory/test_dual_vault_p010.py`: no test that a *non-conflict* failure in `apply_to_playbook` (e.g. `OSError`) still propagates out of `run_weekly` — the "only `VaultConflictError` is caught" property (the no-swallowing rule) is unpinned. One failure-injected test (raise OSError → assert raises, no conflict log) closes it.
- **R-W3b-004 (NIT)** Tier `TestDryRunOverride`: only the True-on-live direction. The False-on-dry direction (`dry_run=False` override on a `TierSweeper(dry_run=True)` must move) is the curator's other composition arm — untested branch of `effective_dry` (tier.py:164).
- **R-W3b-005 (NIT)** Tier invalid-tier schema test uses `pytest.raises(Exception, match="constraint violated")` — broad + message-coupled; `sqlite3.IntegrityError` + `CHECK` would be tighter. Passes today; low value.

## 7. Missing edge cases and tests (prioritized)

**P1**: none. The TREV-W3 P1 (panel happy paths) is closed with real-ledger tests; the cheap P2s (curator failure, override, invalid tier, two-fact limit) are all delivered as sketched in TREV-W3 §7.

**P2**:
1. Phantom CID-half negative test (R-W3b-001) — ready sketch: seed TIER_DEMOTED on `new_correlation_id()` via the existing `_seed_tier_lineage` shape, assert `RESUMED==1` + `len(requeuer.calls)==1`.
2. High-clamp discrimination (R-W3b-002) — seed 55 `swift` facts (same store/embedder), `limit=1000` → assert `count==50`; `limit=51` → 50.
3. (Carried, not newly required) TREV-W3 P2 #3 vault record-window recovery, #4 hydration-DB-failure pin, #6 dict-form attention — all correctly recorded as DO-LATER in W3-FIX-01 §7; unchanged by this fix.

**P3**: False-on-dry override arm (R-W3b-004); non-conflict propagation pin (R-W3b-003 could be P3 — downgraded to P2 only because the no-broad-catch rule is a standing order).

## 8. What I could not verify

- Fail-before by actual revert (read-only; no source edits). Assessed by construction + diff; mutation table is reasoned from assertion shapes, not executed mutants.
- W3-FIX-01's `tests/ledger/` (98), `tests/improvement/` (22), `tests/tui/` (40+1 pre-existing), conventions (21) runs — I ran the 5 fix files + ruff + mypy; the wider scoped claims are consistent but not re-run by me.
- Full `pytest tests/`, PG/S3, Qdrant path — out of scoped-check scope.
- COMMS last line read: `2026-11-07T17:41:41Z W3-FIX-DOCS2 -> ORCH | DONE` (line 99).
