"""The Loop Console service — loop management as a view, not an authority (C8.5).

SPEC 2 Wave 8's console row, stated as a seam: a TUI surface for *managing
loops* — the registry panel (trust, tier, version), install/activate/fork,
the conformance run, evidence-tier badges — "a TUI/agent specifically for
this type of stuff" per owner direction. This module is the textual-free
half (the :mod:`paa.tui.chat.wiring` shape): every read and every action
delegates to a surface that already exists and already decides, so the
console can only ever be a thinner or thicker *view* of the same rules:

* **The registry panel** reads :class:`~paa.pipelines.registry.PipelineRegistry`
  — the one place that answers "which pipelines exist". Rows come from
  ``registry.list()``; nothing here re-states a name, a trust level or a
  version, so the panel cannot disagree with ``paa pipeline list``.
* **Install / activate** drive
  :class:`~paa.chat.extensions.ChatExtensionSurface` — the C8.4 flow:
  discovery through the Claw-Hub adapter, install through ALL five gates
  (the smoke gate really stages and runs the entrypoint under the
  sandbox), activation against the staged bytes. A gate's refusal is
  returned as a refusal and rendered as one; the console never retries
  around it, weakens it, or invents a second path beside it.
* **Fork** is pinned to the two mechanisms the kit already offers, one per
  kind of loop:

  * a **skill fork** copies the gated staged bytes into the personal
    skills dir (``PAA_HOME/skills/personal/<name>`` — the operator's
    drop zone, discovered by the same adapter and installed only through
    the same five gates; the fork itself installs nothing);
  * a **pipeline fork** writes a **personal declaration overlay** seeded
    from the pipeline's own declaration — every value restates what the
    declaration already says, so the seed sits inside
    :func:`paa.pipelines.overlay.load_overlay`'s tighten-only envelope by
    construction, and any widening edit the operator makes is refused at
    load by the existing loader, not by this console.

  Both were chosen over a local-manifest copy deliberately: the manifest
  contract requires all four ``[limits]`` as concrete positive ints
  (``registry.py::_LIMIT_KEYS``), and five of the six built-ins leave
  their limits unbounded — a manifest fork would have to invent numbers
  this surface has no authority to choose.
* **The conformance run** drives the six-check runner
  (:mod:`paa.pipelines.conformance`) for the chosen pipeline on a
  **throwaway ledger** — the same shape the conformance suite runs on: a
  scratch database under a scratch directory, closed afterwards. The
  scripted check runs are diagnostics, not the operator's work, and the
  operator's ledger keeps only that work. A pipeline without a runner
  gets the RED report naming the gap — the honesty rule
  :func:`~paa.pipelines.conformance.every_builtin_conformance` applies to
  builtins — never a faked green.

No new event type, no new gate, nothing model-decided: mutations ride the
extension surface's own ``EXTENSION_*`` lineage, and every refusal this
module can produce names what was refused and why.
"""

from __future__ import annotations

import re
import shutil
import tempfile
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Final

from paa.core.errors import PaaError
from paa.core.types import PermissionMode
from paa.ledger.store import LedgerStore
from paa.pipelines.registry import PipelineRegistry, RegisteredPipeline
from paa.storage.relational.base import RelationalBackend

if TYPE_CHECKING:
    from paa.chat.extensions import (
        Activation,
        ChatExtensionSurface,
        DiscoveredSkill,
        ExtensionInstallReport,
    )
    from paa.pipelines.conformance import ConformanceReport
    from paa.sandbox.base import Sandbox

__all__ = [
    "ConsoleService",
    "PipelineForkRow",
    "SkillPanelRow",
    "render_fork_overlay",
]

#: Where pipeline forks live, relative to ``PAA_HOME``: one directory per
#: source pipeline, one overlay file per fork. The directory name *is* the
#: provenance — an overlay file may not carry metadata sections (the
#: loader's section set is closed), so the layout carries them instead.
FORKS_DIR_NAME = "overlays"

