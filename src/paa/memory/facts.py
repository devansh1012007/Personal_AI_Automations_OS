"""Entity and fact persistence — the hot-serving read/write path.

RFC §4 gives hot serving two tables and one hard rule: a fact is *never*
overwritten. New information **supersedes** old information and the old row
stays addressable, so the chain of evidence that produced a belief can always
be walked backwards. Overwriting would make the runtime unable to answer "why
did you think that?", which is the question a human asks precisely when the
system has gone wrong.

Two consequences shape this entire interface.

**Confidence is derived, never selected.** ``initial_confidence`` is C₀; the
number a caller actually wants is ``C(t) = C₀·e^(-λt)``, computed by
:func:`paa.memory.decay.effective_confidence` from the idle clock. So
:meth:`FactRepository.query_facts` cannot push ``min_confidence`` down into a
SQL ``WHERE``, and cannot push ``LIMIT`` down either: a row that passes the
stored threshold may fail the derived one and vice versa, so filtering before
the computation returns the wrong set. It scans a bounded candidate window and
filters in Python instead — hence ``max_scan``.

**Entity identity is fuzzy; storage is exact.** ``canonical_name`` carries a
UNIQUE constraint under SQLite's default BINARY collation, so "Alice" and
"alice" are two rows to the database and one person to a human. Case folding
therefore happens here rather than in the schema: a ``COLLATE NOCASE``
constraint would fold ASCII only and silently mis-handle every non-English
name the ingestion path will eventually see.

SPEC DEVIATION (docs/adr/0001): the RFC specifies PostgreSQL ``pg_trgm`` for
fuzzy entity resolution. The SQLite equivalent is the FTS5 trigram index the
schema maintains by trigger. Neither engine's own relevance score can
*decide*: FTS5 ranks with bm25, which is unbounded and not comparable across
queries, and ``pg_trgm`` scores the raw alias blob rather than each alias. So
neither engine's score is used to decide, and neither is used to cut: the
candidate rows are ordered by a **portable count of the term's grams** — a
count of the row's text as the query sees it, which is the count the gate
counts with but not the gate's score — and the decision is made in Python by
an explicit character-trigram Jaccard similarity, which is bounded in [0,1]
and means the same thing for every query — otherwise "xyz" would resolve to
whatever row happened to come back first.

The candidate query itself is built by the dialect seam
(:meth:`paa.storage.relational.dialect.Dialect.entity_candidates_sql`), which
is where the two engines' idioms live: an FTS5 ``MATCH`` on one, a portable
``LIKE`` count on the other. The seam returns the statement *together with the
parameters that statement takes*, so this module binds what the engine asked
for rather than what it guessed. That change to the seam's shape amends
ADR-0025 and is recorded in **ADR-0029** (``docs/adr/0029-entity-resolution-parity.md``).

**The case fold is the seam's, and the gate's fold is the same fold.** The
ways the two engines handle case are all different — FTS5's tokenizer folds
the term it is asked about, PostgreSQL's ``LIKE`` is case-sensitive
outright, SQLite's ``LIKE`` folds ASCII case and is case-sensitive only
outside it, SQLite's ``lower()`` folds ASCII only and PostgreSQL's follows
the locale — so a fold left to the engine is a fold the two engines do not
agree on. The seam folds the term once, in Python
(:func:`paa.storage.relational.dialect.fold_case`), and both engines' phrase
predicates are built from the text it produces; the gate's gram model uses
the same function, so the candidate query asks about the same *term* the
gate scores. The *column* the query reads is folded by the engine, and that
fold is not the same one — and the difference is **one-directional**:
PostgreSQL's locale-aware ``lower()`` sees rows the other two substrates
cannot see at all, so the two answers can differ, and the *match kind* can
differ for the same entity. The recorded limitation of
``docs/plans/w3-w5.md`` §1.6.

A note on the ``# noqa: S608`` markers below: they sit on the **first**
fragment of a statement that is assembled from several string literals,
because that is where ruff reports the finding — ruff reads the implicit
concatenation as one expression. A line ruff does not report carries no
marker, because an unused marker is itself a finding.
"""

from __future__ import annotations

import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Literal

import structlog

from paa.core.types import as_text
from paa.memory.decay import effective_confidence
from paa.memory.domains import MemoryDomain
from paa.storage.relational.base import RelationalBackend
from paa.storage.relational.dialect import fold_case
from paa.storage.relational.like import like_pattern, like_predicate

__all__ = [
    "EMBEDDING_STATUSES",
    "Entity",
    "Fact",
    "FactRepository",
    "trigram_similarity",
]

log = structlog.get_logger(__name__)

#: The values ``ck_fact_embed`` permits. Validated here so a typo fails with a
#: readable message rather than a CHECK-constraint stack trace.
EMBEDDING_STATUSES: frozenset[str] = frozenset({"pending", "indexed", "failed", "skipped"})

#: FTS5's trigram tokenizer indexes 3-character sequences, so a query shorter
#: than that produces no tokens and can never match. Callers get an exact-match
#: attempt and nothing else. It is also the length below which a text has no
#: grams at all, which is the one case the gate cannot decide by set overlap.
_MIN_TRIGRAM_CHARS = 3

#: The most grams a term may have before the ladder refuses it.
#:
#: The candidate query's count proxy is a sum of two ``CASE`` expressions per
#: gram, and it sits in the statement's ``ORDER BY`` — so SQLite's
#: expression-depth limit is reached at about 500 *grams* (measured: 498
#: grams still parse, 499 do not). That limit is SQLite's compiled
#: ``SQLITE_MAX_EXPR_DEPTH``, 1000 by default, and the ceiling below is a
#: fraction of it — a build with a *smaller* limit would break earlier, and
#: the crash would come back.
#:
#: A term over the limit does not fail *closed* on the embedded substrate:
#: the statement is refused as "expression tree is too large", which is not
#: a ``QueryRejected``, so it escapes the ladder and takes the caller with
#: it — while the server substrate, which has no such limit, answers "no
#: match". A divergence, and a crash.
#:
#: So the ceiling is here, in Python, and it is the *same* on both
#: substrates: a term with more grams than this resolves to no match on
#: both, rather than raising on one. 400 grams is comfortably below the
#: measured break, and it is far past any name a person has.
#:
#: The refusal is made **before any statement is issued** — in
#: :meth:`FactRepository.resolve_entity`, beside the NUL refusal — so the
#: two substrates cannot disagree about *how* they answer, and the answer
#: is the same one on both.
_MAX_TERM_GRAMS = 400


