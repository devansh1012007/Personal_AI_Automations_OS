"""The chat extension surface — C8.4, SPEC 2 Wave 8.

The one flow this module owns: a capability the runtime did not ship
becomes a capability a chat turn can use, and every step of that is
gated, sandboxed and journalled::

    discover  (a Claw-Hub/OpenClaw-shaped directory — a hub of
     |        ``SKILL.md`` skill directories, or the personal skills
     |        dir under ``PAA_HOME/skills/personal`` — read through
     |        :class:`~paa.skills.adapters.claw_hub.ClawHubAdapter`)
     v
    install   (the five-gate installer, unmodified in its order:
     |        SIGNATURE, CONTENT_HASH, AST_SCAN, PERMISSIONS,
     |        SMOKE_TEST — the smoke gate runs the staged entrypoint
     |        under the sandbox for real; a package that fails any
     |        gate leaves zero trace: no marketplace row, no staged
     |        files, no registry row)
     v
    activate  (the contract — rebuilt against the *staged* bytes, not
     |        the source directory — is registered in the skills
     |        registry, which is what makes it live; a skill that
     |        declares ``kind: coding`` in its SKILL.md frontmatter
     |        selects its coding loop here through the pipeline
     |        registry: :data:`~paa.pipelines.registry.CODING_FALLBACK_PIPELINE`
     |        when no external harness is attached)
     v
    chat      (the capability surfaces in the chat turn's prompt as a
              ``[capabilities]`` section; the model — or the user, the
              convention is plain text — requests one with the
              ``@use-skill`` directive :func:`paa.chat.loop.parse_skill_directive`
              defines; the dispatch crosses policy (the USA's
              permission check fires before any sandbox boots), the
              sandbox (the adapter's own containment), and the coder
              journal (``TOOL_CALL_*`` rows on a fresh per-call lineage,
              the same shape ``paa mcp serve`` gives its mediated calls))

MCP tools enter the same surface one level up: an MCP server is not
files this runtime can gate, so its tools arrive through the existing
:class:`~paa.skills.adapters.mcp.McpServerAdapter` — whose own
discovery already enforces ``manifest ∩ mandate ∩ policy`` and refuses
to run without a sandbox — and activate straight into the registry.
From there a chat turn cannot tell a Claw-Hub skill from an MCP tool:
both are rows in the capability catalogue, both dispatch through the
USA, both journal their calls.

The ledger record is the ADR-0046 pattern instantiated for this
surface: ``EXTENSION_INSTALLED`` / ``EXTENSION_REFUSED`` /
``EXTENSION_ACTIVATED`` on one fresh lineage per install action (see
``core/types.py`` for the payload contracts). Nothing here writes state
the ledger cannot explain: the marketplace row and the staged files
land only after all five gates pass, and the skills-registry row is the
activation's own effect.
"""

from __future__ import annotations

import hashlib
import json
import shutil
import uuid
from collections.abc import Awaitable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

import structlog

from paa.core.errors import PaaError, SkillContractError
from paa.core.types import EventType, Permission, PermissionMode
from paa.ledger.events import LedgerEvent
from paa.ledger.store import LedgerStore
from paa.marketplace.installer import InstallReport
from paa.marketplace.package import PackageManifest, SkillPackage
from paa.pipelines.registry import (
    CODING_FALLBACK_PIPELINE,
    PipelineRegistry,
    RegisteredPipeline,
    select,
)
from paa.skills.adapters.base import SkillAdapter
from paa.skills.adapters.claw_hub import ClawHubAdapter, parse_skill_md
from paa.skills.contracts import SkillContract, SkillResult
from paa.skills.registry import SkillRegistry
from paa.skills.usa import UnifiedSkillAdapter
from paa.storage.relational.base import RelationalBackend

if TYPE_CHECKING:
    from paa.sandbox.base import Sandbox
    from paa.validation.ast_scanner import AstSecurityScanner

__all__ = [
    "Activation",
    "ChatExtensionSurface",
    "DiscoveredSkill",
    "ExtensionInstallReport",
    "ExtensionJournal",
    "render_capability_lines",
    "render_skill_result",
    "resolve_coding_pipeline",
]

log = structlog.get_logger(__name__)

#: The personal skills directory, relative to ``PAA_HOME``: the
#: operator-authored drop zone, the same convention ``~/.claude/skills``
#: established. Discovered with the same Claw-Hub adapter because an
#: OpenClaw skill directory *is* the format it reads.
PERSONAL_DIR_NAME = "personal"

