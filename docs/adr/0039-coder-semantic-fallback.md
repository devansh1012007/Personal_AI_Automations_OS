# ADR-0039: Coder recall falls back to vectors on a keyword miss, and the four touch copies become one

## Status

Accepted

*Recorded 2026-11-07 by W1-IMPL-01 (wave 1) for plan item P-014. The unified
helper, the `Runtime.embedder` field and the `recall_facts` vector tier are
the P-004 implementer's to write. This ADR records the shape they must take —
where the helper lives, what the embedder plumbing is, and when the fallback
may not fire.*

## Context

Recall-after-touch exists in four copies. The `_touch_recalled` helper is
defined independently in the API layer, the coder tool, the planner-side
context agent and the CLI, with the same executable core and drift confined
to a signature annotation (one site types the row handle, three do not), the
import-binding style, and docstring wording. Each copy independently swallows
a touch failure as a redacted warning and emits the same event name — four
copies of one policy again.

The coder's `recall_facts` is keyword-only: entity resolve, fact query, then
a keyword (`LIKE`) tier on a miss, then touch. The embedding sink already
writes facts into the vector collection and the planner already reads them,
but the coder tool never searches the collection, so a paraphrase the
keyword tier cannot spell-match returns nothing even when the vector store
knows the fact.

There is a plumbing gap the plan does not name: `Runtime` holds a
`vector_store` but **no `embedder`** — the embedder is a local in the build
function, handed to the agents and the memory creator, then dropped. The
fallback has nothing to call for an embedding until that is fixed.

## Decision

**One home: `src/paa/memory/touch.py` with a public `touch_recalled`.**
The helper takes `db: RelationalBackend | None` (the stricter annotation,
resolved at runtime like every other `RelationalBackend` mention in the
tree), module-level logging and redaction, the same order-preserving
dedupe, the same single `DecaySweeper.touch_many` call, and the same
derived-state exception wording in its docstring once. The four sites import
it and delete their defs; the touch tests that patch `touch_many` on the
class survive the move because the callee is unchanged. A structural test
asserts exactly one definition remains.

**The fallback is keyword-first, vector second, and only on a miss.** The
recall flow becomes entity resolve, fact query, keyword tier, and — only when
those found nothing — a vector tier over the active-facts collection,
ranked by query-time derived confidence (inheriting the P-002 derivation:
the payload snapshot is provenance, never ranking truth). Every hit from
either tier touches exactly once.

**The embedder plumbing is one field: `Runtime.embedder`.** The build
function already constructs the embedder; it is stored on the runtime
beside `vector_store` instead of being dropped. The alternatives — building
a fresh embedder per query (slow for the real embedder, and it re-reads
config), or reaching through the sink's private attribute (breaks whenever
the sink is absent, which is the common minimal-install case) — are rejected.
The heavy import stays function-local in the tool: a minimal install that
has never heard of the vector extra must still import the coder tools.

**Degrade, never raise.** When the vector store or the embedder is absent —
a minimal install, a disabled backend, a search error — the fallback returns
exactly today's keyword-only result, still touched, still emitted. The
acceptance test has two halves and both must pass: with store plus embedder
present, the paraphrase hits; with either absent, the count is zero and
nothing errors. A fallback that becomes a hard dependency would make recall
*less* reliable than today on hosts without the extra.

### Alternatives rejected

* **Keep four copies and hand-fix the drift.** Rejected: the plan already
  rejected it, and a grep lock makes it unimplementable — correctly, because
  the drift is the defect.
* **Touch unification without the fallback (or vice versa).** Rejected: the
  fallback needs a touch at its own call site, and splitting them would
  create a fifth copy at the new site.
* **Vector-first recall.** Rejected: the keyword tier is exact and cheap; the
  vector tier is the miss path, not the default.

## Consequences

**What it costs.** One new module, a `Runtime` field, a second recall tier,
and an updated `recall_facts` note and the stale self-contradicting docstring
that claims nothing is wired — the note text changes again, and the tests
that pinned the old text are updated deliberately, not silently.

**What it does not change.** `DecaySweeper.touch_many`'s signature, the
`recall_facts` payload shape (`query` / `facts` / `count` / `note`), the
event name `memory.recall_touch_failed`, and the keyword-first ordering.
`FactRepository`'s resolve/query/search surface is unchanged.

**How to roll it back.** Remove the vector tier call — recall returns to
keyword-only, and the "absent backend" half of the test proves that path
still works.
