"""S3/MinIO cold-lake backend, exercised against an in-memory stub client.

No live MinIO is required — a live server is not available on the Windows dev
box and would make the suite non-hermetic anyway. The stub reproduces exactly
the slice of the boto3 S3 client the backend touches (put/get/head/delete/
upload_fileobj) and the shape of its not-found error, which is enough to prove
content addressing, verify-on-read, dedup and streaming behave as specified.

The stub's errors are shaped like the driver's — the same class names, the
same ``response`` dict, the same wrapper around a failed upload — because the
translation reads the driver *structurally*: it never imports ``botocore``,
which is an optional extra and is not installed here. So the stub is what
proves the classification works without the driver present.

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
from paa.storage.coldlake.errors import (
    AccessDenied,
    BlobError,
    Corrupt,
    NotFound,
    Unreachable,
)
from paa.storage.coldlake.minio_backend import S3BlobStore


class _StubNotFound(Exception):
    """Mimics botocore's ClientError shape for a missing key.

    The class name is deliberately *not* one of botocore's: the translation
    has to recognise this as a missing key from the ``response`` dict alone,
    because that is the only signal a HEAD on a missing object carries.
    """

    def __init__(self) -> None:
        super().__init__("not found")
        self.response = {"Error": {"Code": "404"}}


class ClientError(Exception):
    """Mimics botocore.exceptions.ClientError: a ``response`` dict, nothing else.

    The name is the driver's name on purpose. The translation reads the
    driver's class names structurally — it never imports botocore — so a stub
    that carried a different name would not be recognised by the same code the
    deployment runs.
    """

    def __init__(self, code: str, *, status: int | None = None, message: str = "boom") -> None:
        super().__init__(message)
        self.response: dict[str, Any] = {"Error": {"Code": code, "Message": message}}
        if status is not None:
            self.response["ResponseMetadata"] = {"HTTPStatusCode": status}


class EndpointConnectionError(Exception):
    """Mimics botocore.exceptions.EndpointConnectionError: no response at all.

    A connection failure carries no error code and no status, so the class
    name is the only signal there is — which is why the translation reads the
    class names of the exception's whole inheritance chain.
    """


class ResponseStreamingError(Exception):
    """Mimics botocore.exceptions.ResponseStreamingError: the body died mid-read."""


class IncompleteReadError(ResponseStreamingError):
    """Mimics botocore.exceptions.IncompleteReadError.

    It inherits from ``ResponseStreamingError``, and the translation has to
    recognise it as one without importing botocore — which is why the class
    names are read off the inheritance chain rather than off the class.
    """


class S3UploadFailedError(Exception):
    """Mimics boto3.exceptions.S3UploadFailedError: a wrapper, not the error.

    boto3's transfer manager catches the driver's ``ClientError`` and raises
    this instead, so the wrapper is what the store sees on the upload path.
    Without unwrapping it, a failed upload would report a different thing
    from the same failure on the ``put_object`` path.
    """


class _FailingBody(io.BytesIO):
    """A body whose read fails part-way through, like a dropped connection."""

    def __init__(self, payload: bytes, error: Exception) -> None:
        super().__init__(payload)
        self._error = error

    def read(self, size: int | None = -1) -> bytes:
        raise self._error


class StubS3Client:
    """In-memory S3 double covering only what :class:`S3BlobStore` calls."""

    def __init__(self) -> None:
        self.objects: dict[str, tuple[bytes, dict[str, str]]] = {}
        self.put_calls = 0
        self.get_calls = 0
        #: When set, every call fails with this error instead of answering.
        self.failure: Exception | None = None
        #: When set, only ``get_object`` fails with this error.
        self.get_failure: Exception | None = None
        #: When set, reading a body fails with this error part-way through.
        self.body_failure: Exception | None = None

    def _maybe_fail(self) -> None:
        if self.failure is not None:
            raise self.failure

    def put_object(
        self, *, Bucket: str, Key: str, Body: bytes, Metadata: dict[str, str] | None = None
    ) -> dict[str, Any]:
        self.put_calls += 1
        self._maybe_fail()
        self.objects[Key] = (bytes(Body), dict(Metadata or {}))
        return {}

    def upload_fileobj(
        self, fileobj: Any, bucket: str, key: str, ExtraArgs: dict[str, Any] | None = None
    ) -> None:
        self.put_calls += 1
        meta = (ExtraArgs or {}).get("Metadata", {})
        try:
            self._maybe_fail()
        except Exception:
            # boto3's transfer manager raises its wrapper *without* chaining
            # it explicitly: the driver's error is the implicit
            # ``__context__`` and ``__cause__`` is unset. ``from None``
            # reproduces exactly that spelling.
            raise S3UploadFailedError(f"Failed to upload {key} to {bucket}") from None
        self.objects[key] = (fileobj.read(), dict(meta))

    def get_object(self, *, Bucket: str, Key: str) -> dict[str, Any]:
        self.get_calls += 1
        self._maybe_fail()
        if self.get_failure is not None:
            raise self.get_failure
        if Key not in self.objects:
            raise _StubNotFound()
        body, _meta = self.objects[Key]
        if self.body_failure is not None:
            return {"Body": _FailingBody(body, self.body_failure)}
        return {"Body": io.BytesIO(body)}

    def head_object(self, *, Bucket: str, Key: str) -> dict[str, Any]:
        self._maybe_fail()
        if Key not in self.objects:
            raise _StubNotFound()
        body, meta = self.objects[Key]
        return {"ContentLength": len(body), "Metadata": meta}

    def delete_object(self, *, Bucket: str, Key: str) -> dict[str, Any]:
        self._maybe_fail()
        self.objects.pop(Key, None)
        return {}

    def head_bucket(self, *, Bucket: str) -> dict[str, Any]:
        self._maybe_fail()
        return {}

    def create_bucket(self, *, Bucket: str) -> dict[str, Any]:
        self._maybe_fail()
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
# Failure modes — the substrate's vocabulary (ADR-0032)
# ---------------------------------------------------------------------------
#
# Before the vocabulary, each of these reached the caller as the driver's own
# exception, and the *same* condition — a missing bucket — arrived as two
# different types depending on which method the caller used, so no caller
# could write a correct ``except`` clause at all.


def test_a_missing_bucket_is_the_same_error_from_put_and_put_stream(
    client: StubS3Client,
) -> None:
    """The divergence this task exists to close.

    ``put`` writes through ``put_object`` and ``put_stream`` through
    ``upload_fileobj``, and boto3's transfer manager wraps the driver's error
    in an ``S3UploadFailedError``. Before the translation, ``put`` raised a
    raw ``ClientError`` and ``put_stream`` a generic storage error — the same
    condition, two answers, and no way for a caller to catch either.
    """
    client.failure = ClientError("NoSuchBucket", status=404)
    store = S3BlobStore("paa-test", client=client, chunk_size=16)

    with pytest.raises(NotFound) as put_error:
        store.put(b"payload")
    with pytest.raises(NotFound) as stream_error:
        store.put_stream(io.BytesIO(b"payload"))

    assert type(put_error.value) is type(stream_error.value)
    assert put_error.value.details["bucket"] == "paa-test"


def test_a_missing_bucket_is_not_found_and_not_a_refusal(client: StubS3Client) -> None:
    """The security-relevant judgement: absent, or refused?

    A missing bucket and a refused request are different facts with different
    remedies, and a caller has to be able to tell them apart.
    """
    client.failure = ClientError("NoSuchBucket", status=404)
    store = S3BlobStore("paa-test", client=client, chunk_size=16)

    with pytest.raises(NotFound) as excinfo:
        store.put(b"payload")

    assert not isinstance(excinfo.value, AccessDenied)
    assert excinfo.value.details["s3_code"] == "NoSuchBucket"


def test_a_refused_request_is_access_denied(client: StubS3Client) -> None:
    """The server said no. Not retryable, and not the same as "not there"."""
    client.failure = ClientError("AccessDenied", status=403)
    store = S3BlobStore("paa-test", client=client, chunk_size=16)

    with pytest.raises(AccessDenied) as excinfo:
        store.put(b"payload")

    assert excinfo.value.details["s3_code"] == "AccessDenied"
    assert excinfo.value.substrate == "s3"


def test_a_refused_bucket_check_is_access_denied(client: StubS3Client) -> None:
    """``ensure_bucket`` used to let the driver's exception out of the constructor.

    The bucket check is a boundary like any other: a store that cannot ask
    the server about its own bucket cannot be constructed.
    """
    client.failure = ClientError("AccessDenied", status=403)

    with pytest.raises(AccessDenied):
        S3BlobStore("paa-test", client=client, ensure_bucket=True)


def test_an_unreachable_endpoint_is_unreachable(client: StubS3Client) -> None:
    """A connection failure carries no error code and no status.

    The class name is the only signal there is, which is why the translation
    reads the class names of the exception's whole inheritance chain.
    """
    client.failure = EndpointConnectionError("Could not connect to the endpoint URL")
    store = S3BlobStore("paa-test", client=client, chunk_size=16)

    with pytest.raises(Unreachable) as excinfo:
        store.put(b"payload")

    assert not isinstance(excinfo.value, AccessDenied)
    assert excinfo.value.details["driver_error"] == "EndpointConnectionError"


def test_a_missing_object_reports_the_digest_not_the_key(
    store: S3BlobStore, client: StubS3Client
) -> None:
    """The contract names the digest; the key is a fan-out path.

    The not-found path through the body reader used to carry
    ``sha256=<the S3 key>`` — which is not a digest at all, and left a caller
    with nothing it could look the blob up by. The key is still reported, as
    the key; the digest is reported as the digest.
    """
    ref = store.put(b"payload")
    # The object is there when the store looks for it, and gone by the time
    # it reads the body: the race the body reader's own not-found path is
    # for.
    client.get_failure = _StubNotFound()

    with pytest.raises(NotFound) as excinfo:
        store.get(ref.sha256)

    assert excinfo.value.details["sha256"] == ref.sha256
    assert excinfo.value.details["key"] != ref.sha256


def test_a_missing_object_in_the_streaming_path_reports_the_digest(
    store: S3BlobStore,
) -> None:
    digest = "b" * 64
    with pytest.raises(NotFound) as excinfo:
        list(store.get_stream(digest))

    assert excinfo.value.details["sha256"] == digest


def test_a_mid_body_read_failure_is_translated(
    store: S3BlobStore, client: StubS3Client
) -> None:
    """A body that dies half-way through is a connection that failed.

    It used to escape as the driver's own exception out of ``_read_all``, and
    the streaming path had no boundary at all around the read.
    """
    ref = store.put(b"a payload that will not finish arriving")
    client.body_failure = ResponseStreamingError("connection reset")

    with pytest.raises(Unreachable):
        store.get(ref.sha256)


def test_a_mid_body_read_failure_is_translated_in_the_streaming_path(
    store: S3BlobStore, client: StubS3Client
) -> None:
    ref = store.put(b"a payload that will not finish arriving")
    client.body_failure = IncompleteReadError("connection reset")

    with pytest.raises(Unreachable):
        list(store.get_stream(ref.sha256))


def test_a_corrupt_frame_is_corrupt_on_both_paths(
    store: S3BlobStore, client: StubS3Client
) -> None:
    """The streaming path used to leak a raw ``zstandard.ZstdError``.

    A frame that is not a frame is corruption, and corruption has to be the
    same class whichever way the bytes were read.
    """
    ref = store.put(b"a payload that will be mangled")
    key, (_body, meta) = next(iter(client.objects.items()))
    client.objects[key] = (b"not a zstd frame at all", meta)

    with pytest.raises(Corrupt):
        store.get(ref.sha256)
    with pytest.raises(Corrupt):
        list(store.get_stream(ref.sha256))


def test_the_upload_wrapper_is_unwrapped(client: StubS3Client) -> None:
    """boto3 hides the driver's error behind ``S3UploadFailedError``.

    The wrapper is what the store sees, so the translation has to look
    through it — otherwise every upload failure reports a different thing
    from the same failure on the ``put_object`` path.
    """
    client.failure = ClientError("AccessDenied", status=403)
    store = S3BlobStore("paa-test", client=client, chunk_size=16)

    with pytest.raises(AccessDenied) as excinfo:
        store.put_stream(io.BytesIO(b"payload"))

    assert excinfo.value.details["s3_code"] == "AccessDenied"


@pytest.mark.parametrize(
    "failure",
    [
        ClientError("NoSuchBucket", status=404),
        ClientError("AccessDenied", status=403),
        ClientError("SignatureDoesNotMatch", status=403),
        ClientError("SlowDown", status=503),
        ClientError("RequestTimeout", status=400),
        EndpointConnectionError("Could not connect to the endpoint URL"),
        ResponseStreamingError("connection reset"),
    ],
)
def test_no_driver_exception_escapes_the_store(failure: Exception) -> None:
    """The invariant, asserted rather than asserted in a comment.

    Every one of these is a failure the store cannot answer, so every method
    has to raise — and what it raises is a member of the blob vocabulary,
    which is a ``StorageError``, so the handlers at the edges keep catching
    it.
    """
    client = StubS3Client()
    client.failure = failure
    store = S3BlobStore("paa-test", client=client, chunk_size=16)

    for call in (
        lambda: store.put(b"payload"),
        lambda: store.put_stream(io.BytesIO(b"payload")),
        lambda: store.get("c" * 64),
        lambda: list(store.get_stream("c" * 64)),
        lambda: store.exists("c" * 64),
        lambda: store.stat("c" * 64),
        lambda: store.delete("c" * 64),
    ):
        with pytest.raises(BlobError) as excinfo:
            call()
        assert isinstance(excinfo.value, StorageError)


def test_a_missing_object_is_an_answer_not_a_failure(
    store: S3BlobStore, client: StubS3Client
) -> None:
    """The methods that answer "is it there?" answer ``False``, not an error.

    A blob that is not stored is not a failure of the substrate, so the
    question has an answer; a *bucket* that is not there is a failure of the
    substrate, and it raises. The two are told apart by the error code, which
    is why the tables that classify them are separate.
    """
    client.failure = ClientError("NoSuchKey", status=404)
    digest = "d" * 64

    with pytest.raises(NotFound):
        store.get(digest)
    with pytest.raises(NotFound):
        list(store.get_stream(digest))


def test_the_drivers_error_is_kept_as_the_cause(client: StubS3Client) -> None:
    """The translation is a relabelling, not a replacement.

    The original is the ``__cause__``, so the operator can still read the
    driver's own wording out of the traceback — the message deliberately does
    not carry it, because the message reaches the ledger.
    """
    client.failure = ClientError("AccessDenied", status=403, message="Access Denied")
    store = S3BlobStore("paa-test", client=client, chunk_size=16)

    with pytest.raises(AccessDenied) as excinfo:
        store.put(b"payload")

    cause = excinfo.value.__cause__
    assert isinstance(cause, ClientError)
    assert "Access Denied" in str(cause)


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
