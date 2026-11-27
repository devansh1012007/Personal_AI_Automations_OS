"""The publish side of the pipeline marketplace (SPEC P5 full, W9 C9.1).

The install flow (:mod:`paa.pipelines.packages`) answers "may these bytes
run here". This module answers the question before it: "how do the bytes
reach another machine at all" — and it stays on the operator's side of
the door the whole way::

    source     (a local directory carrying a ``pipeline.toml``, or an
     |         already-installed pipeline under
     |         ``PAA_HOME/pipelines/installed/<name>/<version>`` — the
     |         staged bytes re-read, minus the local review artifact)
     v
    declaration (the SAME closed contract the install flow parses under
     |         — ``_read_declaration`` from packages.py, one reader for
     |         both ends of the economy)
     v
    the floor  (the declaration's ``requires_runtime`` range checked
     |         against the RUNNING version — a package whose own range
     |         excludes the runtime publishing it is refused, because it
     |         would be dead on arrival on this machine and a lie on
     |         any machine that shares the version)
     v
    build+sign (a ``pipeline``-kind ``.paapkg`` built by the shared
     |         ``_build_manifest``, signed with the operator's Ed25519
     |         keypair; an unsigned publish refuses with a named reason
     |         unless ``--allow-unverified`` — the install escape
     |         hatch's flag and meaning, mirrored)
     v
    the hub    (the package bytes plus an index entry written through
                 the EXISTING ``LocalDirectoryRegistry.publish``, so
                 what publish writes is exactly what the registry lists
                 and fetches — the round-trip property)

**Publisher identity.** The keys live under ``PAA_HOME/keys/`` and are
generated on first use (mode ``0600``, with a notice the report carries
so the CLI can say it happened). The public key is written into the
package manifest and the hub's index entry; the private key never leaves
``PAA_HOME`` — it is not in the package, not in the index, and not in any
event payload, because there are no event payloads at all: publishing is
a host-side operator action (the C7.2 host-git precedent — host-side
actions are not ledger rows), so this module never touches the ledger.

**What is deliberately not here.** Publishing to a remote (``http(s)://``)
hub is refused with a named reason: ``HttpRegistry.publish`` has no server
to talk to, and an upload protocol with authentication is its own row.
Key rotation (re-keying a publisher identity and re-signing a catalogue)
is likewise deferred; the ``--key`` override is the manual seam that makes
it possible, not a workflow that makes it convenient.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path

import structlog

from paa import __version__
from paa.core.errors import PaaError
from paa.marketplace.package import SkillPackage
from paa.marketplace.registry_client import LocalDirectoryRegistry
from paa.marketplace.signing import KeyPair, generate_keypair, sign, signing_is_secure
from paa.pipelines.packages import (
    REVIEW_ARTIFACT_NAME,
    PipelinePackageError,
    _build_manifest,
    _default_home,
    _read_declaration,
    _read_files,
    _version_tuple,
    compatibility_refusal,
)

__all__ = [
    "KEYS_DIR_NAME",
    "PUBLISHER_KEY_NAME",
    "PUBLISHER_NAME",
    "PipelinePublishError",
    "PublishReport",
    "load_or_generate_publisher_key",
    "publish_pipeline_package",
]

log = structlog.get_logger(__name__)

#: Where the operator's publisher keys live, under ``PAA_HOME``.
KEYS_DIR_NAME = "keys"

#: The default publisher key file (a new one is generated on first use).
PUBLISHER_KEY_NAME = "publisher.json"

#: The manifest's ``publisher`` string for a published package. The
#: *cryptographic* identity is the publisher key, carried in the manifest
#: and the hub index; this field names the role, because on a local-first
#: runtime with no user model the honest answer to "who published this"
#: is "the operator of this machine", and pretending otherwise (a path,
#: a hostname) would be an identity claim nothing verifies.
PUBLISHER_NAME = "operator"

#: How many installed versions a refusal names when a selection cannot be
#: made — the same discipline every "name the candidates" reason uses: a
#: handful is a choice, a catalogue is a log.
_MAX_NAMED_VERSIONS = 5


class PipelinePublishError(PaaError):
    """A publish source or publisher key is unusable as offered.

    Raised for the caller-shaped contract violations (a bad key file) so
    a library caller gets an exception rather than a report it must
    remember to check; the source-shaped refusals come back on the
    report, named, because they are the operator's answers to read.
    """

    wire_code = -32011


@dataclass(frozen=True, slots=True)
class PublishReport:
    """The caller-facing outcome of one publish through this flow.

    ``publisher_key`` is the PUBLIC key only — the private half never
    enters a report, a package or an index entry. ``key_generated`` says
    the flow minted a new publisher identity on this call (the notice the
    CLI surfaces: a key that appeared by itself is a key the operator
    should know about).
    """

    package_name: str
    version: str
    published: bool
    reason: str | None = None
    hub: Path | None = None
    package_path: Path | None = None
    signed: bool = False
    publisher_key: str = ""
    key_generated: bool = False


def _refused(package_name: str, version: str, reason: str) -> PublishReport:
    """The refused report, and the log line that names it in the
    operator's own words — no hub file, no index entry, nothing staged."""
    log.warning("pipeline.publish.refused", package=package_name, reason=reason)
    return PublishReport(
        package_name=package_name,
        version=version,
        published=False,
        reason=reason,
    )


