"""The `pipeline` package kind — local/git/file sources through the gates.

SPEC 2 Wave 8, row C8.6 — the pipeline spec's **P5-lite**: the local
directory first, then the pinned git URL, then the ``pipeline`` kind in
a ``.paapkg``, all through the existing five-gate installer. The row
the spec gates on ("do not start the hosted economy without a real
outside author") is respected: **the hosted marketplace stays parked** —
:mod:`paa.marketplace.registry_client` is untouched, there is no
publishing verb, and the only remotes this flow will talk to are git
URLs the operator names at a ref the operator pins.

The flow, one install action, one fresh lineage::

    source     (a local directory, a ``git+<scheme>://`` URL at a PINNED
     |         ref — a commit SHA or a tag, never a moving branch — or
     |         a signed ``.paapkg`` file; each resolves to the package's
     |         bytes on local disk, and an unpinned or moving git ref is
     |         a refusal before anything else runs)
     v
    declaration (``pipeline.toml`` parsed by the SAME closed contract
     |         local manifests parse under — ``declaration_from_manifest``;
     |         the entrypoint is ``pipeline.py`` at the package root)
     v
    the gates  (the five-gate installer, unmodified in its order:
     |         SIGNATURE, CONTENT_HASH, AST_SCAN, PERMISSIONS,
     |         SMOKE_TEST — the smoke gate stages and runs the
     |         entrypoint under the sandbox for real, so a package that
     |         cannot run its own contract is not installed)
     v
    conformance (the six-check standard driven against the gated bytes
     |         through the sandboxed adapter — a RED report refuses the
     |         install before any human is bothered)
     v
    review     (the capability-review artifact: the declaration's ask,
     |         the conformance verdict, and a HUMAN's approve/refuse —
     |         refusal-by-default, never model-decided, the C8.1
     |         human-gate pattern verbatim)
     v
    stage      (the gated bytes land under
                ``PAA_HOME/pipelines/installed/<name>/<version>/`` —
                which is what makes the pipeline registry row appear:
                the registry scans that layout, and the run door drives
                the staged entrypoint through the sandboxed adapter)

A refusal at any stage leaves zero trace by construction: no
marketplace row, no staged files, no registry row — only the
``PIPELINE_PACKAGE_REFUSED`` event that says it was tried. The events
(``PIPELINE_PACKAGE_INSTALLED`` / ``PIPELINE_PACKAGE_REFUSED``) ride
their own fresh lineage and are informational, never lifecycle.

The loading contract this module pins (with
:mod:`paa.pipelines.sandboxed`): **installed code is never imported
into this process.** The gates clear *bytes*; those bytes execute as a
sandboxed subprocess speaking the ``--paa-args`` JSON-in / JSON-on-
stdout contract. The capability ask is reviewed and recorded, not
minted — an installed pipeline cannot call ``ctx.think`` in v1, because
a subprocess holds no facade tokens; that half is the P3 RPC
transport's work, exactly where the spec's P5 placed executable package
loading. The review artifact says so per capability, so a human never
approves an ask that silently will not be honoured.
"""

from __future__ import annotations

import json
import re
import shutil
import uuid
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

import structlog

from paa.core.errors import PaaError
from paa.core.types import EventType, PermissionMode
from paa.improve.gate import (
    DECISION_APPROVED,
    DECISION_REJECTED,
    HumanDecision,
)
from paa.ledger.events import LedgerEvent
from paa.ledger.store import LedgerStore
from paa.marketplace.package import PIPELINE_KIND, PackageManifest, SkillPackage
from paa.pipelines.registry import LocalManifestError, declaration_from_manifest
from paa.pipelines.sandboxed import PIPELINE_ENTRYPOINT
from paa.pipelines.slots import PipelineDeclaration
from paa.storage.relational.base import RelationalBackend

if TYPE_CHECKING:
    from paa.sandbox.base import Sandbox
    from paa.validation.ast_scanner import AstSecurityScanner

__all__ = [
    "REVIEW_ARTIFACT_NAME",
    "SOURCE_GIT",
    "SOURCE_LOCAL_DIR",
    "SOURCE_PAAPKG",
    "CapabilityReview",
    "PipelineInstallReport",
    "PipelinePackageError",
    "PipelinePackageInstaller",
    "ReviewApprover",
    "install_pipeline_package",
    "refuse_review",
    "render_review",
    "terminal_review_approver",
]

log = structlog.get_logger(__name__)

#: The three sources a pipeline package may come from — the P5-lite set.
#: Closed vocabulary: the value rides the ``source`` key of both event
#: payloads, where it is a classifier, not free text.
SOURCE_LOCAL_DIR = "local_dir"
SOURCE_GIT = "git_pinned"
SOURCE_PAAPKG = "paapkg"

#: The review artifact's filename inside a staged install: the human's
#: recorded decision travels with the bytes it cleared, so an auditor
#: reading the staged directory finds the approval beside the code.
REVIEW_ARTIFACT_NAME = "capability-review.json"

#: The refusal stages, in flow order. The five gate names are the
#: installer's own ``InstallGate`` values; ``source``, ``declaration``,
#: ``capability_review`` and ``conformance`` are the stages this flow
#: adds around them. Closed vocabulary — it rides the ``stage`` key of
#: the REFUSED payload.
STAGE_SOURCE = "source"
STAGE_DECLARATION = "declaration"
STAGE_CAPABILITY_REVIEW = "capability_review"
STAGE_CONFORMANCE = "conformance"

