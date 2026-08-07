"""The Memory Creator — real-time ETL from the cold lake into hot serving.

RFC §2.1 agent 8. A raw signal lands in ``cold_lake_signals``; this agent turns
it into entities and facts, or refuses to and says why. Its budget line in the
RFC is the design constraint that matters most: *"shallow token classifiers on
the local CPU, zero VRAM"*. Extraction is therefore rule-based and
deterministic — no model call on the ingest path, so ingestion cannot stall
behind an inference queue, cannot hallucinate a fact, and produces the same
output for the same input on every replay. Replay determinism is not a nicety
here: the ledger is the source of truth, and a non-deterministic ETL would make
a replayed memory state differ from the original one with nothing to reconcile
against.

:class:`FactExtractor` is the seam where an LLM extractor can be substituted
later without touching this class. It is declared ``async`` purely for that
reason — the rule-based implementation never awaits, but a protocol that was
sync could not accommodate the thing it exists to accommodate.

Failure containment
-------------------
RFC §2.1 agent 8 requires that malformed input be contained rather than
partially absorbed. Two rules implement it:

1. **Stop on first suspicion.** A parse failure or a suspicious pattern aborts
   extraction for the whole signal. Nothing partial is written, the signal is
   marked ``malformed``, and the raw string is archived so the failure can be
   diagnosed from evidence rather than reconstructed from a log line.
2. **One transaction per signal.** Entity creation, every fact and the signal's
   status change commit together. A crash mid-signal therefore leaves the
   signal ``processing`` and zero facts — a state the next batch simply redoes —
   instead of ``processed`` with half its facts, which nothing could detect.

On contradiction the creator writes **nothing**. Both records are quarantined by
:class:`~paa.memory.contradiction.ContradictionDetector` and the result asks the
caller to park the task on ``AWAITING_HUMAN_ATTESTATION``. Absorbing the
non-conflicting half of a contradictory signal would leave memory in a state no
human ever attested to.
"""

from __future__ import annotations

import asyncio
import enum
import hashlib
import re
import sqlite3
import uuid
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable

import structlog

from paa.core.errors import PaaError
from paa.memory.domains import MemoryDomain
from paa.memory.facts import Fact, FactRepository, trigram_similarity

if TYPE_CHECKING:
    from paa.memory.contradiction import ContradictionDetector
    from paa.storage.relational.database import Database

__all__ = [
    "CandidateFact",
    "EmbeddingSink",
    "FactExtractor",
    "MalformedSignalError",
    "MemoryCreator",
    "ProcessingOutcome",
    "ProcessingResult",
    "RuleBasedExtractor",
    "SignalEnvelope",
    "VectorEmbeddingSink",
]

log = structlog.get_logger(__name__)


# ---------------------------------------------------------------------------
# Containment limits
#
# Every one of these is a *containment* bound, not a performance tuning knob:
# breaching one means the payload is not the shape this ETL was built for, and
# the correct response is to stop rather than to truncate. Truncating would
# absorb the prefix of a hostile or corrupt payload and call it memory.
# ---------------------------------------------------------------------------

#: Nesting depth beyond which a payload is treated as an attack or a bug.
MAX_PAYLOAD_DEPTH = 6

#: Longest single object value accepted. A fact is an assertion, not a blob.
MAX_VALUE_CHARS = 8_192

#: Fan-out ceiling for one signal. A payload that yields hundreds of facts is
#: a document, and documents belong in the cold lake, not the fact table.
MAX_CANDIDATES = 200

#: C0 control characters, excluding tab/newline/carriage-return.
_CONTROL_CHARS = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")

#: The world model's managed-block markers. Their appearance in ingested text is
#: an attempt to have the ETL write a marker into a belief-state document, which
#: would let a signal capture the region the curator is allowed to overwrite.
_MARKER_INJECTION = re.compile(r"<!--\s*paa:managed:", re.IGNORECASE)

_ANSI_ESCAPE = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")


class MalformedSignalError(PaaError):
    """A signal cannot be parsed, or looks hostile. Nothing is written."""

    def __init__(self, reason: str, *, signal_id: str | None = None, **details: Any) -> None:
        super().__init__(reason, signal_id=signal_id, **details)
        self.reason = reason
        self.signal_id = signal_id


# ---------------------------------------------------------------------------
# Wire types
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class SignalEnvelope:
    """A ``cold_lake_signals`` row, parsed and ready to extract from."""

    id: str
    channel: str
    payload: Any
    raw: str
    external_id: str | None = None
    received_at: datetime | None = None
    persisted: bool = True
    """False when the signal is not (yet) a row — status updates are then no-ops."""


@dataclass(frozen=True, slots=True)
class CandidateFact:
    """One extracted assertion, before entity resolution.

    ``entity_name`` is a *name*, not an id: resolution is the creator's job, and
    an extractor that had to resolve ids could not be swapped for an LLM one
    without also handing it database access.
    """

    entity_name: str
    predicate: str
    object_value: str
    entity_class: str = "concept"
    domain: str = MemoryDomain.SEMANTIC.value
    confidence: float = 0.8
    importance: float = 0.5
    provenance: Mapping[str, Any] = field(default_factory=dict)