def _has_nul(value: str) -> bool:
    """Whether a term carries a NUL byte.

    A NUL is the one character the two relational engines refuse in
    *different* places, and the difference is the reason it is refused
    here rather than left to them. SQLite truncates a string at a NUL when
    it reads it through the C string API, so a NUL inside a ``LIKE``
    pattern turns the pattern into a bare ``%`` that matches every row.
    psycopg refuses the *bound parameter* outright, before the statement
    is sent, with a ``DataError`` that carries no SQLSTATE — and therefore
    no classification a caller could catch.

    Refusing it in one place makes the behaviour the same on both
    substrates, and it is the same place the two engines' behaviour would
    otherwise diverge on.
    """
    return "\x00" in value


def trigram_similarity(left: str, right: str) -> float:
    """Jaccard overlap of the two strings' character trigram sets, in [0,1].

    The gram set is the 3-grams of the **raw**, case-folded text — no padding
    and no whitespace-run collapsing — because that is what FTS5's ``trigram``
    tokenizer indexes. The gate has to be decidable from what the index can
    see: a gate that scores a pair the index cannot hold accepts a row that
    no candidate query over the index can return.

    **The gate is the stricter of the two engines' measures.** ``pg_trgm``
    splits the text into words and *pads* each one, so its gram set holds
    every word-internal gram the gate holds — a single word's grams are a
    **subset** of ``pg_trgm``'s — and additionally holds the *boundary* grams
    the gate does not have (``"  a"``, ``"ba "``). Those extra grams are what
    make ``pg_trgm`` the more generous of the two: it accepts pairs the gate
    refuses. What the gate gains is the other direction, the one that
    matters: **a pair the gate accepts is a pair the engine can see.**
    Measured over 702 pairs drawn from a 27-name corpus, all 38 pairs the
    gate accepted at the 0.34 floor had a non-zero ``pg_trgm.similarity`` —
    so the engine's own candidate query was a superset of the gate's
    acceptance set over that corpus, while ``pg_trgm`` accepted 48 pairs at
    that floor and the gate refused 14 of them.

    That measurement is a property of the corpus, not of the design, and it
    is not what the candidate query rests on: the query asks the **gram
    count** — the gate's own precondition — expressed the same way on both
    engines, so it does not depend on ``pg_trgm``'s model at all. Outside
    ASCII the property is **bounded, not claimed**: the engine folds the
    *column* with its own ``lower()`` (ASCII-only on SQLite, locale-aware
    on PostgreSQL), so the two substrates can hold different candidate sets
    there, and a non-ASCII alias is invisible to the count because the
    alias column is stored as JSON text and ``json.dumps`` escapes it.

    On SQLite the containment is *exact* rather than a superset: the gate's
    gram set is the FTS5 token set of the stored text, character for
    character. Under the padded model it was not — the gate's grams were a
    strict superset of the index's tokens, and the excess were grams the
    index cannot hold. The terms ``"aba"`` and ``"abba"`` are the measured
    case: under the padded model they scored **0.50**, over the 0.34 floor,
    on the three grams they shared — ``"  a"``, ``" ab"`` and ``"ba "``, every
    one of them a padding gram — while FTS5's token sets for the two are
    disjoint (``{"aba"}`` and ``{"abb", "bba"}``). The gate accepted a pair
    the index could not see at all, so no candidate query over the index
    could have been a superset of the gate's acceptance set. Under this
    model the same pair scores **0.00**.

    Two texts shorter than a trigram have no grams to overlap, so they are
    compared as text instead of as sets: equal after trimming and
    case-folding scores 1.0, anything else 0.0.
    """
    a, b = _trigrams(left), _trigrams(right)
    if not a or not b:
        return 1.0 if fold_case(left.strip()) == fold_case(right.strip()) else 0.0
    return len(a & b) / len(a | b)


def _trigrams(value: str) -> set[str]:
    """The character trigrams of ``value``, case-folded — FTS5's model.

    The grams are taken from the text **as written**: no padding, and no
    whitespace-run collapsing. Both were removed deliberately, and the reason
    is the same for each — a gram the index does not hold is a gram the gate
    must not score on.

    The padding produced *boundary* grams (``"  a"``, ``"ba "``) that no
    FTS5 trigram token can ever be, so two texts could share a gram and
    nothing else — a pair the index could not see. The collapsing changed
    the text the grams came from: a name written with a double space and the
    same name written with a single one produced the same grams, so a pair
    could score 1.0 on a difference the text does not have — and for a row
    that reached the index by another route the index holds the
    double-spaced text verbatim, which the collapsing gate never scored.

    Collapsing is not lost where it belongs: the entity boundary collapses
    the canonical name when it is stored and the term when it is read, so
    both sides of an entity comparison are already collapsed text by the
    time the gate sees them.

    **The case fold is the seam's** — :func:`paa.storage.relational.dialect.
    fold_case` — and the same function the candidate query's term is folded
    with, so the query asks about the text the gate scores and one place
    decides what a fold is.
    """
    folded = fold_case(value)
    if len(folded) < _MIN_TRIGRAM_CHARS:
        return set()
    return {folded[i : i + 3] for i in range(len(folded) - 2)}


# ---------------------------------------------------------------------------
# Wire types
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Entity:
    """One row of ``hot_serving_entity_index``.

    ``match`` and ``score`` describe *how this instance was found* rather than
    the entity itself. They are carried on the object deliberately: a fuzzy
    resolution that silently looks identical to an exact one is how a runtime
    ends up confidently attaching a fact to the wrong person.
    """

    id: str
    class_: str
    canonical_name: str
    aliases: tuple[str, ...] = ()
    importance: float = 0.5
    confidence: float = 1.0
    attributes: Mapping[str, Any] = field(default_factory=dict)
    created_at: datetime | None = None
    updated_at: datetime | None = None
    match: str = "exact"
    """One of ``exact``, ``alias``, ``fuzzy``."""

    score: float = 1.0
    """Trigram similarity of the lookup term to this entity. 1.0 when exact."""


