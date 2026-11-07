# Brief — W3-IMPL-02 (IMPL) · wave 3 · run 20261107-integration-fix

## 1. Role and mission
Mission: implement plan P-010 dual vault (T-010) — 15-file managed set (13 RFC §9 + 2 live-only), governance `vault/governance/` subdir (identity.md/strategy.md), fence conflict-error semantics, and reflection shadow-writer unification (delegate fence writing to `WorldModel`). Why: the vault is the durable belief surface; today it is a 4-member enum + one shadow writer with no conflict detection — human edits inside the fence are silently overwritten (RSK3).
Your role: implementer (coder). You edit only your WRITE-SET. Commit only your files.

## 2. Required reading (in this order, binding)
1. `docs/agent-ops/20261107-integration-fix/templates/STANDING-ORDERS.md`
2. `AGENTS.md` (atomic write via temp+`os.replace` existing guarantee; transcript/crash-safety; enums `class X(str,enum.Enum)`; vault is filesystem state, not ledger — no EventType needed)
3. `docs/agent-ops/20261107-integration-fix/00-mission.md` (baseline; `vault_path = PAA_HOME/vault` default `~/.paa/vault` — vault lives outside the repo; no vault files on disk in repo)
4. Plan `C:\Users\devan\.opencode\plan\2026-11-07-integration-fix.md` — §E P10 row VERBATIM (15 = 13 RFC + 2 live-only; 11 new files; overlap 2; governance subdir; precedence chain human > governance > managed-block; fence conflict = named-path error, never silent overwrite) + §H T11 + §K (`vault_sync.enabled=False` until one supervised manual sync passes) + §I (fence conflict row) + §J (dual vault doubles doc-sync surface)
5. ADR `docs/adr/0038-dual-vault-and-governance-sync.md` (FULLY — conflict definition, dead-writer wiring decision for `update_belief_state` zero callers, governance placement plan-original not RFC §9, `source` header write, vault seed mechanism decision). Where ADR and plan disagree, ADR wins; record discrepancies as FINDINGs, do not edit the ADR.
6. Investigation: `docs/agent-ops/20261107-integration-fix/reports/w0-investigation/W0-INV-01.md` §P-010 (live enum 4 files :42-48; `path_for` flat join :85-90; fence mechanics `_BEGIN/_END`, `_block_re`, unconditional `_replace_block`; `update_belief_state` ZERO production callers; reflection shadow writer `apply_to_playbook` :206-230 + duplicate `_write_managed_block` :248-270 + private-method use; `anti_goals.md` ⟂ enum reader `runtime.py:594-608`; `_new_document(..., source)` ignores `source` :147-154; Fix A recommended: enum +11 + subdir-aware `path_for` + conflict semantics; vault seed UNKNOWN resolved by ADR-0038 — follow it) + §5 task 5/6.
7. W-wave evidence: `docs/agent-ops/20261107-integration-fix/reports/w1-implementation/W1-IMPL-01.md` (VaultSyncSettings declared — `config.py` CONSUME only) + `reports/w2-implementation/` skim for anything touching reflection/world_model (none — you are the first writer).
8. Template: implementation report ORCH-DIGEST first.
9. Code at HEAD (verify line numbers yourself): `src/paa/memory/world_model.py` (full — enum, `WorldModel`, atomic write, `_new_document`), `src/paa/improvement/reflection.py` (`apply_to_playbook`, `_write_managed_block`, `_extract_block` use), `src/paa/cli/main.py` (`BeliefDocument(doc)` coercion — additive enum change widens CLI acceptance), `src/paa/runtime.py` `_load_anti_goals` (verify-only), `src/paa/config.py` VaultSyncSettings (read-only), existing world_model/reflection tests.

