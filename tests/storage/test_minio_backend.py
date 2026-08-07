"""S3/MinIO cold-lake backend, exercised against an in-memory stub client.

No live MinIO is required — a live server is not available on the Windows dev
box and would make the suite non-hermetic anyway. The stub reproduces exactly
the slice of the boto3 S3 client the backend touches (put/get/head/delete/
upload_fileobj) and the shape of its not-found error, which is enough to prove
content addressing, verify-on-read, dedup and streaming behave as specified.

Byte-oriented throughout, for the same reason the filesystem CAS tests are:
``write_text`` would let Windows rewrite ``\\n`` and break every hash equality.
"""

from __future__ import annotations

import hashlib
import io
from typing import Any

import pytest

from paa.core.errors import StorageError
from paa.storage.coldlake.cas import BlobRef
from paa.storage.coldlake.minio_backend import S3BlobStore


class _StubNotFound(Exception):
    """Mimics botocore's ClientError shape for a missing key."""

    def __init__(self) -> None:
        super().__init__("not found")
        self.response = {"Error": {"Code": "404"}}


class StubS3Client:
    """In-memory S3 double covering only what :class:`S3BlobStore` calls."""

    def __init__(self) -> None:
        self.objects: dict[str, tuple[bytes, dict[str, str]]] = {}
        self.put_calls = 0
        self.get_calls = 0

    def put_object(
        self, *, Bucket: str, Key: str, Body: bytes, Metadata: dict | None = None
    ) -> dict:
        self.put_calls += 1
        self.objects[Key] = (bytes(Body), dict(Metadata or {}))
        return {}

    def upload_fileobj(
        self, fileobj: Any, bucket: str, key: str, ExtraArgs: dict | None = None
    ) -> None:
        self.put_calls += 1
        meta = (ExtraArgs or {}).get("Metadata", {})
        self.objects[key] = (fileobj.read(), dict(meta))

    def get_object(self, *, Bucket: str, Key: str) -> dict:
        self.get_calls += 1
        if Key not in self.objects:
            raise _StubNotFound()
        body, _meta = self.objects[Key]
        return {"Body": io.BytesIO(body)}

    def head_object(self, *, Bucket: str, Key: str) -> dict:
        if Key not in self.objects:
            raise _StubNotFound()
        body, meta = self.objects[Key]
        return {"ContentLength": len(body), "Metadata": meta}

    def delete_object(self, *, Bucket: str, Key: str) -> dict:
        self.objects.pop(Key, None)
        return {}


@pytest.fixture
def client() -> StubS3Client:
    return StubS3Client()


@pytest.fixture
def store(client: StubS3Client) -> S3BlobStore:
    # Tiny chunk size so "larger than one chunk" costs bytes, not megabytes.
    return S3BlobStore("paa-test", client=client, chunk_size=16)


# ---------------------------------------------------------------------------
# Round trips
# ---------------------------------------------------------------------------


def test_put_get_roundtrip_compressed(store: S3BlobStore) -> None:
    data = b"the cold lake remembers everything" * 8
    ref = store.put(data)
    assert isinstance(ref, BlobRef)
    assert ref.sha256 == hashlib.sha256(data).hexdigest()
    assert ref.size_bytes == len(data)
    assert store.get(ref.sha256) == data


def test_put_get_roundtrip_uncompressed(store: S3BlobStore) -> None:
    data = b"raw payload, no zstd frame"
    ref = store.put(data, compress=False)
    assert store.get(ref.sha256) == data
    assert ref.compressed_bytes == len(data)


def test_blobref_uri_is_cas_scheme(store: S3BlobStore) -> None:
    """The URI must stay cas:// so a blob_uri resolves against either backend."""
    ref = store.put(b"interchangeable")
    assert ref.uri == f"cas://{ref.sha256}"
    assert BlobRef.parse_uri(ref.uri) == ref.sha256


def test_empty_payload_roundtrips(store: S3BlobStore) -> None:
    ref = store.put(b"")
    assert ref.size_bytes == 0
    assert store.get(ref.sha256) == b""


# ---------------------------------------------------------------------------
# Content addressing
# ---------------------------------------------------------------------------