#: The URL prefix that names a git source, and the shape the remainder
#: must take: a real scheme (``git+https://``, ``git+file://``, …), so a
#: URL can never begin with ``-`` and be read as a git option — the
#: clone argv is built in code, and this is the second lock on that
#: door.
_GIT_PREFIX = "git+"
_URL_SHAPE = re.compile(r"^[A-Za-z][A-Za-z0-9+.\-]*://\S+$")

#: One host-side git call's wall budget. A clone of a small repository
#: is seconds; anything past this is a hung helper, and the install
#: refuses rather than hanging the operator.
_GIT_TIMEOUT_S = 120.0

#: Bound on a refusal reason carried into an event payload — the same
#: budget every journal reason uses, for the same reason: a longer
#: reason carries risk, not information.
_MAX_REASON_CHARS = 200

#: Bound on the rendered review the CLI shows: the artifact is data, the
#: terminal is a surface, and an unbounded ask description would bury
#: the decision it exists to support.
_MAX_REVIEW_LINE_CHARS = 160

#: The child-environment passthrough for host-side git (the improve
#: loop's set, restated): PATH to find git's helpers, the home variables
#: git needs, the Windows basics. Everything else — ambient secrets
#: included — is dropped.
_GIT_ENV_PASSTHROUGH: frozenset[str] = frozenset(
    {"PATH", "HOME", "USERPROFILE", "HOMEDRIVE", "HOMEPATH", "SystemRoot", "WINDIR",
     "TEMP", "TMP"}
)

#: Forced child variables for host-side git: no credential prompt (the
#: operator named the URL; a prompt would hang a headless install) and
#: no system config (no ambient injection reaches the clone).
_GIT_ENV_FORCED: dict[str, str] = {
    "GIT_TERMINAL_PROMPT": "0",
    "GIT_CONFIG_NOSYSTEM": "1",
}


class PipelinePackageError(PaaError):
    """A pipeline package source or payload is unusable as offered.

    Raised at the ``source`` and ``declaration`` stages — before the
    five gates, before any code executes. The ``stage`` detail names
    where the refusal happened, matching the REFUSED event's own
    vocabulary.
    """

    wire_code = -32015


#: The decision seam: something that can rule on a capability review.
#: The host owns what this is — a human at a terminal (the CLI's
#: :func:`terminal_review_approver`), a scripted answer in a test — and
#: the flow only ever *calls* it. A model is never wired here: the
#: determinism rule (SPEC 2 §9: "no gate is ever model-decided") is the
#: reason this type exists at all.
ReviewApprover = Callable[["CapabilityReview"], Awaitable[HumanDecision]]


# ---------------------------------------------------------------------------
# The capability review (the C8.1 human-gate pattern, instantiated)
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class CapabilityReview:
    """The deterministic artifact a human rules on: the ask, in full.

    Everything on this object is derived from the gated bytes and the
    install's own record — nothing on it is a model's opinion, and
    nothing is free text except the (bounded) note the deciding human
    chose to leave. The ``conformance`` verdict rides along so the
    decision is made on the evidence the machine already produced.
    """

    package_name: str
    version: str
    source: str
    source_ref: str
    content_hash: str
    capabilities: Mapping[str, Any] = field(default_factory=dict)
    required_permissions: tuple[str, ...] = ()
    resume: str = "journal"
    evidence_tier: str = "unrated"
    conformance: Mapping[str, str] = field(default_factory=dict)

    def to_json(self, decision: HumanDecision) -> dict[str, Any]:
        """The artifact as it is staged (and as the event payload reads)."""
        return {
            "package_name": self.package_name,
            "version": self.version,
            "source": self.source,
            "source_ref": self.source_ref,
            "content_hash": self.content_hash,
            "capabilities": dict(self.capabilities),
            "required_permissions": list(self.required_permissions),
            "resume": self.resume,
            "evidence_tier": self.evidence_tier,
            "conformance": dict(self.conformance),
            "decision": decision.to_json(),
        }


async def refuse_review(review: CapabilityReview) -> HumanDecision:
    """The absent-approver answer: refuse, naming the absence.

    The default when a host wires no decision seam. An install that
    cannot ask must not silently install — that would be a gate decided
    by *nobody*, which is the one thing worse than a gate decided by a
    model (the improve loop's rule, restated for this surface).
    """
    return HumanDecision(
        decision=DECISION_REJECTED,
        approver_kind="installer",
        approver_id="unwired-capability-review",
        note="no approver was wired; the pipeline install refuses by default",
    )


