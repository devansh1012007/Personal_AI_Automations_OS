"""The secret proxy — RFC §8.2's answer to "how does a sandbox use a token?".

The threat
----------
A sandboxed worker frequently needs a credential: an API key for an HTTP call,
a token for a git push. The obvious implementation puts it in the child's
environment. That is wrong in a way that is easy to miss, because it *works*:

* the value is readable by every line of agent-authored code in that process,
  including code the AST scanner passed as benign;
* it appears in ``/proc/<pid>/environ`` and in any crash dump;
* it is inherited by every grandchild process the skill spawns;
* it survives in the sandbox's own logs the moment the skill prints its
  environment for debugging.

The design
----------
The sandbox is never given the secret. It calls
``system_call.get_secret("KEY")``; the **host** intercepts that call, verifies
:attr:`~paa.core.types.Permission.SECRET_READ` is granted, decrypts in memory,
and supplies the value for **that single operation** only. The plaintext exists
in the host process, for the duration of one call, and is never written to the
sandbox's environment, filesystem or logs.

The value-never-leaks guarantee
-------------------------------
Everything in this module handles secrets as :class:`SecretValue`, whose
``__repr__``/``__str__`` return ``"***"``. That makes the safe thing the
default: an f-string, a ``log.info(value=...)`` call, a traceback that renders
locals, or a ``PaaError`` detail dict all print the redaction rather than the
credential. Getting at the plaintext requires the explicit, greppable
:meth:`SecretValue.reveal`. Exceptions raised here carry the secret *name*
only — never the value, and never a prefix or length of it, since both leak
usable information about a credential.

Encryption at rest
------------------
``cryptography`` is not a declared dependency of this project (see
``pyproject.toml`` — the runtime targets a memory-constrained machine and every
heavyweight dependency is opt-in). When it is present, secrets are sealed with
Fernet: AES-128-CBC with an HMAC-SHA256 authentication tag, key derived by
PBKDF2-HMAC-SHA256.

When it is absent the store falls back to XOR against a stretched keystream.
**That fallback is not encryption.** It provides obfuscation against casual
disk inspection and nothing else: it is malleable, it is not authenticated, and
a known-plaintext attack recovers the keystream outright. It exists so a
developer can run tests without a compiler toolchain, it logs a warning on
every single use, and :attr:`SecretBroker.is_strongly_encrypted` reports
``False`` so callers can refuse to store real credentials under it. Do not
represent it as secure, because it is not.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import secrets as _stdlib_secrets
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Final

import structlog

from paa.core.errors import PaaError, PermissionDeniedError
from paa.core.types import Permission

__all__ = [
    "REDACTION",
    "SecretAccess",
    "SecretBroker",
    "SecretNotFoundError",
    "SecretValue",
]

log = structlog.get_logger(__name__)

#: What a secret renders as anywhere other than :meth:`SecretValue.reveal`.
REDACTION: Final[str] = "***"

_KDF_ITERATIONS: Final[int] = 200_000
_SALT_BYTES: Final[int] = 16
_MAGIC_FERNET: Final[str] = "paa-fernet-v1"
_MAGIC_XOR: Final[str] = "paa-xor-dev-v1-INSECURE"


class SecretNotFoundError(PaaError):
    """No secret is stored under that name.

    Carries the *name* only. A "did you mean" listing of stored names is
    deliberately omitted: enumerating which credentials exist is itself useful
    to an attacker who has achieved code execution inside a skill.
    """

    def __init__(self, name: str) -> None:
        super().__init__("no secret is stored under that name", secret_name=name)
        self.secret_name = name


class SecretValue:
    """An opaque handle to a plaintext secret.

    Redaction is the default rendering; :meth:`reveal` is the one way out and
    is short enough to grep for in review. ``__eq__`` uses
    :func:`hmac.compare_digest` so comparing a secret against a guess does not
    leak its length or prefix through timing.
    """

    __slots__ = ("_name", "_value")

    def __init__(self, name: str, value: str) -> None:
        self._name = name
        self._value = value

    def reveal(self) -> str:
        """Return the plaintext. Every call site is a place to look hard at."""
        return self._value

    @property
    def name(self) -> str:
        return self._name

    def __str__(self) -> str:
        return REDACTION

    def __repr__(self) -> str:
        return f"SecretValue(name={self._name!r}, value={REDACTION})"

    def __format__(self, spec: str) -> str:
        """Covers ``f"{secret}"`` and ``f"{secret:>20}"`` alike.

        Without this, ``format()`` falls through to ``__str__`` for an empty
        spec but would raise for a non-empty one — and a raised exception
        during logging is its own outage.
        """
        return REDACTION

    def __eq__(self, other: object) -> bool:
        if isinstance(other, SecretValue):
            return hmac.compare_digest(self._value, other._value)
        if isinstance(other, str):
            return hmac.compare_digest(self._value, other)
        return NotImplemented

    def __hash__(self) -> int:
        # Hash the name, never the value: a hash of the value in a dict repr or
        # a debugger view is an offline-guessable fingerprint of a credential.
        return hash(("SecretValue", self._name))

    def __len__(self) -> int:
        """Length of the redaction, not of the secret.

        Returning the true length would let ``len(secret)`` fingerprint the
        credential — and a caller who genuinely needs it can ``reveal()``.
        """
        return len(REDACTION)


@dataclass(frozen=True, slots=True)
class SecretAccess:
    """One audit record. Contains no secret material, by construction."""

    secret_name: str
    requester: str
    granted: bool
    reason: str
    at: datetime
    correlation_id: str | None = None

    def to_payload(self) -> dict[str, Any]:
        return {
            "secret_name": self.secret_name,
            "requester": self.requester,
            "granted": self.granted,
            "reason": self.reason,
            "at": self.at.isoformat(),
            "correlation_id": self.correlation_id,
        }


# ---------------------------------------------------------------------------
# Cipher backends
# ---------------------------------------------------------------------------


class _Cipher:
    """Shared surface for the real and the fallback sealing implementations."""

    magic: str = "unset"
    strong: bool = False

    def seal(self, plaintext: str) -> str:  # pragma: no cover - abstract
        raise NotImplementedError

    def open(self, sealed: str) -> str:  # pragma: no cover - abstract
        raise NotImplementedError


class _FernetCipher(_Cipher):
    """Authenticated encryption via ``cryptography``'s Fernet."""

    magic = _MAGIC_FERNET
    strong = True

    def __init__(self, passphrase: bytes, salt: bytes) -> None:
        from cryptography.fernet import Fernet
        from cryptography.hazmat.primitives import hashes
        from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC

        kdf = PBKDF2HMAC(
            algorithm=hashes.SHA256(),
            length=32,
            salt=salt,
            iterations=_KDF_ITERATIONS,
        )
        self._fernet = Fernet(base64.urlsafe_b64encode(kdf.derive(passphrase)))

    def seal(self, plaintext: str) -> str:
        return self._fernet.encrypt(plaintext.encode("utf-8")).decode("ascii")

    def open(self, sealed: str) -> str:
        return self._fernet.decrypt(sealed.encode("ascii")).decode("utf-8")