# ---------------------------------------------------------------------------
# The publisher key
# ---------------------------------------------------------------------------


def _key_path(home: Path, key: Path | str | None) -> Path:
    """Where the publisher key lives: the ``--key`` override, or the
    ``PAA_HOME`` default."""
    if key is not None:
        return Path(key)
    return home / KEYS_DIR_NAME / PUBLISHER_KEY_NAME


def _write_keypair(path: Path, pair: KeyPair) -> None:
    """Write a keypair, readable by its owner alone.

    The file carries the private half, so it gets mode ``0600`` and its
    parent is created with the default umask — the key lives under
    ``PAA_HOME`` and nowhere else, and the mode is the filesystem's half
    of that promise.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "v": 1,
                "private_b64": pair.private_b64,
                "public_b64": pair.public_b64,
            },
            indent=2,
        ),
        encoding="utf-8",
    )
    os.chmod(path, 0o600)


def load_or_generate_publisher_key(
    key_path: Path | str | None = None, *, home: Path | str | None = None
) -> tuple[KeyPair, bool]:
    """The operator's publisher keypair, and whether this call minted it.

    Read from ``key_path`` (the ``--key`` override) or the ``PAA_HOME``
    default; generated and written on first use, with the generation
    logged — the notice is a fact the caller surfaces, not a print from
    a library. A file that exists but is not a keypair is a named
    :class:`PipelinePublishError` (a corrupt key must not be silently
    replaced: that would re-key a publisher identity out from under the
    packages already signed with it).
    """
    path = _key_path(
        _default_home() if home is None else Path(home),
        key_path,
    )
    if path.is_file():
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise PipelinePublishError(
                f"the publisher key at {path} could not be read ({type(exc).__name__})"
            ) from exc
        if not isinstance(data, dict):
            raise PipelinePublishError(f"the publisher key at {path} is not a key file")
        private_b64 = data.get("private_b64")
        public_b64 = data.get("public_b64")
        if not isinstance(private_b64, str) or not isinstance(public_b64, str):
            raise PipelinePublishError(f"the publisher key at {path} is not a key file")
        log.info("marketplace.publisher_key_loaded", path=str(path))
        return KeyPair(private_b64, public_b64, secure=signing_is_secure()), False

    pair = generate_keypair()
    _write_keypair(path, pair)
    log.info(
        "marketplace.publisher_key_generated",
        path=str(path),
        secure=pair.secure,
        detail="a new publisher identity was generated; the private key stays "
        "under PAA_HOME and is never published",
    )
    return pair, True


# ---------------------------------------------------------------------------
# The source
# ---------------------------------------------------------------------------


def _resolve_source(
    source: str, home: Path, version: str | None
) -> tuple[Path, str, str | None]:
    """The directory to package, its best-known name, and a refusal.

    A directory source is read where it stands. Anything else names an
    *installed* pipeline (the staged-bytes re-publish the brief calls
    for): ``PAA_HOME/pipelines/installed/<name>/<version>``, newest
    staged version when ``--version`` is absent, the named one when it
    is present. The returned refusal is the project's own words; the
    installed names a refusal lists are this home's own, not a remote's.
    """
    candidate = Path(source)
    if candidate.is_dir():
        if version is not None:
            return (
                candidate,
                candidate.name,
                "--version selects among an installed pipeline's staged versions; "
                "a directory's version is its pipeline.toml's own",
            )
        return candidate, candidate.name, None

    installed_root = home / "pipelines" / "installed"
    name_root = installed_root / source
    if not name_root.is_dir():
        installed = (
            sorted(child.name for child in installed_root.iterdir() if child.is_dir())
            if installed_root.is_dir()
            else []
        )
        if installed:
            known = ", ".join(installed[:_MAX_NAMED_VERSIONS])
            if len(installed) > _MAX_NAMED_VERSIONS:
                known += ", …"
        else:
            known = "(none)"
        return (
            name_root,
            source,
            f"no installed pipeline named {source!r} and no such directory; "
            f"installed: {known}",
        )

    versions = sorted(
        (child for child in name_root.iterdir() if child.is_dir()),
        key=lambda child: (_version_tuple(child.name) or (0,)),
    )
    if version is not None:
        staged = name_root / version
        if not staged.is_dir():
            names = [child.name for child in versions]
            return (
                name_root,
                source,
                f"{source!r} has no staged version {version!r}; staged: "
                f"{', '.join(names[:_MAX_NAMED_VERSIONS]) or '(none)'}",
            )
        return staged, source, None
    if not versions:
        return name_root, source, f"the installed pipeline {source!r} has no staged versions"
    return versions[-1], source, None


# ---------------------------------------------------------------------------
# The one-call door
# ---------------------------------------------------------------------------


async def publish_pipeline_package(
    source: str,
    *,
    hub: Path | str,
    home: Path | str | None = None,
    key: Path | str | None = None,
    version: str | None = None,
    allow_unverified: bool = False,
) -> PublishReport:
    """Publish one pipeline package to one hub directory.

    The library door and the CLI's door are this function (the
    one-driver-two-doors rule the install flow already pins). A refusal
    comes back on the report with its reason named and leaves the hub
    exactly as it was — no package file, no index entry — because
    nothing is written until every check above it has passed.
    """
    hub_text = str(hub).strip()
    if hub_text.startswith(("http://", "https://")):
        return _refused(
            source,
            version or "",
            "publishing to a remote hub is not supported yet — name a directory "
            "(the LocalDirectoryRegistry format)",
        )
    hub_path = Path(hub)

    home_path = _default_home() if home is None else Path(home)
    directory, best_name, refusal = _resolve_source(source, home_path, version)
    if refusal is not None:
        return _refused(best_name, version or "", refusal)

    # The declaration: the SAME closed contract the install flow parses
    # under, through the same reader.
    try:
        declaration = _read_declaration(directory)
    except PipelinePackageError as exc:
        return _refused(best_name, version or "", exc.message)

    # The compatibility floor, checked against the RUNNING version: the
    # publisher's runtime is the first machine this range has to hold on.
    problem = compatibility_refusal(declaration.requires_runtime, __version__)
    if problem is not None:
        return _refused(declaration.name, declaration.version, problem)

    # The package: the staged bytes minus the local review artifact (a
    # review is this machine's record, not another machine's payload),
    # hashed, and signed when the operator did not opt out.
    files = {
        name: data
        for name, data in _read_files(directory).items()
        if name != REVIEW_ARTIFACT_NAME
    }
    manifest = _build_manifest(declaration, PUBLISHER_NAME)
    manifest.content_hash = SkillPackage.hash_files(files)

    generated = False
    public_key = ""
    if not allow_unverified:
        # The install escape hatch, mirrored: the same flag, the same
        # meaning — ``--allow-unverified`` is the operator's explicit word
        # that unverified bytes may travel. Without it, the publish signs,
        # and a backend that cannot really sign refuses rather than
        # pretending an HMAC is a signature.
        try:
            pair, generated = load_or_generate_publisher_key(key, home=home_path)
        except PipelinePublishError as exc:
            return _refused(declaration.name, declaration.version, exc.message)
        if not signing_is_secure():
            return _refused(
                declaration.name,
                declaration.version,
                "the signing backend is the insecure HMAC dev fallback; a published "
                "signature must be a real one — pass --allow-unverified to publish "
                "unsigned instead",
            )
        manifest.publisher_key = pair.public_b64
        manifest.signature = sign(manifest.signing_bytes(), pair.private_b64)
        public_key = pair.public_b64

    try:
        await LocalDirectoryRegistry(hub_path).publish(SkillPackage(manifest, files))
    except OSError as exc:
        return _refused(
            declaration.name,
            declaration.version,
            f"the hub directory could not be written ({type(exc).__name__})",
        )

    package_path = hub_path / f"{declaration.name}-{declaration.version}.paapkg"
    log.info(
        "pipeline.publish.published",
        package=declaration.name,
        version=declaration.version,
        hub=str(hub_path),
        signed=bool(manifest.signature),
    )
    return PublishReport(
        package_name=declaration.name,
        version=declaration.version,
        published=True,
        hub=hub_path,
        package_path=package_path,
        signed=bool(manifest.signature),
        publisher_key=public_key,
        key_generated=generated,
    )
