<!-- ORCH-DIGEST:START -->
agent: <ID> | scope: <wave/cluster/tasks> | verdict: APPROVE|APPROVE-WITH-CHANGES|REJECT
tests_reviewed: <n> | fail-before/pass-after confirmed: <n> / reasoned: <n> / not-possible: <n>
findings: BLOCKER <n> | MAJOR <n> | MINOR <n> | NIT <n>
flaky_suspects: none|<list>
missing_edge_cases: P1 <n> | P2 <n> | P3 <n>
mutation_survivors: <n of m plausible bugs not caught>
changed_behavior_without_tests: none|<list>
confidence: high|med|low (<why>)
<!-- ORCH-DIGEST:END -->

# Test quality review — <ID>

## 0. Method (temporary worktree used? repeat counts, coverage tooling)
## 1. Per test/suite assessment
| Test | Asserts behavior? | Fails without fix? | Deterministic/isolated? | Over-mocked? | Notes |
|---|---|---|---|---|---|
## 2. Coverage of changed lines/branches, error paths, boundaries
## 3. Mutation thinking
| Plausible bug (off-by-one, inverted condition, missing await, dropped error, wrong default, …) | Caught? | By which test |
|---|---|---|
## 4. Flakiness evidence (repeated runs) and test-suite health (slow, order-dependent, shared state)
## 5. Findings (R-W<N>-nnn, severity, location, evidence, recommended fix)
## 6. Missing edge cases and tests (prioritized)
P1 (must add) / P2 (should add) / P3 (nice to have). For each: scenario, input/case table, expected behavior, where it belongs, ready-to-write sketch.
Categories to consider: boundaries; empty/null/duplicate; unicode/encoding; oversized payloads; malformed/hostile input (injection, traversal, ReDoS); concurrency/races; idempotency/retry/replay; partial failure/timeouts; resource exhaustion/rate limits; authz bypass; backward compatibility; clock/timezone.
## 7. What I could not verify
