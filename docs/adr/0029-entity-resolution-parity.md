# ADR-0029: Entity resolution is the same answer on both substrates

## Status

Accepted.

> Written on 2026-10-21, after the implementation landed. This record
> **amends [ADR-0025](0025-postgres-dialect-seam.md)** — one of its rules
> about the *shape* of the dialect seam — and closes the "What is still
> open" paragraph ADR-0025 left open: "the candidate sets the two produce
> are not the same set — the dialect seam covers the SQL, not the
> semantics."

## Context

RFC §4 specifies `pg_trgm` for fuzzy entity resolution. ADR-0001 replaced
PostgreSQL with embedded SQLite, and the schema now maintains an FTS5
trigram index over the entity table by trigger. There are therefore two
relational substrates, two fuzzy matchers, and one resolution path that has
to answer the same on both — because both answers arrive as the same
`Entity` object and the runtime cannot tell which substrate served the
request.

The *shape* of the resolution was already right, and it is worth saying why
before saying what was wrong with it. The Python gate —
`paa.memory.facts.trigram_similarity` — decides; the database only decides
**which rows the gate gets to look at**. That split is the only one that can
work across two engines, because an engine's own relevance score is not
comparable with the other engine's: FTS5 ranks with `bm25` (unbounded, and
not comparable across queries), and `pg_trgm` scores the raw alias blob
rather than each alias. So neither engine's score was used to decide, and
neither was used to cut.

Every part of that split was nevertheless wrong, and the suite could not see
any of it, because the suite only ever ran the SQLite half. The defects,
each measured on this host against the repository's own code:

**1. The gate's gram model was not the index's gram model.** The gate took
the 3-grams of `"  " + lower(collapse_ws(s)) + " "` — two leading spaces and
one trailing. FTS5's `trigram` tokenizer indexes the 3-grams of the **raw**
text: no padding, no whitespace-run collapsing. The gate therefore held
grams the index cannot hold, and could accept a pair sharing **no FTS5 token
at all**:

```
term = "aba", candidate = "abba"
  gate score                = 0.5000   (>= 0.34  ->  ACCEPTED)
  shared gate grams         = {'  a', ' ab', 'ba '}   (all three are grams of the *padded* model)
  FTS5 tokens("aba")        = {'aba'}
  FTS5 tokens("abba")       = {'abb', 'bba'}
  intersection              = {}        -> SQLite's MATCH finds NOTHING
```

All three shared grams are grams of the padded string `"  aba "` — two of
them are its boundary grams and `" ab"` is its word-internal middle gram —
and not one of them is in FTS5's token set. So the pair was invisible to
the index while the gate accepted it. Under the model this ADR
records, the same pair scores **0.00**.

**2. `pg_trgm` is not a whole-text measure.** It splits the text into words
and pads *each* one:

```
show_trgm('alpha-beta') = {'  a', '  b', ' al', ' be', 'alp', 'bet', 'eta', 'ha ', 'lph', 'pha', 'ta '}
```

The cross-boundary gram `"t s"` of `"widget service"` is **absent** from
`show_trgm('widget service')`, so `pg_trgm`'s gram set is not the gate's:
the gate's model is the whole text's 3-grams, and it holds grams
`pg_trgm` does not. The two models are not the same measure, and a
candidate query built on `similarity > 0` would therefore be answering a
*different* question from the gate's — in the worst case, one that cannot
see a pair whose only shared grams are the cross-boundary ones.

**That risk did not materialise, and the design does not depend on it not
materialising.** A 462-pair sweep (22 names × 21 others, the short-word,
tie and typo shapes included) against live PostgreSQL 16 found **no** pair
the gate accepted at the 0.34 floor with a zero `pg_trgm.similarity`: 16
pairs were accepted by the gate and 26 by `pg_trgm`, and every
gate-accepted pair was visible to the engine. The reason the risk did not
bite is that the short-word case is *not* the counterexample it looks like
— `pg_trgm` pads **every** word, so `show_trgm('a')` is `{'  a', ' a '}`
and `similarity('a', 'a')` is **1.0**, not 0. The reason the *design* does
not depend on it is decision 2: the candidate query is not built on
`similarity` at all, but on the **gram count**, which is the gate's *own
precondition* expressed in the gate's *own* gram model, identically on
both engines. The superset property is a property of the predicate, not an
empirical claim about `pg_trgm`.