def render_review(review: CapabilityReview) -> str:
    """The review as a human reads it at a terminal: the ask, plainly.

    Deterministic and ordered, so the same package always renders the
    same review — the text a human approves is the text the artifact
    records. Each declared capability carries its v1 status honestly:
    recorded for the RPC transport (P3), not minted by the sandboxed
    adapter, so nobody approves an ask that silently will not be
    honoured.
    """
    lines = [
        f"pipeline package: {review.package_name} v{review.version}",
        f"source: {review.source}"
        + (f" @ {review.source_ref}" if review.source_ref else ""),
        f"content hash: {review.content_hash[:16]}…",
        f"resume: {review.resume}   evidence tier: {review.evidence_tier}",
    ]
    if review.capabilities:
        lines.append("capabilities asked (reviewed and recorded; NOT minted in v1 —")
        lines.append("  execution is the sandboxed entrypoint, no facade tokens):")
        for key in sorted(review.capabilities):
            ask = json.dumps(review.capabilities[key], sort_keys=True)
            if len(ask) > _MAX_REVIEW_LINE_CHARS:
                ask = ask[: _MAX_REVIEW_LINE_CHARS - 1] + "…"
            lines.append(f"  - {key}: {ask}")
    else:
        lines.append("capabilities asked: none (an adapter-shaped pipeline)")
    if review.required_permissions:
        lines.append(f"permissions required: {', '.join(sorted(review.required_permissions))}")
    verdict = "GREEN" if all(v == "pass" for v in review.conformance.values()) else "RED"
    lines.append(f"conformance: {verdict} ({len(review.conformance)} checks)")
    return "\n".join(lines)


def terminal_review_approver(*, who: str) -> ReviewApprover:
    """The CLI's decision seam: a human at a terminal, reading the ask.

    Returns the approver callable (the prompt happens when the install
    reaches the review, exactly then). A ``KeyboardInterrupt``, an EOF
    or typer's ``Abort`` (what a closed stdin raises) is a refusal, not
    an error: a human who walked away did not approve.
    """
    import typer

    async def approve(review: CapabilityReview) -> HumanDecision:
        typer.echo(render_review(review))
        try:
            answer = typer.confirm("install this pipeline package?", default=False)
        except (KeyboardInterrupt, EOFError, typer.Abort):
            answer = False
        return HumanDecision(
            decision=DECISION_APPROVED if answer else DECISION_REJECTED,
            approver_kind="human",
            approver_id=who,
            note="terminal prompt" if answer else "terminal prompt declined",
        )

    return approve


# ---------------------------------------------------------------------------
# The event journal (the ADR-0046 pattern, instantiated for this surface)
# ---------------------------------------------------------------------------


class _PackageJournal:
    """Append the pipeline-package event vocabulary to one install's lineage.

    One instance per install action; the installer mints both the
    lineage and the journal. ``attempt`` is always 0 and the
    discriminators name the package and the stage, so a redelivered
    append dedupes through the store's own transaction while an install
    and a later re-install stay distinct rows.
    """

    def __init__(self, ledger: LedgerStore, lineage: uuid.UUID) -> None:
        self._ledger = ledger
        self._lineage = lineage

    async def _append(
        self, event_type: EventType, payload: dict[str, Any], discriminator: str
    ) -> LedgerEvent:
        return await self._ledger.append(
            LedgerEvent.create(
                self._lineage,
                event_type,
                session_id=self._lineage,
                attempt=0,
                discriminator=discriminator,
                payload=payload,
            )
        )

    async def installed(
        self,
        *,
        package_name: str,
        version: str,
        source: str,
        source_ref: str,
        content_hash: str,
        gates: list[str],
        signature_state: str,
        capabilities: Mapping[str, Any],
        review: HumanDecision,
        conformance: Mapping[str, str],
    ) -> LedgerEvent:
        """Append PIPELINE_PACKAGE_INSTALLED — every stage passed, bytes staged."""
        payload: dict[str, Any] = {
            "v": 1,
            "lineage": str(self._lineage),
            "package_name": package_name,
            "version": version,
            "source": source,
            "source_ref": source_ref,
            "content_hash": content_hash,
            "gates": list(gates),
            "signature_state": signature_state,
            "capabilities": dict(capabilities),
            "review": review.to_json(),
            "conformance": dict(conformance),
        }
        return await self._append(
            EventType.PIPELINE_PACKAGE_INSTALLED,
            payload,
            f"install:{package_name}:{version}",
        )

    async def refused(
        self,
        *,
        package_name: str,
        version: str,
        source: str,
        stage: str,
        reason: str,
    ) -> LedgerEvent:
        """Append PIPELINE_PACKAGE_REFUSED — a stage refused; nothing else landed."""
        payload: dict[str, Any] = {
            "v": 1,
            "lineage": str(self._lineage),
            "package_name": package_name,
            "version": version,
            "source": source,
            "stage": stage,
            "reason": reason[:_MAX_REASON_CHARS],
        }
        return await self._append(
            EventType.PIPELINE_PACKAGE_REFUSED,
            payload,
            f"refused:{package_name}:{stage}",
        )


# ---------------------------------------------------------------------------
# Shapes the flow passes between its steps
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class _ResolvedSource:
    """One source resolved to bytes on local disk.

    ``directory`` is the package's files (for git: the pinned worktree
    with ``.git`` removed; for a ``.paapkg``: the unpacked members).
    ``manifest`` is the package manifest to gate — built from the
    declaration for directory/git sources, read from the archive for a
    ``.paapkg`` (a signature cannot survive a rebuild). ``scratch`` is
    the temporary tree the resolution created, cleaned by the caller.
    """

    directory: Path
    manifest: PackageManifest | None
    source: str
    source_ref: str
    scratch: Path | None


