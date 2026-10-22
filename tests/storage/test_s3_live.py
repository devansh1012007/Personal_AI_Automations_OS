"""The S3 cold lake, against **real S3 servers**.

Every other test of the S3 path runs against ``StubS3Client`` — a duck-typed
fake with no ``head_bucket``, no ``create_bucket`` and no
``list_objects_v2``. That stub is a good proof of the *mechanism* (the
error-code table, the upload-wrapper unwrapping, the bucket probe) and a
proof of nothing at all about the *table*: the codes a server actually
answers with are the codes the table has to name, and a stub shaped like the
driver proves only that the classifier reads the shape it was written
against. That is why six raw-exception escape sites and a ``put`` /
``put_stream`` divergence survived until wave 1 (ADR-0032).

So this file runs the **same lifecycle** against **two distinguishable
servers** — SeaweedFS, which the deployment runs, and RustFS, which is a
different program — and the point is not "does S3 work" but "does *this
client* work against a server it was not written against". A difference
between the two servers is a **finding**, and it is reported rather than
papered over: the suite asserts the invariant that holds on both, and the
ADR records the difference.

**What "two servers" is worth, and what it is not.** This file used to call
them "two independent implementations", and that was a claim it did not and
cannot establish: nothing in the environment says which programs the two
endpoints are, a server's identity is carried entirely by the **label** the
operator typed, and two containers of one image — wired to the two labels —
would have satisfied every assertion in this file.

So the claim is narrowed to what the suite can actually guarantee — two
servers that **answer differently** — and that narrower claim is now
**checked**: `test_the_two_servers_are_distinguishable` reads what each
server says about itself and fails if the two answers are the same. What the
suite still cannot establish is *independence*: nothing in the protocol says
what independence is, and two versions of one program are two programs. The
labels name the implementations the operator pointed the suite at; the suite
checks that the two are not the same one.

The servers
-----------

The suite names a **server**, never a bucket: the bucket is the test's own
(``paa-s3-test-{pid}-{uuid8}``) and it is dropped in teardown, so a run
cannot write to the bucket a deployment is using, whatever the endpoint points
at. The deployment's ``paa-stack-seaweedfs-1`` is never touched — it
publishes no host port, so it is unreachable from the host anyway, and writing
to it would be writing to the operator's data.

Start a server of each kind, with a unique name and a published loopback
port (the exact recipe the run behind this file used):

.. code-block:: console

    # SeaweedFS — the deployment's server. The image and the command are the
    # ones docker-compose.yml runs.
    docker run -d --name paa-test-s3-seaweed-$$ \\
        -p 127.0.0.1::8333 \\
        -e AWS_ACCESS_KEY_ID=paa-test-key \\
        -e AWS_SECRET_ACCESS_KEY=paa-test-secret \\
        chrislusf/seaweedfs:4.48 server -s3 -dir=/data -ip=0.0.0.0

    # RustFS — a different implementation of the same protocol.
    docker run -d --name paa-test-s3-rustfs-$$ \\
        -p 127.0.0.1::9000 \\
        -e RUSTFS_ACCESS_KEY=paa-test-key \\
        -e RUSTFS_SECRET_KEY=paa-test-secret \\
        -e RUSTFS_VOLUMES=/data \\
        rustfs/rustfs:latest

Then point the suite at them, using the ports docker published
(``docker port <name> 8333``):

.. code-block:: console

    export PAA_TEST_S3_SEAWEEDFS_ENDPOINT=http://127.0.0.1:<port>
    export PAA_TEST_S3_SEAWEEDFS_ACCESS_KEY=paa-test-key
    export PAA_TEST_S3_SEAWEEDFS_SECRET_KEY=paa-test-secret
    export PAA_TEST_S3_RUSTFS_ENDPOINT=http://127.0.0.1:<port>
    export PAA_TEST_S3_RUSTFS_ACCESS_KEY=paa-test-key
    export PAA_TEST_S3_RUSTFS_SECRET_KEY=paa-test-secret

    pytest tests/storage/test_s3_live.py -q

The unlabelled names (``PAA_TEST_S3_ENDPOINT`` and its two credentials) are
the same convention with no label, so a single server needs no label at all.
With nothing configured the module skips: a test that fails because there is
no server is a test that fails for the wrong reason.

What this file is not
---------------------

It is not a conformance test of S3. It does not check that the servers
implement the specification; it checks that **this client** answers the right
thing on top of them. The limits are listed in ADR-0033.
"""

from __future__ import annotations

import hashlib
import io
import os
import socket
import tempfile
import uuid
from typing import Any

import pytest
import zstandard

from paa.storage.coldlake.errors import (
    AccessDenied,
    Corrupt,
    MalformedRequest,
    NotFound,
    Unreachable,
)
from paa.storage.coldlake.minio_backend import S3BlobStore
from tests.storage.conftest import S3Server, S3Target, s3_servers

pytest.importorskip("boto3", reason="needs the s3 extra (paa[s3])")

#: The servers this run is against. Empty means the suite skips.
SERVERS: list[S3Server] = s3_servers()