**3. The candidate set was not a superset of the gate's acceptance set.** A
brute-force sweep found **67** (term, candidate) pairs the gate accepted and
FTS5's token sets missed. Over a realistic 8-entity corpus, **5 of 18 terms
violated the superset property on SQLite**; the worst was `Widgt Servce`,
where the gate accepts two entities and SQLite's candidate set is **empty**
— so the answer is `None` on SQLite, while PostgreSQL returned two
entities.

**4. The tie-break was decided by the engine's arbitrary order.** Two
entities scoring *exactly* 0.625:

```
sqlite  answer: 'Abcy Service'    candidate order: ['Abcx Service', 'Abcy Service']
postgres answer: 'Abcx Service'    candidate order: ['Abcy Service', 'Abcx Service', ...]
```

and the PostgreSQL answer was **not even stable** across insertion orders.
`bm25` and `similarity` both tie, and the gate's `>=` tie-break takes the
*last* maximum, so the winner was a function of the heap layout.

**5. The `OR`-token leak.** One parameter meant an FTS5 *expression* on
SQLite and a *literal term* on PostgreSQL, so the second pass compared
against the three-word literal `"Johnson" OR "Alice"` — and `OR` contributes
the grams `"  o"` / `"or "`, which match a large fraction of the table:

```
similarity('["Johnson" OR "Alice"]', 'Doctor')       = 0.0435   > 0
similarity('["Johnson" OR "Alice"]', 'Widget Service') = 0.0667   > 0
```

`'Doctor'` and `'Orchid'` entered the PostgreSQL candidate set and could not
enter SQLite's.

**6. The alias scoring.** PostgreSQL scored `similarity(e.aliases, term)`
over the **raw JSON blob**; the gate scores **each alias individually**:

```
similarity('["Gadget Assembly"]', 'Service')  = 0.0000
similarity('Service',                  'Service')  = 1.0000
```

A row the engine cannot see is a row the gate never gets to score.

Then the three defects the *new* test suites found — the ones the design did
not predict, because the design was not yet written down as code, and two of
them are the kind only a cross-engine suite can see at all:

**A. The PostgreSQL phrase predicate folded the column but not the term.**
`LIKE` is case-sensitive, so a term carrying an upper-case letter matched no
phrase row at all on the server, while FTS5 — which folds the term itself —
matched it on SQLite. Measured: for the same corpus and the same term,
SQLite answered `None` and PostgreSQL answered `Widget Servic`. The two
substrates were running *different queries*.

**B. The exact rung had no total order.** `ORDER BY` and `LIMIT` were
absent, so with two rows that differ only in case the answer followed the
physical scan order, and the two substrates disagreed — the schema's
`UNIQUE` on `canonical_name` is case-*sensitive*, so the table can hold
`café` and `Café` as two rows while the rung's `lower()` comparison sees
them as *one* key.

**C. The phrase-first short-circuit hid rows the gate would have accepted.**
The fallback ran only when the phrase query returned *no rows*, and the
phrase query is not a superset of the gate's acceptance set — it asks about
substrings, while the gate asks about gram overlap. So a row the gate would
have accepted was never scored.

## Decision

**1. The gate is the authority, and its gram model is FTS5's.**
`paa.memory.facts._trigrams` (`facts.py:203`) returns the 3-grams of the
**raw, case-folded** text: no padding, no whitespace-run collapsing. The
gate has to be decidable from what the index can see — a gate that scores a
pair the index cannot hold accepts a row that no candidate query over the
index can return, so the superset property would be unreachable by
construction. On SQLite the containment is now *exact*: the gate's gram set
is the FTS5 token set of the stored text, character for character.

The fold is `paa.storage.relational.dialect.fold_case`
(`dialect.py:121`) — the seam's fold, and the same function the candidate
query folds its term with, so one place decides what a fold is and the
query asks about the same *term* the gate scores.
**2. The database returns a candidate set that is a superset of the gate's
acceptance set — over the corpora and terms the suites use.** The candidate
query is built by the dialect seam (`dialect.py:196`), and it is asked one of
two questions:

| query | parameter | SQLite | PostgreSQL | set |
|---|---|---|---|---|
| the **phrase** | the term's text | `MATCH '"<term>"'` | `lower(col) LIKE '%<term>%' ESCAPE '\'` | the term appears as a substring |
| the **broad** | the term's grams | `MATCH '"g1" OR "g2" OR ...'` | the gram count `> 0` | shares at least one gram |

The broad query asks the gate's *own precondition*: a pair the gate accepts
shares at least one gram, so "shares at least one of the term's grams" is
exactly the condition under which the gate can accept anything. It is the
same question on both engines — each in its own idiom, an FTS5 `MATCH` of
the grams OR'd on one and the gram count tested against zero on the other —
so the two candidate sets are the same set **over the ASCII corpora the
suites use**. Outside ASCII the property is **bounded, not claimed**: the
column's fold is the engine's (limitation (a) below), and an alias carrying a
non-ASCII character is invisible to the count proxy (limitation (c) below),
so the superset property fails there — and it fails on **both** substrates,
not on one.

The count proxy is the gate's **numerator**, expressed in portable SQL: one
`CASE WHEN <column> LIKE ? ESCAPE '\' THEN 1 ELSE 0 END` per (gram, column)
pair, built by `paa.storage.relational.like.like_count_expression`
(`like.py:176`) with its patterns from `like_pattern`, so the escaping rule
stays in one place (ADR-0023). The two columns are the canonical name and
the alias **blob** — the same text on both substrates, so the two queries
ask the same question the same way.

**The count proxy is the gate's numerator, not the gate's score.** The gate
scores the canonical name and each alias *individually*; the proxy counts
over the row's text as a whole, and the count is not a Jaccard. Two rows
can hold the same count and score differently — measured against the term
`"widget service"`: `"Widget Services"` holds all twelve of the term's
grams and scores 0.9231, `"Widget Service Platform"` holds the same twelve
and scores 0.5714 — so **a cut between them is decided by the id, not by
the score**. The proxy is the right thing to *order* by; it is not a
promise about which row the gate would have scored highest.

**3. The phrase query is a precision pass; the broad query is the fallback,
and the fallback is a *gate* decision.** `paa.memory.facts._fuzzy_resolve`
(`facts.py:625`) runs the phrase query first, scores its rows with the
gate, and runs the broad query when **the phrase rows did not produce a
candidate that clears the floor** — not merely when the phrase query
returned nothing. The two are different, and the difference was defect C
above: the phrase query is not a superset of the gate's acceptance set, so
a fallback that runs only on an empty result never shows the gate a row it
would have scored. The broad set contains the phrase set, so the broad rows
alone are the fallback; no union is needed.

**4. The cut is made on a portable total order.** The candidate rows are
ordered by the count proxy `DESC` and then by the unique `e.id ASC` — the
same expression on both engines (`dialect.py:399` and `dialect.py:469`).
`bm25` and `similarity` are not comparable, so a cut made on them is a cut
that can change the answer; the count proxy is the same measure on both
engines **for ASCII text**, and it is the measure the broad predicate uses,
so the rows that survive the cut are the rows that share the most with the
term. Outside ASCII the proxy is *not* the same measure on both engines —
the column's fold is the engine's — so the counts can differ and the cut
is not guaranteed to keep the same rows; that is limitation (a) below.

The gate's `>=` tie-break — the *last* maximum wins — is now deterministic
rather than a function of the heap layout, because the candidates arrive in
a total order (`facts.py:1117`). The exact rung (`facts.py:553`), the alias
rung (`facts.py:618`) and the **write path's** lookup
(`facts.py:409`) carry the same unique key, so the rungs that answer with a
row rather than a score agree on *which* row on both substrates.

**The three rungs do not agree on the direction, and each is pinned.** The
fuzzy gate takes the *last* maximum of a list ordered `id ASC`, so among
two rows with the same score and the same count the **greater** id wins.
The exact rung, the alias rung and the write path's lookup ask for the
*first* row of an `id ASC` order, so among two rows that answer them the
**lesser** id wins. A test that only asserted "the same answer every time"
could not tell a rule from its opposite, so each rung's direction is
pinned by a test.