class ProcessingOutcome(str, enum.Enum):
    """What became of one signal."""

    PROCESSED = "processed"
    """Facts were written."""

    IGNORED = "ignored"
    """Parsed cleanly, but contained no assertion worth storing."""

    MALFORMED = "malformed"
    """Unparseable or suspicious. Zero facts written, raw payload archived."""

    QUARANTINED = "quarantined"
    """Contradicted an incumbent belief. Zero facts written; a human must decide."""

    ERROR = "error"
    """An unexpected failure. Isolated to this signal."""


@dataclass(slots=True)
class ProcessingResult:
    """Outcome of one :meth:`MemoryCreator.process_signal` call."""

    signal_id: str
    outcome: ProcessingOutcome
    candidates: int = 0
    facts_written: list[str] = field(default_factory=list)
    entities_touched: list[str] = field(default_factory=list)
    conflicts: list[str] = field(default_factory=list)
    error: str | None = None

    @property
    def requires_human_attestation(self) -> bool:
        """Whether the caller must park the task on ``AWAITING_HUMAN_ATTESTATION``.

        RFC §4.2: the runtime never picks a winner between contradictory
        beliefs, so the task cannot proceed on memory it has not been given.
        """
        return self.outcome is ProcessingOutcome.QUARANTINED

    def summary(self) -> dict[str, Any]:
        return {
            "signal_id": self.signal_id,
            "outcome": self.outcome.value,
            "candidates": self.candidates,
            "facts_written": len(self.facts_written),
            "conflicts": len(self.conflicts),
            "error": self.error,
        }


@dataclass(slots=True)
class BatchReport:
    """Outcome of one :meth:`MemoryCreator.run_batch` pass."""

    claimed: int = 0
    processed: int = 0
    ignored: int = 0
    malformed: int = 0
    quarantined: int = 0
    errors: int = 0
    facts_written: int = 0
    results: list[ProcessingResult] = field(default_factory=list)

    def summary(self) -> dict[str, int]:
        return {
            "claimed": self.claimed,
            "processed": self.processed,
            "ignored": self.ignored,
            "malformed": self.malformed,
            "quarantined": self.quarantined,
            "errors": self.errors,
            "facts_written": self.facts_written,
        }


# ---------------------------------------------------------------------------
# Extraction
# ---------------------------------------------------------------------------


@runtime_checkable
class FactExtractor(Protocol):
    """Turns a signal into candidate facts.

    Async so that an LLM-backed extractor is a drop-in replacement; the default
    implementation is pure CPU and never awaits.
    """

    async def extract(self, envelope: SignalEnvelope) -> list[CandidateFact]:
        """Candidates from one signal.

        Raises :class:`MalformedSignalError` to abort the signal entirely. That
        is the contract: an extractor must never return a partial result for a
        payload it distrusts, because the caller cannot tell a short answer from
        a truncated one.
        """
        ...


#: Keys whose value names the subject of every other key in the payload.
_ENTITY_KEYS = ("entity", "subject", "entity_name", "canonical_name", "name", "title")
_CLASS_KEYS = ("entity_class", "class", "kind", "entity_type")
_DOMAIN_KEYS = ("memory_domain", "domain")
_TEXT_KEYS = ("text", "body", "content", "message", "note", "summary", "description")
#: Sub-objects that describe the primary entity rather than a nested one, so
#: their keys are flattened without a prefix.
_ATTRIBUTE_KEYS = ("metadata", "meta", "attributes", "attrs", "properties", "fields")
_TRIPLE_KEYS = ("facts", "triples", "assertions")

#: Predicate lexicon for the shallow sentence classifier, longest first so that
#: "depends on" is not shredded into "depends" + a dangling "on".
_PREDICATES = (
    "is responsible for",
    "is blocked by",
    "is owned by",
    "is part of",
    "depends on",
    "reports to",
    "belongs to",
    "works with",
    "works for",
    "works at",
    "works on",
    "lives in",
    "based in",
    "consists of",
    "maintains",
    "replaces",
    "produces",
    "requires",
    "contains",
    "supports",
    "manages",
    "blocks",
    "causes",
    "owns",
    "uses",
    "has",
    "was",
    "were",
    "are",
    "is",
)
_SENTENCE_RE = re.compile(
    r"^(?P<subject>.{2,80}?)\s+(?P<predicate>" + "|".join(_PREDICATES) + r")\s+(?P<object>.{2,200})$",
    re.IGNORECASE,
)
_KEY_VALUE_RE = re.compile(r"^(?P<key>[A-Za-z][\w \-/]{1,40})\s*:\s*(?P<value>\S.*)$")
_SENTENCE_SPLIT = re.compile(r"(?<=[.!?])\s+|[\r\n]+")

#: Subjects that refer to the conversation rather than to anything in the world.
_PRONOUN_SUBJECTS = frozenset(
    {
        "i", "we", "you", "he", "she", "it", "they", "them", "us", "me",
        "this", "that", "these", "those", "there", "here", "who", "what",
        "which", "everyone", "someone", "anyone", "nobody", "everything",
    }
)

#: Whole lines that are social protocol, not information.
_NOISE_LINE_RE = re.compile(
    r"^\W*(hi|hey|hello|yo|thanks|thank you|ta|ok|okay|k|sure|yep|yes|yeah|nope|no|"
    r"lol|haha|hmm|huh|got it|will do|sounds good|makes sense|cool|nice|great|awesome|"
    r"morning|afternoon|evening|good morning|good night|bye|cheers|brb|ttyl|np|fyi)\W*$",
    re.IGNORECASE,
)