@dataclass(frozen=True, slots=True)
class PipelineInstallReport:
    """The caller-facing outcome of one install through this flow."""

    package_name: str
    version: str
    source: str
    source_ref: str
    installed: bool
    stage: str | None = None
    reason: str | None = None
    gates: tuple[str, ...] = ()
    signature_state: str = "unverified"
    review: CapabilityReview | None = None
    decision: HumanDecision | None = None
    conformance: Mapping[str, str] = field(default_factory=dict)
    lineage: uuid.UUID | None = None
    staged: Path | None = None


# ---------------------------------------------------------------------------
# Host-side git (the pinned-ref discipline)
# ---------------------------------------------------------------------------


async def _run_git(args: list[str], *, cwd: Path | None = None) -> tuple[int, str]:
    """One host-side git call: absolute binary, literal argv, capped output.

    The improve loop's discipline (``improve/gitcommit.py``), restated
    for the *fetch* side: the executable is resolved once through
    :func:`shutil.which` so a mid-run ``PATH`` change cannot swap the
    binary, the argv is built in code only, the child environment is an
    allowlist (no ambient ``GIT_*`` injection), and the output is capped
    so a chatty remote cannot deadlock the install. The remote's words
    are never logged and never raised — a URL the remote cannot serve is
    a refusal this flow names in its own vocabulary.
    """
    import asyncio
    import os

    git = shutil.which("git")
    if git is None:
        raise PipelinePackageError(
            "git is not on PATH; a git source cannot be fetched",
            stage=STAGE_SOURCE,
        )
    env = {
        key: value for key, value in os.environ.items() if key in _GIT_ENV_PASSTHROUGH
    }
    env.update(_GIT_ENV_FORCED)
    try:
        proc = await asyncio.create_subprocess_exec(
            git,
            *args,
            cwd=str(cwd) if cwd is not None else None,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            env=env,
        )
    except OSError as exc:
        raise PipelinePackageError(
            f"git could not be started ({type(exc).__name__})",
            stage=STAGE_SOURCE,
        ) from exc
    try:
        stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=_GIT_TIMEOUT_S)
    except TimeoutError:
        proc.kill()
        await proc.communicate()
        raise PipelinePackageError(
            "the git source did not answer within the fetch budget",
            stage=STAGE_SOURCE,
        ) from None
    text = stdout.decode("utf-8", errors="replace")
    if len(text) > 64 * 1024:
        text = text[: 64 * 1024]
    return proc.returncode or 0, text


async def _resolve_git(url: str, ref: str | None, scratch: Path) -> tuple[Path, str]:
    """Clone ``url`` at a PINNED ``ref``; return the worktree and the sha.

    The pinning rule is the row's own: a commit SHA or a tag is pinned,
    a branch is moving and is refused — an install that silently tracks
    a branch is an install whose bytes can change under the gates that
    cleared them. The refusal happens *after* the clone (that is where
    the ref resolves) and *before* any byte is read into a package.
    """
    remainder = url[len(_GIT_PREFIX) :]
    if not _URL_SHAPE.match(remainder):
        raise PipelinePackageError(
            "a git source must be git+<scheme>://… (e.g. git+https://…, "
            "git+file://…)",
            stage=STAGE_SOURCE,
        )
    if not ref or not ref.strip():
        raise PipelinePackageError(
            "a git source needs a pinned ref (--ref): a commit SHA or a tag; "
            "a moving branch is refused because the bytes would change under "
            "the gates that cleared them",
            stage=STAGE_SOURCE,
        )
    ref = ref.strip()
    if ref.startswith("-"):
        raise PipelinePackageError(
            "a git ref may not begin with '-'", stage=STAGE_SOURCE
        )

    clone = scratch / "clone"
    code, _ = await _run_git(["clone", "--no-checkout", remainder, str(clone)])
    if code != 0 or not clone.is_dir():
        raise PipelinePackageError(
            "the git source could not be cloned", stage=STAGE_SOURCE
        )

    code, out = await _run_git(
        ["rev-parse", "--verify", "--quiet", f"{ref}^{{commit}}"], cwd=clone
    )
    sha = out.strip().splitlines()[0].strip() if out.strip() else ""
    if code != 0 or not re.fullmatch(r"[0-9a-f]{40}", sha):
        raise PipelinePackageError(
            "the pinned ref does not resolve to a commit in this source",
            stage=STAGE_SOURCE,
        )

    # The moving-ref check: a ref that names a branch (local or remote)
    # moves when its source moves, and an install pinned to it is not
    # pinned. Tags and raw SHAs pass — a tag is the operator's named
    # snapshot, the same pinning git itself gives a release.
    code, out = await _run_git(
        ["rev-parse", "--symbolic-full-name", ref], cwd=clone
    )
    symbolic = out.strip().splitlines()[0].strip() if out.strip() else ""
    if code == 0 and (
        symbolic.startswith("refs/heads/") or symbolic.startswith("refs/remotes/")
    ):
        raise PipelinePackageError(
            "the named ref is a moving branch; a pipeline package installs "
            "from a pinned commit SHA or tag only",
            stage=STAGE_SOURCE,
        )

    code, _ = await _run_git(["checkout", "--detach", sha], cwd=clone)
    if code != 0:
        raise PipelinePackageError(
            "the pinned ref could not be checked out", stage=STAGE_SOURCE
        )

    # The package is the worktree, not the repository: ``.git`` never
    # enters the payload (the content hash and the gates clear the
    # *files*, and a clone's history is neither).
    package_dir = scratch / "package"
    shutil.copytree(clone, package_dir, ignore=shutil.ignore_patterns(".git"))
    return package_dir, sha