if not SERVERS:
    pytest.skip(
        "needs a live S3 server: set PAA_TEST_S3_ENDPOINT (and its two "
        "credentials), or a labelled PAA_TEST_S3_<LABEL>_ENDPOINT",
        allow_module_level=True,
    )

pytestmark = pytest.mark.requires_s3


def pytest_generate_tests(metafunc: pytest.Metafunc) -> None:
    """Parametrise the live target over every server the environment names.

    The parametrisation is **indirect** — the server arrives as the
    fixture's parameter — so the test bodies carry no server plumbing, and
    the id of a failing case names the implementation it ran against. That
    id is the whole point of running the same body twice: a difference
    between the two servers has to be *visible*, and a difference that only
    shows up in one implementation's output is invisible without it.
    """
    if "s3_bucket" in metafunc.fixturenames:
        metafunc.parametrize(
            "s3_bucket",
            SERVERS,
            ids=[server.title for server in SERVERS],
            indirect=True,
            scope="session",
        )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _object_key(digest: str, *, compressed: bool = True) -> str:
    """The key the store writes a digest to.

    The fan-out is ``blobs/<hh>/<hh>/<digest><suffix>`` — the same one the
    filesystem store uses, so the two substrates stay interchangeable. The
    test asserts it against the server rather than assuming it, so a change
    to the layout fails here instead of quietly making every other
    assertion in this file vacuous.
    """
    suffix = ".zst" if compressed else ".bin"
    return f"blobs/{digest[:2]}/{digest[2:4]}/{digest}{suffix}"


def _object_keys(raw: Any, bucket: str) -> list[str]:
    """Every key in the bucket, read with the raw client.

    The ``BlobStore`` contract has no list operation, and it does not grow
    one for this file's convenience (ADR-0032 records the asymmetry): the
    assertions that need to see the raw object use the raw client, and the
    contract-level assertions use ``exists`` / ``stat`` / ``delete``.
    """
    return sorted(
        obj["Key"]
        for page in raw.get_paginator("list_objects_v2").paginate(Bucket=bucket)
        for obj in page.get("Contents", [])
    )


def _closed_port() -> int:
    """A loopback port that nothing is listening on.

    Bound and released, rather than a fixed port: a fixed port is either in
    use by something else or is a number the test cannot rely on, and both
    turn "the endpoint is unreachable" into "the endpoint is something else".
    """
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


class _CallSpy:
    """A real boto3 client with a record of the calls made through it.

    A **spy**, not a fake: every call goes to the server and the server's
    answer comes back unchanged. What the spy adds is the *request*, and
    the request is the only place one of the store's claims is visible at
    all — a second put of the same bytes shares the same key, so writing
    it again would land on the same object and the bucket's listing would
    look the same either way.

    It forwards by ``__getattr__`` rather than by declaring a method per
    operation, so it cannot drift from the client's own surface: an
    operation it does not know about is an ``AttributeError`` from the
    client, exactly as it would be without the spy.
    """

    def __init__(self, client: Any) -> None:
        self._client = client
        self.calls: list[str] = []

    def __getattr__(self, name: str) -> Any:
        attribute = getattr(self._client, name)
        if not callable(attribute):
            return attribute

        def recorded(*args: Any, **kwargs: Any) -> Any:
            self.calls.append(name)
            return attribute(*args, **kwargs)

        return recorded


def _spied_store(s3_target: S3Target) -> tuple[S3BlobStore, _CallSpy, Any]:
    """A store whose **requests** are recorded, over the real server.

    The spy is a wrapper around a real boto3 client, so every request
    reaches the server and every answer comes back unchanged. The client
    is returned too, because the store does not own an injected one and
    the caller is the one who closes it.
    """
    boto3 = pytest.importorskip("boto3")
    raw_client = boto3.client(
        "s3",
        endpoint_url=s3_target.server.endpoint,
        aws_access_key_id=s3_target.server.access_key,
        aws_secret_access_key=s3_target.server.secret_key,
        region_name="us-east-1",
    )
    spy = _CallSpy(raw_client)
    return S3BlobStore(s3_target.bucket, client=spy), spy, raw_client


def _incompressible(size: int) -> bytes:
    """Deterministic bytes that zstd cannot shrink.

    A repeated pattern compresses to almost nothing, and a payload that
    compresses to nothing never grows the spool past its cap — so a test
    that used one would not test what it claims to test. Hashing a counter
    gives bytes that are incompressible *and* the same on every run.
    """
    out = bytearray()
    counter = 0
    while len(out) < size:
        out += hashlib.sha256(counter.to_bytes(8, "big")).digest()
        counter += 1
    return bytes(out[:size])