#: Hedges that make a sentence an opinion rather than an assertion. RFC §4.2
#: scores contradictions on confidence, so admitting hedged text as a fact would
#: manufacture conflicts between two guesses and page a human about them.
_HEDGE_RE = re.compile(
    r"\b(maybe|perhaps|probably|possibly|might|could be|i think|i guess|i believe|"
    r"not sure|unsure|apparently|allegedly|seems|sounds like|rumou?r)\b",
    re.IGNORECASE,
)

_LEADING_ARTICLE_RE = re.compile(r"^(the|a|an|our|their|its|his|her)\s+", re.IGNORECASE)

#: Extraction confidence by rule, ordered by how much interpretation each does.
#: An explicit triple states its own structure; a sentence match is a guess made
#: by thirty regex alternatives. Encoding that difference here is what lets the
#: contradiction score mean something — a shaky sentence extraction should lose
#: to a structured one without a human ever being asked.
_CONFIDENCE_TRIPLE = 0.95
_CONFIDENCE_STRUCTURED = 0.90
_CONFIDENCE_KEY_VALUE = 0.85
_CONFIDENCE_METADATA = 0.80
_CONFIDENCE_SENTENCE = 0.60


class RuleBasedExtractor:
    """The default, deterministic extractor. No model, no network, no VRAM.

    Four rules, in descending order of how much they assume:

    ``triples``
        ``{"facts": [{"subject": ..., "predicate": ..., "object": ...}]}`` — the
        payload states its own structure.
    ``structured``
        Scalar key/value pairs of a JSON object, subject taken from an entity
        key. Nested objects recurse with a dotted predicate; ``metadata``-like
        objects flatten without one, because they describe the same subject.
    ``key_value``
        ``key: value`` lines inside free text.
    ``sentence``
        ``<subject> <predicate> <object>`` against a closed predicate lexicon.

    Everything else is discarded as conversational noise.
    """

    def __init__(self, *, default_class: str = "concept") -> None:
        self._default_class = default_class

    async def extract(self, envelope: SignalEnvelope) -> list[CandidateFact]:
        _reject_suspicious(envelope.raw, signal_id=envelope.id)
        _unparseable_guard(envelope.payload, envelope.id)
        payload = envelope.payload

        if isinstance(payload, str):
            candidates = self._from_text(payload, None, self._default_class, envelope)
        elif isinstance(payload, Mapping):
            candidates = self._from_mapping(payload, envelope)
        elif isinstance(payload, list):
            candidates = []
            for item in payload:
                if isinstance(item, Mapping):
                    candidates.extend(self._from_mapping(item, envelope))
                elif isinstance(item, str):
                    candidates.extend(self._from_text(item, None, self._default_class, envelope))
        else:
            # A bare number, bool or null carries no assertion and no subject.
            # There is nothing to extract and nothing to diagnose — that is a
            # malformed signal, not an empty one.
            raise MalformedSignalError(
                f"payload is a bare {type(payload).__name__}, which asserts nothing",
                signal_id=envelope.id,
            )

        if len(candidates) > MAX_CANDIDATES:
            raise MalformedSignalError(
                "payload fans out past the candidate ceiling",
                signal_id=envelope.id,
                candidates=len(candidates),
                ceiling=MAX_CANDIDATES,
            )
        return candidates

    # -- mapping paths -----------------------------------------------------

    def _from_mapping(self, payload: Mapping[str, Any], env: SignalEnvelope) -> list[CandidateFact]:
        subject = _first_string(payload, _ENTITY_KEYS)
        entity_class = _first_string(payload, _CLASS_KEYS) or self._default_class
        domain = self._domain_of(payload, env)

        out: list[CandidateFact] = []
        for key in _TRIPLE_KEYS:
            out.extend(self._from_triples(payload.get(key), entity_class, domain, env))

        out.extend(
            self._walk(payload, subject, entity_class, domain, env, prefix="", depth=0)
        )
        return out

    def _from_triples(
        self, raw: Any, entity_class: str, domain: str, env: SignalEnvelope
    ) -> list[CandidateFact]:
        if not isinstance(raw, list):
            return []
        out: list[CandidateFact] = []
        for item in raw:
            if not isinstance(item, Mapping):
                raise MalformedSignalError(
                    "triple list contains a non-object entry", signal_id=env.id
                )
            subject = _first_string(item, ("subject", "entity", "s", "from"))
            predicate = _first_string(item, ("predicate", "p", "relation", "rel"))
            obj = item.get("object", item.get("o", item.get("value")))
            if not subject or not predicate or obj is None:
                raise MalformedSignalError(
                    "triple is missing subject, predicate or object", signal_id=env.id
                )
            value = _scalar_to_text(obj, signal_id=env.id)
            if value is None:
                continue
            out.append(
                CandidateFact(
                    entity_name=_strip_article(subject),
                    predicate=_normalise_predicate(predicate),
                    object_value=value,
                    entity_class=_first_string(item, _CLASS_KEYS) or entity_class,
                    domain=domain,
                    confidence=_as_confidence(item.get("confidence"), _CONFIDENCE_TRIPLE),
                    provenance=_provenance(env, "triple"),
                )
            )
        return out

    def _walk(
        self,
        payload: Mapping[str, Any],
        subject: str | None,
        entity_class: str,
        domain: str,
        env: SignalEnvelope,
        *,
        prefix: str,
        depth: int,
    ) -> list[CandidateFact]:
        if depth > MAX_PAYLOAD_DEPTH:
            raise MalformedSignalError(
                "payload nests deeper than the containment ceiling",
                signal_id=env.id,
                depth=depth,
                ceiling=MAX_PAYLOAD_DEPTH,
            )

        out: list[CandidateFact] = []
        for key, value in payload.items():
            name = str(key)
            if not prefix and name.lower() in _RESERVED_KEYS:
                continue

            if name.lower() in _TEXT_KEYS and isinstance(value, str):
                out.extend(self._from_text(value, subject, entity_class, env, domain=domain))
                continue

            if isinstance(value, Mapping):
                nested_prefix = "" if name.lower() in _ATTRIBUTE_KEYS else f"{prefix}{name}."
                out.extend(
                    self._walk(
                        value, subject, entity_class, domain, env,
                        prefix=nested_prefix, depth=depth + 1,
                    )
                )
                continue

            if subject is None:
                # A predicate with no subject is not a fact. Inventing a
                # synthetic entity per orphaned payload would fill the entity
                # index with rows nothing can ever resolve against.
                continue

            confidence = (
                _CONFIDENCE_METADATA if depth > 0 or prefix else _CONFIDENCE_STRUCTURED
            )
            for item in _iter_scalars(value, signal_id=env.id):
                text = _scalar_to_text(item, signal_id=env.id)
                if text is None or _is_noise(text):
                    continue
                out.append(
                    CandidateFact(
                        entity_name=subject,
                        predicate=_normalise_predicate(f"{prefix}{name}"),
                        object_value=text,
                        entity_class=entity_class,
                        domain=domain,
                        confidence=confidence,
                        provenance=_provenance(env, "structured"),
                    )
                )
        return out

    # -- text paths --------------------------------------------------------

    def _from_text(
        self,
        text: str,
        subject: str | None,
        entity_class: str,
        env: SignalEnvelope,
        *,
        domain: str | None = None,
    ) -> list[CandidateFact]:
        resolved_domain = domain or MemoryDomain.SEMANTIC.value
        out: list[CandidateFact] = []

        for chunk in _SENTENCE_SPLIT.split(text):
            line = chunk.strip()
            if not line or _is_noise(line):
                continue

            if (kv := _KEY_VALUE_RE.match(line)) and not _looks_like_url(line):
                value = kv.group("value").strip().rstrip(".")
                if subject is not None and value and not _is_noise(value):
                    out.append(
                        CandidateFact(
                            entity_name=subject,
                            predicate=_normalise_predicate(kv.group("key")),
                            object_value=value,
                            entity_class=entity_class,
                            domain=resolved_domain,
                            confidence=_CONFIDENCE_KEY_VALUE,
                            provenance=_provenance(env, "key_value"),
                        )
                    )
                continue

            candidate = self._from_sentence(line, entity_class, resolved_domain, env)
            if candidate is not None:
                out.append(candidate)
        return out

    def _from_sentence(
        self, line: str, entity_class: str, domain: str, env: SignalEnvelope
    ) -> CandidateFact | None:
        """One ``<subject> <predicate> <object>`` match, or nothing.

        Questions and hedges are rejected before matching: "is the pipeline
        green?" would otherwise parse as a perfectly well-formed assertion that
        the pipeline *is* "green?".
        """
        if line.endswith("?") or _HEDGE_RE.search(line):
            return None

        match = _SENTENCE_RE.match(line.rstrip(".!"))
        if match is None:
            return None

        subject = _strip_article(match.group("subject"))
        if not subject or subject.lower() in _PRONOUN_SUBJECTS:
            return None

        obj = _strip_article(match.group("object"))
        if not obj or _is_noise(obj):
            return None

        return CandidateFact(
            entity_name=subject,
            predicate=_normalise_predicate(match.group("predicate")),
            object_value=obj,
            entity_class=entity_class,
            domain=domain,
            confidence=_CONFIDENCE_SENTENCE,
            provenance=_provenance(env, "sentence"),
        )

    def _domain_of(self, payload: Mapping[str, Any], env: SignalEnvelope) -> str:
        """The payload's declared memory domain, validated.

        An unrecognised domain is malformed rather than coerced to a default.
        The decay sweep refuses to touch facts whose domain it cannot resolve,
        so a typo here would create rows that never decay and never get evicted
        — memory that grows without any error ever being raised.
        """
        declared = _first_string(payload, _DOMAIN_KEYS)
        if declared is None:
            return MemoryDomain.SEMANTIC.value
        try:
            return MemoryDomain(declared.strip().lower()).value
        except ValueError as exc:
            raise MalformedSignalError(
                f"payload declares unknown memory domain {declared!r}", signal_id=env.id
            ) from exc