async def _resolve_git_source(
    url: str, ref: str | None, scratch: Path
) -> _ResolvedSource:
    """The git source as the flow's one source shape.

    A thin adapter so the install flow reads one type across all three
    sources — the resolve step's contract is "bytes on local disk plus
    where they came from", not "a tuple some callers remember the order
    of".
    """
    package_dir, sha = await _resolve_git(url, ref, scratch)
    return _ResolvedSource(
        directory=package_dir,
        manifest=None,
        source=SOURCE_GIT,
        source_ref=sha,
        scratch=None,
    )


# ---------------------------------------------------------------------------
# The installer
# ---------------------------------------------------------------------------


class PipelinePackageInstaller:
    """One flow, all stages, one lineage: source → gates → conformance →
    review → stage.

    Constructed with the pieces a runtime already owns (the ledger, the
    relational backend, the sandbox, the AST scanner, the permission
    mode) plus the two decisions the host owns: the unsigned-install
    opt-in (a local directory or a git worktree is unsigned — the same
    explicit opt-in ``paa skills install`` asks for) and the review
    approver seam. Nothing here reaches a model; every decision is
    deterministic code or the human the seam delivers.
    """

    def __init__(
        self,
        *,
        ledger: LedgerStore,
        db: RelationalBackend,
        home: Path | str | None = None,
        sandbox: Sandbox | None = None,
        scanner: AstSecurityScanner | None = None,
        permission_mode: PermissionMode = PermissionMode.ASK,
        allow_unverified: bool = False,
        approver: ReviewApprover | None = None,
    ) -> None:
        from paa.marketplace.installer import PackageInstaller

        self._ledger = ledger
        self._home = Path(home) if home is not None else _default_home()
        self._sandbox = sandbox
        self._permission_mode = permission_mode
        self._approver = approver
        # The signature gate's own rule, passed through unchanged: an
        # unsigned source installs only on the operator's explicit
        # opt-in, never on a default this flow quietly applies.
        self._installer = PackageInstaller(
            db,
            scanner=scanner,
            sandbox=sandbox,
            allow_unverified=allow_unverified,
        )

    # -- locations -----------------------------------------------------------

    @property
    def installed_root(self) -> Path:
        """``PAA_HOME/pipelines/installed`` — where gated bytes are staged.

        The layout under this root is ``<name>/<version>/``, which is
        the shape ADR-0052's P4.1 named for installed pipelines and the
        shape :class:`~paa.pipelines.registry.PipelineRegistry` scans —
        staging IS registration, and the registry row appears on the
        next scan with trust ``installed``.
        """
        return self._home / "pipelines" / "installed"

    def staged_dir(self, name: str, version: str) -> Path:
        """The staged location for one ``(name, version)``."""
        return self.installed_root / name / version

    # -- the flow -------------------------------------------------------------

    async def install(
        self,
        source: str,
        *,
        ref: str | None = None,
        mode: PermissionMode | None = None,
    ) -> PipelineInstallReport:
        """Install one pipeline package from one source, through every stage."""
        import tempfile

        effective_mode = mode if mode is not None else self._permission_mode
        lineage = uuid.uuid4()
        journal = _PackageJournal(self._ledger, lineage)
        scratch_root = Path(tempfile.mkdtemp(prefix="paa-pipeline-pkg-"))
        try:
            return await self._install(
                source,
                ref=ref,
                mode=effective_mode,
                lineage=lineage,
                journal=journal,
                scratch_root=scratch_root,
            )
        finally:
            shutil.rmtree(scratch_root, ignore_errors=True)

    async def _install(
        self,
        source: str,
        *,
        ref: str | None,
        mode: PermissionMode,
        lineage: uuid.UUID,
        journal: _PackageJournal,
        scratch_root: Path,
    ) -> PipelineInstallReport:
        # -- stage 1: the source resolves to bytes on local disk ----------------
        if source.startswith(_GIT_PREFIX):
            source_label = SOURCE_GIT
        elif source.endswith(".paapkg"):
            source_label = SOURCE_PAAPKG
        else:
            source_label = SOURCE_LOCAL_DIR
        try:
            if source_label == SOURCE_PAAPKG:
                resolved = await self._resolve_paapkg(source, scratch_root)
            elif source_label == SOURCE_GIT:
                resolved = await _resolve_git_source(source, ref, scratch_root)
            else:
                resolved = self._resolve_directory(source)
        except PipelinePackageError as exc:
            return await self._refuse(
                journal,
                package_name=_best_name(source),
                version="",
                source=source_label,
                stage=STAGE_SOURCE,
                reason=exc.message,
                lineage=lineage,
            )

        # -- stage 2: the declaration (the same closed contract) ----------------
        try:
            declaration = self._read_declaration(resolved.directory)
        except PipelinePackageError as exc:
            return await self._refuse(
                journal,
                package_name=_best_name(source),
                version="",
                source=source_label,
                stage=STAGE_DECLARATION,
                reason=exc.message,
                lineage=lineage,
            )

        # -- the package manifest: built for a directory/git source (unsigned),
        # -- read from the archive for a .paapkg (the signature covers it)
        if resolved.manifest is None:
            manifest = self._build_manifest(declaration, resolved.source)
            # An unsigned source: the manifest is this flow's own, so the
            # content hash is computed here, over the bytes as they stand.
            package = SkillPackage.from_directory(resolved.directory, manifest=manifest)
        else:
            manifest = resolved.manifest
            problem = _check_packaged_manifest(manifest, declaration)
            if problem is not None:
                return await self._refuse(
                    journal,
                    package_name=declaration.name,
                    version=declaration.version,
                    source=source_label,
                    stage=STAGE_DECLARATION,
                    reason=problem,
                    lineage=lineage,
                )
            # The archive's manifest is signed over its content hash, so
            # the package is reassembled around it WITHOUT recomputing
            # that hash: ``from_directory`` would overwrite the very field
            # the signature covers, and a tampered payload would then be
            # mis-attributed to the signature gate. The gates name the
            # gate that actually refused.
            package = SkillPackage(manifest, _read_files(resolved.directory))

        # -- stage 3: the five gates, unmodified in their order -----------------
        report = await self._installer.install(package, mode=mode)
        if not report.installed:
            gate = report.refused_at.value if report.refused_at is not None else "unknown"
            return await self._refuse(
                journal,
                package_name=declaration.name,
                version=declaration.version,
                source=source_label,
                stage=gate,
                reason=report.reason or "refused by a gate",
                lineage=lineage,
                gates=tuple(g.value for g in report.passed_gates),
                signature_state=report.signature_state.value,
            )

        # -- stage 4: conformance, driven against the gated bytes ---------------
        # No sandbox, no conformance: the pipeline kind's contract is that
        # the bytes RUN sandboxed, and an install that cannot verify that
        # is an install that cannot verify its own loading contract. The
        # smoke gate's "skip when unhealthy" rule does not apply one stage
        # later — here the runner itself is what is being verified.
        if self._sandbox is None:
            return await self._refuse(
                journal,
                package_name=declaration.name,
                version=declaration.version,
                source=source_label,
                stage=STAGE_CONFORMANCE,
                reason=(
                    "no sandbox is wired; a pipeline package installs only "
                    "where its execution can be driven and verified"
                ),
                lineage=lineage,
                gates=tuple(g.value for g in report.passed_gates),
                signature_state=report.signature_state.value,
            )
        conformance_dir = scratch_root / "conformance"
        conformance_dir.mkdir()
        _write_files(conformance_dir, package.files)
        conformance = await self._run_conformance(
            conformance_dir, declaration, self._sandbox
        )
        if not conformance.passed:
            # The gates passed and their marketplace row landed; a RED
            # conformance report refuses the install AFTER them, so the
            # row is unregistered again — the zero-trace rule is the
            # whole flow's, not just the gates' own span of it.
            await self._installer.uninstall(declaration.name, declaration.version)
            failed = [
                f"{check.name}: {check.status}"
                for check in conformance.checks
                if check.status == "fail"
            ]
            return await self._refuse(
                journal,
                package_name=declaration.name,
                version=declaration.version,
                source=source_label,
                stage=STAGE_CONFORMANCE,
                reason="conformance report is RED — " + "; ".join(failed),
                lineage=lineage,
                gates=tuple(g.value for g in report.passed_gates),
                signature_state=report.signature_state.value,
            )

        # -- stage 5: the capability review (the human gate) --------------------
        review = CapabilityReview(
            package_name=declaration.name,
            version=declaration.version,
            source=source_label,
            source_ref=resolved.source_ref,
            content_hash=package.manifest.content_hash,
            capabilities=dict(declaration.capabilities),
            required_permissions=tuple(manifest.required_permissions),
            resume=declaration.resume,
            evidence_tier=declaration.evidence_tier,
            conformance={check.name: check.status for check in conformance.checks},
        )
        approver = self._approver if self._approver is not None else refuse_review
        decision = await approver(review)
        if decision.decision != DECISION_APPROVED:
            # The review refuses after the gates passed: unregister their
            # row, exactly as a RED conformance report does — a human's
            # "no" leaves as little trace as a scanner's.
            await self._installer.uninstall(declaration.name, declaration.version)
            return await self._refuse(
                journal,
                package_name=declaration.name,
                version=declaration.version,
                source=source_label,
                stage=STAGE_CAPABILITY_REVIEW,
                reason=(
                    f"the capability review was refused by {decision.approver_kind}:"
                    f"{decision.approver_id}"
                ),
                lineage=lineage,
                gates=tuple(g.value for g in report.passed_gates),
                signature_state=report.signature_state.value,
                review=review,
                decision=decision,
                conformance={check.name: check.status for check in conformance.checks},
            )

        # -- stage 6: stage the gated bytes; the registry row follows the layout
        staged = self._stage(package, review, decision)

        await journal.installed(
            package_name=declaration.name,
            version=declaration.version,
            source=source_label,
            source_ref=resolved.source_ref,
            content_hash=package.manifest.content_hash,
            gates=[gate.value for gate in report.passed_gates],
            signature_state=report.signature_state.value,
            capabilities=dict(declaration.capabilities),
            review=decision,
            conformance={check.name: check.status for check in conformance.checks},
        )
        log.info(
            "pipeline.package.installed",
            package=declaration.name,
            version=declaration.version,
            source=source_label,
        )
        return PipelineInstallReport(
            package_name=declaration.name,
            version=declaration.version,
            source=source_label,
            source_ref=resolved.source_ref,
            installed=True,
            gates=tuple(gate.value for gate in report.passed_gates),
            signature_state=report.signature_state.value,
            review=review,
            decision=decision,
            conformance={check.name: check.status for check in conformance.checks},
            lineage=lineage,
            staged=staged,
        )

    # -- source resolution ------------------------------------------------------

    def _resolve_directory(self, source: str) -> _ResolvedSource:
        """A local directory: the cheapest source, read where it stands."""
        directory = Path(source)
        if not directory.is_dir():
            raise PipelinePackageError(
                f"the source is not a directory: {source}", stage=STAGE_SOURCE
            )
        return _ResolvedSource(
            directory=directory,
            manifest=None,
            source=SOURCE_LOCAL_DIR,
            source_ref="",
            scratch=None,
        )

    async def _resolve_paapkg(self, source: str, scratch: Path) -> _ResolvedSource:
        """A signed ``.paapkg``: unpacked through the hardened loader.

        Every loader refusal (zip-slip, oversize, a manifest that is not
        JSON) is the package kit's own :class:`PackageError` and becomes
        this flow's ``source``-stage refusal — the archive never chose
        an exception type, and it does not choose one here either.
        """
        path = Path(source)
        if not path.is_file():
            raise PipelinePackageError(
                f"the .paapkg is not a file: {source}", stage=STAGE_SOURCE
            )
        try:
            package = SkillPackage.load(source)
        except PaaError as exc:
            raise PipelinePackageError(
                f"the .paapkg could not be loaded: {exc.message}",
                stage=STAGE_SOURCE,
            ) from exc
        directory = scratch / "package"
        directory.mkdir()
        _write_files(directory, package.files)
        return _ResolvedSource(
            directory=directory,
            manifest=package.manifest,
            source=SOURCE_PAAPKG,
            source_ref="",
            scratch=None,
        )

    # -- the declaration ---------------------------------------------------------

    def _read_declaration(self, directory: Path) -> PipelineDeclaration:
        """Parse ``pipeline.toml`` under the SAME closed contract as local
        manifests — the ADR-0052 rules are the rules; an off-contract
        declaration (unknown section, undeclarable capability, bad
        ``run.resume``) is a named refusal before any gate runs."""
        manifest_path = directory / "pipeline.toml"
        if not manifest_path.is_file():
            raise PipelinePackageError(
                "the package has no pipeline.toml — a pipeline package is its "
                "declaration plus its entrypoint",
                stage=STAGE_DECLARATION,
            )
        try:
            declaration = declaration_from_manifest(manifest_path)
        except LocalManifestError as exc:
            raise PipelinePackageError(
                f"the declaration is off contract: {exc.message}",
                stage=STAGE_DECLARATION,
            ) from exc
        entrypoint = directory / PIPELINE_ENTRYPOINT
        if not entrypoint.is_file():
            raise PipelinePackageError(
                f"the package has no {PIPELINE_ENTRYPOINT} entrypoint",
                stage=STAGE_DECLARATION,
            )
        return declaration

    def _build_manifest(
        self, declaration: PipelineDeclaration, source: str
    ) -> PackageManifest:
        """The package manifest for an unsigned source, built from the
        declaration: the kind and the entrypoint this flow pins, the
        sandbox-run permission the adapter's own execution needs, and
        the identity fields the installer's gates and registry read."""
        return PackageManifest(
            package_name=declaration.name,
            version=declaration.version,
            kind=PIPELINE_KIND,
            publisher=source,
            description=declaration.description[:500],
            required_permissions=["PERM_SANDBOX_RUN"],
            entrypoints=[PIPELINE_ENTRYPOINT],
        )

    # -- conformance ---------------------------------------------------------------

    async def _run_conformance(
        self, staged: Path, declaration: PipelineDeclaration, sandbox: Sandbox
    ) -> Any:
        """The six-check standard against the gated bytes.

        The runs journal on their own fresh lineages (real pipeline
        runs, traceable with ``paa pipeline trace``) — only the install
        decision rides the install's lineage. The sandbox arrives as a
        parameter, not off ``self``: the flow refused the no-sandbox
        case one stage up, and this method's contract says so in its
        signature.
        """
        import tempfile

        from paa.pipelines.conformance import installed_conformance

        with tempfile.TemporaryDirectory(prefix="paa-conformance-") as tmp:
            report = await installed_conformance(
                self._ledger,
                Path(tmp),
                staged=staged,
                declaration=declaration,
                sandbox=sandbox,
            )
        return report

    # -- staging ---------------------------------------------------------------------

    def _stage(
        self, package: SkillPackage, review: CapabilityReview, decision: HumanDecision
    ) -> Path:
        """Write the gated bytes — and the review that cleared them.

        The bytes written are the package's: the same bytes the content
        hash attests, the smoke gate staged and the conformance runner
        drove. An existing staging (a re-install of the same version)
        is overwritten — the gates just re-verified these bytes, and the
        registry's scan is idempotent on the same layout.
        """
        target = self.staged_dir(package.manifest.package_name, package.manifest.version)
        target.parent.mkdir(parents=True, exist_ok=True)
        if target.exists():
            shutil.rmtree(target)
        target.mkdir(parents=True)
        _write_files(target, package.files)
        (target / REVIEW_ARTIFACT_NAME).write_text(
            json.dumps(review.to_json(decision), indent=2, sort_keys=True),
            encoding="utf-8",
        )
        return target

    # -- refusals ----------------------------------------------------------------------

    async def _refuse(
        self,
        journal: _PackageJournal,
        *,
        package_name: str,
        version: str,
        source: str,
        stage: str,
        reason: str,
        lineage: uuid.UUID,
        gates: tuple[str, ...] = (),
        signature_state: str = "unverified",
        review: CapabilityReview | None = None,
        decision: HumanDecision | None = None,
        conformance: Mapping[str, str] | None = None,
    ) -> PipelineInstallReport:
        await journal.refused(
            package_name=package_name,
            version=version,
            source=source,
            stage=stage,
            reason=reason,
        )
        log.warning(
            "pipeline.package.refused",
            package=package_name,
            stage=stage,
        )
        return PipelineInstallReport(
            package_name=package_name,
            version=version,
            source=source,
            source_ref="",
            installed=False,
            stage=stage,
            reason=reason,
            gates=gates,
            signature_state=signature_state,
            review=review,
            decision=decision,
            conformance=dict(conformance) if conformance else {},
            lineage=lineage,
        )