@pytest.fixture
def spooled_files(monkeypatch: pytest.MonkeyPatch) -> list[Any]:
    """Every spool file the store opens while the test runs.

    The store spools a streamed upload through a size-capped temporary file,
    and the cap is the boundary this suite claims to cross. Whether the
    spool actually spilled to disk is not observable from the store's
    answers — a round trip over the boundary succeeds either way — so it is
    observed here instead: the recorder is a wrapper that counts and reports,
    and it changes no behaviour.
    """
    created: list[Any] = []
    original = tempfile.SpooledTemporaryFile

    def recorder(*args: Any, **kwargs: Any) -> Any:
        spool = original(*args, **kwargs)
        created.append(spool)
        return spool

    monkeypatch.setattr(tempfile, "SpooledTemporaryFile", recorder)
    return created


class _UploadsFailWith:
    """A real client whose **uploads** fail with a given exception.

    Every other call is forwarded, so the requests the store makes before
    the upload really reach the server and the server's own answers come
    back unchanged. Only the *upload's* error is substituted, because the
    shape of the substrate's error is exactly what the boundary is about:
    the vocabulary has to name the same condition whether the substrate
    reports it raw or wrapped. The upload's **request** therefore does not
    reach the server — what is under test is what the store does with the
    error the server would have answered, and the error it is handed is
    the one the driver produced against this server.

    It is a **spy** in the same sense the rest of this file is: the
    exception it raises is the driver's own, produced by the real driver
    against the real server (see
    :func:`_the_drivers_own_upload_errors`).
    """

    def __init__(self, client: Any, failure: BaseException) -> None:
        self._client = client
        self._failure = failure
        self.upload_calls = 0
        #: What the store handed the client for each upload, in order.
        self.uploaded: list[Any] = []

    def __getattr__(self, name: str) -> Any:
        attribute = getattr(self._client, name)
        if not callable(attribute):
            return attribute

        def call(*args: Any, **kwargs: Any) -> Any:
            if name == "upload_fileobj":
                self.upload_calls += 1
                self.uploaded.append(args[0] if args else None)
                raise self._failure
            return attribute(*args, **kwargs)

        return call


def _the_error_the_driver_raised(call: Any) -> BaseException:
    """The exception the driver's call raised.

    A call that did **not** raise is a loud failure rather than a skipped
    test: the whole point is the shape of the error, and a server that
    accepted the request leaves nothing to assert on.
    """
    try:
        call()
    except Exception as exc:
        return exc
    raise AssertionError(
        "the call succeeded, so there is no driver error to test the boundary against"
    )


def _the_drivers_own_upload_errors(s3_target: S3Target) -> tuple[BaseException, BaseException]:
    """The two shapes the driver's error arrives in — both of them genuine.

    The first is the driver's own ``ClientError``, with its response intact:
    what ``upload_fileobj`` — the call ``put_stream`` makes — propagates.
    The second is boto3's ``S3UploadFailedError``, the wrapper that
    ``upload_file`` — the *filename* variant — raises: it carries no
    response of its own and no ``__cause__``, and the driver's error is its
    implicit ``__context__``.

    Both are produced **by the real driver against the real server**, with
    credentials the server refuses, so neither is a hand-written
    approximation of the shape. The wrapper is the driver's own, so the
    test below is asserting against the thing boto3 really raises.
    """
    boto3 = pytest.importorskip("boto3")
    wrong = boto3.client(
        "s3",
        endpoint_url=s3_target.server.endpoint,
        aws_access_key_id="not-the-access-key",
        aws_secret_access_key="not-the-secret-key",
        region_name="us-east-1",
    )
    try:
        raw = _the_error_the_driver_raised(
            lambda: wrong.upload_fileobj(
                io.BytesIO(b"a payload the server will refuse"),
                s3_target.bucket,
                "some/key",
            )
        )
        with tempfile.NamedTemporaryFile(suffix=".bin", delete=False) as handle:
            handle.write(b"a payload the server will refuse")
            path = handle.name
        try:
            wrapper = _the_error_the_driver_raised(
                lambda: wrong.upload_file(path, s3_target.bucket, "some/key")
            )
        finally:
            os.unlink(path)
    finally:
        wrong.close()
    return raw, wrapper


def _server_fingerprint(server: S3Server) -> tuple[str, ...]:
    """The names of the HTTP headers this server answers with.

    A header **name** is a property of the program that answered; a header
    *value* is not — the request id and the date move on every call. The
    names are stable across repeated requests (measured against both
    servers), and two containers of one image answer with the same ones.

    The request is a ``head_bucket`` on a bucket that is not there, which is
    the request the store itself makes and the one every server answers
    with an error, so the fingerprint does not depend on a permission the
    suite does not already need. A HEAD creates no namespace — the store's
    own bucket probe depends on exactly that, and it was measured on both
    servers — so the probe leaves nothing behind and nothing has to be
    cleaned up.
    """
    boto3 = pytest.importorskip("boto3")
    client = boto3.client(
        "s3",
        endpoint_url=server.endpoint,
        aws_access_key_id=server.access_key,
        aws_secret_access_key=server.secret_key,
        region_name="us-east-1",
    )
    probe = f"{server.bucket_base}-fingerprint-{uuid.uuid4().hex[:8]}"
    try:
        try:
            client.head_bucket(Bucket=probe)
        except Exception as exc:
            response = getattr(exc, "response", None)
            metadata = response.get("ResponseMetadata") if isinstance(response, dict) else None
            headers = metadata.get("HTTPHeaders") if isinstance(metadata, dict) else None
            return tuple(sorted(headers or {}))
        return ()
    finally:
        client.close()