## 3. Task and acceptance criteria
Task T-010-P010. Specifically:
- `BeliefDocument` enum: +11 RFC-only members (values = filenames), zero removals → total managed 15 (`len(BeliefDocument) == 15`). CLI coercion `BeliefDocument(doc)` now accepts the 11 new names, none removed (additive).
- `path_for`: subdir-aware — governance files (`identity.md`, `strategy.md` — NOT enum members; they are governance-only flat paths under `vault/governance/`) resolve under the subdir; the managed 15 stay flat under the vault root (enum metadata carries `subdirectory: str | None` ONLY for governance entries — but governor files are not enum members, so implement the subdir rule as the ADR decides it: named method/branch for governance paths; do NOT add identity/strategy to the enum). All existing `path_for` callers keep working (read_managed/read_full/update_belief_state/_engine bookkeeping).
- Conflict semantics (per ADR-0038): human edit INSIDE the managed block since last atomic write → conflict error with paths named (`ValueError` naming the path — new, currently no exception path), never silent overwrite; human edit OUTSIDE the block → merge-preserve as today. Implement the ADR's exact definition of "since last write" (hash/mtime/sidecar — follow the ADR, do not invent); `_replace_block` unconditional `re.sub` gets the check in front of it (single choke point).
- Reflection unification: `ReflectionEngine.apply_to_playbook` delegates to `WorldModel.update_belief_state` for `playbooks.md`; delete the duplicate `_write_managed_block` (164→198's second implementation) + the private-method `_extract_block` use. Prove single-writer: `grep -rn "_write_managed_block" src/` → 0 defs; playbook writes flow through one fence implementation. (P-013 W4 will add weekly wiring + `ReflectionSettings` consumption to this file — your delegation is its precondition; keep `apply_to_playbook`/`run_weekly` signatures unchanged.)
- `_new_document(..., source)`: write `source` into the generated header (provenance for sync debugging — INV-01 cosmetic fix, ADR-0038 includes it).
- Vault seed: implement ADR-0038's seed mechanism only (e.g. `_ensure_vault_seed` on WorldModel construction vs operator copy — follow the ADR; if the ADR delegates seeding to the operator, implement the READ/CONFLICT side only and record the seed choice). The 11 new managed files + 2 governance files materialise per the ADR (test setup must create a tmp vault — never write to `~/.paa/vault` or the repo).
- `config.py` VaultSyncSettings: CONSUME only (constructor param or sync-entry gate — follow ADR-0038; do not edit config). Sync off (`vault_sync.enabled=False`) leaves files intact (test).
- `runtime.py::_load_anti_goals` + `cli/main.py` world-model path: verify-only (document the anti_goals fence-vs-line-reader agreement in your report; if the fence breaks the anti-goals reader, that is a FINDING — do not silently change runtime.py, it is outside your write-set).
- Tests (must FAIL before, pass after): T11 — `len(BeliefDocument)==15`; 15-file set + 2 governance files present after setup (tmp vault); fence-conflict edit raises named-path error (no silent overwrite); edit OUTSIDE fence merges/preserves; idempotent double-sync → byte-identical files; crash mid-`_atomic_write` → old file intact + temp cleanup; marker-injected human text rejected (not honored as fence); path traversal via CLI arg rejected (enum closed-set — new names likewise); sync-off leaves files intact; `playbooks.md` single-writer (grep def count = 1 implementation). New test file `tests/memory/test_dual_vault_p010.py`.
- Trace tag `PLAN:integration-fix:P010` in code comments.
- Acceptance: T11 green with fail-before shown; reflection tests green unchanged (except any that pinned the duplicate writer — update deliberately with justification if so); ruff 0, mypy total 3; `grep -rn "_write_managed_block" src/` = 0; no `config.py`/`runtime.py` edits.

## 4. Ownership
WRITE-SET (only these): `src/paa/memory/world_model.py`, `src/paa/improvement/reflection.py`, `tests/memory/test_dual_vault_p010.py` (new). READ-ONLY: everything else. FORBIDDEN: `src/paa/config.py`, `src/paa/runtime.py`, `src/paa/cli/main.py`, `docs/adr/*`, new vault template files under the repo (vault is runtime state — seed mechanism only, per ADR-0038).
Branch: `ops/20261107-integration-fix` (verify before committing). Worktree: current directory. CLAIMS: COMMS CLAIM before editing `reflection.py` (shared with W4 P-013 later — sequence by function: fence delegation now, weekly wiring W4).

## 5. Parallel siblings this wave
W3-IMPL-01 (tier sweeper — `memory/tier.py`/`curator.py`), W3-IMPL-03 (panels — `tui/*`/`api/*`), W3-IMPL-04 (coder fallback — `coder/runtime_tools.py`/`runtime.py`). Disjoint; you own `world_model.py` + `reflection.py` exclusively this wave.

## 6. Skip list and known traps
- Dead-writer trap: `update_belief_state` has NO production callers today. The ADR-0038 wiring decision decides who calls it (sync loop vs CLI). If the ADR leaves wiring to a later task, the 11 new files are orphans by ADR design — record that explicitly; do not invent a daemon loop here.
- "Recommended files" RFC heading vs governed enum: `anti_goals.md` is RFC §9 + runtime-read but not currently enum-governed — your enum extension governs it; ensure `path_for` maps it flat under the vault root and the line-reader keeps working (verify, FINDING if broken).
- Fence marker spelling must agree byte-for-byte between the two old implementations (now one) — delegation kills the drift; tests must pin the marker spelling once.
- Baseline failures A–D are not yours. Never write to the operator's real vault or the repo.

## 7. Environment and tests
Scoped only: your new test file + `tests/improvement/test_improvement.py` + `tests/memory/` touch suites it shares (none — but run `tests/memory/test_decay.py` as a guard) + ruff/mypy scoped. Tmp vaults only. No full suite, no containers.
Commit protocol: code+tests commit first, then report in a separate docs-only commit. Trailers `Task: T-010-P010`, `Agent: W3-IMPL-02`, `Wave: W3`, `Report: docs/agent-ops/20261107-integration-fix/reports/w3-implementation/W3-IMPL-02.md`.

## 8. Deliverables
Report: `docs/agent-ops/20261107-integration-fix/reports/w3-implementation/W3-IMPL-02.md` (create at start, ORCH-DIGEST first, append as you work; long-run test answers: source/load/hostile/operability/reversal). COMMS ≤6 one-line appends. Final message ≤8 lines: `STATUS | agent | task | report path | commit SHAs | blockers`.