#: The word that selects the personal skills dir in the console's install
#: flow — the same directory ``paa skills install --personal`` reads.
PERSONAL_SOURCE_WORD = "personal"


# ---------------------------------------------------------------------------
# The conformance runner map (name → runner, normalized to one signature)
# ---------------------------------------------------------------------------


async def _run_builtin(ledger: LedgerStore, scratch: Path) -> ConformanceReport:
    from paa.pipelines.conformance import builtin_conformance

    return await builtin_conformance(ledger)


async def _run_chat(ledger: LedgerStore, scratch: Path) -> ConformanceReport:
    from paa.pipelines.conformance import chat_conformance

    return await chat_conformance(scratch)


async def _run_coder(ledger: LedgerStore, scratch: Path) -> ConformanceReport:
    from paa.pipelines.conformance import coder_conformance

    return await coder_conformance(ledger, scratch)


async def _run_observe(ledger: LedgerStore, scratch: Path) -> ConformanceReport:
    from paa.pipelines.conformance import observe_conformance

    return await observe_conformance(ledger, scratch)


async def _run_manager(ledger: LedgerStore, scratch: Path) -> ConformanceReport:
    from paa.pipelines.conformance import manager_conformance

    return await manager_conformance(ledger)


async def _run_improve(ledger: LedgerStore, scratch: Path) -> ConformanceReport:
    from paa.pipelines.conformance import improve_conformance

    return await improve_conformance(ledger, scratch)


async def _run_mcp_host(ledger: LedgerStore, scratch: Path) -> ConformanceReport:
    from paa.pipelines.conformance import mcp_host_conformance

    return await mcp_host_conformance(scratch)


#: The registered in-process pipelines this console can run conformance
#: for, mapped onto the runners :mod:`paa.pipelines.conformance` already
#: defines (the same set ``every_builtin_conformance`` drives, held in one
#: place here so a pipeline added without a runner reads RED, not missing).
#: A test pins the map against the registry's runnable set, the same lock
#: the CI entry applies to itself.
_CONFORMANCE_RUNNERS: Final[
    dict[str, Callable[[LedgerStore, Path], Awaitable[ConformanceReport]]]
] = {
    "builtin.plan-execute-critique": _run_builtin,
    "builtin.chat": _run_chat,
    "coder.tool-loop": _run_coder,
    "builtin.observe": _run_observe,
    "builtin.manager": _run_manager,
    "improve.reflection-loop": _run_improve,
    "builtin.mcp-host": _run_mcp_host,
}


def _no_runner_report(pipeline: str) -> ConformanceReport:
    """The RED report a pipeline without a conformance runner earns.

    Same wording, same six failed checks, as the gap row
    :func:`~paa.pipelines.conformance.every_builtin_conformance` appends
    for a builtin nobody wrote a runner for — the standard's own honesty
    rule, applied to every name this console can select.
    """
    from paa.pipelines.conformance import CHECK_NAMES, ConformanceCheck, ConformanceReport

    return ConformanceReport(
        pipeline=pipeline,
        checks=tuple(
            ConformanceCheck.failed(
                check, "no conformance runner registered for this pipeline"
            )
            for check in CHECK_NAMES
        ),
    )


# ---------------------------------------------------------------------------
# Panel rows
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class SkillPanelRow:
    """One installed chat extension, merged from its two sources of truth.

    The marketplace package row is the five-gate record (name, version,
    signature state); the skills-registry row is what a chat turn can
    actually dispatch (provider, active). A ``--no-activate`` install has
    the first and not the second; an MCP activation has the second and
    not the first. ``signature`` reads ``—`` when no package row vouches
    for the bytes (the MCP case: no file package was ever gated).
    """

    name: str
    version: str
    provider: str
    signature: str
    active: bool


