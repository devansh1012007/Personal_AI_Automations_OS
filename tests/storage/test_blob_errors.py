"""The blob substrate's error vocabulary — the contract with a repository.

The contract
------------
:mod:`paa.storage.coldlake.errors` says that a caller catches a *concept*
rather than a driver's exception, and that the translation happens at the
substrate boundary, once. Three things make that claim testable:

* every failure the substrate can report is a
  :class:`~paa.core.errors.StorageError`, so a caller that catches
  ``PaaError`` catches a storage fault whichever substrate produced it;
* the *kind* of failure is preserved, because the kinds mean different
  things: a blob that is not there is an answer, a refusal is a
  configuration fact, an unreachable substrate is worth retrying, and a
  corrupt archive is damage; and
* the translation is **idempotent**, so a boundary that runs twice cannot
  flatten a specific failure into a vaguer one.

Before this file, none of that was tested, and the substrate had no
vocabulary at all: the S3 path let ``botocore``'s own exceptions escape from
six sites, the filesystem path let a raw ``OSError`` and a raw
``zstandard.ZstdError`` escape from its streaming read, and the *same*
condition — a missing bucket — arrived as two different exception types
depending on which method the caller had used.

The tests below run without a server and without ``boto3``: the translators
are pure functions of an exception, the S3 error codes are a table of
strings, and the store's boundaries are exercised against a duck-typed stub
whose errors are shaped like the driver's.
"""

from __future__ import annotations

import errno
import io
import subprocess
import sys
from pathlib import Path

import pytest
import zstandard

from paa.core.errors import LedgerError, PaaError, StorageError
from paa.storage.coldlake.cas import ContentAddressedStore
from paa.storage.coldlake.errors import (
    AccessDenied,
    BlobError,
    BlobFailure,
    Corrupt,
    MalformedRequest,
    NotFound,
    NotSupported,
    Unreachable,
    translate_blob_error,
)
from paa.storage.coldlake.minio_backend import (
    _translate_s3_error,
)

#: Every member of the vocabulary, and the class each one is.
VOCABULARY: tuple[type[BlobError], ...] = (
    AccessDenied,
    BlobFailure,
    Corrupt,
    MalformedRequest,
    NotFound,
    NotSupported,
    Unreachable,
)


class _DriverError(Exception):
    """An exception that is not one of ours and not a ``PaaError``.

    Stands for whatever a driver raises that the vocabulary does not name —
    the fallback case.
    """


# ---------------------------------------------------------------------------
# The vocabulary's shape
# ---------------------------------------------------------------------------


class TestTheVocabulary:
    def test_every_member_is_a_storage_error(self) -> None:
        """The one invariant the vocabulary rests on.

        A caller that catches ``StorageError`` catches a storage fault
        whichever substrate produced it, and a caller that catches ``PaaError``
        catches it at the edge of the runtime.
        """
        for member in VOCABULARY:
            assert issubclass(member, StorageError), member
            assert issubclass(member, BlobError), member

    def test_every_member_carries_the_substrate(self) -> None:
        """Which substrate produced it is part of the error, not of the log line.

        The details are merged verbatim into a ledger payload, so the
        substrate is a queryable field rather than something the operator has
        to infer from the message.
        """
        for member in VOCABULARY:
            error = member("something went wrong", substrate="s3")
            assert error.substrate == "s3"
            assert error.details["substrate"] == "s3"

    def test_the_base_is_the_parent_of_every_member(self) -> None:
        """``except BlobError`` catches everything the vocabulary names.

        The base is a grouping base and is never raised directly — the same
        arrangement as the relational substrate's ``ConstraintViolation``.
        """
        for member in VOCABULARY:
            assert issubclass(member, BlobError), member

    def test_the_unclassified_failure_is_not_a_retry_promise(self) -> None:
        """``BlobFailure`` must not be catchable as ``Unreachable``.

        A caller that retries on ``Unreachable`` must not be told to retry on
        a failure whose reason nobody knows: a full disk and a dropped
        connection look the same here, and only one of them is worth
        retrying.
        """
        assert not issubclass(BlobFailure, Unreachable)

    def test_a_missing_substrate_is_not_a_retry_promise(self) -> None:
        """``NotSupported`` is not a subclass of ``Unreachable`` either.

        An unreachable substrate may come back; a missing ``s3`` extra will
        not, and a caller that retries one and not the other has to be able
        to say so.
        """
        assert not issubclass(NotSupported, Unreachable)

    def test_absence_and_refusal_are_different_classes(self) -> None:
        """The judgement the vocabulary exists to make possible.

        "Is this blob absent, or is the store refusing?" is a
        security-relevant question, and one class cannot answer it.
        """
        assert not issubclass(NotFound, AccessDenied)
        assert not issubclass(AccessDenied, NotFound)


