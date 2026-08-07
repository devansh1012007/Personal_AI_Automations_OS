"""Package signing and verification.

A marketplace lets a user run code someone else wrote. The signature is how a
user decides whether to trust a package's *origin*; the content hash (in
``package.py``) is how they verify its *integrity*. Both are necessary — a valid
signature over tampered content, or intact content from an unknown signer, are
each insufficient.

Two backends, selected by availability:

* **Ed25519** via ``cryptography`` when installed — real asymmetric signing.
* **HMAC** dev fallback when it is not — and this is labelled loudly as
  **not real signing**: HMAC is symmetric, so anyone who can verify can also
  forge. It exists so the flow is testable on a bare install, and
  :func:`signing_is_secure` reports which backend is live so the installer can
  refuse untrusted packages when only the fallback is available.
"""

from __future__ import annotations

import base64
import enum
import hashlib
import hmac

import structlog

__all__ = [
    "KeyPair",
    "SignatureState",
    "generate_keypair",
    "sign",
    "signing_is_secure",
    "verify",
]

log = structlog.get_logger(__name__)


class SignatureState(str, enum.Enum):
    """Mirrors the ``marketplace_packages.signature_state`` CHECK values."""

    UNVERIFIED = "unverified"
    VALID = "valid"
    INVALID = "invalid"
    UNTRUSTED_KEY = "untrusted_key"


def _cryptography_available() -> bool:
    import importlib.util

    return importlib.util.find_spec("cryptography") is not None


def signing_is_secure() -> bool:
    """True iff real asymmetric signing is available. False under the HMAC dev
    fallback — the installer uses this to refuse to trust a signature it cannot
    actually authenticate."""
    return _cryptography_available()


class KeyPair:
    """A signing key pair. Public key is safe to publish; private key is not."""

    __slots__ = ("private_b64", "public_b64", "secure")

    def __init__(self, private_b64: str, public_b64: str, *, secure: bool) -> None:
        self.private_b64 = private_b64
        self.public_b64 = public_b64
        self.secure = secure


def generate_keypair() -> KeyPair:
    """Generate a key pair with the strongest available backend."""
    if _cryptography_available():
        from cryptography.hazmat.primitives import serialization
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

        private = Ed25519PrivateKey.generate()
        priv_bytes = private.private_bytes(
            encoding=serialization.Encoding.Raw,
            format=serialization.PrivateFormat.Raw,
            encryption_algorithm=serialization.NoEncryption(),
        )
        pub_bytes = private.public_key().public_bytes(
            encoding=serialization.Encoding.Raw, format=serialization.PublicFormat.Raw
        )
        return KeyPair(_b64(priv_bytes), _b64(pub_bytes), secure=True)

    # HMAC dev fallback: the "key pair" is one shared secret used for both sides.
    import secrets as _secrets

    shared = _secrets.token_bytes(32)
    log.warning(
        "signing.insecure_fallback",
        detail="cryptography not installed; using HMAC (symmetric, forgeable). "
        "Install paa[all] or the cryptography package for real signing.",
    )
    return KeyPair(_b64(shared), _b64(shared), secure=False)


def sign(payload: bytes, private_b64: str) -> str:
    """Sign ``payload``, returning a base64 signature."""
    key = _unb64(private_b64)
    if _cryptography_available():
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

        private = Ed25519PrivateKey.from_private_bytes(key)
        return _b64(private.sign(payload))
    return _b64(hmac.new(key, payload, hashlib.sha256).digest())


def verify(payload: bytes, signature_b64: str, public_b64: str) -> bool:
    """Verify a signature. Never raises — a malformed signature is just False."""
    try:
        sig = _unb64(signature_b64)
        pub = _unb64(public_b64)
    except Exception:
        return False

    if _cryptography_available():
        from cryptography.exceptions import InvalidSignature
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

        try:
            Ed25519PublicKey.from_public_bytes(pub).verify(sig, payload)
            return True
        except (InvalidSignature, ValueError):
            return False

    expected = hmac.new(pub, payload, hashlib.sha256).digest()
    return hmac.compare_digest(expected, sig)


def _b64(data: bytes) -> str:
    return base64.b64encode(data).decode("ascii")


def _unb64(text: str) -> bytes:
    return base64.b64decode(text.encode("ascii"))