_RESERVED_KEYS = frozenset(
    {k.lower() for k in (*_ENTITY_KEYS, *_CLASS_KEYS, *_DOMAIN_KEYS, *_TRIPLE_KEYS)}
)


# ---------------------------------------------------------------------------
# Embedding hand-off
# ---------------------------------------------------------------------------


@runtime_checkable
class EmbeddingSink(Protocol):
    """Where newly written facts go to be embedded.

    Narrow by design: the creator must not need to know whether the vector
    substrate is Qdrant, numpy or absent. ``embedding_status = 'pending'`` in
    the fact table is the durable queue, so a sink that is missing or broken
    costs recall latency, never data.
    """

    async def enqueue(self, facts: Sequence[Fact]) -> Sequence[str]:
        """Index these facts. Returns the ids that were successfully indexed."""
        ...


class VectorEmbeddingSink:
    """Adapter onto :mod:`paa.storage.vector`.

    Imports are deferred to call time so this module stays importable — and
    testable — on a machine where the vector extra, or the vector package
    itself, is unavailable.
    """

    def __init__(self, store: Any, embedder: Any, *, collection: str | None = None) -> None:
        self._store = store
        self._embedder = embedder
        self._collection = collection

    async def enqueue(self, facts: Sequence[Fact]) -> Sequence[str]:
        if not facts:
            return []
        try:
            from paa.storage.vector import ACTIVE_FACTS, VectorPoint
        except Exception as exc:
            log.warning("memory.vector_sink_unavailable", error=str(exc))
            return []

        collection = self._collection or ACTIVE_FACTS.name
        vectors = await self._embedder.embed([f.statement for f in facts])
        points = [
            VectorPoint(
                id=fact.id,
                vector=vector,
                payload={
                    "entity_id": fact.entity_id,
                    "predicate": fact.predicate,
                    "memory_domain": fact.domain,
                    "memory_scope": fact.scope,
                    "statement": fact.statement,
                },
            )
            for fact, vector in zip(facts, vectors, strict=False)
        ]
        await self._store.upsert(collection, points)
        return [p.id for p in points]


