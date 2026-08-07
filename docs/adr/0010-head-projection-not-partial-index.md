# ADR-0010: Bounded head projection; `EXECUTION_COMPLETED` is not terminal

## Status

Accepted.

## Context

Two related defects in the RFC's recovery design.

### 1. The recovery index grows without bound

RFC §1.1.1:

```sql
CREATE INDEX idx_ledger_recovery_sweep ON system_state.ledger(sequence_id ASC)
    WHERE event_type NOT IN ('EXECUTION_COMPLETED', 'EXECUTION_FAILED');
```

A partial index only excludes a row when that row stops matching the
predicate. But the ledger is **append-only** — rows are never updated. So
every `TASK_REQUESTED`, `CONTEXT_HYDRATED`, `PLAN_COMPILED`,
`EXECUTION_STARTED` … row that any task *ever* emitted stays in this index
permanently, including rows belonging to tasks that finished months ago.

The index therefore grows linearly with total system history, not with
in-flight work. On a runtime designed to ingest continuous telemetry, boot
recovery would get monotonically slower forever — the one operation that must
stay fast, because it runs when the user is waiting to get back to work.

### 2. `EXECUTION_COMPLETED` cannot be a lineage terminal

The same index treats `EXECUTION_COMPLETED` as closing a task. But the RFC's
own happy path (§7) continues past it:

> … the sandbox captures the process output, routes it through the AST parser
> and local unit tests, and commits a `CRITIQUE_CONCLUDED` state log. … the
> host application … records `MUTATION_COMMITTED` to close the lifecycle loop.

Both cannot be true. And on a multi-step plan the problem is concrete:
`EXECUTION_COMPLETED` fires **once per step**. Treating it as terminal closes
the lineage after step 1, so recovery marks a half-finished task as done and
silently skips every remaining step — a data-loss bug that only manifests
after a crash, which is precisely when it is hardest to notice.

This was caught by a test, not by inspection:
`test_state_survives_process_death` recovered zero lineages when it should
have recovered one.

## Decision

**Replace the partial index with a mutable head projection.**
`system_state_correlation_head` holds exactly one row per correlation,
upserted inside the same transaction as the append. Recovery scans
`WHERE is_terminal = 0`, which is bounded by in-flight work — typically single
digits — regardless of how much history exists.

This is a standard event-sourcing read model. The ledger stays append-only and
authoritative; the projection is a derived index that can be dropped and
rebuilt by replay at any time.

**Remove `EXECUTION_COMPLETED` from `TERMINAL_EVENTS`.** Only
`MUTATION_COMMITTED` closes a successful lineage. Step-level completion is
tracked in the projection's `completed_steps`, and `replay()` keeps the phase
at `EXECUTING` until `len(completed_steps) == len(plan_steps)`.

## Consequences

- Boot sweep cost is proportional to open tasks, not to lifetime history.
  `test_head_does_not_grow_with_history` asserts one head row after 40 events.
- A task that finishes every step but never commits stays open and gets
  resumed. That is correct: without `MUTATION_COMMITTED` the work was never
  applied to disk.
- The head row can lag the ledger if a crash lands between the insert and the
  upsert. `recover_correlation` detects this (`ALREADY_TERMINAL`) and repairs
  the projection rather than reprocessing the task.
- Cost: one extra upsert per append. Negligible against the WAL write already
  in flight, and it buys a bounded recovery scan.
