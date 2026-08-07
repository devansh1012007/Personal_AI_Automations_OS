"""Marketplace package format — a signed, content-addressed bundle.

A ``.paapkg`` is a zip with a ``manifest.json`` at its root plus the package's
files. It is the unit a publisher ships and a user installs: a skill, an agent
config, a playbook, a config bundle.

The manifest declares everything the installer needs to make a trust decision
*before* unpacking any code: what permissions the package will demand, who
published it, and a content hash over the payload. The content hash is computed
over the files themselves (sorted, streamed), so tampering with any byte after
signing is detectable independently of the signature.
"""

from __future__ import annotations

import hashlib
import io
import json
import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import structlog

from paa.core.errors import PaaError

__all__ = ["PackageError", "PackageManifest", "SkillPackage"]

log = structlog.get_logger(__name__)

_MANIFEST_NAME = "manifest.json"
_VALID_KINDS = frozenset({"skill", "agent", "playbook", "config", "bundle"})


class PackageError(PaaError):
    """A package is malformed or fails a structural check."""


@dataclass(slots=True)
class PackageManifest:
    """The declared metadata of a package. Signed as a unit with the content."""

    package_name: str
    version: str
    kind: str
    publisher: str
    description: str = ""
    required_permissions: list[str] = field(default_factory=list)
    dependencies: list[str] = field(default_factory=list)
    entrypoints: list[str] = field(default_factory=list)
    license: str = "unspecified"
    price: dict[str, Any] = field(default_factory=dict)
    content_hash: str = ""
    publisher_key: str = ""
    signature: str = ""

    def __post_init__(self) -> None:
        if self.kind not in _VALID_KINDS:
            raise PackageError(
                f"invalid package kind {self.kind!r}", valid=sorted(_VALID_KINDS)
            )

    def signing_bytes(self) -> bytes:
        """Canonical bytes that are signed: the manifest minus its own signature.

        The signature field is excluded (it cannot sign itself), but the content
        hash IS included — so a valid signature binds the publisher to this exact
        payload, not just to this metadata.
        """
        payload = {
            "package_name": self.package_name,
            "version": self.version,
            "kind": self.kind,
            "publisher": self.publisher,
            "description": self.description,
            "required_permissions": sorted(self.required_permissions),
            "dependencies": sorted(self.dependencies),
            "entrypoints": sorted(self.entrypoints),
            "license": self.license,
            "content_hash": self.content_hash,
        }
        return json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")

    def as_dict(self) -> dict[str, Any]:
        """Field dict. ``dataclasses.asdict`` because the class is slotted and
        has no ``__dict__``."""
        import dataclasses

        return dataclasses.asdict(self)

    def to_json(self) -> str:
        return json.dumps(self.as_dict(), sort_keys=True, indent=2)

    @classmethod
    def from_json(cls, text: str) -> PackageManifest:
        data = json.loads(text)
        known = {f for f in cls.__dataclass_fields__}
        return cls(**{k: v for k, v in data.items() if k in known})


class SkillPackage:
    """Pack a directory into a ``.paapkg`` and unpack/inspect one."""

    def __init__(self, manifest: PackageManifest, files: dict[str, bytes]) -> None:
        self.manifest = manifest
        self.files = files

    # -- content hashing ---------------------------------------------------

    @staticmethod
    def hash_files(files: dict[str, bytes]) -> str:
        """Content hash over the payload: sorted ``name\\0bytes`` concatenation.

        Sorting makes it order-independent; the ``\\0`` separators stop a file
        named to collide with another's content from forging the same hash.
        """
        h = hashlib.sha256()
        for name in sorted(files):
            h.update(name.encode("utf-8"))
            h.update(b"\x00")
            h.update(files[name])
            h.update(b"\x00")
        return h.hexdigest()

    # -- packing -----------------------------------------------------------

    @classmethod
    def from_directory(
        cls,
        directory: Path | str,
        *,
        manifest: PackageManifest,
    ) -> SkillPackage:
        """Build a package from a directory, computing the content hash."""
        root = Path(directory)
        files: dict[str, bytes] = {}
        for path in sorted(root.rglob("*")):
            if path.is_file() and path.name != _MANIFEST_NAME:
                files[path.relative_to(root).as_posix()] = path.read_bytes()
        manifest.content_hash = cls.hash_files(files)
        return cls(manifest, files)

    def to_bytes(self) -> bytes:
        """Serialise to a ``.paapkg`` (zip) byte string."""
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as zf:
            zf.writestr(_MANIFEST_NAME, self.manifest.to_json())
            for name, data in sorted(self.files.items()):
                zf.writestr(name, data)
        return buffer.getvalue()

    def write(self, path: Path | str) -> Path:
        target = Path(path)
        target.write_bytes(self.to_bytes())
        return target

    # -- unpacking ---------------------------------------------------------

    @classmethod
    def from_bytes(cls, data: bytes) -> SkillPackage:
        """Load a package from ``.paapkg`` bytes. Refuses zip-slip paths."""
        try:
            with zipfile.ZipFile(io.BytesIO(data)) as zf:
                names = zf.namelist()
                if _MANIFEST_NAME not in names:
                    raise PackageError("package has no manifest.json")
                for name in names:
                    # Zip-slip guard: a member path must not escape the root.
                    if name.startswith("/") or ".." in Path(name).parts:
                        raise PackageError(f"unsafe path in package: {name!r}")
                manifest = PackageManifest.from_json(zf.read(_MANIFEST_NAME).decode("utf-8"))
                files = {n: zf.read(n) for n in names if n != _MANIFEST_NAME}
        except zipfile.BadZipFile as exc:
            raise PackageError("not a valid .paapkg (bad zip)") from exc
        return cls(manifest, files)

    @classmethod
    def load(cls, path: Path | str) -> SkillPackage:
        return cls.from_bytes(Path(path).read_bytes())

    def verify_content_hash(self) -> bool:
        """Whether the payload still matches the manifest's declared hash."""
        return self.hash_files(self.files) == self.manifest.content_hash

    def python_sources(self) -> dict[str, str]:
        """Decoded ``.py`` files, for the AST security scan."""
        out: dict[str, str] = {}
        for name, data in self.files.items():
            if name.endswith(".py"):
                try:
                    out[name] = data.decode("utf-8")
                except UnicodeDecodeError:
                    out[name] = ""  # a .py that isn't text is itself suspicious
        return out