# ---------------------------------------------------------------------------
# The agent
# ---------------------------------------------------------------------------


class SimilarityFn(Protocol):
    """Similarity between two object values, in [0,1]."""

    def __call__(self, left: str, right: str) -> float: ...


class MemoryCreator:
    """RFC §2.1 agent 8: cold lake -> hot serving, in real time."""

    def __init__(
        self,
        db: Database,
        *,
        repository: FactRepository | None = None,
        detector: ContradictionDetector | None = None,
        extractor: FactExtractor | None = None,
        embedding_sink: EmbeddingSink | None = None,
        similarity_fn: SimilarityFn | None = None,
        max_write_retries: int = 3,
        retry_backoff_seconds: float = 0.05,
    ) -> None:
        from paa.memory.contradiction import ContradictionDetector as _Detector

        self._db = db
        self._facts = repository or FactRepository(db)
        self._detector = detector or _Detector(db)
        self._extractor = extractor or RuleBasedExtractor()
        self._sink = embedding_sink
        self._similarity = similarity_fn or trigram_similarity
        self._max_retries = max(0, max_write_retries)
        self._backoff = retry_backoff_seconds

    # -- single signal -----------------------------------------------------

    async def process_signal(
        self,
        signal: Mapping[str, Any] | str | SignalEnvelope,
        *,
        correlation_id: str | None = None,
        now: datetime | None = None,
    ) -> ProcessingResult:
        """Parse, filter, resolve, check for contradiction, and commit.

        The phases are ordered so that every read happens before any write. That
        ordering is what makes "never partially commit" achievable rather than
        merely intended: by the time the transaction opens, every decision has
        already been made.
        """
        envelope = await self._load(signal)

        try:
            candidates = await self._extractor.extract(envelope)
        except MalformedSignalError as exc:
            await self._quarantine_malformed(envelope, str(exc))
            return ProcessingResult(
                envelope.id, ProcessingOutcome.MALFORMED, error=exc.reason
            )

        candidates = [c for c in candidates if _is_storable(c)]
        if not candidates:
            await self._set_status(envelope, "processed", now=now)
            log.debug("memory.signal_ignored", signal_id=envelope.id, channel=envelope.channel)
            return ProcessingResult(envelope.id, ProcessingOutcome.IGNORED)

        resolved = await self._resolve_entities(candidates)
        conflicts = await self._detect_conflicts(resolved, correlation_id=correlation_id)
        if conflicts:
            await self._set_status(envelope, "quarantined", now=now)
            log.warning(
                "memory.signal_quarantined",
                signal_id=envelope.id,
                conflicts=len(conflicts),
                candidates=len(candidates),
            )
            return ProcessingResult(
                envelope.id,
                ProcessingOutcome.QUARANTINED,
                candidates=len(candidates),
                conflicts=conflicts,
            )

        fact_ids, entity_ids = await self._commit_with_retry(envelope, resolved, now=now)
        await self._enqueue_embeddings(fact_ids)

        log.info(
            "memory.signal_processed",
            signal_id=envelope.id,
            facts=len(fact_ids),
            entities=len(entity_ids),
        )
        return ProcessingResult(
            envelope.id,
            ProcessingOutcome.PROCESSED,
            candidates=len(candidates),
            facts_written=fact_ids,
            entities_touched=entity_ids,
        )

    # -- batch -------------------------------------------------------------

    async def run_batch(self, limit: int = 50, *, now: datetime | None = None) -> BatchReport:
        """Claim and process unprocessed signals, isolating failures per signal.

        One poison signal must not abort the batch. Anything that escapes
        :meth:`process_signal` is caught, recorded against *that* signal, and
        the pass continues — otherwise a single bad row at the head of the
        backlog would stall ingestion permanently and look like an idle system.
        """
        report = BatchReport()
        for envelope in await self._claim(limit):
            report.claimed += 1
            try:
                # Pass the envelope itself, not ``envelope.__dict__``:
                # SignalEnvelope is a slotted frozen dataclass and has no
                # __dict__, so the attribute access raised AttributeError for
                # *every* claimed signal — the whole batch path silently failed.
                # ``_load`` accepts a SignalEnvelope directly and preserves its
                # ``persisted`` flag, so status updates actually take effect.
                result = await self.process_signal(envelope, now=now)
            except Exception as exc:
                log.error("memory.signal_failed", signal_id=envelope.id, error=str(exc))
                await self._quarantine_malformed(envelope, f"unhandled: {exc}")
                result = ProcessingResult(envelope.id, ProcessingOutcome.ERROR, error=str(exc))

            report.results.append(result)
            report.facts_written += len(result.facts_written)
            match result.outcome:
                case ProcessingOutcome.PROCESSED:
                    report.processed += 1
                case ProcessingOutcome.IGNORED:
                    report.ignored += 1
                case ProcessingOutcome.MALFORMED:
                    report.malformed += 1
                case ProcessingOutcome.QUARANTINED:
                    report.quarantined += 1
                case ProcessingOutcome.ERROR:
                    report.errors += 1

        log.info("memory.batch_completed", **report.summary())
        return report

    async def _claim(self, limit: int) -> list[SignalEnvelope]:
        """Move up to ``limit`` unprocessed signals to ``processing``.

        Select and update share one transaction. Under ``BEGIN IMMEDIATE`` that
        makes the claim atomic against any other writer, so two creators cannot
        both believe they own the same signal.
        """
        from paa.storage.relational.database import to_iso, utc_now

        rows: list[Any] = []
        async with self._db.transaction() as conn:
            async with conn.execute(
                "SELECT id, channel, external_id, raw_payload, received_at "
                "FROM cold_lake_signals WHERE sync_status = 'unprocessed' "
                "ORDER BY received_at ASC, id ASC LIMIT ?",
                (limit,),
            ) as cur:
                rows = list(await cur.fetchall())
            if rows:
                stamp = to_iso(utc_now())
                await conn.executemany(
                    "UPDATE cold_lake_signals SET sync_status = 'processing', processed_at = ? "
                    "WHERE id = ? AND sync_status = 'unprocessed'",
                    [(stamp, row["id"]) for row in rows],
                )

        return [self._envelope_from_row(row) for row in rows]

    # -- phases ------------------------------------------------------------

    async def _resolve_entities(
        self, candidates: Sequence[CandidateFact]
    ) -> list[tuple[CandidateFact, str | None]]:
        """Attach an existing entity id to each candidate, or ``None`` to create.

        Read-only. Creation is deferred into the commit transaction so a
        contradiction discovered afterwards leaves no orphaned entity behind.
        """
        cache: dict[str, str | None] = {}
        out: list[tuple[CandidateFact, str | None]] = []
        for candidate in candidates:
            key = candidate.entity_name.lower()
            if key not in cache:
                entity = await self._facts.resolve_entity(candidate.entity_name)
                cache[key] = entity.id if entity else None
            out.append((candidate, cache[key]))
        return out

    async def _detect_conflicts(
        self,
        resolved: Sequence[tuple[CandidateFact, str | None]],
        *,
        correlation_id: str | None,
    ) -> list[str]:
        """Quarantine every candidate that contradicts a live incumbent.

        ``similarity`` is 1.0 rather than an embedding cosine. The contradiction
        module explains why that term does almost no work once candidates are
        pre-filtered to a shared entity and predicate; passing 1.0 keeps the
        metric conservative, and conservative means over-flagging, which costs a
        human prompt instead of a wrong belief acted upon. Value divergence is
        the term that actually discriminates, and it is computed from trigram
        overlap so the deterministic path needs no embedder.
        """
        buffer_ids: list[str] = []
        for candidate, entity_id in resolved:
            if entity_id is None:
                continue  # a brand-new entity has no incumbent to contradict
            incumbents = await self._detector.find_incumbents(
                entity_id, candidate.predicate, candidate.object_value
            )
            for incumbent in incumbents:
                divergence = 1.0 - self._similarity(
                    str(incumbent.get("object_value", "")), candidate.object_value
                )
                assessment = self._detector.assess(
                    incumbent,
                    _challenger_of(candidate, entity_id),
                    similarity=1.0,
                    value_divergence=divergence,
                )
                if assessment.is_conflict:
                    buffer_ids.append(
                        await self._detector.quarantine(
                            assessment, correlation_id=correlation_id
                        )
                    )
        return buffer_ids

    async def _commit_with_retry(
        self,
        envelope: SignalEnvelope,
        resolved: Sequence[tuple[CandidateFact, str | None]],
        *,
        now: datetime | None,
    ) -> tuple[list[str], list[str]]:
        """Commit the signal, retrying on write contention.

        RFC §2.1 agent 8 caps this at three retries. The cap matters: SQLite
        reports contention as ``SQLITE_BUSY`` and an unbounded retry loop turns a
        deadlock into a hang, which is strictly harder to diagnose than a
        failure. The transaction is all-or-nothing, so a retry re-runs it from
        a clean slate rather than resuming a half-written signal.
        """
        attempt = 0
        while True:
            try:
                return await self._commit(envelope, resolved, now=now)
            except sqlite3.OperationalError as exc:
                if attempt >= self._max_retries or not _is_contention(exc):
                    raise
                delay = self._backoff * (2**attempt)
                attempt += 1
                log.warning(
                    "memory.write_contention_retry",
                    signal_id=envelope.id,
                    attempt=attempt,
                    max_retries=self._max_retries,
                    delay=round(delay, 4),
                )
                await asyncio.sleep(delay)

    async def _commit(
        self,
        envelope: SignalEnvelope,
        resolved: Sequence[tuple[CandidateFact, str | None]],
        *,
        now: datetime | None,
    ) -> tuple[list[str], list[str]]:
        from paa.storage.relational.database import to_iso, utc_now

        stamp = to_iso(now or utc_now())
        fact_ids: list[str] = []
        entity_ids: list[str] = []

        async with self._db.transaction() as conn:
            created: dict[str, str] = {}
            for candidate, existing_id in resolved:
                key = candidate.entity_name.lower()
                entity_id = existing_id or created.get(key)
                if entity_id is None:
                    entity_id = await self._facts.upsert_entity(
                        candidate.entity_class,
                        candidate.entity_name,
                        importance=candidate.importance,
                        now=now,
                        conn=conn,
                    )
                    created[key] = entity_id
                if entity_id not in entity_ids:
                    entity_ids.append(entity_id)

                fact_ids.append(
                    await self._facts.add_fact(
                        entity_id,
                        candidate.predicate,
                        candidate.object_value,
                        candidate.domain,
                        candidate.confidence,
                        candidate.importance,
                        envelope.id if envelope.persisted else None,
                        dict(candidate.provenance),
                        now=now,
                        conn=conn,
                    )
                )

            if envelope.persisted:
                await conn.execute(
                    "UPDATE cold_lake_signals SET sync_status = 'processed', processed_at = ?, "
                    "error_detail = NULL WHERE id = ?",
                    (stamp, envelope.id),
                )
        return fact_ids, entity_ids

    async def _enqueue_embeddings(self, fact_ids: Sequence[str]) -> None:
        """Hand new facts to the vector substrate, if one is attached.

        A sink failure leaves the rows ``pending`` rather than marking them
        ``failed``: ``failed`` is a terminal state and a transient Qdrant hiccup
        must not permanently exclude a fact from semantic recall.
        """
        if self._sink is None or not fact_ids:
            return
        facts = [f for f in (await self._facts.get_fact(fid) for fid in fact_ids) if f is not None]
        try:
            indexed = await self._sink.enqueue(facts)
        except Exception as exc:
            log.error("memory.embedding_enqueue_failed", error=str(exc), facts=len(facts))
            return
        if indexed:
            await self._facts.mark_embedded(list(indexed))

    # -- signal bookkeeping ------------------------------------------------

    async def _load(self, signal: Mapping[str, Any] | str | SignalEnvelope) -> SignalEnvelope:
        if isinstance(signal, str):
            row = await self._db.fetch_one(
                "SELECT id, channel, external_id, raw_payload, received_at "
                "FROM cold_lake_signals WHERE id = ?",
                (signal,),
            )
            if row is None:
                raise KeyError(f"no cold-lake signal with id {signal}")
            return self._envelope_from_row(row)

        if isinstance(signal, SignalEnvelope):  # pragma: no cover - defensive
            return signal
        return self._envelope_from_row(signal, check_persistence=True)

    def _envelope_from_row(self, row: Any, *, check_persistence: bool = False) -> SignalEnvelope:
        from paa.storage.relational.database import from_iso, loads

        if isinstance(row, SignalEnvelope):
            return row

        keys = row.keys() if hasattr(row, "keys") else ()
        raw = str(row["raw_payload"] if "raw_payload" in keys else row.get("raw", "null"))
        signal_id = str(row["id"])
        received = row["received_at"] if "received_at" in keys else None

        payload = loads(raw, _UNPARSEABLE)
        if payload is _UNPARSEABLE:
            # Deliberately *not* raised here. Parse failure is an extraction
            # outcome, and routing it through the extractor keeps every
            # malformed path — bad JSON, hostile bytes, bad shape — converging
            # on one containment routine instead of three.
            payload = _Unparseable(raw)

        return SignalEnvelope(
            id=signal_id,
            channel=str(row["channel"] if "channel" in keys else "unknown"),
            payload=payload,
            raw=raw,
            external_id=row["external_id"] if "external_id" in keys else None,
            received_at=from_iso(received) if isinstance(received, str) else None,
            persisted=bool(row["persisted"]) if "persisted" in keys else True,
        )

    async def _set_status(
        self, envelope: SignalEnvelope, status: str, *, now: datetime | None = None
    ) -> None:
        from paa.storage.relational.database import to_iso, utc_now

        if not envelope.persisted:
            return
        await self._db.execute(
            "UPDATE cold_lake_signals SET sync_status = ?, processed_at = ? WHERE id = ?",
            (status, to_iso(now or utc_now()), envelope.id),
        )

    async def _quarantine_malformed(self, envelope: SignalEnvelope, reason: str) -> None:
        """Mark the signal malformed and archive its raw string, atomically.

        The archive is the point. ``cold_lake_signals.raw_payload`` is immutable
        and already holds the bytes, but a malformed signal is exactly the case
        where someone needs the payload *and* the verdict together, addressable
        by a stable URI, without re-deriving which of thousands of rows failed.
        """
        from paa.storage.relational.database import to_iso, utc_now

        raw = envelope.raw
        digest = hashlib.sha256(raw.encode("utf-8", errors="replace")).hexdigest()
        stamp = to_iso(utc_now())

        async with self._db.transaction() as conn:
            await conn.execute(
                "INSERT INTO cold_lake_artifacts_archive "
                "(id, signal_id, virtual_uri, absolute_host_path, sha256_checksum, size_bytes,"
                " compression, payload_content, archived_at) VALUES (?,?,?,?,?,?,?,?,?) "
                "ON CONFLICT(virtual_uri) DO NOTHING",
                (
                    str(uuid.uuid4()),
                    envelope.id if envelope.persisted else None,
                    f"paa://malformed/{envelope.id}",
                    "",  # no host file: the payload is inline in payload_content
                    digest,
                    len(raw.encode("utf-8", errors="replace")),
                    "none",
                    raw,
                    stamp,
                ),
            )
            if envelope.persisted:
                await conn.execute(
                    "UPDATE cold_lake_signals SET sync_status = 'malformed', "
                    "processed_at = ?, error_detail = ? WHERE id = ?",
                    (stamp, reason[:2000], envelope.id),
                )

        log.warning("memory.signal_malformed", signal_id=envelope.id, reason=reason)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