# ---------------------------------------------------------------------------
# The shared translator
# ---------------------------------------------------------------------------


class TestTheSharedTranslation:
    def test_the_translation_is_idempotent(self) -> None:
        """A wider boundary must not flatten a specific failure.

        The store's boundaries nest — a method catches the driver's error and
        translates it, and the code above may translate again — and without
        the guard a specific failure would be flattened into a vaguer one on
        its way out.
        """
        original = NotFound("blob not found", substrate="s3")
        assert translate_blob_error(original, substrate="s3") is original

    def test_a_storage_error_that_is_not_ours_is_not_touched(self) -> None:
        """Somebody else's storage error is not this function's to relabel."""
        original = StorageError("the queue refused", substrate="redis")
        assert translate_blob_error(original, substrate="s3") is original

    def test_a_domain_error_is_not_translated(self) -> None:
        """A ``LedgerError`` must stay a ``LedgerError``.

        A caller that catches ``LedgerError`` to recognise a lost race must
        not find a ``StorageError`` instead — that is the same divergence
        this vocabulary exists to remove, one layer up.
        """
        original = LedgerError("two writers raced")
        assert translate_blob_error(original, substrate="s3") is original

    def test_a_codec_failure_is_corruption(self) -> None:
        """A frame that is not a frame is damage.

        The codec's own exception is a codec-specific thing; the concept is
        "the bytes are not what the address says", and the two paths that can
        meet it have to report the same class.
        """
        translated = translate_blob_error(
            zstandard.ZstdError("Unknown frame descriptor"), substrate="cas"
        )
        assert isinstance(translated, Corrupt)

    def test_a_missing_file_is_not_found(self) -> None:
        translated = translate_blob_error(
            FileNotFoundError(errno.ENOENT, "no such file"), substrate="cas"
        )
        assert isinstance(translated, NotFound)

    def test_a_refused_file_is_a_refusal(self) -> None:
        translated = translate_blob_error(
            PermissionError(errno.EACCES, "permission denied"), substrate="cas"
        )
        assert isinstance(translated, AccessDenied)

    def test_a_failed_connection_is_unreachable(self) -> None:
        translated = translate_blob_error(
            ConnectionRefusedError(errno.ECONNREFUSED, "connection refused"),
            substrate="cas",
        )
        assert isinstance(translated, Unreachable)

    def test_a_full_disk_is_not_reachable_and_not_retryable(self) -> None:
        """A full disk is neither "absent" nor "refused", and it is not a
        connection that failed.

        It is also not worth retrying — the disk will be just as full on the
        next attempt — so it must not land in the class that says "retry me".
        """
        translated = translate_blob_error(
            OSError(errno.ENOSPC, "no space left on device"), substrate="cas"
        )
        assert isinstance(translated, BlobFailure)
        assert not isinstance(translated, Unreachable)
        assert translated.details["errno"] == errno.ENOSPC

    def test_an_unnamed_failure_is_the_unclassified_one(self) -> None:
        """The contract is that a caller never sees a driver's exception.

        A failure the vocabulary does not name is still wrapped rather than
        passed through: a raw exception escaping breaks the contract just as
        surely as a misclassified one does.
        """
        translated = translate_blob_error(_DriverError("boom"), substrate="s3")
        assert isinstance(translated, BlobFailure)
        assert isinstance(translated, BlobError)
        assert translated.details["driver_error"] == "_DriverError"

    def test_the_details_are_merged_into_the_error(self) -> None:
        """The identifiers travel with the error.

        They are merged verbatim into a ledger payload, so they have to be
        identifiers — a key, a digest, a bucket — and never text that could
        carry a credential.
        """
        translated = translate_blob_error(
            _DriverError("boom"), substrate="s3", key="blobs/ab/cd/x.zst"
        )
        assert isinstance(translated, BlobError)
        assert translated.details["key"] == "blobs/ab/cd/x.zst"
        assert translated.details["substrate"] == "s3"


