"""The blob substrate's error vocabulary — neutral names for a substrate's failures.

ADR-0025 established the rule for the relational substrates: *the translation
happens at the substrate boundary, once, and the repository layer catches a
concept rather than a driver's exception.* The blob substrate never got one.
There was no ``storage/errors.py`` — the only neutral vocabulary in the tree,
``storage/relational/errors.py``, is built from relational concepts
(``UniqueViolation``, ``SerializationFailure``, ``SchemaVersionError``, …)
that cannot express "absent", "forbidden", "unreachable" or "corrupt". So the
S3 path raised the generic :class:`paa.core.errors.StorageError` with a
free-text message and **let the driver's exception escape** from six sites,
and the :class:`~paa.storage.coldlake.base.BlobStore` protocol declared no
error behaviour at all — which is exactly why the divergence between
``put()`` and ``put_stream()`` on a missing bucket went unnoticed.

This module is the blob analogue of ``storage/relational/errors.py``: the
vocabulary, and nothing else. The *dialect* — the S3 error-code table that
says which of the driver's codes means which concept — lives beside the S3
client in :mod:`paa.storage.coldlake.minio_backend`, for the same reason
ADR-0025's SQL dialects live beside their engines.

What the vocabulary has to express
----------------------------------
Four judgements, which are the ones a caller actually has to make:

``NotFound``
    The addressed thing is not there. A missing object and a missing *bucket*
    are the same judgement — "it is not there" — and they are told apart by
    the details, not by the type. A caller that catches ``NotFound`` gets
    one answer to "is this blob absent, or is the store broken?", which is
    the question the whole seam exists to answer.

``AccessDenied``
    The substrate refused: wrong credentials, or a principal that is not
    permitted to do this. **Not** retryable — the same credentials will fail
    identically however often the request is repeated.

``Unreachable``
    The substrate could not be reached, or could not complete the request: a
    refused connection, a timeout, a throttling response, a server-side
    error. **Retryable**: the same request, re-run, may well succeed.

``Corrupt``
    The bytes are not what the address says. A hash that does not match, a
    frame that is not a frame. The archive is damaged, and no retry fixes it.

Three more, because the code raises these conditions today and would
otherwise have to report them as "something went wrong":

``NotSupported``
    The substrate cannot serve the request at all — the ``s3`` extra is not
    installed, or the deployment selected a substrate this build cannot
    construct. Deliberately **not** a subclass of ``Unreachable``: an
    unreachable substrate may come back, a missing extra will not.

``MalformedRequest``
    The request was refused as malformed: a digest that is not a digest, a
    size hint that disagrees with the bytes. This is the caller's mistake, not
    the substrate's, and it will fail identically on a retry. The relational
    seam made the same call for the same reason
    (:class:`~paa.storage.relational.errors.ParameterCountMismatch` is "a bug
    in this codebase's SQL rather than anything the engine or the data did").

``BlobFailure``
    The substrate failed and the reason is not one this vocabulary names.
    Deliberately a **sibling** of the named concepts, not their parent: a
    caller that retries on ``Unreachable`` must not be told to retry on this,
    and a caller that treats ``Corrupt`` as archive damage must not treat
    this as damage.

Every member is a :class:`paa.core.errors.StorageError`, so the existing
``except StorageError`` handlers at the edges keep working, and each carries
the ``substrate`` that produced it.

The one rule about the boundary
-------------------------------
**The boundary translates the substrate's errors and nothing else.** An error
that is already one of ours comes back unchanged (the translation is
*idempotent*), and a :class:`paa.core.errors.PaaError` that is not ours is
passed through untouched — a ``LedgerError`` stays a ``LedgerError`` and a
``TypeError`` stays a ``TypeError`` rather than being reported as an
infrastructure fault. That is the rule ADR-0025 established for the
relational substrates, and it is the reason the two blob substrates can be
interchangeable at all.
"""

from __future__ import annotations

import errno
from typing import Any, Final

import zstandard

from paa.core.errors import PaaError, StorageError

__all__ = [
    "AccessDenied",
    "BlobError",
    "BlobFailure",
    "Corrupt",
    "MalformedRequest",
    "NotFound",
    "NotSupported",
    "Unreachable",
    "translate_blob_error",
]