# ---------------------------------------------------------------------------
# The lifecycle
# ---------------------------------------------------------------------------


def test_put_then_get_returns_the_bytes(s3_target: S3Target) -> None:
    """The round trip, on a server that has never seen this client before."""
    payload = b"the cold lake remembers everything" * 8

    ref = s3_target.store.put(payload)

    assert ref.sha256 == hashlib.sha256(payload).hexdigest()
    assert ref.size_bytes == len(payload)
    assert s3_target.store.get(ref.sha256) == payload


def test_put_stream_then_get_stream_returns_the_bytes(s3_target: S3Target) -> None:
    """The streaming round trip — the path that writes through a different call.

    ``put`` writes through ``put_object`` and ``put_stream`` through
    ``upload_fileobj``, and boto3's transfer manager wraps the second one's
    error. The two paths agreeing on a *success* is worth as much as their
    agreeing on a failure.
    """
    payload = b"streamed payload spanning several chunks " * 20

    ref = s3_target.store.put_stream(io.BytesIO(payload))

    assert ref.sha256 == hashlib.sha256(payload).hexdigest()
    assert b"".join(s3_target.store.get_stream(ref.sha256)) == payload


def test_exists_and_stat_describe_a_stored_blob(s3_target: S3Target) -> None:
    """The metadata answers, and the raw size survives the round trip.

    The raw size is written as object metadata at upload time and read back
    at ``stat`` time. It is the half of the answer that does not need the
    blob's bytes, and it is the half a server can lose: the metadata is the
    server's own namespace, and a server that lower-cases it (both of these
    do) has to round-trip it exactly.

    **And the answer must come from the head.** The store has a fallback —
    an object written before the record existed is re-read to count its
    bytes — and the fallback *hides* a broken round-trip: the size comes
    out right either way, so the assertion on the size alone proves
    nothing about the metadata. The request is observed instead, so the
    claim the docstring makes is the claim the test proves.
    """
    payload = b"measure me" * 100
    store, spy, raw_client = _spied_store(s3_target)

    try:
        ref = store.put(payload)
        spy.calls.clear()
        assert store.exists(ref.sha256) is True
        stat = store.stat(ref.sha256)
    finally:
        raw_client.close()

    assert stat is not None
    assert stat.sha256 == ref.sha256
    assert stat.size_bytes == len(payload)
    assert stat.compressed_bytes == ref.compressed_bytes
    assert "get_object" not in spy.calls, (
        "stat must answer from the object's head; it re-read the blob, so the "
        "raw size came from the fallback and not from the metadata"
    )


def test_delete_removes_the_blob_and_the_object_is_really_gone(
    s3_target: S3Target,
) -> None:
    """The delete, checked against the server rather than against the store.

    The store is asked whether the blob is there *and* the raw client is
    asked what the bucket holds: a store that reported "gone" for an object
    that was still there would satisfy the first and fail the second, and
    only one of them is the truth about the substrate.
    """
    ref = s3_target.store.put(b"transient")

    assert s3_target.store.delete(ref.sha256) is True

    assert s3_target.store.exists(ref.sha256) is False
    assert _object_keys(s3_target.raw, s3_target.bucket) == []
    assert s3_target.store.delete(ref.sha256) is False


def test_a_second_put_of_the_same_bytes_stores_one_object(s3_target: S3Target) -> None:
    """The dedup, measured on the server.

    Content addressing means two identical payloads converge on one object.
    The store answers that with the same digest twice; the server answers it
    with one object. Only the second is the claim the cold lake rests on, so
    only the second is asserted.
    """
    payload = b"stored exactly once"

    first = s3_target.store.put(payload)
    second = s3_target.store.put(payload)

    assert first.sha256 == second.sha256
    assert _object_keys(s3_target.raw, s3_target.bucket) == [_object_key(first.sha256)]


def test_a_second_put_of_the_same_bytes_is_a_no_op(s3_target: S3Target) -> None:
    """The dedup is a **no-op**, and the no-op is in the request, not the bucket.

    The listing above cannot prove it. Two identical payloads share one key,
    so a second *write* would land on the very same object and the bucket
    would hold one object either way — the listing is blind to the
    difference between "the store looked and did not write" and "the store
    wrote again". And the no-op is the claim the cold lake rests on: a
    second write of the same bytes costs a request, a version and a
    multipart abort for nothing.

    So the **request** is observed instead, through a spy in front of a real
    client, over a real server. The second put must look before it writes
    and must not write.
    """
    store, spy, raw_client = _spied_store(s3_target)
    payload = b"stored exactly once"

    try:
        first = store.put(payload)
        spy.calls.clear()
        second = store.put(payload)
    finally:
        raw_client.close()

    assert second.sha256 == first.sha256
    assert "head_object" in spy.calls, "the store must look before it writes"
    assert "put_object" not in spy.calls, "a second put of the same bytes wrote again"
    assert "upload_fileobj" not in spy.calls, "a second put of the same bytes wrote again"
    assert _object_keys(s3_target.raw, s3_target.bucket) == [_object_key(first.sha256)]


