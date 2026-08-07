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
schema maintains by trigger. FTS5 ranks with bm25, which is unbounded and not
comparable across queries, so a bm25 score cannot be thresholded. We rank with
bm25 but *gate* with an explicit character-trigram Jaccard similarity, which is
bounded in [0,1] and means the same thing for every query — otherwise "xyz"
would resolve to whatever row happened to come back first.
"""

from __future__ import annotations

import re
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from typing import TYPE_CHECKING, Any

import structlog

from paa.memory.decay import effective_confidence
from paa.memory.domains import MemoryDomain

if TYPE_CHECKING:
    from paa.storage.relational.database import Database

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
#: attempt and nothing else.
_MIN_TRIGRAM_CHARS = 3

_WORD_RE = re.compile(r"[^\W_]+", re.UNICODE)


def trigram_similarity(left: str, right: str) -> float:
    """Jaccard overlap of the two strings' character trigram sets, in [0,1].

    Deliberately the same shape as ``pg_trgm.similarity`` so the SQLite and
    PostgreSQL paths gate fuzzy matches at the same threshold. Case-folded and
    whitespace-normalised, because "Deploy  Pipeline" and "deploy pipeline" are
    the same name.
    """
    a, b = _trigrams(left), _trigrams(right)
    if not a or not b:
        return 1.0 if left.strip().lower() == right.strip().lower() else 0.0
    return len(a & b) / len(a | b)


def _trigrams(value: str) -> set[str]:
    normalised = " ".join(value.lower().split())
    if len(normalised) < _MIN_TRIGRAM_CHARS:
        return set()
    padded = f"  {normalised} "
    return {padded[i : i + 3] for i in range(len(padded) - 2)}


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

    def __init__(self, db: Database) -> None:
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
            "WHERE lower(canonical_name) = lower(?)",
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

        entity_id = str(row["id"])
        merged = _merge_aliases(loads(row["aliases"], []) or [], incoming, exclude=name)
        attrs = dict(loads(row["attributes"], {}) or {})
        attrs.update(attributes or {})
        await conn.execute(
            "UPDATE hot_serving_entity_index SET class = ?, aliases = ?, "
            "importance_weight = MAX(importance_weight, ?), attributes = ?, updated_at = ? "
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

        ``fuzzy_floor`` defaults to 0.34, the ``pg_trgm`` default, which
        tolerates a wrong or missing word in a short name while rejecting
        unrelated strings.
        """
        term = " ".join(name_or_alias.split())
        if not term:
            return None

        exact = await self._db.fetch_one(
            "SELECT * FROM hot_serving_entity_index WHERE lower(canonical_name) = lower(?)",
            (term,),
        )
        if exact is not None:
            return _to_entity(exact)

        alias = await self._db.fetch_one(
            "SELECT e.* FROM hot_serving_entity_index e, json_each(e.aliases) a "
            "WHERE lower(a.value) = lower(?) LIMIT 1",
            (term,),
        )
        if alias is not None:
            return _to_entity(alias, match="alias")

        if not fuzzy or len(term) < _MIN_TRIGRAM_CHARS:
            return None
        return await self._fuzzy_resolve(term, fuzzy_floor, candidate_limit)

    async def _fuzzy_resolve(self, term: str, floor: float, limit: int) -> Entity | None:
        """Trigram search over ``hot_serving_entity_fts``.

        Two passes: the whole term as a phrase, then its words OR'd together.
        The second pass is what lets "Johnson Alice" find "Alice Johnson" —
        trigram phrase matching is order-sensitive, and humans are not.
        """
        candidates = await self._fts_candidates(_fts_phrase(term), limit)
        if not candidates:
            words = [w for w in _WORD_RE.findall(term) if len(w) >= _MIN_TRIGRAM_CHARS]
            if not words:
                return None
            candidates = await self._fts_candidates(
                " OR ".join(_fts_phrase(w) for w in words), limit
            )

        best: Entity | None = None
        best_score = floor
        for row in candidates:
            entity = _to_entity(row)
            score = max(
                trigram_similarity(term, entity.canonical_name),
                *(trigram_similarity(term, a) for a in entity.aliases or ("",)),
            )
            if score >= best_score:
                best, best_score = entity, score

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

    async def _fts_candidates(self, query: str, limit: int) -> list[Any]:
        import sqlite3

        try:
            return await self._db.fetch_all(
                "SELECT e.* FROM hot_serving_entity_fts f "
                "JOIN hot_serving_entity_index e ON e.rowid = f.rowid "
                "WHERE hot_serving_entity_fts MATCH ? "
                "ORDER BY bm25(hot_serving_entity_fts) LIMIT ?",
                (query, limit),
            )
        except sqlite3.OperationalError as exc:
            # A malformed MATCH expression is a bad *query*, not a broken
            # database. Resolution failing closed (no match) is correct; taking
            # the whole ingestion batch down with it is not.
            log.warning("memory.entity_fts_query_rejected", query=query, error=str(exc))
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


def _fts_phrase(value: str) -> str:
    """Quote a term as an FTS5 phrase literal.

    Everything the ingestion path resolves is untrusted text, and FTS5 MATCH is
    a query language: an unquoted ``AND`` or a stray ``*`` would be parsed as an
    operator. Doubling embedded quotes makes the whole term a literal.
    """
    return '"' + value.replace('"', '""') + '"'


def _clean_aliases(aliases: Sequence[str] | None, *, exclude: str) -> tuple[str, ...]:
    out: list[str] = []
    seen = {exclude.lower()}
    for alias in aliases or ():
        normalised = " ".join(str(alias).split())
        if not normalised or normalised.lower() in seen:
            continue
        seen.add(normalised.lower())
        out.append(normalised)
    return tuple(out)


def _merge_aliases(
    existing: Sequence[Any], incoming: Sequence[str], *, exclude: str
) -> list[str]:
    combined = [str(a) for a in existing] + list(incoming)
    return list(_clean_aliases(combined, exclude=exclude))


def _to_entity(row: Any, *, match: str = "exact") -> Entity:
    from paa.storage.relational.database import from_iso, loads

    aliases = tuple(str(a) for a in (loads(row["aliases"], []) or []))
    return Entity(
        id=str(row["id"]),
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
        id=str(row["id"]),
        entity_id=str(row["entity_id"]),
        predicate=str(row["predicate"]),
        object_value=str(row["object_value"]),
        domain=domain,
        scope=str(row["memory_scope"]),
        initial_confidence=initial,
        confidence=confidence,
        importance=float(row["importance"]),
        use_count=int(row["use_count"]),
        source_signal_id=row["source_signal_id"],
        provenance=loads(row["provenance"], {}) or {},
        embedding_status=str(row["embedding_status"]),
        created_at=from_iso(row["created_at"]),
        last_queried_at=last_queried,
        superseded_by=row["superseded_by"],
        entity_name=row["entity_name"] if "entity_name" in keys else None,
    )