# ---------------------------------------------------------------------------
# The S3 dialect
# ---------------------------------------------------------------------------


class ClientError(Exception):
    """A driver error shaped like botocore's ``ClientError``."""

    def __init__(self, code: str, *, status: int | None = None) -> None:
        super().__init__(code)
        self.response: dict[str, object] = {"Error": {"Code": code}}
        if status is not None:
            self.response["ResponseMetadata"] = {"HTTPStatusCode": status}


class EndpointConnectionError(Exception):
    """A connection failure: no code, no status, only a class name."""


class ResponseStreamingError(Exception):
    """The body died mid-read."""


class IncompleteReadError(ResponseStreamingError):
    """A body that died mid-read, by another name."""


class StatusOnlyError(Exception):
    """A driver error that reported an HTTP status and no code of its own."""

    def __init__(self, status: int) -> None:
        super().__init__("no code at all")
        self.response: dict[str, object] = {"ResponseMetadata": {"HTTPStatusCode": status}}


class TestTheS3Translation:
    @pytest.mark.parametrize(
        "code",
        ["NoSuchKey", "NoSuchVersion", "NotFound", "404"],
    )
    def test_a_missing_object_is_not_found(self, code: str) -> None:
        translated = _translate_s3_error(ClientError(code, status=404))
        assert isinstance(translated, NotFound)
        assert translated.details["s3_code"] == code

    def test_a_missing_bucket_is_not_found(self) -> None:
        """The bucket is the substrate's namespace, and its absence is "not there".

        It is the same judgement as a missing object, so it is the same
        class — and the details say which of the two is missing.
        """
        translated = _translate_s3_error(ClientError("NoSuchBucket", status=404))
        assert isinstance(translated, NotFound)
        assert translated.details["s3_code"] == "NoSuchBucket"

    @pytest.mark.parametrize(
        "code",
        [
            "AccessDenied",
            "AllAccessDisabled",
            "ExpiredToken",
            "InvalidAccessKeyId",
            "InvalidToken",
            "SignatureDoesNotMatch",
            "Unauthorized",
        ],
    )
    def test_a_refusal_is_access_denied(self, code: str) -> None:
        translated = _translate_s3_error(ClientError(code, status=403))
        assert isinstance(translated, AccessDenied)

    @pytest.mark.parametrize(
        "code",
        ["SlowDown", "RequestTimeout", "ServiceUnavailable", "InternalError", "500"],
    )
    def test_a_retryable_failure_is_unreachable(self, code: str) -> None:
        """These are worth retrying, which is what ``Unreachable`` promises."""
        translated = _translate_s3_error(ClientError(code, status=503))
        assert isinstance(translated, Unreachable)

    def test_an_unnamed_code_is_the_unclassified_failure(self) -> None:
        """The vocabulary does not name every code the server can send.

        One that it does not name must not be guessed at — and must not
        escape raw either.
        """
        translated = _translate_s3_error(ClientError("SomethingBrandNew"))
        assert isinstance(translated, BlobFailure)
        assert translated.details["s3_code"] == "SomethingBrandNew"

    def test_a_status_is_the_fallback_when_there_is_no_code(self) -> None:
        """Some drivers report the status and no code of their own."""
        assert isinstance(_translate_s3_error(StatusOnlyError(403)), AccessDenied)
        assert isinstance(_translate_s3_error(StatusOnlyError(503)), Unreachable)
        assert isinstance(_translate_s3_error(StatusOnlyError(404)), NotFound)

    def test_a_connection_failure_is_read_off_the_class_name(self) -> None:
        """A connection failure carries no code and no status.

        The class name is the only signal there is, and the translation reads
        it without importing the driver — because the driver is an optional
        extra and may not be installed at all.
        """
        translated = _translate_s3_error(EndpointConnectionError("could not connect"))
        assert isinstance(translated, Unreachable)

    def test_the_class_hierarchy_is_read_off_the_mro(self) -> None:
        """A subclass of a known failure is that failure.

        The MRO is what ``isinstance`` consults; only the names are read.
        """
        translated = _translate_s3_error(IncompleteReadError("short read"))
        assert isinstance(translated, Unreachable)
        assert translated.details["driver_error"] == "IncompleteReadError"

    def test_a_wrapper_is_unwrapped(self) -> None:
        """boto3 hides the driver's error behind its own wrapper.

        Without looking through it, every upload failure would report a
        different thing from the same failure on the ``put_object`` path.
        """
        try:
            raise ClientError("AccessDenied", status=403)
        except Exception as cause:
            wrapper = UploadFailedError("Failed to upload")
            wrapper.__cause__ = cause
            translated = _translate_s3_error(wrapper)
        assert isinstance(translated, AccessDenied)

    def test_a_wrapper_is_unwrapped_through_its_context(self) -> None:
        """The wrapper boto3 actually raises chains implicitly.

        boto3 raises ``S3UploadFailedError`` without ``from``, so the
        driver's error is the implicit ``__context__`` and ``__cause__`` is
        unset. Both spellings have to work.
        """
        with pytest.raises(UploadFailedError) as excinfo:
            try:
                raise ClientError("NoSuchBucket", status=404)
            except Exception:
                # Raised without ``from``: the original is the implicit
                # ``__context__``, exactly as boto3 raises it.
                raise UploadFailedError("Failed to upload") from None

        assert excinfo.value.__cause__ is None
        assert excinfo.value.__context__ is not None
        assert isinstance(_translate_s3_error(excinfo.value), NotFound)

    def test_the_translation_is_idempotent(self) -> None:
        original = NotFound("blob not found", substrate="s3")
        assert _translate_s3_error(original) is original

    def test_a_domain_error_is_not_translated(self) -> None:
        """A ``PaaError`` that is not ours is not the driver's to relabel."""
        original = LedgerError("two writers raced")
        assert _translate_s3_error(original) is original

    def test_an_unrelated_context_is_not_unwrapped(self) -> None:
        """The unwrap follows a cause that *looks* like a driver's error.

        Otherwise an unrelated ``__context__`` — raised by the code above,
        not by the driver — would be mistaken for the reason the request
        failed.
        """
        with pytest.raises(UploadFailedError) as excinfo:
            try:
                raise ValueError("something else entirely")
            except Exception:
                raise UploadFailedError("Failed to upload") from None

        assert isinstance(_translate_s3_error(excinfo.value), BlobFailure)


