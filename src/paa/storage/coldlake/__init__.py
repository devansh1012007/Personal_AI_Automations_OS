"""Cold lake — immutable raw history (RFC §1.2).

SPEC DEVIATION (docs/adr/0004): the RFC specifies MinIO for the object layer.
This runtime uses a local content-addressed store instead — no server, no
container, no credentials. See :mod:`paa.storage.coldlake.cas` for the reasoning
and the contract it preserves.

Three pieces:

``cas``
    Content-addressed, zstd-compressed, deduplicating blob storage.
``signals``
    Verbatim external events, idempotent on ``(channel, external_id)``.
``artifacts``
    Immutable snapshots of host files, checksummed and verifiable.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from paa.storage.coldlake.artifacts import ArchivedArtifact, ArtifactArchive
from paa.storage.coldlake.cas import (
    CAS_URI_SCHEME,
    DEFAULT_CHUNK_SIZE,
    BlobRef,
    ContentAddressedStore,
    copy_file_into,
    export_to_file,
)
from paa.storage.coldlake.signals import (
    DEFAULT_INLINE_THRESHOLD_BYTES,
    Signal,
    SignalRepository,
)

if TYPE_CHECKING:
    from paa.config import StorageSettings

__all__ = [
    "CAS_URI_SCHEME",
    "DEFAULT_CHUNK_SIZE",
    "DEFAULT_INLINE_THRESHOLD_BYTES",
    "ArchivedArtifact",
    "ArtifactArchive",
    "BlobRef",
    "ContentAddressedStore",
    "Signal",
    "SignalRepository",
    "copy_file_into",
    "export_to_file",
    "get_blob_store",
    "get_content_store",
]


def get_content_store(settings: StorageSettings) -> ContentAddressedStore:
    """Build the CAS from configuration.

    The zstd level comes from :class:`paa.config.StorageSettings`, where it is
    documented as the speed/ratio knee for a write-heavy, read-rare archive.
    """
    return ContentAddressedStore(settings.cold_lake_path, zstd_level=settings.zstd_level)


def get_blob_store(settings: StorageSettings) -> Any:
    """Build the cold-lake object store named by ``backend_coldlake``.

    ``"filesystem"`` (default, ADR-0004) returns the local
    :class:`ContentAddressedStore`; ``"minio"`` (ADR-0019) returns an
    :class:`~paa.storage.coldlake.minio_backend.S3BlobStore` talking to the
    MinIO/S3 server configured on :class:`~paa.config.StorageSettings`. Both
    satisfy the identical put/get/exists/delete/stat/put_stream/get_stream
    contract and both mint ``cas://`` :class:`BlobRef` URIs, so a ``blob_uri``
    written by one resolves against the other — the signal and artifact
    repositories are backend-agnostic.

    The MinIO backend (and its ``boto3`` dependency) is imported lazily, so a
    laptop install without the ``minio`` extra stays importable.
    """
    if settings.backend_coldlake == "minio":
        from paa.storage.coldlake.minio_backend import _blob_store_from_settings

        return _blob_store_from_settings(settings)
    return get_content_store(settings)
