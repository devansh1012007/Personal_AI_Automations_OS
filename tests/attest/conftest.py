"""The attestation suite needs the asymmetric signing backend.

``paa[attest]`` supplies ``cryptography``; a minimal install can still import
every attest module (the optional import is function-local), but it cannot
produce or verify a real Ed25519 bundle. Skip the directory at collection,
the same way ``tests/api`` and the live storage suites skip for their extras:
a skipped dependency is a missing dependency, not a green result.
"""

from __future__ import annotations

import pytest

pytest.importorskip("cryptography", reason="needs the attest extra (paa[attest])")
