<!-- ORCH-DIGEST:START -->
agent: <ID> | domain: <domain> | status: DONE|PARTIAL|BLOCKED
items: <n verified EXISTS> / <n ALREADY-FIXED> / <n NOT-REPRODUCIBLE> / <n PARTIAL> / <n MISCLASSIFIED>
proposed_tasks: <n> (SURFACE <n>, DEEP <n>)
top_risks: <≤3 short items>
cross_domain_conflicts: none|<list>
out_of_plan_discoveries: <n>
confidence: high|med|low (<why>)
last_comms_line_read: <timestamp>
<!-- ORCH-DIGEST:END -->

# Investigation report — <ID> — <domain>

## 0. Scope and method
Files/dirs examined, commands run, what I did not look at and why.

## 1. Per plan item (repeat the block)
### P-xxx <title>
- **Existence verdict:** EXISTS | ALREADY-FIXED | NOT-REPRODUCIBLE | PARTIAL | MISCLASSIFIED
- **Evidence:** reproduction steps / code-path reasoning with `[VERIFIED path:line]`, `[RAN cmd -> result]`; git log/blame findings
- **Root cause chain:** symptom → mechanism → origin (and why earlier fixes, if any, didn't hold)
- **Blast radius:** per symbol/contract/config key: grep queries run, result counts, callers/consumers/tests/docs/scripts affected
- **Fix options:** option A / B (/C): approach, pros, cons, risk, effort. **Recommended:** … **Why:** …
- **Long-run test:** Source / Load / Hostile / Operability / Reversal
- **Classification:** SURFACE | DEEP (criteria hit)
- **WRITE-SET:** exact files. **CONTRACT-SET:** … **DEPENDS-ON:** … **ENV-SET:** …
- **Test plan:** test cases by category (boundary, malformed, hostile, concurrency, idempotency, partial failure, exhaustion, authz), which must fail before the fix
- **Conflicts/ordering:** other items/domains touched; must-run-before/after; hotspot files involved
- **Acceptance criteria (measurable):**

## 2. Duplicates / shared root causes across items
## 3. Out-of-plan discoveries (not auto-added to scope)
## 4. Assumptions and unknowns
## 5. Recommended task breakdown and ordering
