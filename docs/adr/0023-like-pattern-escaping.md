# ADR-0023: Every LIKE pattern comes from one helper

## Status

Accepted. Correctness class — this is not a departure from the RFC but a
return to what the RFC already assumes: that a query parameter is searched
for, not compiled into a pattern. The RFC's design places user text in
fact predicates and object values and then *searches* those columns; it
never says the text may be a wildcard, and nothing about the design
licenses a keyword becoming a query language.

## Context

Three places in the runtime search a relational column for text that came
from outside: a keyword the user typed, a term an agent derived, an intent
a model produced. All three built their `LIKE` pattern the same way —
`f"%{term}%"` — and all three were wrong in the same three ways.

**A metacharacter became a wildcard.** `%` matches any run of characters
and `_` matches any one of them, so the term `foo_bar` also retrieved
`fooXbar`, and the term `%` retrieved every row in the table. Nothing
raised: the search simply answered a different question from the one asked.

**A NUL ended the pattern.** SQLite reads a `LIKE` pattern through the C
string API, so a NUL terminates it: `%\x00%` is `%` and nothing else, and a
bare `%` matches every row. The subject is truncated the same way, so a
stored value carrying a NUL matches a prefix of itself. Escaping cannot
save this — a NUL is not a metacharacter, it is the end of the string.

**The escape character differed by backend.** SQLite's `LIKE` has no
default escape character; PostgreSQL's defaults to a backslash. The same
escaped pattern is therefore *wrong* on SQLite — the backslash is matched
literally and the `_` is still a wildcard — and *right* on PostgreSQL,
unless the SQL declares `ESCAPE '\'` explicitly. This runtime speaks to
both, so an escaping that was correct on one engine was silently wrong on
the other.

The consequences were not uniform, which is why one fix was not enough.

In the planner's hot tier (`agents/context_agents.py`) a retrieved fact
becomes a `ContextElement` with `relevance=1.0` and `slot=` the fact's
*predicate*, and the gatherer resolves a required slot from that
predicate. An unrelated fact that happens to share a required slot's name
therefore *resolves* that slot, which inflates the context density and
retires the vacancy that would have sent the task to a human. The hot tier
is not relevance-filtered either — the relevance floor applies to the
semantic tier only — so nothing downstream removes a fact that should never
have been retrieved. Those facts then reach the model under the header
"VERIFIED CONTEXT".

In the skills registry (`skills/registry.py`) the `LIKE` is a prefilter
whose fallback — scan the whole active set and rank it by trigram
similarity — fires only when the `LIKE` matches *nothing*. A wildcard
always matched something, so the fallback never ran: the intent `alpha%`
matched whatever contained `alpha` rather than whatever contained `alpha%`,
and the skills the caller actually asked about were dropped rather than
ranked. The docstring already promised the fallback. The wildcard is what
stopped it running.

The memory repository had already been fixed for this, in isolation, with
the mechanism living in the module that needed it. Two more sites then
needed the same protection and did not have it.

## Decision

One helper, `storage/relational/like.py`, builds every `LIKE` pattern that
interpolates text.

- `escape_like(value)` escapes the three characters SQLite's `LIKE` treats
  specially: `\`, `%` and `_`.
- `like_pattern(term)` drops NUL bytes, escapes the metacharacters, and
  returns the substring pattern. A term with nothing left to search for
  once its NUL bytes are gone is **refused**: a pattern built from nothing
  would be `%%`, a bare wildcard, and there is no `LIKE` pattern that
  matches nothing *instead*, so the caller has to be told.
- `like_predicate(*columns)` builds the SQL half, with the escape clause
  declared on every column.

The escape character and the `ESCAPE` clause are derived from one
constant, so the two halves of the contract cannot drift apart — the
pattern half and the SQL half have to agree, and an escape the pattern
uses and an escape the SQL does not declare is a pattern that matches
something other than what it says.

**The policy on a NUL belongs to the caller, and both policies are
deliberate.** The memory repository *refuses* a NUL: its query is a
model-supplied tool argument, so "that query was rejected" is more useful
to the model than a search for something it did not ask for, and the
refusal is reported back as an error value. The internal recall paths
*drop* the NUL and carry on, because they have to answer rather than
refuse. Neither is the default; the helper states the mechanism and each
caller states its policy.

## Consequences

The cost is one module and a rule: a `LIKE` pattern that interpolates text
comes from `storage/relational/like.py`, not from an f-string at the call
site. Three call sites now share one implementation, and the dialect facts
— SQLite's missing default escape character, PostgreSQL's differing one,
the NUL truncation — are stated once instead of rediscovered per site.

The behaviour change is visible in two places, and both are corrections
rather than regressions. The planner's hot tier retrieves fewer facts, and
the ones it no longer retrieves are the ones nobody asked for. The skills
registry returns *more* skills for an intent carrying a metacharacter,
because the literal intent matches nothing and the documented fallback
runs — a scan of the active set rather than a scan of the matches. That is
the price of not quietly answering a different question.

The `LIMIT` cut over the hot tier also gained a `f.id` tiebreaker.
Importance is a float, so a batch of facts sharing one importance value
was truncated to the first sixty of an order the SQL does not define.
SQLite happens to be deterministic for a fixed plan, but that is an
implementation detail rather than a guarantee, and PostgreSQL does not
promise an order among ties at all.

The `%%` case is worth stating plainly, because it is the one that is not
a nuisance but a failure: a pattern of `%%` matches every row, so the
helper refuses a term with nothing to search for rather than producing one.
There is no way to write a `LIKE` pattern that matches nothing, which is
why the refusal is the only available answer.

## Alternatives considered

**Escape at each call site, without a shared helper.** Rejected: it is the
state the code was in. Three copies of the escaping are three chances to
get the `ESCAPE` clause wrong, and the clause is the part that is
backend-dependent — the part most likely to be forgotten by the third
copy.

**Escape the metacharacters and leave the NUL alone.** Rejected: a NUL
truncates the pattern, so the escaped pattern still widens. The only
defence is to refuse or to drop the NUL, and both need saying out loud.

**Refuse every term with a metacharacter.** Rejected: a keyword containing
an underscore is an ordinary word (`deploy_service`), and refusing it would
mean the search does not work for half the identifiers in the system.

**Keep the wildcard semantics and document them.** Rejected: the wildcard
does not implement the documented behaviour — it suppressed the fallback
that the docstring promises — and it answers a question nobody asked.
