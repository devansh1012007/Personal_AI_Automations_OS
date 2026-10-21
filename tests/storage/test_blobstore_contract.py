"""The cold lake's blob-store contract, proved against the two implementations.

``BlobStore`` is ``runtime_checkable``, so ``isinstance(store, BlobStore)``
proves only that the *methods* exist — never that their signatures agree. A
contract that names a keyword argument no implementation accepts passes that
check and then fails in the caller, at runtime, with a ``TypeError`` that
names neither the store nor the contract.

These tests take the contract's declarations as written and hand them to the
implementations, so the contract is checked by *using* it rather than by
reading it. The repository layer is then checked against both substrates,
because "the repositories depend on the contract rather than on either class"
is only true if the contract is one they can actually be handed.
"""

from __future__ import annotations

import hashlib
import inspect
import io
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from paa.storage.coldlake import ArtifactArchive, ContentAddressedStore
from paa.storage.coldlake.base import BlobStore
from paa.storage.coldlake.errors import (
    BlobError,
    Corrupt,
    MalformedRequest,
    NotFound,
)
from paa.storage.coldlake.minio_backend import S3BlobStore
from paa.storage.relational.database import Database
from tests.storage.test_minio_backend import StubS3Client

#: The two classes the contract is written for. The *methods* they are
#: checked against are discovered from the protocol itself, so a method added
#: to the contract is covered by this file the moment it is added.
IMPLEMENTATIONS: tuple[type[Any], ...] = (ContentAddressedStore, S3BlobStore)


@pytest.fixture
def cas(tmp_path: Path) -> ContentAddressedStore:
    return ContentAddressedStore(tmp_path / "cold_lake", zstd_level=3)


@pytest.fixture
def s3_client() -> StubS3Client:
    return StubS3Client()


@pytest.fixture
def s3_store(s3_client: StubS3Client) -> S3BlobStore:
    return S3BlobStore("paa-test", client=s3_client, chunk_size=16)


def _declared_methods() -> list[str]:
    """The methods the protocol itself declares, dunders included."""
    return sorted(
        name
        for name, value in vars(BlobStore).items()
        if getattr(value, "__module__", None) == BlobStore.__module__
    )


def _shape(method: Callable[..., Any]) -> list[tuple[str, Any, Any]]:
    """A signature reduced to what a caller can observe.

    Names, kinds and defaults: the three things that decide whether a call
    written against the contract is accepted by the implementation.
    """
    return [
        (name, param.kind, param.default)
        for name, param in inspect.signature(method).parameters.items()
    ]


def _keyword_arguments(method: Callable[..., Any]) -> dict[str, Any]:
    """The keyword-only arguments a method declares, with their defaults."""
    return {
        name: param.default
        for name, param in inspect.signature(method).parameters.items()
        if param.kind is inspect.Parameter.KEYWORD_ONLY
    }


def _protocol_parameter_names(method: Callable[..., Any]) -> list[str]:
    """The names a caller writes when calling the *protocol's* method.

    A protocol cannot be instantiated, so its receiver is dropped by
    position — it is the one parameter a caller never names.
    """
    return [param.name for param in list(inspect.signature(method).parameters.values())[1:]]


def _caller_parameter_names(store: Any, method_name: str) -> list[str]:
    """The names a caller writes when calling an *implementation's* method.

    The method is taken off an instance, so the receiver is already bound
    and only the caller's own names are left.
    """
    method = getattr(store, method_name)
    return [param.name for param in inspect.signature(method).parameters.values()]


# ---------------------------------------------------------------------------
# Conformance
# ---------------------------------------------------------------------------


def test_both_implementations_satisfy_the_contract(
    cas: ContentAddressedStore, s3_store: S3BlobStore
) -> None:
    """The cheap check — and the one that proves nothing about signatures."""
    assert isinstance(cas, BlobStore)
    assert isinstance(s3_store, BlobStore)


def test_every_contract_method_has_the_implementations_shape() -> None:
    """Each method's parameters must be the ones the implementations accept.

    This is the check ``isinstance`` cannot make. A contract that renamed a
    parameter, added a keyword no implementation has, or dropped one it does
    has is a contract that fails in the caller rather than in the store.
    """
    for name in _declared_methods():
        contract = _shape(getattr(BlobStore, name))
        for implementation in IMPLEMENTATIONS:
            method = getattr(implementation, name, None)
            assert method is not None, f"{implementation.__name__} has no {name}"
            assert _shape(method) == contract, f"{implementation.__name__}.{name}"


