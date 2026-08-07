"""Host-file archival — immutable copies of the files tasks read and produce.

An artifact is a snapshot of a file at a moment: the attachment a signal
carried, the input a worker consumed, the diff it emitted. The row records
*where the file was* (``absolute_host_path``) and *what it contained*
(``sha256_checksum`` plus a CAS pointer), which is what makes a ledger entry
saying "the worker edited ``config.yaml``" mean something six months later, when
that path holds entirely different bytes.

Virtual vs host paths
---------------------
``virtual_uri`` is the stable, content-derived handle other rows reference;
``absolute_host_path`` is where the file happened to live. They are separate
columns because the second one goes stale — the workspace gets cleaned, the file
is renamed, the whole directory moves — while the first must not. The default
virtual URI derives from the content hash, so archiving the same bytes under the
same filename twice is idempotent rather than a duplicate row.

Streaming
---------
Nothing here loads a file to hash it. :func:`paa.storage.coldlake.cas.copy_file_into`
streams host files in chunks, and :meth:`ArtifactArchive.verify` re-reads the
same way. A 500 MB artifact must cost chunk-sized memory, not 500 MB (RFC §11.2).
"""

from __future__ import annotations

import hashlib
import sqlite3
import uuid
from collections.abc import Iterator
from datetime import datetime
from pathlib import Path
from typing import Any, Final

import structlog
from pydantic import BaseModel, ConfigDict

from paa.core.errors import StorageError
from paa.storage.coldlake.cas import (
    BlobRef,
    ContentAddressedStore,
    copy_file_into,
    export_to_file,
)
from paa.storage.relational.database import Database, from_iso, to_iso, utc_now

__all__ = ["ArchivedArtifact", "ArtifactArchive"]

log = structlog.get_logger(__name__)

_SUBSTRATE: Final = "cold_lake_artifacts"

#: Scheme for the stable handle. Distinct from ``cas://`` on purpose: a virtual
#: URI names *an archival act* (this file, from this path, at this time), while a
#: CAS URI names *bytes*. Many artifacts can share one CAS blob.
_VIRTUAL_SCHEME: Final = "paa://artifacts"

_COLUMNS: Final = """
    id, signal_id, correlation_id, virtual_uri, absolute_host_path, sha256_checksum,
    size_bytes, compression, blob_uri, payload_content, archived_at
"""


class ArchivedArtifact(BaseModel):
    """One row of ``cold_lake_artifacts_archive``."""

    model_config = ConfigDict(frozen=True)

    id: str
    signal_id: str | None
    correlation_id: str | None
    virtual_uri: str
    absolute_host_path: str
    sha256_checksum: str
    size_bytes: int
    compression: str
    blob_uri: str | None
    payload_content: str | None
    archived_at: datetime

    @property
    def host_path(self) -> Path:
        return Path(self.absolute_host_path)


