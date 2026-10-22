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
import zlib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import structlog

from paa.core.errors import PaaError

__all__ = ["PackageError", "PackageManifest", "SkillPackage"]

log = structlog.get_logger(__name__)

_MANIFEST_NAME = "manifest.json"
_VALID_KINDS = frozenset({"skill", "agent", "playbook", "config", "bundle"})

#: The fields a manifest cannot do without. The installer gates a package on
#: its name and its version, and the manifest's own constructor needs the
#: kind and the publisher, so a manifest missing any of them is not a
#: manifest at all.
_REQUIRED_MANIFEST_FIELDS = ("package_name", "version", "kind", "publisher")

#: The largest manifest the loader will read. A manifest is metadata: a
#: megabyte is already a catalogue rather than a manifest.
_MAX_MANIFEST_BYTES = 1024 * 1024

#: The largest total unpacked payload the loader will accept. The body on
#: the wire is capped by the registry client, but a zip is a *compressed*
#: body: a few megabytes on the wire can be gigabytes out. The sizes the zip
#: declares are exactly what ``zipfile`` hands back — it reads at most
#: ``file_size`` bytes per member — so the declared total is what this cap
#: is measured on.
_MAX_UNPACKED_BYTES = 64 * 1024 * 1024

#: The largest fragment of remote text a message may name. A remote string
#: is data and a message is a format, so a message may quote a fragment of
#: the remote's words — bounded, and quoted, so it cannot end the message or
#: forge a line. Everything longer than this belongs in the excerpt.
_MAX_REMOTE_FRAGMENT_CHARS = 64

#: The zip errors that mean "this is not a package". They are one failure
#: with six faces: a truncated file, an encrypted member, an unsupported
#: compression method, a corrupt deflate stream, a bad central directory and
#: a zip64 archive the runtime will not read.
_ZIP_ERRORS = (
    zipfile.BadZipFile,
    zipfile.LargeZipFile,
    RuntimeError,
    NotImplementedError,
    EOFError,
    OSError,
    zlib.error,
)


#: A sentinel for "this did not parse". A JSON document can legitimately be
#: ``null``, so a parse failure needs a value no JSON document can produce.
_NOT_JSON = object()


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
        """The manifest's own invariants, checked where the type lives.

        A manifest arrives from a remote registry, so every field in it is
        untrusted: the identity fields are strings, the collection fields are
        lists of strings, and the price is a mapping. A manifest that breaks
        those rules is refused *here* rather than downstream, where the
        refusal would be a ``TypeError`` raised by some other module's
        subscript — which is the same defect this check exists to stop: a
        remote body choosing the exception type.

        The offending value is bounded before it reaches a message. A remote
        string is data, and a message is a format.
        """
        for name in (
            "package_name",
            "version",
            "kind",
            "publisher",
            "description",
            "license",
            "content_hash",
            "publisher_key",
            "signature",
        ):
            if not isinstance(getattr(self, name), str):
                raise PackageError(f"package manifest field {name!r} is not a string")
        for name in ("required_permissions", "dependencies", "entrypoints"):
            value = getattr(self, name)
            if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
                raise PackageError(f"package manifest field {name!r} is not a list of strings")
        if self.price is not None and not isinstance(self.price, dict):
            raise PackageError("package manifest field 'price' is not a mapping")
        if self.kind not in _VALID_KINDS:
            # The kind is the remote's word, so it is bounded and quoted
            # before it reaches the message: a manifest may declare a kind
            # of any length, and an unbounded one would be an unbounded log
            # line.
            raise PackageError(
                f"invalid package kind {self.kind[:_MAX_REMOTE_FRAGMENT_CHARS]!r}",
                valid=sorted(_VALID_KINDS),
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
        """The manifest from its JSON text, or a :class:`PackageError`.

        A manifest is untrusted input: it comes out of a package a remote
        registry served, so every failure here is the project's own error
        and never a ``json.JSONDecodeError`` or a ``TypeError`` about a
        missing argument.

        The JSON error is not chained, and that is deliberate. A
        ``json.JSONDecodeError`` carries the whole document in its ``doc``
        attribute, and the document is exactly what must not travel with the
        error; the parse failure is therefore caught, the value replaced with
        a sentinel, and the refusal raised *outside* the handler — so the
        error carries no context at all.
        """
        try:
            data = json.loads(text)
        except json.JSONDecodeError:
            data = _NOT_JSON
        if data is _NOT_JSON:
            raise PackageError("package manifest is not valid JSON")
        if not isinstance(data, dict):
            raise PackageError("package manifest is not a JSON object")
        missing = [name for name in _REQUIRED_MANIFEST_FIELDS if name not in data]
        if missing:
            raise PackageError("package manifest is missing a required field", missing=missing)
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
        """Load a package from ``.paapkg`` bytes. Refuses zip-slip paths.

        Every failure is a :class:`PackageError`. The bytes are untrusted —
        they came off a remote registry — and a malformed archive must not
        choose the exception type: a truncated file, an encrypted member, an
        unsupported compression method and a corrupt deflate stream are all
        "not a valid .paapkg" as far as a caller is concerned, and the
        loader says so with the project's own error.

        The zip error is not chained, for the same reason the JSON error is
        not: a ``BadZipFile`` names the member it choked on, and a member
        name is the remote's word.
        """
        outcome = cls._unpack(data)
        if isinstance(outcome, PackageError):
            raise outcome
        manifest, files = outcome
        return cls(manifest, files)

    @classmethod
    def _unpack(cls, data: bytes) -> tuple[PackageManifest, dict[str, bytes]] | PackageError:
        """The manifest and the members, or the error to raise instead.

        The zip failure is *returned* rather than raised, so that
        :meth:`from_bytes` can raise it outside the handler: an exception
        raised inside an ``except`` block inherits the handled exception as
        its ``__context__``, and that exception's message can name a member
        of the remote's archive.
        """
        try:
            with zipfile.ZipFile(io.BytesIO(data)) as zf:
                infos = zf.infolist()
                unpacked = sum(info.file_size for info in infos)
                if unpacked > _MAX_UNPACKED_BYTES:
                    raise PackageError(
                        "package is larger than this loader unpacks",
                        unpacked_bytes=unpacked,
                    )
                names = zf.namelist()
                if _MANIFEST_NAME not in names:
                    raise PackageError("package has no manifest.json")
                for name in names:
                    # Zip-slip guard: a member path must not escape the root.
                    if name.startswith("/") or ".." in Path(name).parts:
                        raise PackageError(
                            f"unsafe path in package: {name[:_MAX_REMOTE_FRAGMENT_CHARS]!r}"
                        )
                manifest = cls._read_manifest(zf)
                files = {n: zf.read(n) for n in names if n != _MANIFEST_NAME}
        except _ZIP_ERRORS:
            return PackageError("not a valid .paapkg (bad zip)")
        return manifest, files

    @staticmethod
    def _read_manifest(zf: zipfile.ZipFile) -> PackageManifest:
        """The manifest out of the archive, or a :class:`PackageError`.

        The manifest is bounded before it is parsed: it is the one member
        whose *contents* are read as text, and a member that decompresses to
        a gigabyte of JSON is a denial of service dressed as a package.
        """
        info = zf.getinfo(_MANIFEST_NAME)
        if info.file_size > _MAX_MANIFEST_BYTES:
            raise PackageError("package manifest is too large", manifest_bytes=info.file_size)
        raw = zf.read(_MANIFEST_NAME)
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError:
            raise PackageError("package manifest is not valid UTF-8") from None
        return PackageManifest.from_json(text)

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