def test_dedup_second_put_is_a_noop(store: S3BlobStore, client: StubS3Client) -> None:
    data = b"stored exactly once"
    first = store.put(data)
    second = store.put(data)
    assert first.sha256 == second.sha256
    assert client.put_calls == 1  # the second put uploaded nothing


def test_exists_and_delete(store: S3BlobStore) -> None:
    ref = store.put(b"transient")
    assert store.exists(ref.sha256)
    assert store.delete(ref.sha256) is True
    assert not store.exists(ref.sha256)
    assert store.delete(ref.sha256) is False


def test_stat_reports_sizes_without_download(store: S3BlobStore, client: StubS3Client) -> None:
    data = b"measure me" * 100
    ref = store.put(data)
    before = client.get_calls
    stat = store.stat(ref.sha256)
    assert stat is not None
    assert stat.size_bytes == len(data)
    assert stat.compressed_bytes > 0
    assert client.get_calls == before  # stat used head, never downloaded the body


def test_stat_missing_is_none(store: S3BlobStore) -> None:
    assert store.stat("0" * 64) is None


# ---------------------------------------------------------------------------
# Verify-on-read
# ---------------------------------------------------------------------------


def test_get_missing_raises(store: S3BlobStore) -> None:
    with pytest.raises(StorageError, match="blob not found"):
        store.get("a" * 64)


def test_corruption_is_detected(store: S3BlobStore, client: StubS3Client) -> None:
    data = b"trust but verify"
    ref = store.put(data, compress=False)
    key, (body, meta) = next(iter(client.objects.items()))
    client.objects[key] = (body + b"tampered", meta)
    with pytest.raises(StorageError, match="hash verification"):
        store.get(ref.sha256)


def test_invalid_digest_rejected(store: S3BlobStore) -> None:
    with pytest.raises(StorageError, match="sha256"):
        store.exists("../../etc/passwd")


# ---------------------------------------------------------------------------
# Streaming
# ---------------------------------------------------------------------------


def test_put_stream_get_stream_roundtrip(store: S3BlobStore) -> None:
    data = b"streamed payload spanning several chunks " * 20
    ref = store.put_stream(io.BytesIO(data))
    assert ref.sha256 == hashlib.sha256(data).hexdigest()
    assert ref.size_bytes == len(data)
    out = b"".join(store.get_stream(ref.sha256))
    assert out == data


def test_put_stream_with_size_hint(store: S3BlobStore) -> None:
    data = b"known length" * 10
    ref = store.put_stream(io.BytesIO(data), size=len(data))
    assert store.stat(ref.sha256).size_bytes == len(data)  # type: ignore[union-attr]


def test_put_stream_size_mismatch_raises(store: S3BlobStore) -> None:
    data = b"actually thirty-two bytes long!!"
    with pytest.raises(StorageError, match="declared size"):
        store.put_stream(io.BytesIO(data), size=len(data) + 5)


def test_put_stream_uncompressed_roundtrip(store: S3BlobStore) -> None:
    data = b"no compression here, thanks" * 5
    ref = store.put_stream(io.BytesIO(data), compress=False)
    assert b"".join(store.get_stream(ref.sha256)) == data


# ---------------------------------------------------------------------------
# Drop-in interchangeability with the filesystem CAS
# ---------------------------------------------------------------------------


async def test_signal_repository_accepts_the_s3_store(store: S3BlobStore) -> None:
    """A SignalRepository built on the S3 store must behave like the CAS one.

    This is the whole point of the shared interface: swap the object substrate,
    change nothing above it. Oversized payloads are offloaded to the blob store
    and read back verified.
    """
    import tempfile
    from pathlib import Path

    from paa.storage.coldlake import SignalRepository
    from paa.storage.relational.database import Database

    with tempfile.TemporaryDirectory() as tmp:
        db = Database(Path(tmp) / "paa.db")
        await db.connect()
        try:
            repo = SignalRepository(db, store, inline_threshold_bytes=64)
            big = {"blob": "x" * 500}  # comfortably over the inline threshold
            signal = await repo.record("email", big)
            assert signal.blob_uri is not None
            assert signal.blob_uri.startswith("cas://")
            assert repo.payload_json(signal) == big
        finally:
            await db.close()