@dataclass(frozen=True, slots=True)
class PipelineForkRow:
    """One personal pipeline fork (an overlay file), with its load verdict.

    ``note`` is ``ok`` when the file still loads against the source
    pipeline's declaration, ``invalid (<reason>)`` when a widening edit
    has since been refused by the loader, and ``source no longer
    registered`` when the pipeline it forked is gone. The listing refuses
    to present a broken fork as a healthy one.
    """

    source: str
    name: str
    path: Path
    note: str


# ---------------------------------------------------------------------------
# The fork-overlay seed (pure function, round-trip tested)
# ---------------------------------------------------------------------------


def _toml_str(value: str) -> str:
    """One basic TOML string, escaped for the closed value vocabulary."""
    out = value.replace("\\", "\\\\").replace('"', '\\"')
    out = out.replace("\n", "\\n").replace("\r", "\\r").replace("\t", "\\t")
    return f'"{out}"'


_BARE_KEY = re.compile(r"[A-Za-z0-9_-]+\Z")


def _toml_key(key: str) -> str:
    """One TOML key: bare when it may be, a quoted string when it must be.

    The manifest contract does not bound key shapes, so a local
    declaration may legally carry a quoted key (``\"foo bar\" = 3``); a
    seed that wrote it bare would emit TOML the loader refuses — a fork
    born broken. Anything outside the bare-key vocabulary is quoted.
    """
    return key if _BARE_KEY.match(key) else _toml_str(key)


def _toml_value(value: object) -> str:
    """Render one seedable value: int, bool, str, list of str, or roles table.

    Anything else is refused — the seed only restates what a declaration
    can carry, and a value the renderer cannot express is a bug in the
    declaration vocabulary, not something to approximate.
    """
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, str):
        return _toml_str(value)
    if isinstance(value, (list, tuple)):
        if not all(isinstance(item, str) for item in value):
            raise PaaError(
                "fork overlay seed met a non-string list item it cannot render"
            )
        return "[" + ", ".join(_toml_str(item) for item in value) + "]"
    if isinstance(value, dict):
        parts = [
            f"{_toml_key(key)} = {_toml_value(item)}"
            for key, item in value.items()
            if isinstance(key, str)
        ]
        return "{ " + ", ".join(parts) + " }"
    raise PaaError(
        f"fork overlay seed met a {type(value).__name__} it cannot render"
    )


def render_fork_overlay(declaration: RegisteredPipeline) -> str:
    """The seeded personal overlay for one pipeline's declaration.

    Every value restates the declaration's own — params at their declared
    defaults, slots at their declared refs, capabilities exactly as asked,
    limits only where the declaration bounds them — so the file
    :func:`paa.pipelines.overlay.load_overlay` validates against the same
    declaration loads it unchanged (the round-trip the tests pin). From
    there the operator edits *down* only: the loader refuses any widening
    at load, which is what makes an editable personal copy safe to hand
    out from a console.
    """
    d = declaration.declaration
    lines = [
        f"# Loop Console fork of {d.name} {d.version} (trust: {declaration.trust})",
        "# Every value restates the pipeline's own declaration. Edit from here:",
        "# the overlay loader refuses any widening at load (pipelines/overlay.py).",
    ]
    if d.params:
        lines.append("")
        lines.append("[params]")
        for key, spec in d.params.items():
            if spec.default is None:
                # A param with no declared default cannot be restated; the
                # fork simply leaves it unset, exactly as the declaration
                # leaves it. Never invented here.
                continue
            lines.append(f"{_toml_key(key)} = {_toml_value(spec.default)}")
    if d.slots:
        lines.append("")
        lines.append("[slots]")
        for key, slot in d.slots.items():
            lines.append(f"{_toml_key(key)} = {_toml_value(slot.default)}")
    if d.capabilities:
        lines.append("")
        lines.append("[capabilities]")
        for key, value in d.capabilities.items():
            lines.append(f"{_toml_key(key)} = {_toml_value(value)}")
    concrete_limits = {
        key: getattr(d.limits, key)
        for key in ("max_tokens", "max_wall_s", "max_calls", "max_sub_runs")
        if getattr(d.limits, key) is not None
    }
    lines.append("")
    lines.append("[limits]")
    if concrete_limits:
        for key, value in concrete_limits.items():
            lines.append(f"{key} = {value}")
    else:
        lines.append(
            "# omitted keys stay unbounded — a fork may only tighten (add"
            " bounds), never widen"
        )
    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------------------