def test_contract_return_types_match_the_implementations() -> None:
    """A contract that promises the wrong return type is a contract that lies.

    The comparison is textual because every module in the cold lake uses PEP
    563, so all three annotations are the same source text.
    """
    for name in _declared_methods():
        contract = inspect.signature(getattr(BlobStore, name)).return_annotation
        for implementation in IMPLEMENTATIONS:
            method = getattr(implementation, name, None)
            assert method is not None, f"{implementation.__name__} has no {name}"
            assert inspect.signature(method).return_annotation == contract, (
                f"{implementation.__name__}.{name}"
            )


def test_contract_keywords_are_accepted_by_both_stores(
    cas: ContentAddressedStore, s3_store: S3BlobStore
) -> None:
    """A caller that trusts the contract's keywords must not get a TypeError.

    The keywords are taken from the contract, not written out here, so this
    fails the moment the contract and the stores disagree — which is the whole
    point: the contract is the thing under test.
    """
    keywords = _keyword_arguments(BlobStore.put_stream)
    assert keywords, "the contract declares no keyword arguments to check"

    for store in (cas, s3_store):
        ref = store.put_stream(io.BytesIO(b"payload"), **keywords)
        assert ref.size_bytes == len(b"payload")
        assert store.get(ref.sha256) == b"payload"


def test_contract_names_the_parameters_the_stores_use(
    cas: ContentAddressedStore, s3_store: S3BlobStore
) -> None:
    """The contract's parameter names must be the ones the stores accept.

    A parameter renamed on one side only is invisible to ``isinstance`` and
    invisible to a positional call, and fatal to a keyword call.

    Both sides are compared as a *caller* sees them, receiver left out. The
    raw signatures cannot be compared directly: the receiver is the first
    ``POSITIONAL_OR_KEYWORD`` parameter of an unbound method, so a naive
    comparison would read ``"self"`` against ``"self"`` and pass whatever
    the payload parameter was actually called.
    """
    contract = _protocol_parameter_names(BlobStore.put_stream)
    for store in (cas, s3_store):
        theirs = _caller_parameter_names(store, "put_stream")
        assert theirs == contract, f"{type(store).__name__}.put_stream"


# ---------------------------------------------------------------------------
# The error clause
# ---------------------------------------------------------------------------
#
# The contract now says what each method raises, and this checks that the two
# implementations agree with it. Before the vocabulary the contract was
# silent about failure, and the silence is what let the same condition — a
# missing bucket — arrive as two different exception types depending on which
# method the caller had used.


def _corrupt_the_only_blob(store: Any, s3_client: StubS3Client) -> None:
    """Replace the stored blob's bytes with something that is not a frame.

    A frame that is not a frame is corruption, and corruption has to be the
    same class on both substrates and on both read paths.
    """
    if isinstance(store, ContentAddressedStore):
        path = next(store.root.rglob("*.zst"))
        path.write_bytes(b"not a zstd frame at all")
        return
    key, (_body, meta) = next(iter(s3_client.objects.items()))
    s3_client.objects[key] = (b"not a zstd frame at all", meta)