def test_put_and_put_stream_converge_on_one_object(s3_target: S3Target) -> None:
    """The two write paths write the same bytes to the same place.

    ``put`` and ``put_stream`` are different calls to the server and they
    must be interchangeable — the repositories use whichever fits — so the
    same payload through either has to leave the same object behind, and
    the second write has to be a no-op rather than a second copy.
    """
    payload = b"one payload, two write paths"

    via_put = s3_target.store.put(payload)
    via_stream = s3_target.store.put_stream(io.BytesIO(payload))

    assert via_put.sha256 == via_stream.sha256
    assert _object_keys(s3_target.raw, s3_target.bucket) == [_object_key(via_put.sha256)]
    assert s3_target.store.get(via_put.sha256) == payload


def test_an_empty_payload_round_trips(s3_target: S3Target) -> None:
    """Zero bytes is a payload, and it has an address like any other."""
    ref = s3_target.store.put(b"")

    assert ref.size_bytes == 0
    assert s3_target.store.get(ref.sha256) == b""


def test_an_uncompressed_payload_round_trips(s3_target: S3Target) -> None:
    """The uncompressed write path, and the other suffix.

    The store keeps compressed and uncompressed objects apart by their
    suffix, and it looks for both when it is asked for a blob. An object
    written uncompressed has to be found by a store that looks for the
    compressed one first, or a blob written by one caller is invisible to
    the next.
    """
    payload = b"raw payload, no zstd frame"

    ref = s3_target.store.put(payload, compress=False)

    assert ref.compressed_bytes == len(payload)
    assert s3_target.store.get(ref.sha256) == payload
    assert _object_keys(s3_target.raw, s3_target.bucket) == [
        _object_key(ref.sha256, compressed=False)
    ]


def test_a_payload_above_the_spool_boundary_round_trips(
    s3_target: S3Target,
    spooled_files: list[Any],
) -> None:
    """A payload bigger than the spool's cap, spilled to disk and written.

    ``put_stream`` spools through a size-capped temporary file so that a
    500 MB artifact never becomes a 500 MB ``bytes``. The cap is a MiB, and
    a payload under it never touches the disk — so a test that used a small
    payload would prove nothing about the path that matters. The payload
    here is incompressible on purpose: a compressible one would shrink below
    the cap and never spill at all.

    The spill itself is observed, not assumed: a round trip over the
    boundary succeeds whether the spool spilled or not, so the recorder
    checks that it did.
    """
    payload = _incompressible(1024 * 1024 + 128 * 1024)

    ref = s3_target.store.put_stream(io.BytesIO(payload))

    assert ref.size_bytes == len(payload)
    assert b"".join(s3_target.store.get_stream(ref.sha256)) == payload
    assert any(getattr(spool, "_rolled", False) for spool in spooled_files), (
        "the payload was over the spool's cap and the spool never rolled over"
    )


def test_a_unicode_digest_is_refused(s3_target: S3Target) -> None:
    """A digest that is not a digest is the caller's mistake, not the substrate's.

    The digest becomes a key, and a key that is not hexadecimal is not a
    digest. The refusal is the caller's — ``MalformedRequest`` — and not the
    substrate's, because retrying it would fail identically.
    """
    with pytest.raises(MalformedRequest) as excinfo:
        s3_target.store.exists("é" * 64)

    assert excinfo.value.details["value"].startswith("é")
    assert _object_keys(s3_target.raw, s3_target.bucket) == []


def test_a_traversal_shaped_digest_is_refused(s3_target: S3Target) -> None:
    """A digest shaped like a path is refused before it becomes one.

    The digest is the store's only path-like input, so it is the store's
    path-traversal guard: a value carrying separators or ``..`` must never
    reach the key. The refusal is checked against the server as well — the
    bucket is still empty, so nothing was written under any name.
    """
    with pytest.raises(MalformedRequest):
        s3_target.store.exists("../../etc/passwd")

    with pytest.raises(MalformedRequest):
        s3_target.store.exists(".." * 32)

    assert _object_keys(s3_target.raw, s3_target.bucket) == []


def test_the_object_is_written_to_the_documented_fan_out(s3_target: S3Target) -> None:
    """The key layout is part of the contract, and it is asserted on the server.

    The fan-out is what keeps a bucket's directory listing from being one
    enormous directory, and it is the same on both substrates so a blob
    written by one is found by the other. Asserting it against the server
    is what stops it from being an assumption.
    """
    ref = s3_target.store.put(b"where does this land")

    assert _object_keys(s3_target.raw, s3_target.bucket) == [_object_key(ref.sha256)]
    head = s3_target.raw.head_object(Bucket=s3_target.bucket, Key=_object_key(ref.sha256))
    assert head["ContentLength"] == ref.compressed_bytes


