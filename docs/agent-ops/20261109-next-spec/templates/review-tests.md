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
## 0. Method
## 1. Per test/suite assessment
## 2. Coverage of changed lines/branches
## 3. Mutation thinking
## 4. Flakiness evidence
## 5. Findings
## 6. Missing edge cases and tests (prioritized)
## 7. What I could not verify