# ---------------------------------------------------------------------------
# The one-call door (the driver the CLI and tests share)
# ---------------------------------------------------------------------------


async def install_pipeline_package(
    source: str,
    *,
    ledger: LedgerStore,
    db: RelationalBackend,
    home: Path | str | None = None,
    sandbox: Sandbox | None = None,
    scanner: AstSecurityScanner | None = None,
    permission_mode: PermissionMode = PermissionMode.ASK,
    allow_unverified: bool = False,
    approver: ReviewApprover | None = None,
    ref: str | None = None,
) -> PipelineInstallReport:
    """Install one pipeline package — the one-call form of the flow.

    The CLI's door and the tests' door are this function, so both leave
    identical rows and identical staging (the observe pattern's
    one-driver-two-doors rule, restated).
    """
    installer = PipelinePackageInstaller(
        ledger=ledger,
        db=db,
        home=home,
        sandbox=sandbox,
        scanner=scanner,
        permission_mode=permission_mode,
        allow_unverified=allow_unverified,
        approver=approver,
    )
    return await installer.install(source, ref=ref)


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _default_home() -> Path:
    """``PAA_HOME`` from settings — the same root everything else uses."""
    from paa.config import get_settings

    return get_settings().home


def _best_name(source: str) -> str:
    """The best-known package name before the declaration is read.

    A refusal before the ``declaration`` stage still wants a name to
    refuse *by*; the source's own basename is the honest best guess and
    never a fabrication.
    """
    trimmed = source.rstrip("/")
    if trimmed.endswith(".paapkg"):
        return Path(trimmed).stem
    if _GIT_PREFIX in trimmed:
        trimmed = trimmed.split("/")[-1] or trimmed
        return trimmed.removesuffix(".git") or trimmed
    return Path(trimmed).name or trimmed