#: Where a skill's gated bytes are staged after all five gates pass:
#: ``PAA_HOME/skills/installed/<skill_name>/<version>/``. Staging the
#: *package's* bytes (not a re-copy of the source directory) is what
#: makes "what the smoke test ran" and "what is installed" the same
#: files.
INSTALLED_DIR_NAME = "installed"

#: The source labels used in events and the marketplace ``publisher``
#: column. Closed vocabulary on purpose: an event payload's ``source``
#: is a classifier, not free text.
SOURCE_CLAW_HUB = "claw_hub"
SOURCE_PERSONAL = "personal"
SOURCE_MCP = "mcp_server"

#: The one SKILL.md frontmatter key the activation reads beyond what the
#: contract already carries: ``kind: coding`` declares that the skill
#: needs a coding loop, which the activation then selects through the
#: pipeline registry (C8.3's re-scope made the fallback's name the
#: machine-readable answer to "which loop when no harness is attached").
_CODING_KIND = "coding"

#: Bound on a refusal reason carried into an event payload. Same budget
#: as the chat journal's, for the same reason: a longer reason carries
#: risk, not information.
_MAX_REASON_CHARS = 200

#: Bound on the rendered skill result a chat turn feeds back to the
#: model. A tool result is context, not a document: the model needs
#: enough to answer, and an unbounded result would silently blow the
#: turn's token budget from below.
_MAX_RESULT_CHARS = 4000


# ---------------------------------------------------------------------------
# The extension journal (ADR-0046 pattern)
# ---------------------------------------------------------------------------


class ExtensionJournal:
    """Append the extension event vocabulary to one install's lineage.

    One instance per install action; the surface mints both. ``attempt``
    is always 0 and the discriminators name the fact and the package, so
    a redelivered append dedupes through the store's own transaction
    while an install and its activation stay distinct rows.
    """

    def __init__(self, ledger: LedgerStore, lineage: uuid.UUID) -> None:
        self._ledger = ledger
        self._lineage = lineage

    @property
    def lineage(self) -> uuid.UUID:
        return self._lineage

    def _append(
        self, event_type: EventType, payload: dict[str, Any], discriminator: str
    ) -> Awaitable[LedgerEvent]:
        return self._ledger.append(
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
        content_hash: str,
        gates: Sequence[str],
        signature_state: str,
    ) -> LedgerEvent:
        """Append EXTENSION_INSTALLED — all five gates passed, bytes staged."""
        payload: dict[str, Any] = {
            "v": 1,
            "lineage": str(self._lineage),
            "package_name": package_name,
            "version": version,
            "kind": "skill",
            "source": source,
            "content_hash": content_hash,
            "gates": list(gates),
            "signature_state": signature_state,
        }
        return await self._append(
            EventType.EXTENSION_INSTALLED,
            payload,
            f"install:{package_name}:{version}",
        )

    async def refused(
        self,
        *,
        package_name: str,
        version: str,
        source: str,
        gate: str,
        reason: str | None,
    ) -> LedgerEvent:
        """Append EXTENSION_REFUSED — a gate refused; nothing else landed."""
        payload: dict[str, Any] = {
            "v": 1,
            "lineage": str(self._lineage),
            "package_name": package_name,
            "version": version,
            "kind": "skill",
            "source": source,
            "gate": gate,
            "reason": (reason or "refused")[:_MAX_REASON_CHARS],
        }
        return await self._append(
            EventType.EXTENSION_REFUSED,
            payload,
            f"refused:{package_name}:{gate}",
        )

    async def activated(
        self,
        *,
        skill_name: str,
        version: str,
        provider: str,
        source: str,
        pipeline: str | None,
    ) -> LedgerEvent:
        """Append EXTENSION_ACTIVATED — the capability is live for chat.

        ``pipeline`` is the coding loop a ``kind: coding`` skill's
        activation selected through the pipeline registry (``None`` both
        for skills that need no loop and for a loop an attached external
        harness replaces — the fallback is selected only when it would
        actually run).
        """
        payload: dict[str, Any] = {
            "v": 1,
            "lineage": str(self._lineage),
            "skill_name": skill_name,
            "version": version,
            "provider": provider,
            "source": source,
            "pipeline": pipeline,
        }
        return await self._append(
            EventType.EXTENSION_ACTIVATED,
            payload,
            f"activate:{skill_name}:{version}",
        )