# The service
# ---------------------------------------------------------------------------


class ConsoleService:
    """Every read and action the Loop Console screen needs, over real surfaces.

    Constructed from the pieces the chat wiring already holds (ledger,
    relational backend, ``PAA_HOME``, permission mode, and the runtime's
    sandbox when there is one) — the service owns no state beyond those
    references and a lazily built console for the screens to read. It
    introduces no gate and no event type; the mutating actions construct
    the C8.4 :class:`~paa.chat.extensions.ChatExtensionSurface` per call
    with the operator's own opt-in, so what runs is exactly what
    ``paa skills`` runs.
    """

    def __init__(
        self,
        *,
        ledger: LedgerStore,
        db: RelationalBackend,
        home: Path | None,
        permission_mode: PermissionMode,
        sandbox: Sandbox | None = None,
    ) -> None:
        self._ledger = ledger
        self._db = db
        self._home = home
        self._permission_mode = permission_mode
        self._sandbox = sandbox

    # -- the registry panel --------------------------------------------------

    def pipeline_registry(self) -> PipelineRegistry:
        """A fresh registry — the same one-shot rule the CLI doors apply.

        The local directory can change under a long-lived console, and a
        panel rendered from a stale registry is a panel the operator
        never asked for. Built per call, like
        :meth:`ChatExtensionSurface._pipeline_registry` and every
        ``paa pipeline`` verb.
        """
        local_dir = self._home / "pipelines" if self._home is not None else None
        return PipelineRegistry(local_dir=local_dir)

    def pipeline_rows(self) -> list[RegisteredPipeline]:
        """Every registered pipeline — the registry's own rows, unfiltered."""
        return self.pipeline_registry().list()

    def local_manifest_errors(self) -> dict[str, str]:
        """Off-contract local manifests, named — the registry's own report."""
        return self.pipeline_registry().local_errors

    async def skill_rows(self) -> list[SkillPanelRow]:
        """The installed chat extensions: packages merged with registry rows."""
        from paa.skills.registry import SkillRegistry

        packages = await self._db.fetch_all(
            "SELECT package_name, version, publisher, signature_state "
            "FROM marketplace_packages"
        )
        contracts = await SkillRegistry(self._db).list_active()
        by_contract = {(c.skill_name, c.version): c for c in contracts}
        rows: dict[tuple[str, str], SkillPanelRow] = {}
        for row in packages:
            name, version = str(row["package_name"]), str(row["version"])
            contract = by_contract.get((name, version))
            rows[(name, version)] = SkillPanelRow(
                name=name,
                version=version,
                provider=contract.provider if contract else str(row["publisher"]),
                signature=str(row["signature_state"]),
                active=contract is not None,
            )
        for (name, version), contract in by_contract.items():
            # Activated without a package row (the MCP door): listed with
            # an honest "no gated bytes vouch for this one" signature.
            if (name, version) not in rows:
                rows[(name, version)] = SkillPanelRow(
                    name=name,
                    version=version,
                    provider=contract.provider,
                    signature="—",
                    active=True,
                )
        return [rows[key] for key in sorted(rows)]

    def fork_rows(self) -> list[PipelineForkRow]:
        """The personal pipeline forks, each with its load verdict."""
        if self._home is None:
            return []
        from paa.pipelines.overlay import OverlayError, load_overlay

        registry = self.pipeline_registry()
        root = self._home / "pipelines" / FORKS_DIR_NAME
        if not root.is_dir():
            return []
        out: list[PipelineForkRow] = []
        for source_dir in sorted(root.iterdir()):
            if not source_dir.is_dir():
                continue
            for path in sorted(source_dir.glob("*.toml")):
                source = source_dir.name
                row = registry.get(source)
                if row is None:
                    out.append(
                        PipelineForkRow(
                            source=source,
                            name=path.stem,
                            path=path,
                            note="source no longer registered",
                        )
                    )
                    continue
                try:
                    load_overlay(path, row.declaration)
                    note = "ok"
                except OverlayError as exc:
                    note = f"invalid ({exc.message})"
                out.append(
                    PipelineForkRow(source=source, name=path.stem, path=path, note=note)
                )
        return out

    # -- discovery, install, activate (the C8.4 surface, unmodified) --------

    def _require_home(self) -> Path:
        """Refuse a home-needing action when ``PAA_HOME`` is unset."""
        if self._home is None:
            raise PaaError(
                "this action needs PAA_HOME — staging, forking and the local "
                "pipeline directory all live under it"
            )
        return self._home

    def _surface(self, *, allow_unverified: bool) -> ChatExtensionSurface:
        """The production extension surface, built per action.

        The ``allow_unverified`` opt-in is the operator's word, never this
        console's default: the screen passes ``True`` only after the
        install confirmation names it, the same opt-in
        ``paa skills install --allow-unverified`` asks for.
        """
        from paa.chat.extensions import ChatExtensionSurface
        from paa.validation.ast_scanner import AstSecurityScanner

        return ChatExtensionSurface(
            ledger=self._ledger,
            db=self._db,
            home=self._home,
            sandbox=self._sandbox,
            scanner=AstSecurityScanner(),
            permission_mode=self._permission_mode,
            allow_unverified=allow_unverified,
        )

    async def discover(
        self, source: Path | str | None, *, personal: bool = False
    ) -> list[DiscoveredSkill]:
        """Enumerate a source's skills — read-only, through the C8.4 adapter."""
        self._require_home()
        return await self._surface(allow_unverified=False).discover(
            source, personal=personal
        )

    async def install(
        self,
        skill: DiscoveredSkill,
        *,
        allow_unverified: bool,
        activate: bool = True,
    ) -> ExtensionInstallReport:
        """Install one discovered skill through all five gates, then activate.

        Delegates whole to the extension surface — the gates, the staging,
        the ``EXTENSION_*`` events and the zero-trace refusal are all its
        own behaviour, and a refusal is returned to the caller as a
        refusal to render, never retried around.
        """
        self._require_home()
        return await self._surface(allow_unverified=allow_unverified).install(
            skill, activate=activate
        )

    async def activate(self, skill_name: str) -> Activation:
        """(Re-)activate an installed skill — the surface's own two cases."""
        self._require_home()
        return await self._surface(allow_unverified=False).activate(skill_name)

    # -- fork ----------------------------------------------------------------

    def fork_skill(self, skill_name: str) -> Path:
        """Copy a skill's gated staged bytes into the personal skills dir.

        The pinned skill-fork mechanism: the personal dir is the
        operator-authored drop zone, so the copy lands there and nowhere
        else — nothing is registered, staged or activated by the fork
        itself. Installing the copy goes back through discovery and all
        five gates, exactly as a hand-written personal skill would.

        Refuses when the skill has no staged bytes (nothing to fork) or
        when the personal dir already holds that name (the operator's
        edits there are not this console's to overwrite).
        """
        home = self._require_home()
        from paa.chat.extensions import INSTALLED_DIR_NAME, PERSONAL_DIR_NAME

        name_root = home / "skills" / INSTALLED_DIR_NAME / skill_name
        versions = (
            [child for child in name_root.iterdir() if child.is_dir()]
            if name_root.is_dir()
            else []
        )
        if not versions:
            raise PaaError(
                f"no staged bytes for {skill_name!r} — fork copies what the "
                "five gates installed; install it first"
            )
        # Newest wins, the same reading ChatExtensionSurface's staged
        # activation applies to the same layout.
        staged = max(versions, key=lambda path: path.name)
        target = home / "skills" / PERSONAL_DIR_NAME / skill_name
        if target.exists():
            raise PaaError(
                f"the personal skills dir already holds {skill_name!r} at "
                f"{target} — edit that copy, or fork under a new name"
            )
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copytree(staged, target)
        return target

    def fork_pipeline(self, name: str, fork_name: str) -> Path:
        """Seed a personal declaration overlay for one registered pipeline.

        The pinned pipeline-fork mechanism (see the module docstring for
        why not a local manifest). The fork name becomes the file name
        under ``PAA_HOME/pipelines/overlays/<pipeline>/``; the seed is
        :func:`render_fork_overlay`'s restatement of the declaration, and
        the write is refused when the name is taken, empty, shaped like a
        path, or already registered as a pipeline (a fork that shadowed
        its source would be unreachable behind it in the registry).
        """
        home = self._require_home()
        registry = self.pipeline_registry()
        row = registry.get(name)
        if row is None:
            known = ", ".join(sorted(r.name for r in registry.list())) or "(none)"
            raise PaaError(
                f"no registered pipeline named {name!r}; registered: {known}"
            )
        cleaned = fork_name.strip()
        if not cleaned or cleaned in {".", ".."} or "/" in cleaned or "\\" in cleaned:
            raise PaaError(
                "a fork name must be a non-empty file name (no path separators)"
            )
        if cleaned == name:
            raise PaaError("a fork needs a new name — it is a copy beside its source")
        if registry.get(cleaned) is not None:
            raise PaaError(
                f"{cleaned!r} is already a registered pipeline; a fork that "
                "shadowed its registry row would be unreachable"
            )
        target = home / "pipelines" / FORKS_DIR_NAME / name / f"{cleaned}.toml"
        if target.exists():
            raise PaaError(
                f"a fork named {cleaned!r} of {name!r} already exists at "
                f"{target} — edit it, or pick another name"
            )
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(render_fork_overlay(row), encoding="utf-8")
        return target

    # -- conformance ----------------------------------------------------------

    async def run_conformance(
        self, pipeline_name: str, *, scratch_dir: Path | None = None
    ) -> ConformanceReport:
        """Run the six-check standard for one pipeline, on a throwaway ledger.

        The runners journal their scripted check runs; those rows are
        diagnostics, so they land on a scratch database under a scratch
        directory (``scratch_dir`` when a caller hands one over, the OS
        temp dir otherwise), closed before the report returns. The
        operator's ledger is left exactly as it was — pinned by test.
        """
        registry = self.pipeline_registry()
        if registry.get(pipeline_name) is None:
            known = ", ".join(sorted(r.name for r in registry.list())) or "(none)"
            raise PaaError(
                f"no registered pipeline named {pipeline_name!r}; registered: {known}"
            )
        runner = _CONFORMANCE_RUNNERS.get(pipeline_name)
        if runner is None:
            return _no_runner_report(pipeline_name)
        if scratch_dir is not None:
            return await self._run_named_conformance(runner, scratch_dir)
        with tempfile.TemporaryDirectory(prefix="paa-conformance-") as scratch:
            return await self._run_named_conformance(runner, Path(scratch))

    async def _run_named_conformance(
        self,
        runner: Callable[[LedgerStore, Path], Awaitable[ConformanceReport]],
        scratch_dir: Path,
    ) -> ConformanceReport:
        """One runner over one scratch database, always closed afterwards."""
        from paa.storage.relational.database import Database

        db = Database(scratch_dir / "conformance.db")
        await db.connect()
        try:
            return await runner(LedgerStore(db), scratch_dir)
        finally:
            await db.close()