class BlobError(StorageError):
    """The base of the blob vocabulary.

    A grouping base, never raised directly — the same arrangement as
    :class:`~paa.storage.relational.errors.ConstraintViolation`, whose
    subclasses do the naming. It exists so that a caller who wants "anything
    the blob substrate said" has one name to catch, and so that the
    translation can recognise an error that is already ours without
    enumerating the members.
    """


class NotFound(BlobError):
    """The addressed blob is not there.

    A missing object and a missing bucket are the same judgement — "it is
    not there" — and the details say which: ``sha256`` for an object,
    ``bucket`` for a bucket. They are one class because a caller that has to
    answer "is this blob absent, or is the store unreachable?" wants one
    answer, and splitting the class would give two types to catch where the
    judgement is one.

    Not retryable and not damage: nothing is wrong, the thing simply is not
    there.
    """


class AccessDenied(BlobError):
    """The substrate refused the request.

    Wrong credentials, or a principal that is not permitted to do this. The
    two are the same judgement for a caller — an operator has to go and fix
    the credentials or the policy — and the driver's own code, carried in the
    ``s3_code`` detail, says which.

    **Not** retryable: the same credentials fail identically however often the
    request is repeated. This is the class that separates "the store says no"
    from "the store is not there", which is the security-relevant judgement
    the vocabulary exists to make possible.
    """


class Unreachable(BlobError):
    """The substrate could not be reached, or could not complete the request.

    A refused connection, a DNS failure, a timeout, a throttling response, a
    server-side error, a connection that died half-way through a body.

    **Retryable**: the same request, re-run, may well succeed. That is the
    difference from every other member of the family — the others are
    statements about the request or the data, and will fail identically
    however often they are retried.
    """


class Corrupt(BlobError):
    """The bytes are not what the address says.

    A hash that does not match, a frame that is not a frame. This is the
    cold lake's worst failure mode — silent bit-rot in an archive nobody
    reads until they urgently need it — and it is a distinct class because
    the remedy is different: quarantine the blob, do not retry the read, and
    do not treat it as a connectivity problem.
    """


class NotSupported(BlobError):
    """The substrate cannot serve this request at all.

    The ``s3`` extra is not installed in this deployment, or the deployment
    selected the S3 substrate with no endpoint configured.

    Deliberately **not** a subclass of :class:`Unreachable`: an unreachable
    substrate may come back, and a missing extra will not. A caller that
    retries on ``Unreachable`` must not be told to retry on this.
    """


class MalformedRequest(BlobError):
    """The request was refused as malformed.

    A digest that is not a digest, a size hint that disagrees with the bytes
    that were read. The caller's mistake rather than the substrate's, and it
    will fail identically on a retry.

    It is a member of the vocabulary rather than a bare ``StorageError``
    because "you gave me a bad address" and "the blob is not there" are
    different answers, and a caller that has to distinguish them should be
    able to.
    """


class BlobFailure(BlobError):
    """The substrate failed, and the reason is not one this vocabulary names.

    The fallback. A driver error this build does not model arrives here
    rather than escaping raw — a raw driver exception reaching a repository
    breaks the contract this module exists to keep, just as surely as a
    misclassified one does — and the driver's own error is the ``__cause__``.

    Deliberately **not** a subclass of :class:`Unreachable`: it makes no
    retry promise, because it does not know what failed.
    """


#: The errno values that mean the substrate **refused** — as opposed to
#: "it is not there" or "it could not be reached".
#:
#: Matched on the errno rather than only on the exception class, because the
#: class and the errno are the same fact stated twice and the errno is the
#: one that survives a wrapper: ``os.replace`` raising ``PermissionError``
#: and ``open`` raising ``PermissionError`` are the same condition and the
#: same errno.
_ERRNO_REFUSED: Final[frozenset[int]] = frozenset(
    {
        errno.EACCES,
        errno.EPERM,
        errno.EROFS,
    }
)

