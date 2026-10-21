## D1. Design decision record (written BEFORE coding)
Problem and constraints · invariants that must hold · options with tradeoffs · decision and rationale · what was explicitly NOT done and why
## D2. Failure-mode table
| Failure / abuse scenario | Behavior before | Behavior after | Covered by test |
|---|---|---|---|
## D3. Load and scalability analysis
Complexity, hot paths, contention, memory, backpressure; what changes at 10x/100x.
## D4. Security / threat model
Trust boundaries, attacker capabilities, mitigations, residual risk.
## D5. Rollout, migration and rollback plan
Ordering, flags (non-masking), data migration, compatibility window, how to revert safely.
## D6. Observability
Logs/metrics/alerts added or needed.
## D7. Open risks and follow-ups (ticket-ready)
