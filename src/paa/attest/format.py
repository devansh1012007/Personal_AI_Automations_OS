"""The attested bundle's format facts — the leaf both sides import.

The producer (:mod:`paa.attest.bundle`) and the verifier
(:mod:`paa.attest.verify`) must agree on exactly one thing: the shape of
the artifact. That agreement lives here, in a module whose import graph
is deliberately bare — no settings, no ledger store, no runtime — so the
verifier can be imported on a clean checkout without dragging the
producer's world along, and the format's identity can never drift
between the two sides because there is only one spelling of it.
"""

from __future__ import annotations

import hashlib
from collections.abc import Sequence

from paa import __version__
from paa.ledger.events import canonical_json

__all__ = [
    "APPROVAL_DECISIONS",
    "ATTEST_SCHEMA",
    "ATTEST_TOOL_VERSION",
    "SECTION_ORDER",
    "chain_steps",
    "sha256_bytes",
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
