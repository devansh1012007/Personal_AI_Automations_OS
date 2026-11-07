# Brief — W5-IMPL-03 (IMPL) · wave 5 · run 20261107-integration-fix

## 1. Role and mission
Mission: T-007b closer — (a) schedule the allocator loop (`run_allocation` daemon tick, R-W4-002), (b) name the `workers:alloc` producer decision (or the ADR-0036 amendment recording operator-fed — NO invented producer), (c) populate W3's honest-null `attention_top` from the live allocator (P-006 → P-007 fill), (d) emit `DRAIN_THROTTLED` from the creator's ledger handle (R-W4-006). Why: callable-only code with no scheduler repeats the P-013 disease ("engine tested, never scheduled"); `attention_top` must not stay null forever.
Your role: implementer (coder). You edit only your WRITE-SET. Commit only your files.

## 2. Required reading (in this order, binding)
1. `docs/agent-ops/20261107-integration-fix/templates/STANDING-ORDERS.md`
2. `AGENTS.md` (zero-baseline; single-daemon philosophy; ledger-before-side-effect; EventType checklist — DRAIN_THROTTLED already exists: add NO new member)
3. `docs/agent-ops/20261107-integration-fix/00-mission.md` (baseline; A-005)
4. Plan `C:\Users\devan\.opencode\plan\2026-11-07-integration-fix.md` — §E P07 (`allocate_attention()` consumes `workers:alloc`, controller top3-5→notify; starvation fallback; `allocator.enabled`, observability `allocator.*`) + §H T07/T08 + §K (flag off = safe)
5. ADR `docs/adr/0036-allocator-and-attention-loop.md` (FULLY — loop shape, notify semantics, observability, DRAIN_THROTTLED consequences incl. "two new EventType members as the implementer splits them"; R-W4b verdict: vocab-only is sound SOUND, but a ledger task must own the emitter — you are that task)
6. Reviews: `docs/agent-ops/20261107-integration-fix/reports/w4-review/REV-W4.md` §4 R-W4-002 (scheduling + producer), R-W4-006 (emitter still unowned), R-W4-007 (claim guard — DONE, respect it); `REV-W4b.md` §8 (P-006→P-007: `attention_top` stays honestly null UNTIL this task); `reports/w4-implementation/W4-IMPL-01.md` §5 + F4 (notify at-most-once semantics + `run_allocation` wiring + creator has no ledger)
7. W-wave evidence: `reports/w4-implementation/W4-IMPL-02.md` (P-013 daemon-loop shape — MIRROR it, do not invent a second scheduler shape), `reports/w3-implementation/W3-IMPL-03.md` (`attention_top` null + per-panel isolation + counts builder).
8. Template: implementation report ORCH-DIGEST first.
9. Code at HEAD (verify line numbers yourself): `src/paa/daemon.py` (the loops incl. the W4 reflection loop — add the allocation tick following its shape; ordering vs maintenance?), `src/paa/agents/orchestrator.py` (`run_allocation`), `src/paa/tui/runner.py` (`attention_top` population site + `_POLICY` heads), `src/paa/memory/creator.py` throttle (the `DRAIN_THROTTLED` emit point — does the creator hold a ledger handle? read `_build_memory_creator`/`self._ledger` or equivalent; if NO handle exists, the emitter needs the smallest plumbing — read the construction sites, do not invent a global), `src/paa/context/allocator.py` (consumer + notify contract), `src/paa/config.py` (`AllocatorSettings` read-only).