class _XorDevCipher(_Cipher):
    """Development-only obfuscation. NOT ENCRYPTION. See the module docstring.

    Keystream is PBKDF2-stretched and per-record (a fresh nonce per seal), so
    two records with the same plaintext do not produce the same ciphertext —
    that much at least is done properly. An HMAC tag is appended so *corruption*
    is detected. None of that makes the confidentiality property sound: the
    cipher is a stream XOR with a keystream the attacker can recover from any
    single known plaintext. It is here to keep the runtime testable, nothing
    more.
    """

    magic = _MAGIC_XOR
    strong = False

    def __init__(self, passphrase: bytes, salt: bytes) -> None:
        self._key = hashlib.pbkdf2_hmac("sha256", passphrase, salt, _KDF_ITERATIONS, dklen=32)
        log.warning(
            "skills.secrets.weak_cipher_in_use",
            detail=(
                "the 'cryptography' package is not installed; secrets are being "
                "obfuscated with XOR, which is NOT encryption and must not be "
                "used for real credentials. Install paa's cryptography extra."
            ),
        )

    def _keystream(self, nonce: bytes, length: int) -> bytes:
        out = bytearray()
        counter = 0
        while len(out) < length:
            out += hashlib.sha256(self._key + nonce + counter.to_bytes(4, "big")).digest()
            counter += 1
        return bytes(out[:length])

    def seal(self, plaintext: str) -> str:
        raw = plaintext.encode("utf-8")
        nonce = os.urandom(16)
        cipher = bytes(a ^ b for a, b in zip(raw, self._keystream(nonce, len(raw)), strict=True))
        tag = hmac.new(self._key, nonce + cipher, hashlib.sha256).digest()[:16]
        return base64.urlsafe_b64encode(nonce + tag + cipher).decode("ascii")

    def open(self, sealed: str) -> str:
        blob = base64.urlsafe_b64decode(sealed.encode("ascii"))
        nonce, tag, cipher = blob[:16], blob[16:32], blob[32:]
        expected = hmac.new(self._key, nonce + cipher, hashlib.sha256).digest()[:16]
        if not hmac.compare_digest(tag, expected):
            raise ValueError("sealed secret failed its integrity check")
        raw = bytes(a ^ b for a, b in zip(cipher, self._keystream(nonce, len(cipher)), strict=True))
        return raw.decode("utf-8")