**The alias rung deliberately carries no `LIMIT`.** With one, the engine may
satisfy the order by walking the id index and *stopping at the first
match*, so the alias expansion — a table-wide operation — would be
evaluated for some rows and not others. A corrupt row would then break
the lookup only for the terms whose match lies beyond it, which makes the
degradation a function of the id order (and of the engine's plan) rather
than a property of the rung. Without the `LIMIT` the expansion is
evaluated for every row, the degradation stays total and identical on both
substrates, and the rung still picks a deterministic row: the first of a
totally ordered result.

**5. The case fold happens once, in Python, in the seam.** The ways the two
engines handle case are all different: FTS5's `trigram` tokenizer folds the
term it is asked about (a Unicode *simple* fold), SQLite's `lower()` folds
ASCII only, PostgreSQL's `lower()` is locale-aware, and PostgreSQL's
`LIKE` — which the server's phrase predicate is built from — is
case-sensitive outright. A fold left to the engine is a fold the two
engines do not agree on, so `fold_case` does it in Python, once, and both
engines' phrase predicates are built from the text it produces. That is the
repair for defect A: the two substrates now ask the *same* question.

**The gate has one case left to it: the text comparison.** Two texts
shorter than a trigram have no grams to overlap, so they are compared as
*text* instead: equal after trimming and case-folding scores 1.0, anything
else 0.0. That is the one branch of the gate that is not a set overlap, and
it is the branch that scores the short-word corpus outright.

**6. The seam returns the statement together with the parameters it takes.
This amends ADR-0025.** ADR-0025's rule was that the candidate query "must
take exactly the parameters the caller supplies — `(query, limit)` — so the
two engines are interchangeable at the call site." That rule cannot hold for
this design: the two engines take different parameters for the same
question — an FTS5 expression plus the count proxy's patterns on one, a
term's patterns plus the count proxy's patterns on the other — and the
number of them is not the same. The old method **ignored both of its
arguments in both implementations**: neither the SQLite form nor the
PostgreSQL form interpolated `query` or `limit` into the statement, so the
statement and the parameters the caller bound could have disagreed about
their own shape and nothing would have noticed.

What replaces it is a rule with the same *intent* and a stronger
*warrant*: **the dialect returns the statement together with the parameters
it takes**, so the two engines' idioms cannot drift apart at the call site
— the call site binds what the engine asked for, and never guesses. The
new shape is asserted by counting: `tests/storage/test_dialect.py` counts
the statement's placeholders (through the production placeholder
translator, not a count of `?`) against the length of the parameter tuple
the seam returned.

**7. The FTS5 quoting lives in the seam.** `MATCH` is a *query language*,
and quoting a term for it is an engine difference, so `_fts_phrase`
(`dialect.py:563`) and `_fts_broad` (`dialect.py:588`) moved out of
`paa.memory.facts` and into `dialect.py`. They are the only place in the
repository that quotes for FTS5: a second quoting is a second place for the
injection barrier to be wrong.

**8. A term the two engines refuse in different places is refused in
Python, before any statement.** A NUL is refused in `resolve_entity`
(`facts.py:482`), because SQLite truncates a string at a NUL when it reads
it through the C string API — so a NUL inside a pattern degrades it to a
bare `%` that matches every row — while psycopg refuses the *bound
parameter* outright, with a `DataError` that carries no SQLSTATE and
therefore no classification a caller could catch. Refused in one place, the
behaviour is the same on both substrates and on every rung of the ladder.

**9. A term with too many grams is refused in Python, before any
statement.** The count proxy is a sum of two `CASE` expressions per gram
and it sits in the statement's `ORDER BY`, so SQLite's expression-depth
limit is reached at about 500 grams (measured: 498 parse, 499 do not). The
statement is then *refused* rather than answered, as "expression tree is
too large" — which is **not** a `QueryRejected`, so it escapes the
fail-closed ladder and takes the caller with it, while the server substrate,
which has no such limit, answers "no match". A divergence, and a crash.
So the ceiling is in Python and it is the same on both substrates
(`_MAX_TERM_GRAMS`, `facts.py:135`; the refusal, `facts.py:508`): a term
with more grams than the ceiling resolves to no match on both, rather than
raising on one.