class _Unparseable:
    """Marker for a payload that is not JSON. Rejected by the extractor."""

    __slots__ = ("raw",)

    def __init__(self, raw: str) -> None:
        self.raw = raw


_UNPARSEABLE = object()


def _reject_suspicious(raw: str, *, signal_id: str | None) -> None:
    """Stop before parsing anything that looks hostile or corrupt."""
    if _CONTROL_CHARS.search(raw):
        raise MalformedSignalError(
            "payload contains C0 control characters", signal_id=signal_id
        )
    if _ANSI_ESCAPE.search(raw):
        raise MalformedSignalError("payload contains ANSI escape sequences", signal_id=signal_id)
    if _MARKER_INJECTION.search(raw):
        raise MalformedSignalError(
            "payload contains paa managed-block markers", signal_id=signal_id
        )


def _is_storable(candidate: CandidateFact) -> bool:
    if not candidate.entity_name.strip() or not candidate.predicate.strip():
        return False
    value = candidate.object_value.strip()
    return bool(value) and not _is_noise(value)


def _is_noise(text: str) -> bool:
    """Conversational filler, not information."""
    stripped = text.strip()
    return not stripped or bool(_NOISE_LINE_RE.match(stripped))


def _looks_like_url(line: str) -> bool:
    return bool(re.match(r"^\s*[a-z][a-z0-9+.\-]*://", line, re.IGNORECASE))