#: The errno values that mean the substrate **could not be reached or could
#: not complete the request** — the retryable ones.
#:
#: ``EIO`` is in the list because a failed read is a failed read whatever the
#: filesystem calls it, and re-running it may well succeed. ``ENOSPC`` and
#: ``EDQUOT`` are deliberately *not* in it: a full disk will be just as full
#: on the retry, and calling that "unreachable" would tell a caller to retry
#: something that cannot succeed.
_ERRNO_UNREACHABLE: Final[frozenset[int]] = frozenset(
    {
        errno.EIO,
        errno.ECONNABORTED,
        errno.ECONNREFUSED,
        errno.ECONNRESET,
        errno.EHOSTUNREACH,
        errno.ENETDOWN,
        errno.ENETUNREACH,
        errno.EPIPE,
        errno.ETIMEDOUT,
    }
)


def translate_blob_error(exc: BaseException, *, substrate: str, **details: Any) -> Exception:
    """Turn a substrate failure into one of the neutral blob errors.

    Returns the exception to raise rather than raising it, so the caller
    keeps the ``raise ... from exc`` chain that shows the original. The
    original is always the ``__cause__``, so the driver's own wording is
    never lost — it is simply not *in* the message, because the message
    reaches the ledger through ``PaaError.to_payload()`` and the driver's
    wording can quote an endpoint URL.

    The translation is **idempotent**: an error that is already one of ours
    is returned unchanged. The boundaries in this package nest — a store
    method catches the driver's error and translates it, and the call that
    wraps the store may translate again — and without the guard a specific
    failure would be flattened into a vaguer one on its way out.

    A :class:`paa.core.errors.PaaError` that is **not** ours is returned
    unchanged as well, and is not translated. A ``LedgerError`` that a caller
    catches to recognise a lost race must not arrive as a ``StorageError``,
    and a ``TypeError`` that is a bug in the calling code must not be
    reported as an infrastructure fault. This is the rule ADR-0025
    established for the relational substrates.

    The details are merged into the error, and they are **identifiers**:
    ``key``, ``sha256``, ``bucket``, ``path``, ``errno``. They are merged
    verbatim into a ledger payload by ``PaaError.to_payload()``, so a detail
    that could carry a credential would put it in the ledger.

    The S3 dialect — the driver's error codes — is not here: it lives in
    :mod:`paa.storage.coldlake.minio_backend`, beside the client that raises
    it, and it is applied before this function is called.
    """
    if isinstance(exc, StorageError):
        # Already neutral — ours, or somebody else's. Either way it is not
        # the substrate's raw error and it is not this function's to relabel.
        return exc
    if isinstance(exc, PaaError):
        # A domain error raised by the code above the substrate. Translating
        # it would report a bug in the caller as an infrastructure fault.
        return exc
    if isinstance(exc, zstandard.ZstdError):
        # A frame that is not a frame. The codec's own exception is a
        # codec-specific thing; the concept is "the bytes are damaged".
        return Corrupt(
            "blob is not a readable zstd frame",
            substrate=substrate,
            **details,
        )
    if isinstance(exc, OSError):
        return _translate_os_error(exc, substrate=substrate, **details)
    return BlobFailure(
        "the substrate failed",
        substrate=substrate,
        **{**details, "driver_error": type(exc).__name__},
    )


def _translate_os_error(exc: OSError, *, substrate: str, **details: Any) -> Exception:
    """Classify a filesystem error into the neutral vocabulary.

    The filesystem is the local substrate's dialect, and its dialect is the
    errno: three questions a caller can act on, and everything else.
    """
    code = exc.errno
    if isinstance(exc, FileNotFoundError) or code == errno.ENOENT:
        return NotFound("blob not found", substrate=substrate, **details)
    if isinstance(exc, PermissionError) or code in _ERRNO_REFUSED:
        return AccessDenied(
            "the substrate refused the request", substrate=substrate, **details
        )
    if code in _ERRNO_UNREACHABLE:
        return Unreachable(
            "the substrate could not be reached", substrate=substrate, **details
        )
    # Everything else — a full disk, a quota, a name too long, a directory
    # where a file was expected. It is a failure, and it is not one of the
    # four named judgements, so it is reported as the unclassified one with
    # the errno kept: the operator needs the number, and a caller must not be
    # told to retry something that will fail the same way.
    return BlobFailure(
        "the substrate failed",
        substrate=substrate,
        **{**details, "driver_error": type(exc).__name__, "errno": code},
    )