def _build_cipher(passphrase: bytes, salt: bytes) -> _Cipher:
    """Prefer real crypto; degrade loudly and honestly when it is unavailable."""
    try:
        return _FernetCipher(passphrase, salt)
    except ImportError:
        return _XorDevCipher(passphrase, salt)


# ---------------------------------------------------------------------------
# Broker
# ---------------------------------------------------------------------------


@dataclass
class _Store:
    """On-disk shape of the secret file."""

    magic: str
    salt: str
    entries: dict[str, str] = field(default_factory=dict)


class SecretBroker:
    """Host-side custodian of credentials. RFC §8.2.

    One instance per runtime. Skills never hold a reference to it — they emit a
    ``get_secret`` system call, and :meth:`handle_system_call` is what the host
    runs on their behalf.

    Usage::

        broker = SecretBroker(path, passphrase="…")
        broker.put("GITHUB_TOKEN", "ghp_…")

        with broker.lease("GITHUB_TOKEN", granted=mode.granted,
                          requester="skill:deploy") as token:
            do_one_thing(token.reveal())
    """

    def __init__(
        self,
        path: Path | str,
        *,
        passphrase: str | bytes | None = None,
        audit_limit: int = 10_000,
    ) -> None:
        self._path = Path(path)
        self._audit: list[SecretAccess] = []
        self._audit_limit = audit_limit

        material = passphrase if passphrase is not None else self._machine_passphrase()
        self._passphrase = material.encode("utf-8") if isinstance(material, str) else material

        self._store = self._load_or_init()
        self._cipher = _build_cipher(self._passphrase, base64.b64decode(self._store.salt))

    # -- properties --------------------------------------------------------

    @property
    def is_strongly_encrypted(self) -> bool:
        """``False`` when the XOR dev fallback is in force.

        Callers holding real credentials should check this and refuse. It is a
        property rather than a log line because a warning scrolls past and a
        boolean can gate an action.
        """
        return self._cipher.strong

    @property
    def path(self) -> Path:
        return self._path

    def names(self) -> list[str]:
        """Stored secret names. Host-side introspection only — never exposed
        to a sandbox, which would turn the broker into a credential index."""
        return sorted(self._store.entries)

    # -- persistence -------------------------------------------------------

    def _machine_passphrase(self) -> str:
        """Derive a passphrase when the caller supplies none.

        Uses ``PAA_SECRET_PASSPHRASE`` if set. Otherwise a per-store random
        passphrase is generated and written beside the store with 0600 where
        the platform honours it. This is key-at-rest-next-to-data, which
        protects against a stolen backup of the store alone and not against a
        compromised host — the honest bound, stated rather than implied.
        """
        if env := os.environ.get("PAA_SECRET_PASSPHRASE"):
            return env
        key_file = self._path.with_suffix(".key")
        if key_file.exists():
            return key_file.read_text(encoding="ascii").strip()

        generated = _stdlib_secrets.token_urlsafe(32)
        key_file.parent.mkdir(parents=True, exist_ok=True)
        key_file.write_text(generated, encoding="ascii")
        _restrict_permissions(key_file)
        log.warning(
            "skills.secrets.generated_local_key",
            path=str(key_file),
            detail=(
                "no PAA_SECRET_PASSPHRASE was set; a random key was written next "
                "to the secret store. Anyone who can read the store can read the "
                "key. Set PAA_SECRET_PASSPHRASE for a real trust boundary."
            ),
        )
        return generated

    def _load_or_init(self) -> _Store:
        if self._path.exists():
            try:
                raw = json.loads(self._path.read_text(encoding="utf-8"))
                return _Store(
                    magic=str(raw["magic"]),
                    salt=str(raw["salt"]),
                    entries=dict(raw.get("entries", {})),
                )
            except (json.JSONDecodeError, KeyError, OSError) as exc:
                raise PaaError(
                    "secret store is unreadable or corrupt",
                    path=str(self._path),
                    detail=type(exc).__name__,
                ) from exc
        return _Store(magic="pending", salt=base64.b64encode(os.urandom(_SALT_BYTES)).decode())

    def _flush(self) -> None:
        """Write the store atomically.

        Atomic because a torn write loses *every* secret at once, and the
        recovery story for that is "the user re-enters all their credentials".
        """
        self._store.magic = self._cipher.magic
        self._path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self._path.with_suffix(self._path.suffix + ".tmp")
        payload = {
            "magic": self._store.magic,
            "salt": self._store.salt,
            "entries": self._store.entries,
        }
        tmp.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
        _restrict_permissions(tmp)
        tmp.replace(self._path)

    # -- mutation ----------------------------------------------------------

    def put(self, name: str, value: str) -> None:
        """Store a secret, sealed.

        Nothing here logs the value. The log line records the name and the
        cipher in force, which is what an operator needs to audit and what an
        attacker with log access learns nothing from.
        """
        if not name:
            raise ValueError("secret name must not be empty")
        self._store.entries[name] = self._cipher.seal(value)
        self._flush()
        log.info("skills.secrets.stored", secret_name=name, strong=self._cipher.strong)

    def delete(self, name: str) -> bool:
        removed = self._store.entries.pop(name, None) is not None
        if removed:
            self._flush()
            log.info("skills.secrets.deleted", secret_name=name)
        return removed

    # -- the proxy ---------------------------------------------------------

    def get_secret(
        self,
        name: str,
        *,
        granted: frozenset[Permission],
        requester: str,
        correlation_id: str | None = None,
    ) -> SecretValue:
        """RFC §8.2's ``system_call.get_secret``, executed host-side.

        :param granted: the active :class:`~paa.core.types.PermissionMode`'s
            grant set. Passed in rather than read from global settings so a
            per-skill grant narrower than the mode's can be enforced.
        :raises PermissionDeniedError: when ``SECRET_READ`` is not granted.
            Raised *before* the store is consulted, so a denied caller cannot
            even learn whether a given secret name exists.
        :raises SecretNotFoundError: when no such secret is stored.
        """
        if Permission.SECRET_READ not in granted:
            self._record(name, requester, granted=False, reason="missing_secret_read",
                         correlation_id=correlation_id)
            log.warning(
                "skills.secrets.denied",
                secret_name=name,
                requester=requester,
                reason="missing_secret_read",
            )
            raise PermissionDeniedError(missing=[Permission.SECRET_READ.value], mode="skill_grant")

        sealed = self._store.entries.get(name)
        if sealed is None:
            self._record(name, requester, granted=False, reason="not_found",
                         correlation_id=correlation_id)
            raise SecretNotFoundError(name)

        try:
            plaintext = self._cipher.open(sealed)
        except Exception as exc:
            # Never surface the underlying exception's args: a cipher error can
            # echo ciphertext, and ciphertext next to a known algorithm is more
            # than we want in a log.
            self._record(name, requester, granted=False, reason="unsealing_failed",
                         correlation_id=correlation_id)
            raise PaaError(
                "stored secret could not be unsealed",
                secret_name=name,
                detail=type(exc).__name__,
            ) from None

        self._record(name, requester, granted=True, reason="ok", correlation_id=correlation_id)
        log.info("skills.secrets.issued", secret_name=name, requester=requester)
        return SecretValue(name, plaintext)

    @contextmanager
    def lease(
        self,
        name: str,
        *,
        granted: frozenset[Permission],
        requester: str,
        correlation_id: str | None = None,
    ) -> Iterator[SecretValue]:
        """Scope a secret to one operation, per the RFC's "single operation" rule.

        The handle is invalidated on exit so a caller that stashes it outside
        the block gets a redaction rather than a live credential. Python cannot
        scrub an immutable ``str`` from memory, so this is a *scoping*
        guarantee, not a zeroisation one — claiming otherwise would be exactly
        the kind of overstatement this module refuses to make elsewhere.
        """
        handle = self.get_secret(
            name, granted=granted, requester=requester, correlation_id=correlation_id
        )
        try:
            yield handle
        finally:
            object.__setattr__(handle, "_value", "")

    def handle_system_call(
        self,
        call: dict[str, Any],
        *,
        granted: frozenset[Permission],
        requester: str,
        correlation_id: str | None = None,
    ) -> dict[str, Any]:
        """Host-side handler for a sandbox's ``get_secret`` system call.

        This is the boundary method: a sandbox emits a JSON call on its control
        channel, the host runs this, and only the *result of the operation* —
        never the credential — is what the sandbox's own protocol carries back.
        The return value therefore deliberately contains no ``value`` key. A
        caller wiring this to a real sandbox must use the returned handle
        host-side (see :meth:`lease`), not forward it.
        """
        if call.get("op") != "get_secret":
            return {"ok": False, "error": f"unsupported system call {call.get('op')!r}"}
        name = call.get("name")
        if not isinstance(name, str) or not name:
            return {"ok": False, "error": "get_secret requires a 'name' string"}
        try:
            self.get_secret(
                name, granted=granted, requester=requester, correlation_id=correlation_id
            )
        except PermissionDeniedError:
            return {"ok": False, "error": "permission denied", "secret_name": name}
        except SecretNotFoundError:
            return {"ok": False, "error": "not found", "secret_name": name}
        return {"ok": True, "secret_name": name, "note": "value withheld from sandbox by design"}

    # -- audit -------------------------------------------------------------

    def _record(
        self,
        name: str,
        requester: str,
        *,
        granted: bool,
        reason: str,
        correlation_id: str | None,
    ) -> None:
        from paa.storage.relational.database import utc_now

        self._audit.append(
            SecretAccess(
                secret_name=name,
                requester=requester,
                granted=granted,
                reason=reason,
                at=utc_now(),
                correlation_id=correlation_id,
            )
        )
        # Bounded so a skill in a retry loop cannot exhaust host memory through
        # the audit trail — availability matters as much as the record does.
        if len(self._audit) > self._audit_limit:
            del self._audit[: len(self._audit) - self._audit_limit]

    def audit_log(self) -> list[SecretAccess]:
        """Every access attempt, granted or refused, oldest first."""
        return list(self._audit)

    def denied_accesses(self) -> list[SecretAccess]:
        return [record for record in self._audit if not record.granted]


def _restrict_permissions(path: Path) -> None:
    """Best-effort 0600. POSIX honours it; Windows ACLs are not modelled here."""
    try:
        path.chmod(0o600)
    except (OSError, NotImplementedError):  # pragma: no cover - platform dependent
        log.debug("skills.secrets.chmod_unsupported", path=str(path))