@dataclass(frozen=True, slots=True)
class Fact:
    """One row of ``hot_serving_active_facts``, with confidence already decayed.

    ``initial_confidence`` is what is stored; ``confidence`` is what is true
    now. Callers should always rank and threshold on ``confidence``.
    """

    id: str
    entity_id: str
    predicate: str
    object_value: str
    domain: str
    scope: str
    initial_confidence: float
    confidence: float
    importance: float
    use_count: int
    source_signal_id: str | None
    provenance: Mapping[str, Any]
    embedding_status: str
    created_at: datetime
    last_queried_at: datetime
    superseded_by: str | None = None
    entity_name: str | None = None

    @property
    def statement(self) -> str:
        """Natural-language rendering, used as the embedding text."""
        subject = self.entity_name or self.entity_id
        return f"{subject} {self.predicate} {self.object_value}"

    def as_dict(self) -> dict[str, Any]:
        """Mapping form for :class:`~paa.memory.contradiction.ContradictionDetector`.

        That class reads ``confidence`` with ``initial_confidence`` as a
        fallback, so both are present and the *decayed* value wins — a stale
        incumbent should not defend its position with a confidence it no longer
        has.
        """
        return {
            "id": self.id,
            "entity_id": self.entity_id,
            "predicate": self.predicate,
            "object_value": self.object_value,
            "memory_domain": self.domain,
            "confidence": self.confidence,
            "initial_confidence": self.initial_confidence,
            "importance": self.importance,
            "source_signal_id": self.source_signal_id,
        }


# ---------------------------------------------------------------------------

_FACT_COLUMNS = """
    f.id, f.entity_id, f.predicate, f.object_value, f.memory_domain, f.memory_scope,
    f.initial_confidence, f.importance, f.use_count, f.source_signal_id, f.provenance,
    f.embedding_status, f.created_at, f.last_queried_at, f.superseded_by
"""


