"""Marketplace — buy, sell, and safely install agents, skills, tools and configs.

The platform layer you asked for: a way for people to package and share parts of
this runtime, with the trust machinery that makes running someone else's code
survivable.

``signing``
    Ed25519 (real) or HMAC (dev fallback) package signing.
``package``
    The ``.paapkg`` format: a signed, content-addressed bundle + manifest.
``registry_client``
    Where packages live — a local directory (default, no network) or a remote
    HTTP index.
``installer``
    The five gates (signature → hash → AST scan → permissions → smoke test)
    between a downloaded package and a live skill. A package that fails any gate
    leaves zero trace.

The design rule: a registry is untrusted; nothing it returns becomes runnable
until the installer's gates pass.
"""

from __future__ import annotations

from paa.marketplace.installer import (
    InstallGate,
    InstallOutcome,
    InstallReport,
    PackageInstaller,
)
from paa.marketplace.package import PackageError, PackageManifest, SkillPackage
from paa.marketplace.registry_client import (
    HttpRegistry,
    LocalDirectoryRegistry,
    MarketplaceRegistry,
    RegistryEntry,
)
from paa.marketplace.signing import (
    KeyPair,
    SignatureState,
    generate_keypair,
    sign,
    signing_is_secure,
    verify,
)

__all__ = [
    "HttpRegistry",
    "InstallGate",
    "InstallOutcome",
    "InstallReport",
    "KeyPair",
    "LocalDirectoryRegistry",
    "MarketplaceRegistry",
    "PackageError",
    "PackageInstaller",
    "PackageManifest",
    "RegistryEntry",
    "SignatureState",
    "SkillPackage",
    "generate_keypair",
    "sign",
    "signing_is_secure",
    "verify",
]