class UploadFailedError(Exception):
    """Stands for boto3's ``S3UploadFailedError``."""


# ---------------------------------------------------------------------------
# The filesystem substrate's boundaries
# ---------------------------------------------------------------------------


class TestTheFilesystemBoundaries:
    """The local substrate had two raw escapes of its own.

    A raw ``OSError`` out of the streaming read, and a raw
    ``zstandard.ZstdError`` out of a corrupt frame. Neither is a driver's
    exception, but both broke the same contract: a caller that catches
    ``StorageError`` caught nothing when they happened.
    """

    def test_a_file_that_vanished_is_not_found(self, tmp_path: Path) -> None:
        store = ContentAddressedStore(tmp_path / "cold_lake")
        ref = store.put(b"payload")
        path = store.root / "blobs" / ref.sha256[:2] / ref.sha256[2:4] / f"{ref.sha256}.zst"
        path.unlink()

        with pytest.raises(NotFound):
            list(store.get_stream(ref.sha256))

    def test_a_corrupt_frame_is_corrupt_in_the_streaming_path(
        self, tmp_path: Path
    ) -> None:
        """The streaming path used to leak a raw ``zstandard.ZstdError``.

        A frame that is not a frame is corruption, and corruption has to be
        the same class on both paths — the streaming one and the
        whole-payload one.
        """
        store = ContentAddressedStore(tmp_path / "cold_lake")
        ref = store.put(b"payload")
        path = store.root / "blobs" / ref.sha256[:2] / ref.sha256[2:4] / f"{ref.sha256}.zst"
        path.write_bytes(b"not a zstd frame at all")

        with pytest.raises(Corrupt):
            list(store.get_stream(ref.sha256))

    def test_a_corrupt_frame_is_corrupt_in_the_whole_payload_path(
        self, tmp_path: Path
    ) -> None:
        store = ContentAddressedStore(tmp_path / "cold_lake")
        ref = store.put(b"payload")
        path = store.root / "blobs" / ref.sha256[:2] / ref.sha256[2:4] / f"{ref.sha256}.zst"
        path.write_bytes(b"not a zstd frame at all")

        with pytest.raises(Corrupt):
            store.get(ref.sha256)

    def test_a_hostile_digest_is_a_malformed_request(self, tmp_path: Path) -> None:
        """A digest becomes path components, so it is refused at the door.

        And the refused value is echoed back only up to a bound: it is
        caller-supplied, and a caller-supplied value has no business being
        an unbounded string in a ledger payload.
        """
        store = ContentAddressedStore(tmp_path / "cold_lake")
        with pytest.raises(MalformedRequest) as excinfo:
            store.exists("x" * 10_000)

        assert len(excinfo.value.details["value"]) <= 128

    def test_a_lying_size_hint_is_a_malformed_request(self, tmp_path: Path) -> None:
        store = ContentAddressedStore(tmp_path / "cold_lake")
        with pytest.raises(MalformedRequest):
            store.put_stream(io.BytesIO(b"twelve bytes"), size=999)