# ---------------------------------------------------------------------------
# Shapes the flow passes between its steps
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class DiscoveredSkill:
    """One skill a source offered: its contract, its directory, its extras.

    ``frontmatter`` carries the keys the contract does not — ``kind``
    today — parsed by the same reader the adapter used, so the flow and
    the adapter can never disagree about what the SKILL.md said.
    """

    contract: SkillContract
    skill_dir: Path
    source: str
    frontmatter: dict[str, Any]


@dataclass(frozen=True, slots=True)
class Activation:
    """One capability made live: what was activated, and its coding loop."""

    skill_name: str
    version: str
    provider: str
    source: str
    pipeline: str | None
    lineage: uuid.UUID


@dataclass(frozen=True, slots=True)
class ExtensionInstallReport:
    """The caller-facing outcome of one install through the extension flow.

    Wraps the installer's own :class:`InstallReport` (the five gates'
    verdict) with what this flow added around it: the activation, when
    the install passed, and the lineage the events landed on either way.
    """

    install: InstallReport
    source: str
    skill_dir: Path
    activation: Activation | None = None
    lineage: uuid.UUID | None = None

    @property
    def installed(self) -> bool:
        return self.install.installed

    @property
    def skill_name(self) -> str:
        return self.install.package_name


# ---------------------------------------------------------------------------
# Rendering helpers (the chat-facing half)
# ---------------------------------------------------------------------------


def render_capability_lines(contracts: Sequence[SkillContract]) -> list[str]:
    """One prompt line per capability: ``name v<version> — description``.

    Deterministic and ordered by name, so a replayed turn renders the
    same section byte-for-byte. The description is capped so a chatty
    contract cannot dominate the prompt.
    """
    lines: list[str] = []
    for contract in sorted(contracts, key=lambda c: c.skill_name):
        stripped = contract.description.strip()
        description = stripped.splitlines()[0] if stripped else ""
        if len(description) > 120:
            description = description[:119] + "…"
        lines.append(f"{contract.skill_name} v{contract.version} — {description}")
    return lines


def render_skill_result(skill_name: str, result: SkillResult) -> str:
    """Render one dispatch outcome as the text the model reads back.

    The rendering keeps the *outcome* honest — a failed skill says it
    failed, with its error, rather than a fabricated empty success —
    and bounds the text so a large output cannot silently blow the
    turn's context budget from below.
    """
    if result.ok:
        try:
            body = json.dumps(result.output, sort_keys=True)
        except (TypeError, ValueError):
            body = repr(result.output)
        text = f"{skill_name} ok: {body}"
    else:
        error = result.error or "skill failed"
        text = f"{skill_name} failed: {error}"
    if len(text) > _MAX_RESULT_CHARS:
        text = text[: _MAX_RESULT_CHARS - 1] + "…"
    return text


# ---------------------------------------------------------------------------
# The coding-loop resolution (where CODING_FALLBACK_PIPELINE is consumed)
# ---------------------------------------------------------------------------


def resolve_coding_pipeline(
    registry: PipelineRegistry, *, harness_attached: bool
) -> RegisteredPipeline | None:
    """The coding loop a ``kind: coding`` extension selects at activation.

    The C8.3 position, made operational: the coder tool loop is the
    *fallback* — selected explicitly, by name, through the same
    :func:`~paa.pipelines.registry.select` every explicit selection
    uses — and only when **no external harness is attached**. A harness
    (``paa record`` mediating one, ``paa thread`` binding one) is the
    coding loop; selecting the fallback beside it would be a second
    loop nobody asked for, so an attached harness returns ``None`` and
    the activation records that honestly instead.

    Selection is never scored and never defaulted: the fallback's name
    is passed explicitly, the registry validates it is registered, and a
    registry that did not carry it would refuse here — at activation,
    before a turn ever depends on it.
    """
    if harness_attached:
        return None
    return select(CODING_FALLBACK_PIPELINE, registry=registry)


# ---------------------------------------------------------------------------
# The surface
# ---------------------------------------------------------------------------


