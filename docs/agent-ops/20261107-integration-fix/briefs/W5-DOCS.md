# Brief — W5-DOCS (IMPL) · wave 5 · run 20261107-integration-fix

## 1. Role and mission
Docs sweep: (a) walk-and-verify the ~65 lock-blind stale citations (R-W4b-003) SEMANTICALLY (each citing sentence re-read; re-point if wrong, record if right), (b) file ADR-0029 amendment as `docs/adr/0044-*` + index (the normalised-columns closure), (c) re-point any lock-visible citations the W5 code waves break. Why: the lock only catches blank/past-EOF; the doc-truth half is human work and this is the last sweep before the final review. The ADR amendment closes the P-003 story with a citable decision record.
Your role: implementer (coder), docs-only. You edit only your WRITE-SET. Commit only your files.

## 2. Required reading (in this order, binding)
1. `docs/agent-ops/20261107-integration-fix/templates/STANDING-ORDERS.md`
2. `AGENTS.md` (`A path:line is a promise`; "A deviation without an ADR is a review failure"; docs by line number — the file's append-at-end rule: do NOT insert rules mid-file)
3. `docs/agent-ops/20261107-integration-fix/reports/w4-review/REV-W4b.md` §4 R-W4b-003 + §8 (~65 claims) + `W4-FIX-DOCS.md` / `W4-FIX-DOCS2.md` (their enumeration process — reproduce the list, do not copy it blind: the "65" is an enumeration from W4 HEAD; your RUN list is W5 HEAD)
4. `docs/agent-ops/20261107-integration-fix/reports/w4-implementation/W4-IMPL-03.md` (stored-normalisation what/where — your amendment ADR's evidence base) + ADR `docs/adr/0029-entity-resolution-parity.md` limitations (a)-(d) + prescription :551-556 + `docs/adr/0043-*`/`0035-*` as style exemplars
5. `tests/conventions/test_docs_references.py` (lock + exclusions + the agents.md:59 backtick-wrap invisibility class)

## 3. Task and acceptance criteria
Task W5-DOCS. Specifically:
- **Sweep (a)**: enumerate every `path:line` citation in the lock's scope (run the lock's own extractor approach — read the checker to reproduce its scope, or run a targeted grep/python one-off in your session dir and paste the list into your report); read EACH citing sentence + the cited line at HEAD; classify Correct / Stale-but-non-blank (re-point by reading, W1 precedent) / Unresolvable (target content gone → NEED + record, do not invent). Target: zero Stale. Fix the `agents.md:59`-class invisible references too (backtick-wrapped refs the lock cannot see — read agents.md:59 and verify its claims to reality).
- **Amendment (b)**: new `docs/adr/0044-*.md` amending ADR-0029: what shipped (stored normalised columns, Python backfill v6, entity path points at norm columns, alias blob escape moot for the gate, graph second writer included, U+0130 closed, other call sites residual), Context/Decision/Alternatives/Consequences, with exact `path:line` cites that MUST pass the lock (lines you write must exist and be non-blank — verify before committing; this is where previous waves' ADRs got caught — check each cite by reading the target line). Index it in `docs/adr/README.md` in the existing row format. **Amendment content source**: the P-003 implementation report `W4-IMPL-03.md` + ADR-0029 itself. (New ADR numbers: 0044 is verified free at dispatch — glob `docs/adr/` to confirm no intervening file; if 0044 exists, STOP and NEED.)
- **Lock-visible (c)**: after W5 code waves land (check `git log --oneline -6` when you run the lock), re-point any citations they break. If W5 code is still landing, record the residual list for the orchestrator's final gate (do not edit src/tests).
- Also**: file the ADR-0029 amendment's TEST companion: none (docs-only; the tests shipped in W4).
- Acceptance: sweep report table (cited file:line → Correct / re-pointed old→new / Unresolvable) in your report; lock all-pass at your final re-run (or exact residual); ADR-0044 + index row present; ruff/mypy untouched-by-you (docs-only — state so).

## 4. Ownership
WRITE-SET (only these): paths under `docs/` EXCEPT `docs/agent-ops/**` and `docs/plans/**` (includes `docs/adr/0044-*`, `docs/adr/README.md`). FORBIDDEN: `src/**`, `tests/**`. New ADR number: 0044 (verified free at dispatch; re-glob before writing).
Branch: `ops/20261107-integration-fix` (verify before committing). Worktree: current directory.

## 5. Parallel siblings this wave
W5-IMPL-01 (cost gate — metrics/curator), W5-IMPL-02 (coding pilot), W5-IMPL-03 (allocator scheduling). Disjoint: you edit zero of their files. Gate timing per §3 (re-run the lock at the end, after checking whether W5 code landed).

## 6. Skip list and known traps
- Sweep scope is exactly the lock's scope (checker exclusions apply: `docs/agent-ops/` + `docs/plans/` are out; `docs/adr/0044` cites are IN — they must pass the lock like everything else).
- The amendment ADR records the closure; it does NOT re-litigate normalisation design (W4 decided it; cite, don't relitigate).
- ~65 is an old enumeration: your RUN list governs; if it differs, report the delta (HEAD moved).
- Target gone = UNRESOLVABLE + NEED. Never blind +N.

## 7. Environment and tests
Scoped only: `pytest tests/conventions/test_docs_references.py -q`. No full suite.
Commit protocol: docs commit first, then report in a separate docs-only commit. Trailers `Task: W5-DOCS`, `Agent: W5-DOCS`, `Wave: W5`, `Report: docs/agent-ops/20261107-integration-fix/reports/w5-implementation/W5-DOCS.md`.

## 8. Deliverables
Report: `docs/agent-ops/20261107-integration-fix/reports/w5-implementation/W5-DOCS.md` (create at start, ORCH-DIGEST first, append as you work; full sweep table + ADR-0044 verification). COMMS ≤6 appends. Final message ≤8 lines: `STATUS | agent | task | report path | commit SHAs | blockers`.