**The refusal is made in the ladder itself, beside the NUL's, so no
statement is issued at all.** The ceiling is a property of the *term*, not
of one rung, and the exact and alias rungs bind the term as a *value* —
they cannot hit the expression-depth limit. Refusing the term before the
ladder starts means the two substrates cannot disagree about *how* they
answer, and the answer is the same one on both. The term's grams are
computed once, in `resolve_entity` (`facts.py:506`), and handed to the
fuzzy rung, so one place decides what a gram is.

**The refusal costs the exact rung a pathological case, and that is
deliberate.** A canonical name longer than about 402 characters — 400
grams — can no longer be resolved at all, not even exactly. The
alternative, refusing only where the candidate query is about to be built,
would leave the exact and alias rungs issuing statements for a term the
ladder has already decided it cannot answer, and the two substrates would
then disagree about *how* they answered: the exact rung's answer on one
substrate, "no match" on the other. Uniform refusal is the property worth
having, and a name that long is not a name.

**The alias de-duplication folds with the same function.** `_clean_aliases`
(`facts.py:1091`) folds with `fold_case`, so "one place decides what a fold
is" holds for the alias column too: a change to the fold would otherwise
split the gate from the alias de-duplication silently.

**10. The cross-substrate contract is three parts, and the match kind is
one of them.** The two substrates have to agree on `(id, match kind,
score)` — *all three*, because a suite that compares only the id cannot
see two engines agreeing about the entity and disagreeing about how
confident they are, or about *how* they found it. The match kind is
therefore part of the contract: `"exact"`, `"alias"` and `"fuzzy"` are
three different claims about the same row, and the fold divergence below
is a case where the two substrates make *different* claims about the same
entity.

## Consequences

**The gate is now the stricter of the two engines' measures, and the
floor's meaning has shifted.** This is the honest cost of the model change:
the floor of 0.34 was adopted as "`pg_trgm`'s default" — a number that is
not quite PostgreSQL's documented default for `pg_trgm.similarity_threshold`
(0.3) — and it is no longer that engine's score at all. It is now the
gate's own score, which is *stricter*. Measured over 702 pairs drawn from a
27-name corpus: the gate accepted **38**
pairs at the 0.34 floor and every one of them had a non-zero
`pg_trgm.similarity` — the engine's own measure could see every pair the
gate accepts — while `pg_trgm` accepted **48**
pairs at that floor and the gate refused **14** of them. So the direction
that matters is preserved: a pair the gate accepts is a pair the engine can
see. **And the candidate query that is actually issued does not rest on
that measurement**: it is the gram-count proxy, so the superset property is
a property of the predicate (decision 2) rather than of an empirical
comparison with `pg_trgm`. The pairs the gate now refuses are pairs
`pg_trgm` would have accepted
on the strength of boundary grams (`"  a"`, `"ba "`) that no trigram index
holds. The floor's *number* is unchanged and its *meaning* is now the
gate's own; a deployment that calibrated against `pg_trgm` will see fewer
fuzzy matches than it did.

**The phrase-first policy is a deliberate narrowing.** While the phrase rows
clear the floor, the broad set is *not* consulted — so a row only the broad
query can see is not scored even when the gate would have scored it higher.
That is the phrase query doing what it says: a precision pass, and a name
the term spells out is taken over a name it only resembles. The alternative
— always scoring the broad set — would make the phrase query a pure
restriction, since the broad set contains the phrase set. The narrowing is
identical on both substrates, which is the property this ADR is about; the
superset property is asserted over the *corpora and terms the suites use*,
and the phrase query is not a superset of the gate's acceptance set in
general.

**The plan's predicted corrupt-row degradation did not materialise.** The
plan's risk surface predicted that "the candidate query now touches the
alias column, so the corrupt-row degradation moves from the alias rung to
the fuzzy rung". It does not move: the candidate query reads the alias
column as **plain text** (`lower(e.aliases)`) and never casts it to
`jsonb` — only the *alias rung* casts it, to expand it
(`jsonb_array_elements_text((e.aliases)::jsonb)`). So a row whose alias
column holds something that is not JSON breaks the alias rung and nothing
else: the fuzzy rung reads the blob as text, counts grams in it, and
answers. Measured on both substrates with one corrupt row in the table:
the fuzzy rung still resolves the term, and the alias rung answers "no
match". The degradation is total, identical on both substrates, and it is
the alias rung's alone — which is also why the alias rung carries no
`LIMIT` (see decision 4).