class ChatExtensionSurface:
    """Discovery, the five-gate install, activation, and the dispatch door.

    Constructed with the pieces a runtime already owns — the ledger, the
    relational backend, the sandbox backend, the AST scanner, the
    permission mode — and wired into :class:`~paa.chat.service.ChatService`
    as its optional ``extensions``. The service turns the surface into
    per-turn grants: the capability catalogue the prompt renders and the
    dispatcher the facade mints. Nothing here reaches a model; every
    decision the surface makes (gates, policy, selection) is
    deterministic, per the spec's own rule.
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
        mandate_permissions: frozenset[Permission] | None = None,
        harness_attached: bool = False,
    ) -> None:
        from paa.marketplace.installer import PackageInstaller

        self._ledger = ledger
        self._db = db
        self._home = Path(home) if home is not None else _default_home()
        self._sandbox = sandbox
        self._permission_mode = permission_mode
        self._mandate_permissions = mandate_permissions
        self._harness_attached = harness_attached
        # The signature gate's own rule, passed through unchanged: a
        # local skill directory is unsigned, and installing one is an
        # explicit opt-in the operator makes — never a default this
        # surface quietly applies for them.
        self._installer = PackageInstaller(
            db,
            scanner=scanner,
            sandbox=sandbox,
            allow_unverified=allow_unverified,
        )
        self._registry = SkillRegistry(db)
        # One adapter instance for discovery and claw_hub invocation:
        # invoke reads the contract's working_dir, so a single stateless
        # adapter serves every installed skill directory.
        self._claw = ClawHubAdapter(self._home)

    # -- locations ---------------------------------------------------------

    @property
    def personal_dir(self) -> Path:
        """``PAA_HOME/skills/personal`` — the operator's drop zone."""
        return self._home / "skills" / PERSONAL_DIR_NAME

    @property
    def installed_root(self) -> Path:
        """``PAA_HOME/skills/installed`` — where gated bytes are staged."""
        return self._home / "skills" / INSTALLED_DIR_NAME

    def staged_dir(self, skill_name: str, version: str) -> Path:
        """The staged location for one ``(skill, version)``."""
        return self.installed_root / skill_name / version

    # -- discovery -----------------------------------------------------------

    async def discover(
        self, source: Path | str | None = None, *, personal: bool = False
    ) -> list[DiscoveredSkill]:
        """Enumerate the skills a source offers, without installing anything.

        ``personal=True`` reads the personal skills dir; otherwise
        ``source`` names a Claw-Hub/OpenClaw-shaped directory (a hub of
        skill subdirectories or a single skill directory — the layout
        ``claw pull`` produces). Discovery is read-only: the adapter
        builds contracts, this method keeps the directories and the
        frontmatter keys the contract does not carry, and nothing is
        registered, staged or journalled.
        """
        if personal:
            root, label = self.personal_dir, SOURCE_PERSONAL
        else:
            if source is None:
                raise PaaError(
                    "discovery needs a source directory, or personal=True for "
                    "the personal skills dir"
                )
            root, label = Path(source), SOURCE_CLAW_HUB
        adapter = ClawHubAdapter(root)
        contracts = await adapter.discover()
        found: list[DiscoveredSkill] = []
        for contract in contracts:
            skill_dir = Path(contract.invocation.working_dir or root)
            found.append(
                DiscoveredSkill(
                    contract=contract,
                    skill_dir=skill_dir,
                    source=label,
                    frontmatter=_read_frontmatter(skill_dir),
                )
            )
        return found

    # -- install: the five gates ---------------------------------------------

    async def install(
        self,
        skill: DiscoveredSkill,
        *,
        mode: PermissionMode | None = None,
        activate: bool = True,
    ) -> ExtensionInstallReport:
        """Install one discovered skill through all five gates.

        The gates run over an **in-memory** package built from the skill
        directory — the installer's smoke gate stages its own throwaway
        copy — so a refusal leaves zero trace by construction: no
        marketplace row, no staged files, no registry row. Only after
        the installer reports success are the *gated bytes* staged under
        ``PAA_HOME/skills/installed/<name>/<version>/`` and the contract
        rebuilt against that directory, which is what makes "what the
        smoke test ran" and "what a chat turn executes" the same files.

        ``activate=True`` (the default) registers the rebuilt contract
        immediately — the row's own flow is discover → install →
        activate — and returns the :class:`Activation` on the report.
        """
        effective_mode = mode if mode is not None else self._permission_mode
        lineage = uuid.uuid4()
        journal = ExtensionJournal(self._ledger, lineage)

        contract = skill.contract
        manifest = PackageManifest(
            package_name=contract.skill_name,
            version=contract.version,
            kind="skill",
            publisher=skill.source,
            description=contract.description[:500],
            required_permissions=[p.value for p in contract.required_permissions],
            entrypoints=[contract.invocation.entrypoint_file or "main.py"],
        )
        package = SkillPackage.from_directory(skill.skill_dir, manifest=manifest)

        report = await self._installer.install(package, mode=effective_mode)
        if not report.installed:
            await journal.refused(
                package_name=contract.skill_name,
                version=contract.version,
                source=skill.source,
                gate=report.refused_at.value if report.refused_at is not None else "unknown",
                reason=report.reason,
            )
            return ExtensionInstallReport(
                install=report,
                source=skill.source,
                skill_dir=skill.skill_dir,
                lineage=lineage,
            )

        staged = self._stage(package)
        await journal.installed(
            package_name=contract.skill_name,
            version=contract.version,
            source=skill.source,
            content_hash=package.manifest.content_hash,
            gates=[gate.value for gate in report.passed_gates],
            signature_state=report.signature_state.value,
        )

        if not activate:
            return ExtensionInstallReport(
                install=report,
                source=skill.source,
                skill_dir=skill.skill_dir,
                lineage=lineage,
            )
        activation = await self._activate_staged(
            staged, source=skill.source, frontmatter=skill.frontmatter, lineage=lineage
        )
        return ExtensionInstallReport(
            install=report,
            source=skill.source,
            skill_dir=skill.skill_dir,
            activation=activation,
            lineage=lineage,
        )

    def _stage(self, package: SkillPackage) -> Path:
        """Write the gated bytes to their installed location.

        The bytes written are the package's — the same bytes the content
        hash attests and the smoke test executed — never a re-copy of
        the source directory, which could have changed since. An
        existing staging (a re-install of the same version) is
        overwritten: the gates just re-verified these bytes, and the
        registry's own register is idempotent on the same version.
        """
        target = self.staged_dir(package.manifest.package_name, package.manifest.version)
        target.parent.mkdir(parents=True, exist_ok=True)
        if target.exists():
            shutil.rmtree(target)
        target.mkdir(parents=True)
        for name, data in package.files.items():
            out = target / name
            out.parent.mkdir(parents=True, exist_ok=True)
            out.write_bytes(data)
        return target

    # -- activation -----------------------------------------------------------

    async def activate(self, skill_name: str) -> Activation:
        """(Re-)activate an installed skill by name.

        The contract is read back from the skills registry (the row an
        install left, pointing at the staged directory) and re-registered
        — the registry's own re-activation semantics — on a fresh
        lineage, so a re-activation is a new audit fact, not a dedupe
        hit against the install's activation.
        """
        contract = await self._registry.get(skill_name, include_inactive=True)
        if contract is None:
            raise SkillContractError(
                "no installed skill by that name", skill_name=skill_name
            )
        working_dir = contract.invocation.working_dir
        frontmatter = _read_frontmatter(Path(working_dir)) if working_dir else {}
        return await self._activate_contract(
            contract,
            source=_source_of(contract),
            frontmatter=frontmatter,
            lineage=uuid.uuid4(),
        )

    async def _activate_staged(
        self,
        staged: Path,
        *,
        source: str,
        frontmatter: Mapping[str, Any],
        lineage: uuid.UUID,
    ) -> Activation:
        """Register the contract rebuilt against the staged directory."""
        adapter = ClawHubAdapter(staged)
        contract = adapter.build_contract(staged)
        # Provenance the directory itself cannot carry: the gated content
        # hash and the staged location the gates cleared.
        contract = contract.model_copy(
            update={
                "source_checksum": _directory_hash(staged),
                "source_uri": staged.as_uri(),
            }
        )
        return await self._activate_contract(
            contract, source=source, frontmatter=frontmatter, lineage=lineage
        )

    async def _activate_contract(
        self,
        contract: SkillContract,
        *,
        source: str,
        frontmatter: Mapping[str, Any],
        lineage: uuid.UUID,
    ) -> Activation:
        """Register one contract and journal its activation."""
        await self._registry.register(contract)
        pipeline: str | None = None
        if str(frontmatter.get("kind") or "") == _CODING_KIND:
            resolved = resolve_coding_pipeline(
                self._pipeline_registry(), harness_attached=self._harness_attached
            )
            pipeline = resolved.name if resolved is not None else None
        journal = ExtensionJournal(self._ledger, lineage)
        await journal.activated(
            skill_name=contract.skill_name,
            version=contract.version,
            provider=contract.provider,
            source=source,
            pipeline=pipeline,
        )
        return Activation(
            skill_name=contract.skill_name,
            version=contract.version,
            provider=contract.provider,
            source=source,
            pipeline=pipeline,
            lineage=lineage,
        )

    def _pipeline_registry(self) -> PipelineRegistry:
        """The pipeline registry this surface selects coding loops through.

        Built per call: the local directory can change under a
        long-lived surface, and a selection made against a stale
        registry is a selection the operator never made.
        """
        return PipelineRegistry(local_dir=self._home / "pipelines")

    # -- MCP tools as chat capabilities ---------------------------------------

    async def install_mcp_server(
        self,
        server_command: Sequence[str],
        *,
        version: str = "0.1.0",
    ) -> list[Activation]:
        """Expose a self-hosted MCP server's tools as chat capabilities.

        An MCP server is not a file package this runtime can gate, so it
        does not ride the five-gate installer; it rides the existing
        adapter's own enforcement instead — discovery refuses to run
        without a sandbox, the tool allowlist is
        ``manifest ∩ mandate ∩ policy``, and every invocation is
        journalled on its own lineage by the adapter itself. What this
        method adds is the *activation*: each granted tool becomes a
        registry row (a chat capability) with an EXTENSION_ACTIVATED
        record, on one shared lineage for the server's set.
        """
        if self._sandbox is None:
            raise SkillContractError(
                "installing an MCP server as a chat capability requires a "
                "sandbox; none was provided",
            )
        from paa.skills.adapters.mcp import McpServerAdapter

        adapter = McpServerAdapter(
            list(server_command),
            version=version,
            sandbox=self._sandbox,
            permission_mode=self._permission_mode,
            mandate_permissions=self._mandate_permissions,
            ledger=self._ledger,
        )
        contracts = await adapter.discover()
        if not contracts:
            log.warning("chat.extensions.mcp_no_granted_tools", command=server_command[0])
            return []
        lineage = uuid.uuid4()
        return [
            await self._activate_contract(
                contract, source=SOURCE_MCP, frontmatter={}, lineage=lineage
            )
            for contract in contracts
        ]

    # -- the catalogue and the dispatch door ------------------------------------

    async def capability_catalogue(self) -> list[SkillContract]:
        """The active skills a chat turn may use, filtered by policy.

        Only what a turn could actually dispatch is surfaced — the same
        rule the MCP adapter states for discovery — so a capability the
        active mode would refuse never reaches a prompt. Ordered by
        name; the prompt render is a pure function of this list.
        """
        granted = self._grants()
        active = await self._registry.list_active()
        return [
            contract for contract in active if not contract.missing_permissions(granted)
        ]

    async def dispatch_skill(
        self,
        skill_name: str,
        arguments: Mapping[str, Any],
        *,
        session_id: uuid.UUID,
    ) -> SkillResult:
        """Run one skill for one chat turn: policy, sandbox, coder journal.

        The dispatch is the USA's own pipeline — arguments validated
        against the contract's input schema, the permission check fired
        *before* any sandbox boots, the adapter invoked under
        containment, the output validated against the output schema —
        wrapped in the coder journal's ADR-0046 order on a fresh
        per-call lineage, the same shape ``paa mcp serve`` gives its
        mediated tool calls. A policy refusal is journalled
        ``TOOL_CALL_DENIED`` and re-raised; a crash is journalled
        ``TOOL_CALL_FAILED`` and re-raised; only a completed call
        returns.
        """
        from paa.coder.journal import LedgerToolJournal, args_preview_redacted, args_sha256_of
        from paa.coder.loop import ToolCtx
        from paa.sandbox.base import IsolationLevel

        contract = await self._registry.get(skill_name)
        if contract is None:
            raise SkillContractError(
                "skill is unknown or inactive", skill_name=skill_name
            )

        lineage = uuid.uuid4()
        isolation = (
            self._sandbox.isolation_level
            if self._sandbox is not None
            else IsolationLevel.NONE
        )
        ctx = ToolCtx(
            session_id=str(session_id),
            call_id=uuid.uuid4().hex,
            tool=skill_name,
            args_sha256=args_sha256_of(dict(arguments)),
            args_preview_redacted=args_preview_redacted(skill_name, dict(arguments)),
            isolation_level=isolation,
        )
        journal = LedgerToolJournal(self._ledger, lineage)
        await journal.before(ctx)  # TOOL_CALL_PROPOSED — before gate and effect

        usa = self._usa_for(contract)
        try:
            result = await usa.dispatch(
                skill_name,
                dict(arguments),
                mode=self._permission_mode,
                sandbox=self._sandbox,
                correlation_id=str(lineage),
            )
        except PaaError as exc:
            # A refusal before execution (unknown skill, invalid args,
            # permission denied): DENIED, then the refusal propagates.
            await journal.denied(ctx, str(exc)[:_MAX_REASON_CHARS])
            raise
        except Exception:
            await journal.after(ctx, ok=False, result_sha=None)
            raise
        if result.ok and result.output_valid:
            digest = hashlib.sha256(
                json.dumps(result.output, sort_keys=True).encode("utf-8")
            ).hexdigest()
            await journal.after(ctx, ok=True, result_sha=digest)
        else:
            await journal.after(ctx, ok=False, result_sha=None)
        return result

    async def dispatch_skill_text(
        self,
        skill_name: str,
        arguments: Mapping[str, Any],
        *,
        session_id: uuid.UUID,
    ) -> str:
        """:func:`dispatch_skill`, rendered for the prompt (:func:`render_skill_result`)."""
        result = await self.dispatch_skill(skill_name, arguments, session_id=session_id)
        return render_skill_result(skill_name, result)

    def _usa_for(self, contract: SkillContract) -> UnifiedSkillAdapter:
        """A USA bound to the adapter this contract's provider needs.

        Built per dispatch, because ``mcp_server`` contracts each carry
        their own server command — a single provider→adapter map cannot
        serve two servers, and pretending it could would dispatch one
        server's tool to another's process. The USA itself is stateless;
        construction is a dict build. Providers this surface installs
        none of (``native``) get an empty adapter list, and the USA
        refuses the dispatch by name.
        """
        adapter: SkillAdapter | None = None
        if contract.provider == "mcp_server":
            from paa.skills.adapters.mcp import McpServerAdapter

            adapter = McpServerAdapter(
                list(contract.invocation.server_command),
                sandbox=self._sandbox,
                permission_mode=self._permission_mode,
                mandate_permissions=self._mandate_permissions,
                ledger=self._ledger,
            )
        elif contract.provider == "claw_hub":
            adapter = self._claw
        return UnifiedSkillAdapter(self._registry, [adapter] if adapter else [])

    def _grants(self) -> frozenset[Permission]:
        """The ``mandate ∩ policy`` legs, tighten-only (the adapter's rule)."""
        granted = self._permission_mode.granted
        if self._mandate_permissions is not None:
            granted &= self._mandate_permissions
        return granted


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _default_home() -> Path:
    """``PAA_HOME`` from settings — the same root everything else uses."""
    from paa.config import get_settings

    return get_settings().home