# ---------------------------------------------------------------------------
# The failure modes
# ---------------------------------------------------------------------------


def test_a_missing_bucket_is_not_found_with_the_bucket_namespace(
    s3_target: S3Target,
) -> None:
    """A missing **bucket** is not a missing blob, and the detail says which.

    The store cannot answer "is this blob there?" at all when the bucket it
    lives in is gone, and answering "no" would be a lie a daemon would act
    on. So every method raises ``NotFound``, and the ``namespace="bucket"``
    detail is present if and only if the substrate's namespace is what is
    missing — a rule a caller can test, rather than "the details that
    happen to be there".

    This is the assertion the stub could not make: the stub's missing bucket
    and its missing key were told apart by a *code* that a HEAD cannot
    carry, so the store learned to earn the distinction by asking the server
    about the bucket. Only a real server can answer that question.
    """
    absent = s3_target.absent_store()
    digest = "b" * 64

    for call in (
        lambda: absent.exists(digest),
        lambda: absent.stat(digest),
        lambda: absent.delete(digest),
        lambda: absent.get(digest),
        lambda: list(absent.get_stream(digest)),
    ):
        with pytest.raises(NotFound) as excinfo:
            call()
        assert excinfo.value.details["namespace"] == "bucket"
        assert "bucket not found" in str(excinfo.value)
        assert not isinstance(excinfo.value, AccessDenied), (
            "a missing bucket is an absence, not a refusal"
        )


def test_a_missing_object_is_not_found_without_the_bucket_namespace(
    s3_target: S3Target,
) -> None:
    """The ordinary case: the bucket is there and the blob is not.

    This is the case the bucket probe must not break. The store asks about
    the bucket, the bucket is there, and the absence is the blob's — so
    ``exists`` answers ``False`` and the read paths say "blob not found"
    **without** the namespace detail, because the namespace is not what is
    missing.
    """
    digest = "d" * 64

    assert s3_target.store.exists(digest) is False
    assert s3_target.store.stat(digest) is None
    assert s3_target.store.delete(digest) is False

    with pytest.raises(NotFound) as excinfo:
        s3_target.store.get(digest)

    assert "namespace" not in excinfo.value.details
    assert "blob not found" in str(excinfo.value)
    assert excinfo.value.details["sha256"] == digest


def test_wrong_credentials_are_access_denied(s3_target: S3Target) -> None:
    """Wrong credentials are a refusal, and a refusal is not retryable.

    "The store says no" and "the store is not there" are different facts
    with different remedies, and the vocabulary is what makes them
    tellable apart.

    The refusal is observed on the HEAD, because a HEAD is the first thing
    every method does. The write path's own refusal is not exercised here:
    both servers refuse the HEAD before the write is reached, and a server
    that permits a HEAD and refuses a PUT would have to be invented to reach
    it — which would be the mock this file exists to avoid.
    """
    wrong = S3BlobStore(
        s3_target.bucket,
        endpoint_url=s3_target.server.endpoint,
        access_key="not-the-access-key",
        secret_key="not-the-secret-key",
    )

    with pytest.raises(AccessDenied) as excinfo:
        wrong.exists("a" * 64)

    assert not isinstance(excinfo.value, Unreachable)
    assert excinfo.value.substrate == "s3"
    assert "s3_code" in excinfo.value.details or "http_status" in excinfo.value.details, (
        "the refusal has to carry the driver's own answer, so that the "
        "operator can tell a wrong key from a wrong policy"
    )

    with pytest.raises(AccessDenied):
        wrong.put(b"payload")


def test_an_unreachable_endpoint_is_unreachable(s3_target: S3Target) -> None:
    """A server that cannot be reached is retryable, and says so.

    A connection failure carries no error code and no status at all, so the
    class name is the only signal there is — which is why the translation
    reads the class names of the exception's whole inheritance chain. The
    point of the assertion is the *class*: a refusal would say the request
    was rejected, and a caller that retries a refusal waits for ever.
    """
    unreachable = S3BlobStore(
        s3_target.bucket,
        endpoint_url=f"http://127.0.0.1:{_closed_port()}",
        access_key=s3_target.server.access_key,
        secret_key=s3_target.server.secret_key,
    )

    with pytest.raises(Unreachable) as excinfo:
        unreachable.exists("a" * 64)

    assert not isinstance(excinfo.value, AccessDenied)
    assert not isinstance(excinfo.value, NotFound)


