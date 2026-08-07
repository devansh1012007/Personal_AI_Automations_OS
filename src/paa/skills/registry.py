"""The hot-serving skill registry — RFC §8's index of what the runtime can do.

Every capability the runtime is allowed to invoke has exactly one row in
``hot_serving_skill_registry``. The registry is the *authority* on which skills
exist, which are live, and how reliable each has proven to be; the Unified Skill
Adapter (:mod:`paa.skills.usa`) reads it during discovery and writes back a
reliability signal after every run.

Two design decisions are worth stating up front.

**Idempotent registration keyed on (skill_name, version).** The schema puts a
UNIQUE constraint on ``skill_name`` alone — there is one live row per capability,
not one per version — so re-registering the *same* ``(name, version)`` must be a
no-op rather than a conflict, and registering a *new* version of an existing name
is an upgrade that rewrites the row. Marketplace refresh loops and start-up
seeding both call ``register`` repeatedly with unchanged contracts; making that
free is what lets them run on every boot without special-casing "already there".

**Search degrades honestly.** Semantic recall needs an embedding index the base
runtime does not ship (see ``pyproject.toml`` — the ``vector`` extra is opt-in).
When a vector store is supplied :meth:`SkillRegistry.search` uses it; when it is
not, it falls back to a trigram/``LIKE`` scan over name and description and ranks
with the same bounded Jaccard similarity the entity resolver uses
(:func:`paa.memory.facts.trigram_similarity`), so the fallback returns a
*meaningfully ordered* list rather than whatever SQL happened to emit first.
"""

from __future__ import annotations

import uuid
from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable

import structlog

from paa.core.errors import SkillContractError
from paa.memory.facts import trigram_similarity
from paa.skills.contracts import SkillContract
from paa.storage.relational.database import dumps, loads, to_iso, utc_now

if TYPE_CHECKING:
    from paa.storage.relational.database import Database

__all__ = ["SkillRegistry", "SkillVectorStore"]

log = structlog.get_logger(__name__)

#: Columns selected when rebuilding a contract, in a fixed order so the row->
#: contract mapping never depends on ``SELECT *`` column ordering.
_CONTRACT_COLUMNS = (
    "skill_name",
    "provider",
    "version",
    "description",
    "input_schema",
    "output_schema",
    "risk_profile",
    "required_permissions",
    "reliability_weight",
    "invocation",
    "source_uri",
    "source_checksum",
    "signature",
)

#: The full contract projection, assembled once from the fixed column tuple. Built
#: by concatenation of module constants — never from caller input — so every read
#: query reuses one trusted, unchanging SELECT list rather than interpolating one.
_SELECT_CONTRACT = (
    "SELECT " + ", ".join(_CONTRACT_COLUMNS) + " FROM hot_serving_skill_registry"  # noqa: S608
)


@runtime_checkable
class SkillVectorStore(Protocol):
    """Minimal semantic-recall surface the registry can drive.

    Kept to a single method so any of the project's vector backends — or a test
    double — satisfies it structurally. Returns skill *names* ranked best-first;
    the registry hydrates the full contracts itself, because the store indexes
    embeddings, not the authoritative registry rows.
    """

    async def search(self, intent: str, *, limit: int) -> list[str]:
        """Return up to ``limit`` skill names most relevant to ``intent``."""
        ...