def _read_frontmatter(skill_dir: Path) -> dict[str, Any]:
    """The SKILL.md frontmatter of a directory, or an empty mapping.

    Read through the adapter's own parser so the contract and the flow
    agree on what the file said. A missing or malformed file yields
    ``{}`` rather than raising: by the time this runs the adapter has
    already built a contract from the same file, so a failure here is a
    directory that changed underneath the flow — the flow carries on
    with no extension keys rather than dying mid-install.
    """
    skill_file = skill_dir / "SKILL.md"
    try:
        frontmatter, _ = parse_skill_md(skill_file.read_text(encoding="utf-8"))
    except (OSError, PaaError):
        return {}
    return frontmatter if isinstance(frontmatter, dict) else {}


def _directory_hash(staged: Path) -> str:
    """The content hash of a staged directory, in the package's own form.

    Recomputed over the files on disk so the contract's
    ``source_checksum`` attests what a chat turn will actually execute —
    the same ``name\\0bytes`` scheme :meth:`SkillPackage.hash_files`
    uses, which is what makes the two comparable.
    """
    files = {
        path.relative_to(staged).as_posix(): path.read_bytes()
        for path in sorted(staged.rglob("*"))
        if path.is_file()
    }
    return SkillPackage.hash_files(files)


def _source_of(contract: SkillContract) -> str:
    """The closed-vocabulary source label for a contract's provider."""
    if contract.provider == "mcp_server":
        return SOURCE_MCP
    return SOURCE_CLAW_HUB
