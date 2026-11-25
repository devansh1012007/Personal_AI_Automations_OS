"""The attested bundle's format facts — the leaf both sides import.

The producer (:mod:`paa.attest.bundle`) and the verifier
(:mod:`paa.attest.verify`) must agree on exactly one thing: the shape of
the artifact. That agreement lives here, in a module with no settings,
no ledger store and no runtime — only hashing, canonical JSON and the
stateless signing backend — so the verifier can be imported on a clean
checkout without dragging the producer's world along, and the format's
identity can never drift between the two sides because there is only one
spelling of it. Algorithm dispatch lives here too: a manifest's
``ed25519`` label is never verified by the symmetric fallback.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
from collections.abc import Sequence

from paa import __version__
from paa.ledger.events import canonical_json
from paa.marketplace.signing import sign as _marketplace_sign
from paa.marketplace.signing import signing_is_secure
from paa.marketplace.signing import verify as _marketplace_verify

__all__ = [
    "APPROVAL_DECISIONS",
    "ATTEST_SCHEMA",
    "ATTEST_TOOL_VERSION",
    "SECTION_ORDER",
    "attestation_algorithm",
    "chain_steps",
    "sha256_bytes",
    "sign_attestation",
    "verify_attestation_signature",
]

#: The bundle format's identity. Additive changes only within ``/1``.
ATTEST_SCHEMA = "paa-attest-bundle/1"

#: The producer's version, stamped so a reviewer can tell which writer
#: made the artifact. Display-only: verification never branches on it.
ATTEST_TOOL_VERSION = __version__

#: The six sections, in the one order the chain ever walks. Fixed on
#: purpose: a chain over a re-orderable list is a chain a re-orderer can
#: recompute, and the order is part of what the signature covers.
SECTION_ORDER: tuple[str, ...] = (
    "mandate",
    "diff",
    "commands",
    "tests",
    "approvals",
    "evidence",
)

#: The decisions an approval row may carry. ``rejected`` and
#: ``changes_requested`` are recorded, not refused — an attestation of a
#: change somebody rejected is *more* evidence, not less.
APPROVAL_DECISIONS: frozenset[str] = frozenset(
    {"approved", "rejected", "changes_requested"}
)

#: The seed the first chain step folds; a word, not a hash, so it can
#: never collide with a real predecessor's digest.
GENESIS = "genesis"


def sha256_bytes(data: bytes) -> str:
    """The SHA-256 of raw bytes, hex — the content address of a payload."""
    return hashlib.sha256(data).hexdigest()


def chain_steps(section_digests: Sequence[tuple[str, str]]) -> list[str]:
    """The hash chain over the sections, in the given order.

    Each step folds the previous step's digest, the section's name and
    the section payload's digest: ``genesis -> mandate -> diff -> ...``.
    Substituting a payload, renaming a section or reordering the walk
    all change the root, and the root is inside the signed header — so
    none of it can happen without breaking the signature too.
    """
    steps: list[str] = []
    prev = GENESIS
    for name, digest in section_digests:
        step = sha256_bytes(
            canonical_json({"prev": prev, "section": name, "sha256": digest}).encode()
        )
        steps.append(step)
        prev = step
    return steps


def attestation_algorithm() -> str:
    """The one algorithm the current host can honestly produce.

    The label is derived from the backend, never from a key file's prose:
    a fallback key used on a host with ``cryptography`` installed is not a
    symmetric signature, and an Ed25519 label on a host without it must
    fail closed rather than silently verify as HMAC.
    """
    return "ed25519" if signing_is_secure() else "hmac-dev-fallback"


def sign_attestation(payload: bytes, private_b64: str, algorithm: str) -> str:
    """Sign with exactly the declared algorithm, or refuse.

    The generic marketplace helper dispatches on the *verifier's* local
    backend; that is safe for package signatures labelled by the same
    helper, but an attestation's algorithm is a claim a reviewer acts on.
    Dispatching it here, from the label, makes producer and verifier agree
    on one meaning for ``ed25519`` and one for ``hmac-dev-fallback``.
    """
    if algorithm == "ed25519":
        if not signing_is_secure():
            raise ValueError("Ed25519 signing is unavailable on this host")
        return _marketplace_sign(payload, private_b64)
    if algorithm == "hmac-dev-fallback":
        if signing_is_secure():
            raise ValueError(
                "the symmetric fallback cannot be produced on a host with "
                "Ed25519 available; the algorithm label must match the backend"
            )
        key = base64.b64decode(private_b64, validate=True)
        return base64.b64encode(
            hmac.new(key, payload, hashlib.sha256).digest()
        ).decode("ascii")
    raise ValueError(f"unknown attestation signature algorithm {algorithm!r}")


def verify_attestation_signature(
    payload: bytes,
    value_b64: str,
    public_b64: str,
    algorithm: str,
    *,
    allow_insecure_fallback: bool = False,
) -> bool:
    """Verify one signature under exactly the label the manifest declares.

    ``hmac-dev-fallback`` is never trusted by default: its "public" key is
    the shared secret, so a bundle that publishes it publishes the means to
    forge it. It is accepted only by an explicitly insecure caller, and no
    amount of relabelling lets it masquerade as Ed25519.
    """
    if algorithm == "ed25519":
        if not signing_is_secure():
            return False
        return _marketplace_verify(payload, value_b64, public_b64)
    if algorithm == "hmac-dev-fallback" and allow_insecure_fallback:
        try:
            key = base64.b64decode(public_b64, validate=True)
            value = base64.b64decode(value_b64, validate=True)
        except (ValueError, UnicodeEncodeError):
            return False
        return hmac.compare_digest(hmac.new(key, payload, hashlib.sha256).digest(), value)
    return False