class SkillRegistry:
    """CRUD + discovery over ``hot_serving_skill_registry``.

    Holds no state beyond the :class:`~paa.storage.relational.database.Database`
    handle; every method reads or writes the table directly, so two registries
    over the same database can never disagree.
    """

    def __init__(self, db: Database) -> None:
        self._db = db

    # -- registration ------------------------------------------------------

    async def register(self, contract: SkillContract) -> SkillContract:
        """Insert or upgrade ``contract``. Idempotent on ``(skill_name, version)``.

        The three outcomes:

        * **no existing row** — insert.
        * **existing row, same version** — no write at all; the stored row is
          authoritative and re-registration is a boot-time no-op.
        * **existing row, different version** — rewrite the row in place
          (the UNIQUE constraint on ``skill_name`` forbids two rows), preserving
          the row ``id`` and ``installed_at`` so provenance survives the upgrade.

        Reactivates a previously :meth:`deactivate`\\ d skill: re-registering a
        capability is the operator asking for it back.

        :raises SkillContractError: if ``contract`` is not a valid
            :class:`~paa.skills.contracts.SkillContract`.
        """
        if not isinstance(contract, SkillContract):  # pragma: no cover - defensive
            raise SkillContractError("register expects a SkillContract instance")

        row = contract.to_row()
        stamp = to_iso(utc_now())

        async with self._db.transaction() as conn:
            async with conn.execute(
                "SELECT id, version, installed_at FROM hot_serving_skill_registry "
                "WHERE skill_name = ?",
                (contract.skill_name,),
            ) as cur:
                existing = await cur.fetchone()

            if existing is not None and str(existing["version"]) == contract.version:
                # Same name and version: nothing to do. Re-activate in case it
                # was deactivated, but do not churn the schema/reliability data.
                await conn.execute(
                    "UPDATE hot_serving_skill_registry SET is_active = 1, updated_at = ? "
                    "WHERE skill_name = ?",
                    (stamp, contract.skill_name),
                )
                log.debug(
                    "skills.registry.register_idempotent",
                    skill=contract.skill_name,
                    version=contract.version,
                )
                return contract

            params = (
                dumps(row["input_schema"]),
                dumps(row["output_schema"]),
                row["risk_profile"],
                dumps(row["required_permissions"]),
                row["reliability_weight"],
                dumps(row["invocation"]),
                row["source_uri"],
                row["source_checksum"],
                row["signature"],
            )

            if existing is None:
                await conn.execute(
                    "INSERT INTO hot_serving_skill_registry "
                    "(id, skill_name, provider, version, description, input_schema, "
                    " output_schema, risk_profile, required_permissions, reliability_weight, "
                    " invocation, source_uri, source_checksum, signature, is_active, "
                    " installed_at, updated_at) "
                    "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,1,?,?)",
                    (
                        str(uuid.uuid4()),
                        row["skill_name"],
                        row["provider"],
                        row["version"],
                        row["description"],
                        *params,
                        stamp,
                        stamp,
                    ),
                )
                log.info(
                    "skills.registry.registered",
                    skill=contract.skill_name,
                    version=contract.version,
                    provider=contract.provider,
                )
            else:
                # Upgrade: rewrite in place, keeping id and installed_at.
                await conn.execute(
                    "UPDATE hot_serving_skill_registry SET provider = ?, version = ?, "
                    "description = ?, input_schema = ?, output_schema = ?, risk_profile = ?, "
                    "required_permissions = ?, reliability_weight = ?, invocation = ?, "
                    "source_uri = ?, source_checksum = ?, signature = ?, is_active = 1, "
                    "updated_at = ? WHERE skill_name = ?",
                    (
                        row["provider"],
                        row["version"],
                        row["description"],
                        *params,
                        stamp,
                        contract.skill_name,
                    ),
                )
                log.info(
                    "skills.registry.upgraded",
                    skill=contract.skill_name,
                    from_version=str(existing["version"]),
                    to_version=contract.version,
                )
        return contract

    # -- reads -------------------------------------------------------------

    async def get(self, name: str, *, include_inactive: bool = False) -> SkillContract | None:
        """Return the contract for ``name``, or ``None`` if absent.

        Deactivated skills are hidden by default: a caller asking "can I run
        this?" must get ``None`` for a retired skill rather than a contract it
        will then be refused at dispatch. Pass ``include_inactive=True`` for
        administrative introspection.
        """
        clause = "" if include_inactive else " AND is_active = 1"
        row = await self._db.fetch_one(
            _SELECT_CONTRACT + " WHERE skill_name = ?" + clause,
            (name,),
        )
        return _row_to_contract(row) if row is not None else None

    async def list_active(self) -> list[SkillContract]:
        """Every live skill, ordered by name for stable output."""
        rows = await self._db.fetch_all(
            _SELECT_CONTRACT + " WHERE is_active = 1 ORDER BY skill_name"
        )
        return [c for row in rows if (c := _row_to_contract(row)) is not None]

    async def search(
        self,
        intent: str,
        *,
        vector_store: SkillVectorStore | None = None,
        limit: int = 10,
    ) -> list[SkillContract]:
        """Discover active skills relevant to ``intent`` (RFC §8.2 step 1).

        Semantic when ``vector_store`` is supplied, trigram/``LIKE`` otherwise.
        Only active skills are ever returned — discovery must never surface a
        capability the runtime would then refuse to dispatch.
        """
        if not intent.strip():
            return []

        if vector_store is not None:
            return await self._semantic_search(intent, vector_store, limit)
        return await self._trigram_search(intent, limit)

    async def _semantic_search(
        self, intent: str, vector_store: SkillVectorStore, limit: int
    ) -> list[SkillContract]:
        names = await vector_store.search(intent, limit=limit)
        results: list[SkillContract] = []
        for name in names:
            contract = await self.get(name)
            if contract is not None:  # a stale index entry must not crash discovery
                results.append(contract)
        return results

    async def _trigram_search(self, intent: str, limit: int) -> list[SkillContract]:
        """Fallback recall: bounded ``LIKE`` prefilter, then Jaccard re-rank.

        The ``LIKE`` clause keeps the candidate set small on a large registry;
        the trigram similarity then produces an ordering that means the same
        thing for every query, which ``LIKE`` alone does not. A query that
        matches nothing by substring still gets a full trigram pass over the
        active set, so a fuzzy/misspelled intent is not silently dropped.
        """
        like = f"%{intent.strip()}%"
        rows = await self._db.fetch_all(
            _SELECT_CONTRACT + " WHERE is_active = 1 AND (skill_name LIKE ? OR description LIKE ?)",
            (like, like),
        )
        if not rows:
            rows = await self._db.fetch_all(_SELECT_CONTRACT + " WHERE is_active = 1")

        scored: list[tuple[float, SkillContract]] = []
        for row in rows:
            contract = _row_to_contract(row)
            if contract is None:
                continue
            haystack = f"{contract.skill_name} {contract.description}"
            score = max(
                trigram_similarity(intent, contract.skill_name),
                trigram_similarity(intent, haystack),
            )
            scored.append((score, contract))

        # Stable secondary sort on name so equal scores are deterministic.
        scored.sort(key=lambda item: (-item[0], item[1].skill_name))
        return [contract for _, contract in scored[:limit]]

    # -- mutation ----------------------------------------------------------

    async def deactivate(self, name: str) -> bool:
        """Retire a skill without deleting its row (and its reliability history).

        Returns ``True`` if a live row was flipped to inactive, ``False`` if the
        skill was absent or already inactive. Soft-delete rather than ``DELETE``
        so the learned ``reliability_weight`` survives a re-activation and so the
        ledger's references to the skill never dangle.
        """
        affected = await self._db.execute(
            "UPDATE hot_serving_skill_registry SET is_active = 0, updated_at = ? "
            "WHERE skill_name = ? AND is_active = 1",
            (to_iso(utc_now()), name),
        )
        if affected:
            log.info("skills.registry.deactivated", skill=name)
        return bool(affected)

    async def update_reliability(self, name: str, delta: float) -> float:
        """Nudge a skill's reliability weight by ``delta``, clamped to ``[0, 1]``.

        RFC §8.2 step 7. Called by the USA after every invocation: a small
        positive ``delta`` on a clean, schema-valid success and a negative one on
        malformed output. The clamp is applied here rather than trusting the
        caller because the value feeds a CHECK-constrained column and a weight
        outside ``[0, 1]`` would abort the write.

        :returns: the new weight. Returns the *current* weight unchanged if the
            skill is absent, so a race with :meth:`deactivate` or a deleted skill
            does not raise on the learning path.
        """
        async with self._db.transaction() as conn:
            async with conn.execute(
                "SELECT reliability_weight FROM hot_serving_skill_registry WHERE skill_name = ?",
                (name,),
            ) as cur:
                row = await cur.fetchone()
            if row is None:
                log.warning("skills.registry.reliability_skill_absent", skill=name)
                return 0.0

            current = float(row["reliability_weight"])
            updated = min(1.0, max(0.0, current + delta))
            await conn.execute(
                "UPDATE hot_serving_skill_registry SET reliability_weight = ?, updated_at = ? "
                "WHERE skill_name = ?",
                (updated, to_iso(utc_now()), name),
            )
        log.debug(
            "skills.registry.reliability_updated",
            skill=name,
            delta=round(delta, 4),
            weight=round(updated, 4),
        )
        return updated


def _row_to_contract(row: Any) -> SkillContract | None:
    """Rebuild a contract from a registry row, tolerating a corrupt one.

    A single unparseable row — a hand-edited registry, a schema migration bug —
    must not take down discovery for every *other* skill, so this logs and
    returns ``None`` rather than propagating. The JSON columns are decoded with
    the database module's tolerant :func:`loads` for the same reason.
    """
    try:
        return SkillContract.parse(
            {
                "skill_name": row["skill_name"],
                "provider": row["provider"],
                "version": row["version"],
                "description": row["description"],
                "input_schema": loads(row["input_schema"], {}),
                "output_schema": loads(row["output_schema"], {}),
                "risk_profile": row["risk_profile"],
                "required_permissions": loads(row["required_permissions"], []),
                "reliability_weight": row["reliability_weight"],
                "invocation": loads(row["invocation"], {}),
                "source_uri": row["source_uri"],
                "source_checksum": row["source_checksum"],
                "signature": row["signature"],
            }
        )
    except SkillContractError:
        log.warning("skills.registry.corrupt_row", skill=row["skill_name"])
        return None
