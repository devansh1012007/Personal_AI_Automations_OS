# ADR-0008: Idempotency key must include an attempt counter

## Status

Accepted.

## Context

RFC §1.1.1 specifies the ledger's deduplication key as:

```
idempotency_hash VARCHAR(64) NOT NULL,  -- SHA-256(correlation_id + event_type + state_version)
CONSTRAINT unique_idempotency_hash UNIQUE (idempotency_hash)
```

Taken literally this is unimplementable, and the failure mode is severe.

`state_version` is never defined anywhere else in the RFC. There are two
readings, and both break:

**Reading A — `state_version` is a per-correlation monotonic counter.**
Then the key is a function of a value that increases with every append, so
*every* event is unique and the constraint deduplicates nothing. The stated
purpose ("completely eliminate race conditions and execution drift") is not
achieved at all.

**Reading B — `state_version` identifies a logical stage.**
Then the key is effectively `(correlation_id, event_type)`, and a given event
type can be recorded **at most once per task, ever**. This is the reading the
comment implies, and it is actively harmful:

- A task whose first `EXECUTION_STARTED` failed can never emit a second one.
  The `UNIQUE` constraint rejects the retry.
- RFC §2.1 grants workers "max 2 execution retry passes", §9.2 budgets
  retries per modality, and §6 dedicates an entire Redis stream
  (`queue:retry:failed`) to re-runs. None of those can produce a ledger entry.
- Worst of all, recovery (RFC §7 "Sad Path") re-queues crashed tasks — and the
  re-queued task's first act is to append `EXECUTION_STARTED`, which collides.
  **The mechanism deadlocks on exactly the tasks it exists to rescue.**

There is a second, subtler problem. A single plan legitimately emits the same
event type concurrently for different steps: `EXECUTION_STARTED` for step 3
and step 4 are distinct facts, not duplicates.

## Decision

The key is:

```python
sha256(f"{correlation_id}|{event_type}|{attempt}|{discriminator}")
```

- **`attempt`** — incremented by the caller on each genuine retry. Retry *n*
  is a distinct event; a *redelivery* of attempt *n* is still suppressed. This
  is what separates "the transport delivered this twice" from "the work was
  attempted twice", which the RFC's scheme conflates.
- **`discriminator`** — an optional caller-chosen natural key, typically the
  step index. Distinguishes legitimately concurrent same-typed events.

The event payload is deliberately **excluded** from the key. Payloads routinely
carry timestamps, durations and host paths, so hashing them would make every
redelivery look novel and defeat deduplication entirely — reintroducing
Reading A's failure through the back door.

On a duplicate, `LedgerStore.append` returns the **existing** event rather than
raising. Suppression is the normal outcome of at-least-once delivery; forcing
every caller to catch an exception on the happy path would be noise. Note that
the original payload wins — last-write-wins would let a redelivery silently
rewrite history.

## Consequences

- Retries and recovery both work. `tests/ledger/test_store.py::TestIdempotency`
  covers exact redelivery, genuine retry, concurrent-step discrimination, and
  20 racing coroutines collapsing to one row.
- Callers must pass `attempt` when they mean "try again". Forgetting it means
  the retry is silently suppressed. This is the one sharp edge, and it is why
  `append` logs at debug on every suppression.
- The `(correlation_id, state_version)` UNIQUE constraint is retained
  separately, which gives the ordering guarantee the RFC seemed to want from
  the idempotency key.