class FactRepository:
    """The only writer of ``hot_serving_entity_index`` and ``hot_serving_active_facts``."""

    def __init__(self, db: RelationalBackend) -> None:
        self._db = db

    # -- entities ----------------------------------------------------------

    async def upsert_entity(
        self,
        class_: str,
        canonical_name: str,
        aliases: Sequence[str] | None = None,
        importance: float = 0.5,
        *,
        attributes: Mapping[str, Any] | None = None,
        now: datetime | None = None,
        conn: Any = None,
    ) -> str:
        """Create or refresh an entity. Idempotent on ``canonical_name``.

        On a repeat observation aliases are **unioned** and importance takes the
        **maximum**, never the latest. A low-signal re-mention ("saw the name in
        a footer") must not demote an entity a hundred earlier signals
        established as important, and an alias learned once must not be
        forgotten because the next signal did not repeat it.

        Read and write happen in one transaction so two concurrent ingests of
        the same name cannot both decide the row is absent. ``conn`` enlists in
        a transaction the caller already owns — necessary rather than merely
        convenient, because :meth:`Database.transaction` takes a non-reentrant
        write lock and opening a second one from inside the first deadlocks.

        **The lookup is ordered, and it is the same order the read path
        uses.** The schema's ``UNIQUE`` on ``canonical_name`` is
        case-*sensitive*, so the table can hold ``café`` and ``Café`` as two
        rows while this lookup's ``lower()`` comparison sees one key; an
        unordered lookup would refresh whichever row the engine's scan
        reached first, and the two substrates would refresh *different*
        rows — while :meth:`resolve_entity`, which is ordered, reads one of
        them. The order is the unique ``id``, so the write path and the
        read path agree on which row the entity is.

        (For text the two engines fold the same way. Outside ASCII their
        ``lower()``\\ s are not the same fold, so they can hold different
        key sets — the recorded limitation of ``docs/plans/w3-w5.md`` §1.6,
        and the one this order does not address.)
        """
        name = " ".join(canonical_name.split())
        if not name:
            raise ValueError("canonical_name must not be blank")
        if not 0.0 <= importance <= 1.0:
            raise ValueError(f"importance must be in [0,1], got {importance}")

        if conn is not None:
            return await self._upsert_entity_on(
                conn, class_, name, aliases, importance, attributes, now
            )
        async with self._db.transaction() as owned:
            return await self._upsert_entity_on(
                owned, class_, name, aliases, importance, attributes, now
            )

    async def _upsert_entity_on(
        self,
        conn: Any,
        class_: str,
        name: str,
        aliases: Sequence[str] | None,
        importance: float,
        attributes: Mapping[str, Any] | None,
        now: datetime | None,
    ) -> str:
        from paa.storage.relational.database import dumps, loads, to_iso, utc_now

        stamp = to_iso(now or utc_now())
        incoming = _clean_aliases(aliases, exclude=name)

        async with conn.execute(
            "SELECT id, aliases, importance_weight, attributes FROM hot_serving_entity_index "
            "WHERE lower(canonical_name) = lower(?) ORDER BY id ASC LIMIT 1",
            (name,),
        ) as cur:
            row = await cur.fetchone()

        if row is None:
            entity_id = str(uuid.uuid4())
            await conn.execute(
                "INSERT INTO hot_serving_entity_index "
                "(id, class, canonical_name, aliases, importance_weight, attributes,"
                " created_at, updated_at) VALUES (?,?,?,?,?,?,?,?)",
                (
                    entity_id,
                    class_,
                    name,
                    dumps(list(incoming)),
                    importance,
                    dumps(dict(attributes or {})),
                    stamp,
                    stamp,
                ),
            )
            log.debug("memory.entity_created", entity_id=entity_id, name=name)
            return entity_id

        entity_id = as_text(row["id"])
        merged = _merge_aliases(loads(row["aliases"], []) or [], incoming, exclude=name)
        attrs = dict(loads(row["attributes"], {}) or {})
        attrs.update(attributes or {})
        # The interpolated fragment is a dialect constant — a closed set of
        # two spellings chosen by the engine, with no user input in it.
        keep_max = self._db.dialect.scalar_max("importance_weight", "?")
        await conn.execute(
            "UPDATE hot_serving_entity_index SET class = ?, aliases = ?, "  # noqa: S608
            f"importance_weight = {keep_max}, "
            "attributes = ?, updated_at = ? "
            "WHERE id = ?",
            (class_, dumps(merged), importance, dumps(attrs), stamp, entity_id),
        )
        return entity_id

    async def get_entity(self, entity_id: str) -> Entity | None:
        row = await self._db.fetch_one(
            "SELECT * FROM hot_serving_entity_index WHERE id = ?", (entity_id,)
        )
        return _to_entity(row) if row is not None else None

    async def resolve_entity(
        self,
        name_or_alias: str,
        *,
        fuzzy: bool = True,
        fuzzy_floor: float = 0.34,
        candidate_limit: int = 20,
    ) -> Entity | None:
        """Find the entity a name refers to. Exact first, then alias, then fuzzy.

        The ladder is ordered by how much it can go wrong. An exact hit is a
        fact; an alias hit is a fact the system was told; a trigram hit is a
        *guess*, so it must clear ``fuzzy_floor`` and it reports itself as a
        guess through :attr:`Entity.match`.

        ``fuzzy_floor`` defaults to 0.34 — the number this repository
        adopted as "``pg_trgm``'s default", which is not quite PostgreSQL's
        documented default for ``pg_trgm.similarity_threshold`` (0.3), and
        after ADR-0029 is not ``pg_trgm``'s score at all: it is the
        **gate's** own score, the Jaccard over the 3-grams of the raw,
        case-folded text. A floor of 0.34 tolerates a wrong or missing word
        in a short name while rejecting unrelated strings.
        """
        term = " ".join(name_or_alias.split())
        if not term:
            return None
        if _has_nul(term):
            # A NUL is refused here, on both substrates, rather than left to
            # the engine. The two engines refuse it differently: SQLite's
            # full-text matcher rejects the *statement* ("unterminated
            # string"), which the candidate query catches and fails closed on,
            # while psycopg refuses the *bound parameter* before the
            # statement is sent — with a ``DataError`` that carries no
            # SQLSTATE, and therefore no classification the repository layer
            # could catch.
            #
            # Refusing it here makes the behaviour the same on both
            # substrates and the same on every rung of the ladder, rather
            # than a property of where the term happens to be bound. The
            # refusal is the internal-path one: the term resolves to no
            # match, and the caller carries on. (The tool path, where the
            # term is a model-supplied argument, refuses it as an error
            # value instead — see ``coder/runtime_tools.py``.)
            log.warning("memory.entity_term_refused", reason="nul_byte")
            return None

        # **The term's grams are computed once, here.** The gate scores the
        # term with them, the candidate query counts them, and the ceiling
        # below is a property of *them* — so one place computes the set and
        # every consumer of the term's grams gets the same one.
        grams = tuple(sorted(_trigrams(term)))

        if len(grams) > _MAX_TERM_GRAMS:
            # The candidate query's count proxy is a sum of two CASE
            # expressions per gram, and SQLite's expression-depth limit is
            # reached at about 500 grams — where the statement is refused
            # as "expression tree is too large" rather than answered, and
            # the refusal is not a QueryRejected, so it would escape this
            # ladder and take the caller with it. The server substrate has
            # no such limit and would answer "no match". Refused here, the
            # two substrates agree, and neither raises.
            #
            # The refusal is *before any statement*, beside the NUL's: a
            # term this long is refused the way a term carrying a NUL is,
            # so the answer is "no match" on both substrates and the
            # question is never put to a engine that might refuse it.
            log.warning(
                "memory.entity_term_refused",
                reason="too_many_grams",
                grams=len(grams),
            )
            return None

        # **The exact rung has a total order.** The schema's ``UNIQUE`` on
        # ``canonical_name`` is case-*sensitive* — SQLite's BINARY
        # collation, PostgreSQL's default — so the table can hold ``café``
        # and ``Café`` as two rows while the rung's ``lower()`` comparison
        # sees them as *one* key. Without an order the answer was whichever
        # row the engine's scan reached first, and the two substrates
        # reached different rows over the same data.
        #
        # ``id`` is the unique key ``AGENTS.md`` requires for a cut: a
        # ``TEXT`` primary key on SQLite and a ``UUID`` on PostgreSQL, and
        # ``upsert_entity`` writes the canonical hyphenated form, whose
        # lexicographic order *is* the UUID's byte order — so
        # ``ORDER BY id ASC`` is the same order on both engines, and the
        # answer is the same row on both.
        #
        # The order is the part this fixes; the *key set* is the engine's
        # ``lower()``, and the two engines' folds are the same for ASCII
        # and not the same outside it — so for non-ASCII names the two
        # substrates can still hold different keys, and can still answer
        # differently. That is the recorded limitation of
        # ``docs/plans/w3-w5.md`` §1.6, and it is not what this order
        # addresses.
        exact = await self._db.fetch_one(
            "SELECT * FROM hot_serving_entity_index "
            "WHERE lower(canonical_name) = lower(?) ORDER BY id ASC LIMIT 1",
            (term,),
        )
        if exact is not None:
            return _to_entity(exact)

        # The interpolated fragment is a dialect constant — a closed set of
        # two spellings chosen by the engine, with no user input in it.
        alias = await self._alias_lookup(term)
        if alias is not None:
            return _to_entity(alias, match="alias")

        # The term is whitespace-collapsed above, so its length is the length
        # the gate counts grams from: a term shorter than a trigram has no
        # grams at all, and a pair with no grams cannot be fuzzy-matched.
        if not fuzzy or len(term) < _MIN_TRIGRAM_CHARS:
            return None
        return await self._fuzzy_resolve(term, grams, fuzzy_floor, candidate_limit)

    async def _alias_lookup(self, term: str) -> Any:
        """The entity that carries ``term`` as an alias, if one does.

        The alias columns are JSON held as *text* on both substrates, and
        the server dialect casts them back to ``jsonb`` to expand them
        (``jsonb_array_elements_text``). A row whose JSON is corrupt makes
        that cast fail with ``22P02 invalid_text_representation`` — and
        because the expansion scans the table, ONE corrupt row makes the
        lookup fail for *every* term.

        That is a data problem, not a query problem, and the vocabulary says
        so: the error is a
        :class:`~paa.storage.relational.errors.MalformedDocument`. But the
        resolution ladder is written to fail closed — the ingestion batch
        that asked the question must not go down because one row in the
        table is unreadable — so the corrupt row degrades to "no alias
        matches" here rather than taking the caller with it.
        """
        from paa.storage.relational.errors import QueryRejected

        alias_values = self._db.dialect.alias_values("e.aliases")
        try:
            # **The alias rung has a total order too**, for the same reason
            # the exact rung has one: the alias column can hold the same
            # alias twice, under two canonical names that differ only in
            # case, and a cut with no order is a cut on an order the SQL
            # does not define. The order is the unique ``id`` — the same
            # key, and the same order on both substrates, as the exact
            # rung's.
            #
            # **The cut is made here rather than in SQL, and that is
            # deliberate.** With a ``LIMIT 1`` the engine may satisfy the
            # order by walking the id index and *stopping at the first
            # match*, so the alias expansion — which is a table-wide
            # operation — would be evaluated for some rows and not others.
            # A corrupt row would then break the lookup only for the
            # terms whose match lies beyond it, which makes the
            # degradation a function of the id order (and of the engine's
            # plan) rather than a property of the rung. Without the
            # ``LIMIT`` the expansion is evaluated for every row, the
            # degradation stays total and identical on both substrates,
            # and the rung still picks a deterministic row: the first of a
            # totally ordered result.
            return await self._db.fetch_one(
                "SELECT e.* FROM hot_serving_entity_index e, "  # noqa: S608
                f"{alias_values} a "
                "WHERE lower(a.value) = lower(?) ORDER BY e.id ASC",
                (term,),
            )
        except QueryRejected as exc:
            log.warning("memory.entity_alias_lookup_rejected", term=term, error=str(exc))
            return None

    async def _fuzzy_resolve(
        self, term: str, grams: tuple[str, ...], floor: float, limit: int
    ) -> Entity | None:
        """Trigram search over the entity index, gated in Python.

        **Two queries: the phrase one first, the broad one as the
        fallback.** The phrase query asks "is the term a substring of the
        row's text"; the broad query asks "does the row's text share at
        least one of the term's grams". The broad set contains the phrase
        set, and a phrase is the stronger signal, so the broad set does not
        dilute it. (The broad query is what lets "Johnson Alice" find
        "Alice Johnson" — a phrase is order-sensitive and humans are not.)

        **The fallback is a *gate* decision, not a query decision.** The
        broad query runs when the phrase rows do not produce a candidate
        that clears the floor — not merely when the phrase query returned
        nothing. The two are different, and the difference was a defect:
        the phrase query is *not* a superset of the gate's acceptance set
        (it asks about substrings, and the gate asks about gram overlap),
        so whenever it answered at all the gate never saw the broad set —
        and a row the gate would have accepted was never scored. Measured:
        with the entities "Widget Service Platform Operations And
        Deployment" (a phrase match, scored 0.2553 — below the floor) and
        "Service Widget" (scored 0.6), the resolution answered **no
        match**, because the phrase row was scored, missed the floor, and
        the broad query — the only one that can see the second row — never
        ran.

        **The narrowing this policy keeps, stated plainly.** While the
        phrase rows clear the floor, the broad set is *not* consulted —
        so a row only the broad query can see is not scored even when the
        gate would have scored it higher. That is the phrase-first policy
        doing what it says: the phrase query is a precision pass, and a
        name the term spells out is taken over a name it only resembles.
        It is a narrowing, and it is deliberate; the alternative — always
        scoring the broad set — would make the phrase query a pure
        restriction, since the broad set contains the phrase set.

        **The term's grams are computed once**, by the caller, with the
        same :func:`_trigrams` the gate scores with, so one place decides
        what a gram is. They are sorted, because the set's iteration order
        is not stable between processes and the grams end up in the
        statement's parameter list — a candidate query whose parameters
        changed shape between two runs of the same data is a candidate
        query whose result cannot be reproduced.

        **The gate decides.** Every candidate is re-scored here, in Python,
        and the best score wins. The database only decides which rows the
        gate gets to look at — and the candidates arrive in a total order
        (the count proxy, then the unique id), so the gate's ``>=``
        tie-break — the last maximum wins — is now deterministic rather
        than a function of the heap layout.
        """
        if not grams:
            # The ladder refuses a term shorter than a trigram, so this
            # cannot happen — and the resolution path is written to fail
            # closed rather than to raise, so it returns "no match" if it
            # ever does.
            return None

        # The phrase query is a *precision* first pass: it is the query
        # that can find the row the term spells out. It is run first, and
        # its rows are scored by the same gate that scores the broad rows.
        candidates = await self._entity_candidates(
            mode="phrase", term=term, grams=grams, limit=limit
        )
        best, best_score = _best_candidate(candidates, term, floor)
        if best is None:
            # The phrase rows did not produce anything the gate accepts —
            # either there were none, or the ones there were fell below
            # the floor. The broad set contains the phrase set, so the
            # broad rows alone are the fallback; no union is needed.
            candidates = await self._entity_candidates(
                mode="broad", term=term, grams=grams, limit=limit
            )
            best, best_score = _best_candidate(candidates, term, floor)

        if best is None:
            return None
        log.debug(
            "memory.entity_fuzzy_resolved",
            term=term,
            resolved=best.canonical_name,
            score=round(best_score, 4),
        )
        return Entity(
            id=best.id,
            class_=best.class_,
            canonical_name=best.canonical_name,
            aliases=best.aliases,
            importance=best.importance,
            confidence=best.confidence,
            attributes=best.attributes,
            created_at=best.created_at,
            updated_at=best.updated_at,
            match="fuzzy",
            score=best_score,
        )

    async def _entity_candidates(
        self,
        *,
        mode: Literal["phrase", "broad"],
        term: str,
        grams: tuple[str, ...],
        limit: int,
    ) -> list[Any]:
        """The candidate rows for one of the two queries, or none.

        The statement and the parameters that statement takes both come
        from the dialect seam, so this call site is engine-agnostic: there
        is no ``if backend == "postgres"`` here, and there is no way for
        the two engines' parameters to drift apart at the call site.
        """
        from paa.storage.relational.errors import QueryRejected

        try:
            sql, params = self._db.dialect.entity_candidates_sql(
                mode=mode,
                term=term,
                grams=grams,
                limit=limit,
            )
            return await self._db.fetch_all(sql, params)
        except QueryRejected as exc:
            # A malformed MATCH expression is a bad *query*, not a broken
            # database. Resolution failing closed (no match) is correct; taking
            # the whole ingestion batch down with it is not.
            log.warning(
                "memory.entity_candidate_query_rejected", mode=mode, term=term, error=str(exc)
            )
            return []

    async def resolve_or_create(
        self,
        name: str,
        class_: str = "concept",
        *,
        importance: float = 0.5,
        aliases: Sequence[str] | None = None,
        fuzzy: bool = True,
    ) -> Entity:
        """Resolve a name, creating the entity when nothing matches."""
        found = await self.resolve_entity(name, fuzzy=fuzzy)
        if found is not None:
            return found
        entity_id = await self.upsert_entity(class_, name, aliases, importance)
        created = await self.get_entity(entity_id)
        if created is None:  # pragma: no cover - the row was just written
            raise KeyError(f"entity {entity_id} vanished immediately after creation")
        return created

    # -- facts -------------------------------------------------------------

    async def add_fact(
        self,
        entity_id: str,
        predicate: str,
        object_value: str,
        domain: MemoryDomain | str = MemoryDomain.SEMANTIC,
        confidence: float = 1.0,
        importance: float = 0.5,
        source_signal_id: str | None = None,
        provenance: Mapping[str, Any] | None = None,
        *,
        scope: str = "global",
        now: datetime | None = None,
        conn: Any = None,
    ) -> str:
        """Append a new fact version. Returns its id.

        ``conn`` lets a caller enlist this write in a transaction it already
        owns. The memory creator needs that: entity creation, every fact from
        one signal, and the signal's status change have to commit together or
        not at all, or a crash leaves a signal marked processed with only half
        its facts written.
        """
        from paa.storage.relational.database import dumps, to_iso, utc_now

        if not predicate.strip():
            raise ValueError("predicate must not be blank")
        if not 0.0 <= confidence <= 1.0:
            raise ValueError(f"confidence must be in [0,1], got {confidence}")
        if not 0.0 <= importance <= 1.0:
            raise ValueError(f"importance must be in [0,1], got {importance}")

        domain_value = domain.value if isinstance(domain, MemoryDomain) else str(domain)
        fact_id = str(uuid.uuid4())
        stamp = to_iso(now or utc_now())
        params = (
            fact_id,
            entity_id,
            predicate.strip(),
            object_value,
            domain_value,
            scope,
            confidence,
            importance,
            source_signal_id,
            dumps(dict(provenance or {})),
            stamp,
            stamp,
        )
        sql = (
            "INSERT INTO hot_serving_active_facts "
            "(id, entity_id, predicate, object_value, memory_domain, memory_scope,"
            " initial_confidence, importance, source_signal_id, provenance,"
            " created_at, last_queried_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)"
        )

        if conn is not None:
            await conn.execute(sql, params)
        else:
            await self._db.execute(sql, params)
        return fact_id

    async def get_fact(self, fact_id: str, *, now: datetime | None = None) -> Fact | None:
        row = await self._db.fetch_one(
            f"SELECT {_FACT_COLUMNS}, e.canonical_name AS entity_name "  # noqa: S608
            "FROM hot_serving_active_facts f "
            "LEFT JOIN hot_serving_entity_index e ON e.id = f.entity_id WHERE f.id = ?",
            (fact_id,),
        )
        return _to_fact(row, now=now) if row is not None else None

    async def supersede(
        self, old_fact_id: str, new_fact_id: str, *, conn: Any = None
    ) -> None:
        """Point an old fact at the version that replaced it.

        Refuses to close a cycle. A cycle is not a theoretical worry: an
        A-supersedes-B, B-supersedes-A pair makes :meth:`supersession_chain`
        non-terminating, and every consumer that walks provenance would hang
        rather than fail.
        """
        if old_fact_id == new_fact_id:
            raise ValueError("a fact cannot supersede itself")

        for fact_id in (old_fact_id, new_fact_id):
            exists = await self._db.fetch_value(
                "SELECT 1 FROM hot_serving_active_facts WHERE id = ?", (fact_id,)
            )
            if not exists:
                raise KeyError(f"no fact with id {fact_id}")

        if old_fact_id in await self.supersession_chain(new_fact_id):
            raise ValueError(
                f"superseding {old_fact_id} by {new_fact_id} would close a provenance cycle"
            )

        sql = "UPDATE hot_serving_active_facts SET superseded_by = ? WHERE id = ?"
        if conn is not None:
            await conn.execute(sql, (new_fact_id, old_fact_id))
        else:
            await self._db.execute(sql, (new_fact_id, old_fact_id))
        log.debug("memory.fact_superseded", old=old_fact_id, new=new_fact_id)

    async def supersession_chain(self, fact_id: str) -> list[str]:
        """``fact_id`` and every version that replaced it, oldest first.

        Guarded by a visited set so a chain corrupted by a direct SQL write
        still terminates.
        """
        chain: list[str] = []
        seen: set[str] = set()
        cursor: str | None = fact_id
        while cursor and cursor not in seen:
            seen.add(cursor)
            chain.append(cursor)
            cursor = await self._db.fetch_value(
                "SELECT superseded_by FROM hot_serving_active_facts WHERE id = ?", (cursor,)
            )
        return chain

    async def latest_version(self, fact_id: str, *, now: datetime | None = None) -> Fact | None:
        """The live head of ``fact_id``'s supersession chain."""
        chain = await self.supersession_chain(fact_id)
        return await self.get_fact(chain[-1], now=now) if chain else None

    async def query_facts(
        self,
        entity_id: str | None = None,
        predicate: str | None = None,
        domain: MemoryDomain | str | None = None,
        min_confidence: float = 0.0,
        limit: int = 50,
        *,
        include_superseded: bool = False,
        now: datetime | None = None,
        max_scan: int = 10_000,
    ) -> list[Fact]:
        """Live facts matching the filters, ranked by **effective** confidence.

        ``min_confidence`` is applied to the decayed value, not the stored one.
        That is the whole point of the method — a fact stored at 0.95 and
        untouched for two years is not a 0.95 fact any more, and a caller that
        filtered in SQL would be handed exactly the stale beliefs decay exists
        to suppress.

        The scan is bounded by ``max_scan`` rather than by ``LIMIT``: the filter
        runs after the rows are read, so a SQL ``LIMIT`` would truncate the
        candidate pool before the ranking that decides which rows matter. Hitting
        the ceiling is logged, never silent.
        """
        clauses: list[str] = []
        params: list[Any] = []
        if not include_superseded:
            clauses.append("f.superseded_by IS NULL")
        if entity_id is not None:
            clauses.append("f.entity_id = ?")
            params.append(entity_id)
        if predicate is not None:
            clauses.append("f.predicate = ?")
            params.append(predicate)
        if domain is not None:
            clauses.append("f.memory_domain = ?")
            params.append(domain.value if isinstance(domain, MemoryDomain) else str(domain))

        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        rows = await self._db.fetch_all(
            f"SELECT {_FACT_COLUMNS}, e.canonical_name AS entity_name "  # noqa: S608
            "FROM hot_serving_active_facts f "
            f"LEFT JOIN hot_serving_entity_index e ON e.id = f.entity_id {where} "
            "ORDER BY f.id LIMIT ?",
            (*params, max_scan),
        )
        if len(rows) == max_scan:
            log.warning("memory.query_scan_ceiling_hit", max_scan=max_scan, filters=len(clauses))

        facts = [_to_fact(row, now=now) for row in rows]
        matched = [f for f in facts if f.confidence >= min_confidence]
        matched.sort(key=lambda f: (-f.confidence, -f.importance, f.created_at))
        return matched[: max(0, limit)]

    async def search_facts_by_text(
        self, query: str, limit: int = 50, *, max_scan: int = 10_000
    ) -> list[Fact]:
        """Keyword fallback over fact predicates and object values.

        There is no fact-level FTS index — only entities have one
        (:mod:`paa.memory.facts` module docstring) — so this is a bounded LIKE
        scan, the relational analogue of ``context_agents._hot_facts``. It
        exists for callers whose query is a *phrase* rather than an entity
        name: the coder agent's ``recall_facts`` resolves the query as an
        entity first, and falls back to this when resolution misses even though
        the phrase may still appear inside a stored object value.

        **The ``LIMIT`` is a scan bound, not a ranking.** Ranking mirrors
        :meth:`query_facts` — effective confidence, then importance, then age —
        and it is applied in Python, over the whole candidate pool, for the
        same reason that method does not push its ``LIMIT`` into SQL. Ordering
        the pool by ``importance`` in SQL and then ranking it by confidence
        would choose the candidate set by one criterion and rank it by
        another, so a high-confidence, low-importance fact outside the top
        slice would never be seen at all. Here the pool is taken in ``id``
        order — neutral, so no criterion quietly decides what is a candidate —
        and the ceiling being hit is logged, never silent.

        The ceiling is the same 10 000 :meth:`query_facts` uses. The LIKE
        predicate is unindexed, so the SQL cost is the table scan and not the
        size of the pool; a bigger pool only buys a more complete ranking,
        which is what two tiers of one recall need in order to agree.

        Superseded rows are excluded: resurrecting a supersession tail would
        hand back beliefs a newer version replaced. LIKE metacharacters in
        ``query`` are escaped, so a stray ``%`` cannot silently widen the scan
        to every row, and a NUL byte is refused outright — SQLite truncates a
        LIKE pattern at a NUL, so ``%\\x00%`` would degrade to a bare ``%`` and
        match everything. See :mod:`paa.storage.relational.like`.

        The NUL is refused rather than dropped, unlike the internal recall
        paths: this query is a model-supplied tool argument, so telling the
        model "that query was rejected" is more useful than searching for
        something the model did not ask for. The tool turns the refusal into
        an error value.

        :raises ValueError: if ``query`` contains a NUL byte.
        """
        term = " ".join(query.split())
        if not term:
            return []
        if "\x00" in term:
            raise ValueError("query must not contain a NUL byte")
        pattern = like_pattern(term)
        rows = await self._db.fetch_all(
            f"SELECT {_FACT_COLUMNS}, e.canonical_name AS entity_name "  # noqa: S608
            "FROM hot_serving_active_facts f "
            "LEFT JOIN hot_serving_entity_index e ON e.id = f.entity_id "
            "WHERE f.superseded_by IS NULL AND "
            f"({like_predicate('f.predicate', 'f.object_value')}) "
            "ORDER BY f.id LIMIT ?",
            (pattern, pattern, max_scan),
        )
        if len(rows) == max_scan:
            log.warning("memory.search_scan_ceiling_hit", max_scan=max_scan, term=term[:120])

        facts = [_to_fact(row) for row in rows]
        facts.sort(key=lambda f: (-f.confidence, -f.importance, f.created_at))
        return facts[: max(0, limit)]

    async def facts_by_source_signal(self, source_signal_id: str) -> list[Fact]:
        """Facts written from one cold-lake signal, oldest first.

        The other end of the ``source_signal_id`` pointer. A fact's evidence is
        walkable because that column resolves to a ``cold_lake_signals`` row
        holding the verbatim assertion; this is how a caller follows it back.

        Superseded rows are included: a fact that a later version replaced is
        still evidence of what was believed at the time, and the chain of
        evidence is the thing being walked.
        """
        rows = await self._db.fetch_all(
            f"SELECT {_FACT_COLUMNS}, e.canonical_name AS entity_name "  # noqa: S608
            "FROM hot_serving_active_facts f "
            "LEFT JOIN hot_serving_entity_index e ON e.id = f.entity_id "
            "WHERE f.source_signal_id = ? ORDER BY f.created_at ASC, f.id ASC",
            (source_signal_id,),
        )
        return [_to_fact(row) for row in rows]

    # -- embedding queue ---------------------------------------------------

    async def pending_embeddings(self, limit: int = 100) -> list[Fact]:
        """The head of the embedding backlog, oldest first.

        Drained rather than paginated: :meth:`mark_embedded` moves rows out of
        the ``embedding_status = 'pending'`` partial index, so the next call
        naturally sees the next batch. An ``OFFSET`` here would skip exactly the
        rows the previous batch removed — the same defect keyset pagination
        exists to avoid in the decay sweep.
        """
        rows = await self._db.fetch_all(
            f"SELECT {_FACT_COLUMNS}, e.canonical_name AS entity_name "  # noqa: S608
            "FROM hot_serving_active_facts f "
            "LEFT JOIN hot_serving_entity_index e ON e.id = f.entity_id "
            "WHERE f.embedding_status = 'pending' AND f.superseded_by IS NULL "
            "ORDER BY f.created_at ASC, f.id ASC LIMIT ?",
            (limit,),
        )
        return [_to_fact(row) for row in rows]

    async def mark_embedded(self, ids: Sequence[str], *, status: str = "indexed") -> None:
        """Record the outcome of an embedding attempt for a batch of facts."""
        if status not in EMBEDDING_STATUSES:
            raise ValueError(
                f"embedding status must be one of {sorted(EMBEDDING_STATUSES)}, got {status!r}"
            )
        if not ids:
            return
        await self._db.execute_many(
            "UPDATE hot_serving_active_facts SET embedding_status = ? WHERE id = ?",
            [(status, fact_id) for fact_id in ids],
        )

    async def count_facts(self, *, include_superseded: bool = False) -> int:
        sql = "SELECT COUNT(*) FROM hot_serving_active_facts"
        if not include_superseded:
            sql += " WHERE superseded_by IS NULL"
        return int(await self._db.fetch_value(sql) or 0)