**The known limitations, with their mechanisms.**

- **(a) The column is folded by the engine; the term is folded by Python.**
  The gate folds the row's text with Python; the candidate query folds it
  with the engine's `lower()` — ASCII-only on SQLite, locale-aware on
  PostgreSQL. For text outside ASCII the two substrates can still hold
  different candidate sets, and can still answer differently. This is the
  limitation `docs/plans/w3-w5.md` §1.6 records, and it is not removed
  here. The count proxy is portable **for ASCII text**; outside it the
  counts differ — measured, the row `"CAFÉ NOIR"` against the term
  `"café"` counts **1** on SQLite and **2** on PostgreSQL — so the cut is
  not guaranteed to keep the same rows.
- **(b) `fold_case` is a full lower-case mapping, and FTS5's is not.**
  Python's `str.lower()` maps each character to its lowercase form, and for
  exactly one code point in Unicode — `U+0130` `İ` — that mapping *changes
  the length* (`"İ"` → `"i̇"`, two characters). FTS5's simple fold cannot
  do that, so the token set of the folded term and the token set of the raw
  term differ for that one character; PostgreSQL's `lower()` gives a third
  answer (it maps `İ` to a single `i`). One code point, three engines, three
  answers. Every other code point folds to the same token set, and the fold
  itself is idempotent everywhere.

  **The consequence of the third answer is a divergence, and its direction
  matters.** PostgreSQL's locale-aware `lower()` folds `İ` to a single `i`,
  while SQLite's ASCII-only `lower()` and FTS5's simple fold leave it
  alone — so **PostgreSQL sees rows the other substrate cannot see at
  all**, and the two answers are not the same answer. Measured on this
  host, live PostgreSQL 16 and the embedded substrate, the same corpus in
  both, through the repository's own code:

  ```
  stored 'İstanbul'  term 'istanbul'  sqlite: ('İstanbul','fuzzy',0.625)
                                        postgres: ('İstanbul','exact',1.0)
  stored 'İZMİR'     term 'izmir'      sqlite: None
                                        postgres: ('İZMİR','exact',1.0)
  stored 'éåİ'       term 'éåi'        sqlite: None
                                        postgres: ('éåİ','exact',1.0)
  ```

  So the divergence is not a narrowing that both substrates share: it
  changes *whether* a term resolves at all (the second and third rows), and
  it changes the **match kind and the score** for the same entity (the
  first row — the same row, found "exactly" on one substrate and guessed
  at 0.625 on the other). The broad candidate set diverges with it: the
  term `"izmir"` offers the row `"İZMİR"` on PostgreSQL and offers nothing
  on SQLite. The parity suite measures this rather than assuming it —
  `tests/storage/test_entity_parity.py::TestTheRecordedDivergence`, whose
  tests **pass while the divergence exists**.
- **(c) The alias column is JSON text, and `json.dumps` escapes non-ASCII.**
  The alias blob is stored with `json.dumps(value, separators=(",", ":"))`,
  whose default `ensure_ascii=True` turns `café` into `caf\u00e9`. The count
  proxy's pattern is built from the *raw* folded text, so it never matches
  the escaped blob: an alias carrying a non-ASCII character is invisible to
  the count proxy, and the broad query can miss a row the gate would
  accept. The breakage is **symmetric** — the blob is the same text on both
  substrates, so both answer "no match" and the superset property fails on
  both rather than on one. Measured: the alias `ééé` scores 0.5 against
  the term `éééx` (over the floor) and the answer is "no match" on both
  substrates. Closing this needs a **stored normalised form** — a column
  holding a normalised, non-escaped text — and its backfill cannot be
  written in SQL, so it needs a Python-executed re-index. That is a larger
  change than this one, and it is the same class of change limitation (a)
  needs.