def _write_files(directory: Path, files: Mapping[str, bytes]) -> None:
    """Write a package's members under ``directory`` (in-member paths only).

    The members came through :class:`SkillPackage`, whose loader already
    refused zip-slip paths — this writer keeps the same promise by
    construction: a member that escaped would have to have escaped the
    loader first.
    """
    for name, data in files.items():
        target = directory / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)


def _read_files(directory: Path) -> dict[str, bytes]:
    """Read a staged directory back into the package's member mapping.

    The inverse of :func:`_write_files`, used where the members must be
    reassembled around a manifest that is already signed: unlike
    :meth:`SkillPackage.from_directory`, this never touches the manifest,
    so the content hash a signature covers stays the archive's own.
    """
    files: dict[str, bytes] = {}
    for path in sorted(directory.rglob("*")):
        if path.is_file():
            files[path.relative_to(directory).as_posix()] = path.read_bytes()
    return files


def _check_packaged_manifest(
    manifest: PackageManifest, declaration: PipelineDeclaration
) -> str | None:
    """The identity cross-checks for a manifest that arrived inside a
    ``.paapkg``: the archive's own metadata must agree with the
    declaration its payload carries, or the marketplace row and the
    registry row would disagree about what is installed."""
    if manifest.kind != PIPELINE_KIND:
        return f"the package manifest kind is {manifest.kind!r}, not {PIPELINE_KIND!r}"
    if manifest.package_name != declaration.name:
        return (
            f"the manifest names package {manifest.package_name!r} but the "
            f"declaration names pipeline {declaration.name!r}"
        )
    if manifest.version != declaration.version:
        return (
            f"the manifest version {manifest.version!r} does not match the "
            f"declaration version {declaration.version!r}"
        )
    if PIPELINE_ENTRYPOINT not in manifest.entrypoints:
        return f"the manifest does not declare the {PIPELINE_ENTRYPOINT} entrypoint"
    return None