def _strip_article(value: str) -> str:
    return " ".join(_LEADING_ARTICLE_RE.sub("", value.strip()).split()).rstrip(".,;:")


def _normalise_predicate(value: str) -> str:
    """Predicates are keys, so they are folded to a single canonical form.

    Without this, "Owns", "owns" and "owned_by " would be three predicates and
    the contradiction detector — which filters on an exact predicate match —
    would never see them as talking about the same thing.
    """
    cleaned = re.sub(r"[^\w.]+", "_", value.strip().lower()).strip("_")
    return cleaned or "value"


def _first_string(payload: Mapping[str, Any], keys: Sequence[str]) -> str | None:
    for key in keys:
        value = payload.get(key)
        if isinstance(value, str) and value.strip():
            return " ".join(value.split())
    return None


def _iter_scalars(value: Any, *, signal_id: str | None) -> Iterable[Any]:
    if isinstance(value, list):
        for item in value:
            if isinstance(item, list | dict):
                # A list of containers under a scalar key is a shape this ETL
                # does not model. Flattening it would invent structure.
                raise MalformedSignalError(
                    "list values must be scalars", signal_id=signal_id
                )
            yield item
    else:
        yield value


def _scalar_to_text(value: Any, *, signal_id: str | None) -> str | None:
    if value is None:
        return None
    if isinstance(value, bool):
        text = "true" if value else "false"
    elif isinstance(value, int | float | str):
        text = str(value)
    else:
        raise MalformedSignalError(
            f"unsupported value type {type(value).__name__}", signal_id=signal_id
        )
    text = text.strip()
    if len(text) > MAX_VALUE_CHARS:
        raise MalformedSignalError(
            "object value exceeds the containment ceiling",
            signal_id=signal_id,
            length=len(text),
            ceiling=MAX_VALUE_CHARS,
        )
    return text or None


