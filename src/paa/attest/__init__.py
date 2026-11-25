"""The attested change bundle — the artifact that catches succeeding wrongly.

``paa attest`` builds the spec's H3.1 artifact: a self-contained directory
that carries the *mandate* (who asked for this work, under which policy),
the *diff* (git's own view plus the workspace's content manifest), the
*commands run* and *tests actually run* (observed where a T1 bundle
witnessed them, asserted — and labelled as such — where the operator said
so), the *approvals*, and a *hash chain* over all of it, signed with
Ed25519.

:mod:`paa.attest.bundle` is the producer side — it may touch the runtime,
the policy and the cold lake's blob substrate. :mod:`paa.attest.verify`
is the consumer side — it reads the bundle and nothing else, so a reviewer
can verify one on a clean checkout with no PAA database at all.

The two sides meet in exactly one place: the bundle directory's shape, as
documented for consumers in ``docs/interfaces/attested-bundle-spec.md``.
"""

from __future__ import annotations

from paa.attest.bundle import (
    Approval,
    BuildResult,
    KeyFile,
    build_bundle,
    load_or_generate_key,
    read_manifest,
)
from paa.attest.format import (
    APPROVAL_DECISIONS,
    ATTEST_SCHEMA,
    SECTION_ORDER,
)
from paa.attest.verify import (
    CheckResult,
    VerificationResult,
    verify_bundle,
)

__all__ = [
    "APPROVAL_DECISIONS",
    "ATTEST_SCHEMA",
    "SECTION_ORDER",
    "Approval",
    "BuildResult",
    "CheckResult",
    "KeyFile",
    "VerificationResult",
    "build_bundle",
    "load_or_generate_key",
    "read_manifest",
    "verify_bundle",
]
