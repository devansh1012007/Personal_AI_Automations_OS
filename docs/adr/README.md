# Architecture Decision Records

Every place where this implementation departs from the v4.0 RFC is recorded
here, with the reasoning and the trade-off accepted. The rule is: **no silent
deviations**. If the code does something the RFC did not ask for, there is an
ADR explaining why, and the source cites it inline as
`SPEC DEVIATION (docs/adr/NNNN)`.

Deviations fall into three classes:

| Class | Meaning |
|---|---|
| **Hardware/platform** | The RFC assumed a workstation with CUDA and Docker. The target is a Windows laptop with a 2 GB AMD iGPU, ~3.5 GB free RAM, and no Docker. |
| **Correctness** | The RFC's specification, taken literally, does not work — a constraint that deadlocks, a formula with wrong dimensions, an index that grows without bound. |
| **Direction** | The user explicitly asked for something different from the RFC. |

## Index

| ADR | Title | Class |
|---|---|---|
| [0001](0001-sqlite-over-postgres.md) | SQLite (WAL) as the relational substrate | Hardware |
| [0002](0002-embedded-vector-store.md) | Qdrant embedded mode, with a numpy fallback | Hardware |
| [0003](0003-graph-engine.md) | KuzuDB embedded, with a SQLite recursive-CTE fallback | Hardware |
| [0004](0004-content-addressed-cold-lake.md) | Content-addressed filesystem store instead of MinIO | Hardware |
| [0005](0005-durable-queue-over-redis.md) | SQLite-backed durable queue instead of Redis Streams | Hardware |
| [0006](0006-sandbox-without-gvisor.md) | Pluggable sandbox; gVisor is unavailable on Windows | Hardware |
| [0007](0007-model-provider-abstraction.md) | Provider abstraction instead of pinned vLLM + Llama-3-8B | Hardware |
| [0008](0008-idempotency-key-with-attempt.md) | Idempotency key must include an attempt counter | **Correctness** |
| [0009](0009-peak-rss-not-integral.md) | Peak RSS, not the integral of memory over time | **Correctness** |
| [0010](0010-head-projection-not-partial-index.md) | Bounded head projection; `EXECUTION_COMPLETED` is not terminal | **Correctness** |
| [0011](0011-optional-router.md) | The router is optional, not mandatory | Direction |
| [0012](0012-attention-score-division.md) | `A_score` denominator cannot be a raw probability | **Correctness** |
| [0013](0013-hybrid-retrieval-decay.md) | Temporal decay applies to the whole retrieval score | **Correctness** |
| [0014](0014-memory-domains-consolidated.md) | 18 memory domains map to 6 mechanisms | Direction |
| [0015](0015-capability-ceiling.md) | On matching Claude Code: what architecture can and cannot buy | **Honesty** |
| [0019](0019-docker-server-deployment.md) | Dual embedded/server topology for the Docker deployment | Direction |

## Template

```markdown
# ADR-NNNN: Title

## Status
Accepted | Superseded by ADR-MMMM

## Context
What the RFC specifies, and what forced a decision.

## Decision
What we do instead.

## Consequences
What this costs, and what it would take to move back to the RFC's design.
```