def _as_confidence(value: Any, default: float) -> float:
    try:
        parsed = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return default
    return min(1.0, max(0.0, parsed))


def _provenance(env: SignalEnvelope, rule: str) -> dict[str, Any]:
    return {
        "extractor": "rule_based",
        "rule": rule,
        "channel": env.channel,
        "signal_id": env.id,
    }


def _challenger_of(candidate: CandidateFact, entity_id: str) -> dict[str, Any]:
    return {
        "id": None,
        "entity_id": entity_id,
        "predicate": candidate.predicate,
        "object_value": candidate.object_value,
        "memory_domain": candidate.domain,
        "confidence": candidate.confidence,
        "initial_confidence": candidate.confidence,
        "provenance": dict(candidate.provenance),
    }


def _is_contention(exc: sqlite3.OperationalError) -> bool:
    """Whether an OperationalError is write contention rather than a real fault.

    Matched on the message because sqlite3 does not expose SQLITE_BUSY as a
    distinct exception class. A schema error retried three times would only
    delay the inevitable, so anything unrecognised propagates immediately.
    """
    text = str(exc).lower()
    return "locked" in text or "busy" in text


def _unparseable_guard(payload: Any, signal_id: str) -> None:
    if isinstance(payload, _Unparseable):
        raise MalformedSignalError("payload is not valid JSON", signal_id=signal_id)