# ---------------------------------------------------------------------------
# Row mapping
# ---------------------------------------------------------------------------


def _clean_aliases(aliases: Sequence[str] | None, *, exclude: str) -> tuple[str, ...]:
    """The aliases, normalised, de-duplicated case-insensitively.

    The de-duplication folds with the same :func:`fold_case` the gate
    uses, so "one place decides what a fold is" holds here too: a change
    to the fold would otherwise split the gate from the alias
    de-duplication silently.
    """
    out: list[str] = []
    seen = {fold_case(exclude)}
    for alias in aliases or ():
        normalised = " ".join(str(alias).split())
        if not normalised or fold_case(normalised) in seen:
            continue
        seen.add(fold_case(normalised))
        out.append(normalised)
    return tuple(out)


def _merge_aliases(
    existing: Sequence[Any], incoming: Sequence[str], *, exclude: str
) -> list[str]:
    combined = [str(a) for a in existing] + list(incoming)
    return list(_clean_aliases(combined, exclude=exclude))


def _best_candidate(rows: Sequence[Any], term: str, floor: float) -> tuple[Entity | None, float]:
    """The best-scoring candidate of a set, or nothing at all.

    **The gate, in one place.** Every candidate is re-scored here, in
    Python, and the best score wins; the database only decides which rows
    the gate gets to look at. A row is scored against its canonical name
    and against each of its aliases *individually* — an alias is a name
    the entity answers to, and the JSON blob the column holds is a
    different text, so scoring the blob would score something the entity
    is not called.

    **The tie-break is the last maximum, and it is deterministic.** The
    ``>=`` is the repository's own rule and it is not changed here: among
    candidates with the same score, the *last* one wins. That is only
    deterministic because the candidates arrive in a total order — the
    count proxy, then the unique id — so "the last" is a well-defined row
    and not a function of the heap layout.

    **The floor is inclusive.** A candidate scoring exactly the floor is
    accepted, so a floor of 0.34 accepts a 0.34. The floor is the
    *initial* score too: a candidate that does not reach it is not a
    candidate at all, and the returned score is the floor when nothing
    was accepted.
    """
    best: Entity | None = None
    best_score = floor
    for row in rows:
        entity = _to_entity(row)
        score = max(
            trigram_similarity(term, entity.canonical_name),
            *(trigram_similarity(term, a) for a in entity.aliases or ("",)),
        )
        if score >= best_score:
            best, best_score = entity, score
    return best, best_score


