"""Shared fixtures for the effect-registry suite (C9.2).

The house style is the C6.3 chaos suite's: the tests drive the real
journal, the real ledger store and the real workspace jail — only the
blob store is a fake, because the content-addressed stores it stands in
for are already covered by their own suites and the contract needed here
is exactly the two-method :class:`~paa.ledger.events.BlobStoreLike`
shape.
"""

from __future__ import annotations

import hashlib
import types
import uuid
from dataclasses import dataclass, field
from pathlib import Path

import pytest

from paa.effects.files import capture_prior
from paa.effects.spec import EffectKind, EffectSpec
from paa.ledger.store import LedgerStore
from paa.pipelines.journal import CallJournal


class FakeBlobStore:
    """The two-method blob contract over a dict: content-addressed, in-memory.

    ``put`` returns an object with a ``sha256`` (what
    :func:`paa.ledger.events.store_blob_bytes` verifies against), and
    ``get`` raises on an unknown address, matching the cold lake's own
    ``NotFound`` behaviour closely enough for the resolution paths.
    """

    def __init__(self) -> None:
        self._blobs: dict[str, bytes] = {}
        self.puts: list[str] = []

    def put(self, data: bytes, *, compress: bool = True) -> object:
        digest = hashlib.sha256(data).hexdigest()
        self._blobs[digest] = data
        self.puts.append(digest)
        return types.SimpleNamespace(sha256=digest)

    def get(self, digest: str) -> bytes:
        if digest not in self._blobs:
            raise KeyError(digest)
        return self._blobs[digest]

    def __contains__(self, digest: object) -> bool:
        return digest in self._blobs

    def corrupt(self, digest: str, data: bytes) -> None:
        """The operator-error injection door: wrong bytes under an address.

        A content-addressed store should never be able to hold this state
        (the address IS the digest); the rollback tests use it to produce
        the one failure a restore can genuinely hit at execution time —
        the fetched bytes do not match the digest the record pinned, so
        the compensation must refuse rather than write wrong content.
        """
        self._blobs[digest] = data


@pytest.fixture
def blob_store() -> FakeBlobStore:
    return FakeBlobStore()


@dataclass
class ScopeBuilder:
    """Drives the call journal the way an effect-declaring call site does.

    One helper per journalled mutation, in the exact order ADR-0054's door
    expects: capture prior state *before* the mutation, do the work,
    attach the effect to the COMPLETED row. Tests that want a *malformed*
    or unusual record build their own rows; everything else shares this
    so the seeding cannot drift from the discipline it exists to pin.
    """

    ledger: LedgerStore
    blobs: FakeBlobStore
    workspace: Path

    _runs: set[uuid.UUID] = field(default_factory=set)

    async def _ensure_started(self, journal: CallJournal, run_id: uuid.UUID) -> None:
        if run_id not in self._runs:
            await journal.run_started("demo.writer", "1.0.0")
            self._runs.add(run_id)

    async def write_file(
        self,
        run_id: uuid.UUID,
        seq: int,
        rel_path: str,
        content: str,
        *,
        method: str = "fs.write",
        kind: EffectKind = EffectKind.FILE_WRITE,
    ) -> EffectSpec:
        """Journal one file-writing call and perform the real write."""
        journal = CallJournal(self.ledger, run_id, blob_store=self.blobs)
        await self._ensure_started(journal, run_id)
        compensation = capture_prior(self.workspace, rel_path, self.blobs)
        await journal.call_proposed(seq, method, {"path": rel_path})
        target = self.workspace / rel_path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(content.encode("utf-8"))
        effect = EffectSpec.for_call(run_id, seq, kind, compensation)
        await journal.call_completed(seq, method, {"written": rel_path}, effect=effect)
        return effect

    async def mark_irreversible(
        self, run_id: uuid.UUID, seq: int, *, method: str = "mail.send"
    ) -> EffectSpec:
        """Journal one external call recorded as the irreversible marker."""
        journal = CallJournal(self.ledger, run_id, blob_store=self.blobs)
        await self._ensure_started(journal, run_id)
        await journal.call_proposed(seq, method, {"to": "world"})
        effect = EffectSpec.for_call(run_id, seq, EffectKind.IRREVERSIBLE)
        await journal.call_completed(seq, method, {"sent": True}, effect=effect)
        return effect


@pytest.fixture
def workspace(tmp_path: Path) -> Path:
    root = tmp_path / "ws"
    root.mkdir()
    return root


@pytest.fixture
def builder(
    ledger: LedgerStore, blob_store: FakeBlobStore, workspace: Path
) -> ScopeBuilder:
    return ScopeBuilder(ledger=ledger, blobs=blob_store, workspace=workspace)