## 3. Task and acceptance criteria
Task T-007b-P007b. Specifically (4 sub-parts, do all 4 or record the undone half with a NEED):
- **Scheduling (R-W4-002)**: daemon tick calling `orchestrator.run_allocation` (mirror the reflection 4th-loop shape: supervised, gated by `AllocatorSettings.enabled`, interval per the daemon's existing spacing idiom, errors logged + tick continues, claim-failure underfed semantics preserved). Disabled (current default `enabled=False`) → loop never fires (production inert). Order AFTER maintenance/curator like the reflection loop (verify the seam with the daemon file). Test: enabled + fake queue non-empty → tick allocates + notifies; disabled → queue untouched.
- **Producer decision (R-W4-002)**: the queue needs a producer for the loop to be live. Decide per ADR-0036: if ADR-0036 names a producer payload/owner, implement exactly that (controller notify = THE producer — the consumer side already notifies; the producer is what ENQUEUES work items). If ADR-0036 is silent, the honest options are (i) operator-fed: document in the allocator module docstring + your report that `workers:alloc` is operator-fed (CLI/signal ingress — the ingress path, name it precisely: `creator.py` ETL enqueues? signal path? — read the ETL and name the exact enqueue site or state operator-fed explicitly), or (ii) a named ingress producer if one exists. What is FORBIDDEN: inventing a synthetic producer (a poll-loop that fabricates work items) or claiming the loop is "live" with an empty-handed design. Record the decision + evidence in your report; if (i), add a code comment citing ADR-0036 + the missing-producer state so W6/final review cannot mistake it for live.
- **attention_top fill (P-006→P-007)**: populate `runner._build_info` (or the panel builder) `attention_top` from the live allocator output shape (the `AllocationResult`/decision events — read the allocator's return payload; rank by score desc, `max_results` bound). Tests: allocated state renders ranked rows; empty allocator → "No data yet" (back to honest-null); the W3 null test stays green (it pins the empty shape).
- **DRAIN_THROTTLED emitter (R-W4-006)**: emit `DRAIN_THROTTLED` from the creator drain-throttle path (the vocab + replay exist; the creator needs the ledger handle — smallest plumbing from the construction sites; if NO handle exists and plumbing exceeds one callsite, that is a NEED + stop that half and record it — do NOT invent a global ledger). Emit ONLY on breach (throttle fires), payload per ADR-0036 (read it; if ADR-0036 is silent on payload, mirror the tier-throttle event shape `{fact/threshold}` + discriminator per the ADR-0008 pattern and say so). Idempotent by full key. Test: breach → 1 event + log; no-breach → 0 events.
- Tests: scheduling (enabled/disabled), producer-decision (enqueue-path behavior test or operator-fed comment test — whichever you implement), attention_top ranked/empty, DRAIN_THROTTLED emit/no-emit. New `tests/context/test_allocator_scheduling_p007b.py` (+ panel append into `tests/tui/test_dashboard.py`).
- Trace tag `PLAN:integration-fix:P007`.
- Acceptance: daemon tick test green; attention_top ranked test green; DRAIN_THROTTLED emit test green (or NEED with evidence); gatherer/allocator suites green unmodified; ruff 0, mypy total 3; no config/schema/EventType edits.

## 4. Ownership
WRITE-SET (only these): `src/paa/daemon.py` (allocation tick only), `src/paa/tui/runner.py` (`attention_top` population only), `src/paa/memory/creator.py` (DRAIN_THROTTLED emit only), `src/paa/context/allocator.py` (comments + producer-decision documentation only — NO logic change without NEED), `tests/context/test_allocator_scheduling_p007b.py` (new), `tests/tui/test_dashboard.py` (append only). READ-ONLY: everything else. FORBIDDEN: `src/paa/config.py`, `src/paa/core/types.py`, `src/paa/ledger/*`, `src/paa/agents/orchestrator.py` (verify-only), `docs/adr/*` (ADR amendment = NEED), new scheduler machinery beyond mirroring the reflection loop.
Branch: `ops/20261107-integration-fix` (verify before committing). Worktree: current directory. CLAIMS: COMMS CLAIM before editing `daemon.py` (shared with reflection loop — sequence by function) + `creator.py`.

## 5. Parallel siblings this wave
W5-IMPL-01 (cost gate — `metrics.py`/`curator.py`), W5-IMPL-02 (coding pilot — `types.py`/`runtime.py`/`orchestrator.py` — note: orchestrator.py (run_allocation) is YOUR read-only; their block-emit is theirs — different functions, sequence by function), W5-DOCS (docs only). Disjoint.

## 6. Skip list and known traps
- Producer honesty (§3): a fabricated producer is a defect; operator-fed with a pointer is the fallback; "loop live" claims need an executable enqueue→claim path or an explicit operator-fed statement.
- `attention_top` before this task is null BY DESIGN (W3). Populating it changes the W3 pin — update that pin deliberately (append, don't rewrite: the null-shape test must still pass for the empty state).
- Daemon ordering: allocation AFTER maintenance/curator (same as reflection; no parallel-prune race for the queue's sake — verify the seam).
- DRAIN_THROTTLED: only on breach; full-key idempotency; do not emit from the log-only path.
- Baseline failures are not yours.

## 7. Environment and tests
Scoped only: your new test files + `tests/context/test_allocator.py` + `tests/tui/` + `tests/context/` + ruff/mypy scoped. Tmp fixtures only. No full suite.
Commit protocol: code+tests commit first, then report in a separate docs-only commit. Trailers `Task: T-007b-P007b`, `Agent: W5-IMPL-03`, `Wave: W5`, `Report: docs/agent-ops/20261107-integration-fix/reports/w5-implementation/W5-IMPL-03.md`.

## 8. Deliverables
Report: `docs/agent-ops/20261107-integration-fix/reports/w5-implementation/W5-IMPL-03.md` (create at start, ORCH-DIGEST first, append as you work; long-run answers). COMMS ≤6 appends. Final message ≤8 lines: `STATUS | agent | task | report path | commit SHAs | blockers`.