def _to_entity(row: Any, *, match: str = "exact") -> Entity:
    from paa.storage.relational.database import from_iso, loads

    aliases = tuple(str(a) for a in (loads(row["aliases"], []) or []))
    return Entity(
        id=as_text(row["id"]),
        class_=str(row["class"]),
        canonical_name=str(row["canonical_name"]),
        aliases=aliases,
        importance=float(row["importance_weight"]),
        confidence=float(row["confidence_rating"]),
        attributes=loads(row["attributes"], {}) or {},
        created_at=from_iso(row["created_at"]),
        updated_at=from_iso(row["updated_at"]),
        match=match,
        score=1.0,
    )


def _to_fact(row: Any, *, now: datetime | None = None) -> Fact:
    """Build a :class:`Fact`, decaying its confidence on the way out.

    An unknown ``memory_domain`` cannot be decayed, so the stored value is
    passed through unchanged and the anomaly is logged. Defaulting to a decaying
    domain would quietly erode facts whose domain was merely misspelt.
    """
    from paa.storage.relational.database import from_iso, loads

    initial = float(row["initial_confidence"])
    last_queried = from_iso(row["last_queried_at"])
    domain = str(row["memory_domain"])
    try:
        confidence = effective_confidence(initial, last_queried, domain, now=now)
    except KeyError:
        log.warning("memory.fact_unknown_domain", fact_id=row["id"], domain=domain)
        confidence = initial

    keys = row.keys()
    return Fact(
        id=as_text(row["id"]),
        entity_id=as_text(row["entity_id"]),
        predicate=str(row["predicate"]),
        object_value=str(row["object_value"]),
        domain=domain,
        scope=str(row["memory_scope"]),
        initial_confidence=initial,
        confidence=confidence,
        importance=float(row["importance"]),
        use_count=int(row["use_count"]),
        source_signal_id=(
            as_text(row["source_signal_id"]) if row["source_signal_id"] is not None else None
        ),
        provenance=loads(row["provenance"], {}) or {},
        embedding_status=str(row["embedding_status"]),
        created_at=from_iso(row["created_at"]),
        last_queried_at=last_queried,
        superseded_by=(
            as_text(row["superseded_by"]) if row["superseded_by"] is not None else None
        ),
        entity_name=row["entity_name"] if "entity_name" in keys else None,
    )