class TestTheErrorClause:
    def test_every_contract_method_states_what_it_raises(self) -> None:
        """The clause has to be *in the contract*, not only in the code.

        A method whose docstring names no failure is a method whose failure
        behaviour is unspecified — which is how the divergence between
        ``put`` and ``put_stream`` went unnoticed for so long.
        """
        members = (
            "NotFound",
            "AccessDenied",
            "Unreachable",
            "Corrupt",
            "MalformedRequest",
            "NotSupported",
            "BlobFailure",
        )
        for name in _declared_methods():
            if name == "__repr__":
                continue
            doc = inspect.getdoc(getattr(BlobStore, name)) or ""
            assert any(member in doc for member in members), name

    def test_a_missing_blob_is_not_found_on_both_substrates(
        self, cas: ContentAddressedStore, s3_store: S3BlobStore
    ) -> None:
        """The same condition, the same class, on either substrate.

        And the error names the **digest**: the address the caller asked
        about, not the substrate's internal key.
        """
        digest = "a" * 64
        for store in (cas, s3_store):
            with pytest.raises(NotFound) as excinfo:
                store.get(digest)
            assert excinfo.value.details["sha256"] == digest
            assert isinstance(excinfo.value, BlobError)

    def test_a_missing_blob_is_not_found_in_the_streaming_path_on_both(
        self, cas: ContentAddressedStore, s3_store: S3BlobStore
    ) -> None:
        digest = "b" * 64
        for store in (cas, s3_store):
            with pytest.raises(NotFound) as excinfo:
                list(store.get_stream(digest))
            assert excinfo.value.details["sha256"] == digest

    def test_a_hostile_digest_is_a_malformed_request_on_both(
        self, cas: ContentAddressedStore, s3_store: S3BlobStore
    ) -> None:
        """A digest becomes path components, so it is refused at the door."""
        for store in (cas, s3_store):
            with pytest.raises(MalformedRequest):
                store.exists("../../etc/passwd")

    def test_a_lying_size_hint_is_a_malformed_request_on_both(
        self, cas: ContentAddressedStore, s3_store: S3BlobStore
    ) -> None:
        """The hint is checked the same way whichever way the bytes arrived."""
        for store in (cas, s3_store):
            with pytest.raises(MalformedRequest):
                store.put_stream(io.BytesIO(b"twelve bytes"), size=999)

    def test_a_corrupt_frame_is_corrupt_on_both(
        self,
        cas: ContentAddressedStore,
        s3_store: S3BlobStore,
        s3_client: StubS3Client,
    ) -> None:
        """Corruption is corruption: one class, both substrates, both paths."""
        for store in (cas, s3_store):
            ref = store.put(b"a payload that will be mangled")
            _corrupt_the_only_blob(store, s3_client)
            with pytest.raises(Corrupt):
                store.get(ref.sha256)
            with pytest.raises(Corrupt):
                list(store.get_stream(ref.sha256))

    def test_a_missing_blob_is_an_answer_not_an_error_on_both(
        self, cas: ContentAddressedStore, s3_store: S3BlobStore
    ) -> None:
        """``exists`` and ``stat`` answer; they do not raise for absence."""
        digest = "c" * 64
        for store in (cas, s3_store):
            assert store.exists(digest) is False
            assert store.stat(digest) is None
            assert store.delete(digest) is False


# ---------------------------------------------------------------------------
# The repositories are backend-agnostic — which is what the contract is for
# ---------------------------------------------------------------------------


async def test_artifact_archive_works_against_the_s3_store(
    db: Database, s3_store: S3BlobStore, s3_client: StubS3Client, tmp_path: Path
) -> None:
    """The artifact layer must not care which substrate is underneath it.

    This is the end of the road for the contract: the archive holds a
    ``BlobStore``, streams a host file in through the shared helper and
    streams it back out, and never asks which implementation it was handed.

    The stub client is inspected afterwards so the test cannot pass by
    quietly reading from somewhere else — the bytes have to have gone
    through the S3 substrate.
    """
    artifacts = ArtifactArchive(db, s3_store)
    source = tmp_path / "report.bin"
    payload = b"the bytes an artifact archives"
    source.write_bytes(payload)

    artifact = await artifacts.archive(source)

    assert artifact.sha256_checksum == hashlib.sha256(payload).hexdigest()
    assert artifact.blob_uri == f"cas://{artifact.sha256_checksum}"
    assert await artifacts.retrieve(artifact.id) == payload
    assert await artifacts.verify(artifact.id) is True

    restored = tmp_path / "restored.bin"
    await artifacts.restore(artifact.id, restored)
    assert restored.read_bytes() == payload

    # The blob really is an object in the S3 store, addressed by its digest.
    digest = artifact.sha256_checksum
    assert any(key.endswith(f"{digest}.zst") for key in s3_client.objects)


async def test_artifact_archive_works_against_the_filesystem_store(
    db: Database, cas: ContentAddressedStore, tmp_path: Path
) -> None:
    """The same journey over the other substrate, so the two are comparable."""
    artifacts = ArtifactArchive(db, cas)
    source = tmp_path / "report.bin"
    payload = b"the bytes an artifact archives"
    source.write_bytes(payload)

    artifact = await artifacts.archive(source)

    assert artifact.sha256_checksum == hashlib.sha256(payload).hexdigest()
    assert await artifacts.retrieve(artifact.id) == payload
    assert await artifacts.verify(artifact.id) is True
