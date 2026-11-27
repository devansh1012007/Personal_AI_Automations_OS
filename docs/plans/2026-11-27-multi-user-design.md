# Multi-user PAA — design-level only (Wave 9, C9.6)

Status: DESIGN. No production code. The spec's own rule gates this work:
"no team features before single-user daily use is proven (G1's spirit,
extended)" (`docs/plans/2026-11-21-next-spec-2.md` §8). This document is
the design ready for the day that gate lifts — nothing here is
implemented, and nothing here should be read as a claim that it is.

## The problem, stated honestly

PAA today is single-user by construction, not by policy: one `PAA_HOME`,
one ledger, one workspace root, one policy mode. Every "user" concept the
runtime has is the operator. Multi-user is not "add a users table" — it is
the question of which of the runtime's single-owner invariants are
load-bearing for its guarantees, and what each one costs to namespace.

## The namespace inventory (what a user would own)

| Surface | Today's owner | Namespacing cost | Notes |
|---|---|---|---|
| Ledger (events, lineages) | the operator | medium | Per-user ledger stores; the event vocabulary is unchanged — a user is a scope, not a schema change. The N2.3 recovery exemptions and the boot sweep must be per-store. |
| Memory (vault, graph, embeddings) | the operator | high | The README's own boundary: memory is identity. Per-user vaults are cheap; the Kùzu graph and the embedding index are shared-resource questions (per-user databases vs. per-user labels — the label route keeps the engine warm but makes every traversal a policy check). |
| Workspaces | the operator | low | `PAA_HOME/workspaces/<user>/` — the sandbox's path jail already enforces containment; the workspace root becomes a per-user root resolved at session open, not a global. |
| Skills, installed pipelines, keys | the operator | low | Per-user `PAA_HOME` trees wholesale — the simplest correct model: a user IS a home. |
| Policy mode / permission mode | the operator, per-run | medium | Mode must become per-user-persisted with a global floor: an operator may tighten a user's ceiling but never loosen it from outside. The deny table (host-git integrity, A1) stays global — a user cannot opt out of the repo's own guards. |
| The marketplace identity (keys) | the operator | low | Per-user keypairs under each user's home; the hub's index already carries publisher keys per entry, so the format needs no change — only the resolution of "whose key" at publish time. |

## The recommended shape: a user is a home

The design that falls out of the inventory: **multi-user PAA is
per-user `PAA_HOME`s plus one shared control plane.** Every per-user
surface (ledger, vault, workspaces, skills, pipelines, keys) lives under
the user's home and needs no schema change; the shared surfaces are the
ones that are deliberately global today (the host-git deny table, the
sandbox backends, the process topology) and stay global deliberately.

The alternative — one ledger, per-user row ownership with per-row policy
checks on every read — trades the ledger's "replay is a fold" simplicity
for storage deduplication the spec never asked for. The event-sourced
core is the last place to add a per-row policy check on the hot path.

## Policy separation (the security argument)

1. **The floor is global.** The deny table, the sandbox backends, the
   gate order, the conformance standard: a user session cannot weaken
   any of them. The per-user policy vocabulary is a *ceiling* on what
   that user's agents may do, never a floor the operator must trust.
2. **Sessions open as a user, not switch.** A runtime binds one user's
   home at construction; "sudo" to another user's home is a new process,
   journalled as the operator, on the operator's home — not a mode flag.
3. **Cross-user effects are the marketplace's, not memory's.** A user
   installs another user's published pipeline through the same gates as
   a stranger's — the hub is untrusted whoever owns it; trust is the
   signature, and the capability review is the human's.

## What must be measured before any of this is built (the G1 extension)

The spec's own gate: single-user daily use proven first. Concretely, the
measurements that would justify lifting the gate: (a) the operator's own
G1 real-model run green; (b) a week of the operator's actual workload on
the current single-user shape with the ledger as the record; (c) the
per-user-home model proven for TWO users on one host with zero shared
state between their homes except the deliberately-global list above.

## Related documents

- `docs/plans/2026-11-21-next-spec-2.md` §8 — the Wave 9 rule this
  document designs under.
- `docs/VISION.md` — the single-operator thesis and the horizon list;
  multi-user is Horizon 3's question, not Horizon 1's.
- `README.md` §Temporal composability — the effect-registry boundary
  (scope-owned effects) is the vocabulary a per-user scope would ride.