def test_content_corrupted_out_of_band_is_corrupt(s3_target: S3Target) -> None:
    """Bytes that are not what the address says, written behind the store's back.

    The overwrite is done with the raw client, so it bypasses everything the
    store does: the address is right, the object is there, and the bytes are
    wrong. The store's answer is the only thing that can catch it, and it
    has to be ``Corrupt`` — not a hash mismatch reported as a transport
    failure, and not plausible-looking data handed back to a caller that
    would then archive it.

    The replacement is a *valid* zstd frame carrying different bytes, so the
    damage is a hash mismatch and not an unreadable frame: the two are
    different failures and are asserted separately.
    """
    ref = s3_target.store.put(b"the bytes that were written")
    key = _object_key(ref.sha256)

    s3_target.raw.put_object(
        Bucket=s3_target.bucket,
        Key=key,
        Body=zstandard.ZstdCompressor(level=3).compress(b"different bytes entirely"),
        Metadata={"raw-size": "24", "sha256": ref.sha256},
    )

    with pytest.raises(Corrupt) as excinfo:
        s3_target.store.get(ref.sha256)

    assert "hash verification" in str(excinfo.value)
    assert excinfo.value.details["expected"] == ref.sha256
    assert excinfo.value.details["actual"] != ref.sha256

    with pytest.raises(Corrupt):
        list(s3_target.store.get_stream(ref.sha256))


def test_a_frame_that_is_not_a_frame_is_corrupt(s3_target: S3Target) -> None:
    """A frame that is not a frame is damage, on both read paths.

    The streaming path used to leak a raw ``zstandard.ZstdError`` out of
    ``get_stream`` while ``get`` reported ``Corrupt`` — the same file, the
    same damage, two answers. Both read paths have to say the same thing.
    """
    ref = s3_target.store.put(b"a payload that will be mangled")
    key = _object_key(ref.sha256)

    s3_target.raw.put_object(
        Bucket=s3_target.bucket,
        Key=key,
        Body=b"not a zstd frame at all",
        Metadata={"raw-size": "16", "sha256": ref.sha256},
    )

    with pytest.raises(Corrupt) as excinfo:
        s3_target.store.get(ref.sha256)

    assert "zstd frame" in str(excinfo.value)

    with pytest.raises(Corrupt):
        list(s3_target.store.get_stream(ref.sha256))


def test_both_write_paths_agree_about_a_missing_bucket(s3_target: S3Target) -> None:
    """``put`` and ``put_stream`` answer **alike** about a missing bucket.

    This is the divergence ADR-0032's decision 4 exists to close: ``put``
    writes through ``put_object`` and ``put_stream`` through
    ``upload_fileobj``, and the two write paths translated the same
    condition differently. Before the fix the same condition arrived as a
    raw ``ClientError`` from one path and a generic error from the other,
    and **no caller could write a correct ``except`` clause at all**.

    Only a real server can prove it. The error is the driver's, the code
    under it is the server's, and the stub the unit suite uses cannot raise
    either — so the classification had never been exercised against the
    thing it classifies.

    Each write path meets a **missing bucket of its own**, because the first
    write on a server that creates buckets would leave the second one
    writing into a bucket that is already there, and the second path would
    never meet the condition at all.

    The two servers disagree about the *condition* — SeaweedFS creates the
    bucket a write names and RustFS refuses it — and that difference is a
    finding, not a reason to skip a branch. The assertion is that **both
    write paths give the same answer**, whatever that answer is.

    This test is about the two **paths**; the two **shapes of the driver's
    error** are a separate question, and
    ``test_the_wrapping_upload_route_answers_alike`` is where that is
    settled.
    """
    payload = b"a payload written into a bucket that was not there"
    absent_put = s3_target.absent_store()
    absent_stream = s3_target.absent_store()
    writes = (
        ("put", absent_put, lambda: absent_put.put(payload)),
        (
            "put_stream",
            absent_stream,
            lambda: absent_stream.put_stream(io.BytesIO(payload)),
        ),
    )

    outcomes: list[str] = []
    for name, store, write in writes:
        try:
            ref = write()
        except NotFound as exc:
            assert exc.details.get("namespace") == "bucket", (
                f"{name}: a server that refuses the write must name the namespace "
                "it refused"
            )
            assert not isinstance(exc, AccessDenied), (
                f"{name}: a missing bucket is an absence, not a refusal"
            )
            outcomes.append("refused")
        except Exception as exc:
            pytest.fail(
                f"{name}: a missing bucket must be one of the vocabulary's names, "
                f"not {type(exc).__name__}: {exc}"
            )
        else:
            # A server that *created* the namespace must leave the store's
            # answers true: the blob is there, and the store says it is.
            assert store.exists(ref.sha256) is True
            assert store.get(ref.sha256) == payload
            outcomes.append("written")

    assert outcomes[0] == outcomes[1], (
        "the two write paths must answer alike about a missing bucket: "
        f"{outcomes[0]} against {outcomes[1]}"
    )


# ---------------------------------------------------------------------------
# The two shapes of the substrate's error
# ---------------------------------------------------------------------------