class ArtifactArchive:
    """Archive and retrieve host files, backed by the content-addressed store."""

    def __init__(self, db: Database, cas: ContentAddressedStore) -> None:
        self._db = db
        self._cas = cas

    # -- writes ------------------------------------------------------------

    async def archive(
        self,
        path: Path | str,
        *,
        correlation_id: str | None = None,
        signal_id: str | None = None,
        virtual_uri: str | None = None,
        compress: bool = True,
    ) -> ArchivedArtifact:
        """Copy a host file into the archive and record it.

        Idempotent on ``virtual_uri``: re-archiving identical content returns the
        existing row. If a *different* checksum turns up under a virtual URI that
        already exists, that is a genuine conflict — two different files claiming
        one name — and it raises rather than silently overwriting the record of
        what was archived first.
        """
        source = Path(path)
        if not source.is_file():
            raise StorageError(
                "cannot archive: not a readable file",
                substrate=_SUBSTRATE,
                path=str(source),
            )

        ref: BlobRef = copy_file_into(self._cas, source, compress=compress)
        uri = virtual_uri or self.default_virtual_uri(source.name, ref.sha256)

        incumbent = await self.get_by_uri(uri)
        if incumbent is not None:
            if incumbent.sha256_checksum == ref.sha256:
                log.debug("artifact.already_archived", virtual_uri=uri, sha256=ref.sha256)
                return incumbent
            raise StorageError(
                "virtual_uri already names different content",
                substrate=_SUBSTRATE,
                virtual_uri=uri,
                existing_checksum=incumbent.sha256_checksum,
                incoming_checksum=ref.sha256,
            )

        artifact = ArchivedArtifact(
            id=str(uuid.uuid4()),
            signal_id=signal_id,
            correlation_id=correlation_id,
            virtual_uri=uri,
            # Resolved so the record survives the caller's working directory
            # changing between archival and any later forensic read.
            absolute_host_path=str(source.resolve()),
            sha256_checksum=ref.sha256,
            size_bytes=ref.size_bytes,
            compression="zstd" if compress else "none",
            blob_uri=ref.uri,
            payload_content=None,
            archived_at=utc_now(),
        )
        try:
            await self._db.execute(
                f"INSERT INTO cold_lake_artifacts_archive ({_COLUMNS}) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    artifact.id,
                    artifact.signal_id,
                    artifact.correlation_id,
                    artifact.virtual_uri,
                    artifact.absolute_host_path,
                    artifact.sha256_checksum,
                    artifact.size_bytes,
                    artifact.compression,
                    artifact.blob_uri,
                    artifact.payload_content,
                    to_iso(artifact.archived_at),
                ),
            )
        except sqlite3.IntegrityError as exc:
            # Lost a race on the UNIQUE virtual_uri. The winner archived the same
            # content (same URI implies same hash under the default scheme), so
            # returning theirs is correct.
            racer = await self.get_by_uri(uri)
            if racer is not None and racer.sha256_checksum == ref.sha256:
                return racer
            raise StorageError(
                f"artifact insert rejected: {exc}",
                substrate=_SUBSTRATE,
                virtual_uri=uri,
            ) from exc

        log.info(
            "artifact.archived",
            virtual_uri=uri,
            sha256=ref.sha256,
            size_bytes=ref.size_bytes,
            stored_bytes=ref.compressed_bytes,
        )
        return artifact

    @staticmethod
    def default_virtual_uri(filename: str, sha256: str) -> str:
        """Content-derived handle: ``paa://artifacts/<hash12>/<filename>``.

        Deriving it from the hash is what makes :meth:`archive` idempotent
        without a lookup — the same bytes under the same name always produce the
        same URI — while keeping the filename visible for humans reading rows.
        """
        return f"{_VIRTUAL_SCHEME}/{sha256[:12]}/{filename}"

    # -- reads -------------------------------------------------------------

    async def get(self, artifact_id: str) -> ArchivedArtifact | None:
        row = await self._db.fetch_one(
            f"SELECT {_COLUMNS} FROM cold_lake_artifacts_archive WHERE id = ?", (artifact_id,)
        )
        return _artifact_from_row(row) if row is not None else None

    async def get_by_uri(self, virtual_uri: str) -> ArchivedArtifact | None:
        row = await self._db.fetch_one(
            f"SELECT {_COLUMNS} FROM cold_lake_artifacts_archive WHERE virtual_uri = ?",
            (virtual_uri,),
        )
        return _artifact_from_row(row) if row is not None else None

    async def get_by_checksum(self, sha256: str) -> list[ArchivedArtifact]:
        """Every archival of these exact bytes, newest first.

        Returns a list, not a single row: content addressing means one blob is
        routinely reached from many artifacts — the same attachment on twenty
        emails is twenty archival acts over one set of bytes.
        """
        rows = await self._db.fetch_all(
            f"SELECT {_COLUMNS} FROM cold_lake_artifacts_archive "
            "WHERE sha256_checksum = ? ORDER BY archived_at DESC, id ASC",
            (sha256,),
        )
        return [_artifact_from_row(row) for row in rows]

    async def list_for_correlation(self, correlation_id: str) -> list[ArchivedArtifact]:
        """Everything one task lineage archived — its forensic file set."""
        rows = await self._db.fetch_all(
            f"SELECT {_COLUMNS} FROM cold_lake_artifacts_archive "
            "WHERE correlation_id = ? ORDER BY archived_at DESC, id ASC",
            (correlation_id,),
        )
        return [_artifact_from_row(row) for row in rows]

    # -- retrieval ---------------------------------------------------------

    async def retrieve(self, artifact_id: str) -> bytes:
        """Full content, hash-verified before it is handed back."""
        artifact = await self._require(artifact_id)
        return self._cas.get(self._digest_of(artifact))

    async def retrieve_stream(self, artifact_id: str) -> Iterator[bytes]:
        """Chunked content for artifacts too large to hold in memory.

        As with :meth:`ContentAddressedStore.get_stream`, verification lands at
        the end of iteration rather than before the first chunk.
        """
        artifact = await self._require(artifact_id)
        return self._cas.get_stream(self._digest_of(artifact))

    async def restore(self, artifact_id: str, destination: Path | str) -> Path:
        """Write an artifact back to disk, streaming, via a temp-and-rename."""
        artifact = await self._require(artifact_id)
        return export_to_file(self._cas, self._digest_of(artifact), Path(destination))

    async def verify(self, artifact_id: str) -> bool:
        """Re-hash the stored blob against the checksum recorded at archival.

        The health check for silent corruption. Returns ``False`` rather than
        raising when the content is wrong or missing, because the caller is a
        sweep over many artifacts that needs a report, not a stack trace on the
        first bad one — the reason is logged. A *missing row* still raises: that
        is a caller bug, not archive damage.

        Streams, so verifying a large archive costs chunk-sized memory.
        """
        artifact = await self._require(artifact_id)
        digest = self._digest_of(artifact)

        hasher = hashlib.sha256()
        total = 0
        try:
            for chunk in self._cas.get_stream(digest):
                hasher.update(chunk)
                total += len(chunk)
        except StorageError as exc:
            log.warning(
                "artifact.verification_failed",
                artifact_id=artifact_id,
                virtual_uri=artifact.virtual_uri,
                reason=str(exc),
            )
            return False

        actual = hasher.hexdigest()
        if actual != artifact.sha256_checksum:
            log.warning(
                "artifact.checksum_mismatch",
                artifact_id=artifact_id,
                expected=artifact.sha256_checksum,
                actual=actual,
            )
            return False
        if total != artifact.size_bytes:
            # Belt and braces: a hash match with a size mismatch would mean the
            # recorded size was wrong at archival, which is worth knowing.
            log.warning(
                "artifact.size_mismatch",
                artifact_id=artifact_id,
                expected=artifact.size_bytes,
                actual=total,
            )
            return False
        return True

    # -- internals ---------------------------------------------------------

    async def _require(self, artifact_id: str) -> ArchivedArtifact:
        artifact = await self.get(artifact_id)
        if artifact is None:
            raise StorageError(
                "artifact not found", substrate=_SUBSTRATE, artifact_id=artifact_id
            )
        return artifact

    def _digest_of(self, artifact: ArchivedArtifact) -> str:
        """Resolve the CAS address, tolerating a row written without a blob_uri.

        ``sha256_checksum`` *is* the CAS address by construction, so a row whose
        ``blob_uri`` was never populated is still retrievable. Preferring the
        explicit pointer keeps the door open for a future backend whose address
        is not the checksum.
        """
        if artifact.blob_uri is not None:
            return BlobRef.parse_uri(artifact.blob_uri)
        return artifact.sha256_checksum


def _artifact_from_row(row: Any) -> ArchivedArtifact:
    return ArchivedArtifact(
        id=row["id"],
        signal_id=row["signal_id"],
        correlation_id=row["correlation_id"],
        virtual_uri=row["virtual_uri"],
        absolute_host_path=row["absolute_host_path"],
        sha256_checksum=row["sha256_checksum"],
        size_bytes=row["size_bytes"],
        compression=row["compression"],
        blob_uri=row["blob_uri"],
        payload_content=row["payload_content"],
        archived_at=from_iso(row["archived_at"]),
    )