# ---------------------------------------------------------------------------
# The driver is never imported
# ---------------------------------------------------------------------------


def test_the_driver_is_never_imported_at_module_scope() -> None:
    """``boto3`` is an optional extra and may not be installed.

    The module has to stay importable without it, and the *classification*
    has to work the same way whether or not it is installed — otherwise the
    unit suite would not be exercising the code the deployment runs. So the
    import is checked in a **fresh interpreter**: the test session has
    already imported half the world, and a module-scope import would hide
    there.
    """
    script = (
        "import sys;"
        "import paa.storage.coldlake.errors;"
        "import paa.storage.coldlake.minio_backend;"
        "import paa.storage.coldlake.cas;"
        "assert 'boto3' not in sys.modules, 'boto3 was imported at module scope';"
        "assert 'botocore' not in sys.modules, 'botocore was imported at module scope'"
    )
    result = subprocess.run(
        [sys.executable, "-c", script],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr


def test_the_vocabulary_module_imports_without_the_driver() -> None:
    """The vocabulary itself has no driver dependency at all."""
    script = (
        "import sys;"
        "import paa.storage.coldlake.errors as errors;"
        "assert errors.NotFound;"
        "assert 'botocore' not in sys.modules"
    )
    result = subprocess.run(
        [sys.executable, "-c", script],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr


def test_a_paa_error_is_never_a_blob_error() -> None:
    """The two families do not overlap.

    The blob vocabulary is a family *within* ``PaaError`` — every member is
    a ``PaaError`` — and ``PaaError`` itself is not a member of the
    vocabulary, so a caller that catches ``BlobError`` catches the
    substrate's failures and nothing else.
    """
    assert issubclass(BlobError, PaaError)
    assert not issubclass(PaaError, BlobError)