def test_the_wrapping_upload_route_answers_alike(s3_target: S3Target) -> None:
    """The two shapes the driver's error arrives in must answer alike.

    boto3's ``upload_file`` — the *filename* variant — wraps the driver's
    ``ClientError`` in an ``S3UploadFailedError`` and does so **without
    chaining it**, so the driver's error is the wrapper's implicit
    ``__context__``. ``upload_fileobj`` — the call ``put_stream`` makes —
    does not wrap at all, so the driver's error arrives with its response
    intact.

    ADR-0032's decision 4 says the two shapes must answer alike, and until
    now **nothing in the tree checked it**: the unit suite's stub has no
    ``upload_file`` at all, and the live path never produces a wrapper — so
    a driver that started wrapping would change nothing any suite could
    see. This is that check, against a real server, with the driver's own
    exceptions.

    Both exceptions are the ones the real driver raised against this server
    one call apart. What the test varies is the *shape of the substrate's
    error*, which is exactly what the boundary is about.
    """
    raw, wrapper = _the_drivers_own_upload_errors(s3_target)

    assert type(raw).__name__ != "S3UploadFailedError", (
        "the driver's own error is already a wrapper, so the two shapes this "
        "test compares are not the two the driver produces"
    )
    assert type(wrapper).__name__ == "S3UploadFailedError", (
        f"boto3's upload_file raised {type(wrapper).__name__}, not the wrapper "
        "ADR-0032's decision 4 describes; the record needs to be re-measured"
    )
    assert getattr(wrapper, "response", None) is None, (
        "the wrapper carries a response of its own, so it is not the shape the "
        "decision describes"
    )
    assert wrapper.__context__ is not None, (
        "the wrapper does not carry the driver's error as its context, so the "
        "translation has nothing to read and the boundary does not hold"
    )

    plain_client = _UploadsFailWith(s3_target.raw, raw)
    wrapped_client = _UploadsFailWith(s3_target.raw, wrapper)
    plain = S3BlobStore(s3_target.bucket, client=plain_client)
    wrapped = S3BlobStore(s3_target.bucket, client=wrapped_client)

    with pytest.raises(AccessDenied) as plain_error:
        plain.put_stream(io.BytesIO(b"a payload the server will refuse"))
    with pytest.raises(AccessDenied) as wrapped_error:
        wrapped.put_stream(io.BytesIO(b"a payload the server will refuse"))

    assert plain_client.upload_calls == 1, "the upload was never reached"
    assert wrapped_client.upload_calls == 1, "the upload was never reached"
    assert type(plain_error.value) is type(wrapped_error.value), (
        f"the two shapes answered differently: {type(plain_error.value).__name__} "
        f"against {type(wrapped_error.value).__name__}"
    )
    assert plain_error.value.details["s3_code"] == wrapped_error.value.details["s3_code"]
    assert (
        plain_error.value.details["http_status"]
        == wrapped_error.value.details["http_status"]
    )

    # And the store hands the driver a **file object**, not a filename. The
    # module's own docstring says it — "spools through a size-capped temp
    # file and uploads a file object" — and it is what lets a payload under
    # the cap stay in memory. A store that wrote the spool to disk and
    # uploaded a path would break that, and nothing else in this file would
    # notice: the round trip succeeds either way, and the spill assertion
    # only looks at payloads that are *over* the cap.
    assert hasattr(plain_client.uploaded[0], "read"), (
        f"the store uploaded {type(plain_client.uploaded[0]).__name__}, not a file "
        "object — the spool's in-memory phase is what that protects, and "
        "nothing else in this suite would notice"
    )


# ---------------------------------------------------------------------------
# The two servers
# ---------------------------------------------------------------------------


def test_the_two_servers_are_distinguishable() -> None:
    """The suite ran against two **different** servers, and it checks that.

    The suite's whole value is that the same body runs against more than
    one program. Nothing in the environment says so: a server's identity is
    carried entirely by the **label** the operator typed, the test ids are
    those labels, and two containers of one image — wired to the two labels
    — would satisfy every other assertion in this file.

    So the suite reads what each server says about itself and checks that
    the two answers differ. A response's **header names** are a property of
    the program that answered (the values move — request ids, dates — but
    the names do not), and they are stable across repeated requests, which
    was measured against both servers.

    What this does **not** establish is that the two are *independent*
    implementations: nothing in the protocol says what independence is, and
    two versions of one program are two programs. It establishes the
    narrower thing — the two do not answer identically — and the labels
    name the implementations the operator pointed the suite at.
    """
    by_endpoint: dict[str, S3Server] = {}
    for server in SERVERS:
        by_endpoint.setdefault(server.endpoint, server)

    if len(by_endpoint) < 2:
        pytest.skip(
            "fewer than two distinct servers are configured; there is nothing "
            "to compare"
        )

    fingerprints = {
        endpoint: _server_fingerprint(server) for endpoint, server in by_endpoint.items()
    }

    unreadable = sorted(endpoint for endpoint, names in fingerprints.items() if not names)
    assert not unreadable, (
        f"the servers {unreadable} answered without a readable header set, so "
        "the suite cannot tell whether they are the same program"
    )

    distinct = set(fingerprints.values())
    assert len(distinct) == len(fingerprints), (
        "the configured servers answered with the same response headers, so the "
        f"suite cannot tell them apart: {fingerprints}. They are very likely the "
        "same program — two containers of one image — and the suite's value is "
        "that the same body runs against two servers that answer differently: "
        "point the two labels at two different implementations."
    )