- **(d) The exact and alias rungs still compare with the engine's
  `lower()`.** `lower(canonical_name) = lower(?)` and `lower(a.value) =
  lower(?)` are the engine's fold, so a non-ASCII canonical name can resolve
  on one substrate and not on the other. This ADR gives those rungs a
  *total order* — so they answer with the same row on both substrates — but
  it does not give them the same *key set*. The first row of the measurement
  above is this limitation: the same entity, an *exact* answer on one
  substrate and a *fuzzy* guess on the other.

**What it costs.**

- The seam grows. `fold_case` (a public function beside the seam),
  `_count_proxy`, `_fts_phrase` and `_fts_broad` are four new pieces in
  `storage/relational/dialect.py`, and ADR-0025's principle is that a seam
  member is a difference that *has* to be somewhere. The case fold is the
  awkward one: it is a value-level operation, and ADR-0025 put value
  coercions in `paa.core.types` on the grounds that "a repository reads a
  column, it does not execute a query about it". The fold is here because it
  exists *because* the engines' folds differ, and the difference has to be
  resolved in one place — but it is a departure from ADR-0025's placement
  rule and is recorded as such.
- **The candidate query is a full scan, and it gets slower with the term's
  length.** The count proxy is one `CASE` per (gram, column) pair — two per
  gram — and the server's broad statement carries it twice, once in the
  predicate and once in the `ORDER BY`, so that statement holds `4n` of them
  for a term of *n* grams. That is what the expression-depth ceiling of
  decision 9 is about, and it is also the query's *cost*: the predicate is
  O(rows × grams) with no index to help it, and the filter is evaluated
  twice per row (once in the predicate and once in the sort key). Measured
  on PostgreSQL 16 over a 20 000-row entity table:

  | grams | time |
  |---|---|
  | 4 | 0.057 s |
  | 18 | 0.466 s |
  | 45 | 1.146 s |
  | 132 | 2.443 s |

  `EXPLAIN ANALYZE` shows `Seq Scan on hot_serving_entity_index` with
  `Rows Removed by Filter: 20000` — every row is examined and the filter
  is computed for it. **The `pg_trgm` GIN indexes cannot be used by this
  predicate**, because the column is wrapped in `lower()`: the indexes are
  on `canonical_name` and on `(aliases::text)`, not on the folded
  expressions, so a `lower(canonical_name) LIKE '%x%'` comparison has
  nothing to match them against. The trigram indexes the RFC asked for are
  still in the schema and still useful to anything that queries the raw
  column; this predicate does not use them.

  The cost is not uniform, and the short case is the cheap one: for a short
  term the new query is *faster* than the `pg_trgm` query it replaced
  (12.9 ms against 52.9 ms on the same table), because a trigram similarity
  is a much heavier expression than a `LIKE` count. The cost is in the
  long tail — a term with a hundred grams is a hundred times the work.
- The gate's scores change, so the fuzzy rung's answers change, so any
  caller that depended on a specific row resolving fuzzily may see a
  different row. That is the point of the change, but it is a change in
  behaviour and not only in implementation.
- The cross-engine half of the suite needs a live PostgreSQL
  (`tests/storage/test_entity_parity.py`, marked `requires_postgres` and
  skipped without `PAA_TEST_POSTGRES_DSN`). The default suite still runs
  everywhere; the parity half does not, and a deployment that never runs it
  does not have it.

**What it would take to move back.** One relational substrate, or one
matcher. With a single engine the seam's four new pieces collapse into the
engine's own idiom, the count proxy becomes the engine's relevance score,
and the gate's model could return to the padded one — at the cost of the
superset property, which is only meaningful when two engines have to agree.
Moving back *within* the two-substrate design would mean reverting decision
2 to `pg_trgm`'s `similarity` on the server, and accepting that the
candidate query would then answer a *different* question from the gate's —
`pg_trgm`'s gram model is not the gate's (it has no cross-boundary gram) —
so the superset property would become an empirical accident rather than a
property of the predicate.

**What is still open.** Limitation (c) — the escaped alias blob — is the
one this ADR names and does not close, and the stored normalised form it
needs is the same change limitation (a) and (d) need. A single stored,
Python-computed normalisation for the canonical name, the aliases and the
alias blob would close all three, and would be a schema change in both DDL
files with a Python-executed backfill. It is not taken here.
