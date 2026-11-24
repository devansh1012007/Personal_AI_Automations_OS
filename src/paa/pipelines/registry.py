"""The pipeline registry v0 and explicit selection (C6.5 / P4-lite).

What the registry is: one place that answers "which pipelines exist,
what do they ask for, and where do they come from" without running
anything. Sources, with their trust levels:

* ``builtin`` — the three in-process pipelines the runtime ships
  (:mod:`paa.pipelines.slots` declarations carry their asks);
* ``local`` — ``PAA_HOME/pipelines/<name>/pipeline.toml``, the local
  directory of ADR-0052's distribution order. Parsed and validated
  against the manifest's closed contract at load; **listed and
  inspected, not executed** — running an out-of-process pipeline is
  P3's RPC transport, and pretending otherwise would be a trust claim
  the tree cannot defend. The row says so.

Selection (:func:`select`) is explicit and mandate-scoped, never
scored — the C6.5 rule, restating ADR-0052's supersession of the
auto-score design: explicit name → must be registered *and* inside the
mandate's pipeline scope when the mandate names one; no name and a
single-pipeline scope → that pipeline; no name and no scope → the
default (``builtin.plan-execute-critique``); no name and a multi-
pipeline scope → refused, because "pick one for me" is the auto-score
in a coat.
"""

from __future__ import annotations

import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from paa.pipelines.base import PipelineError, RunLimits
from paa.pipelines.slots import (
    BUILTIN_DECLARATIONS,
    ParamSpec,
    PipelineDeclaration,
    SlotDeclaration,
)

__all__ = [
    "DEFAULT_PIPELINE",
    "LocalManifestError",
    "PipelineRegistry",
    "PipelineSelectionError",
    "RegisteredPipeline",
    "select",
]

#: What a run gets when nothing and no mandate names a pipeline.
DEFAULT_PIPELINE = "builtin.plan-execute-critique"

#: The manifest's closed section set and the declarable capability
#: names — ADR-0052's parse contract, the same constants the throwaway
#: ``scripts/pipeline_manifest_check.py`` proved.
_MANIFEST_SECTIONS = frozenset(
    {"pipeline", "run", "capabilities", "limits", "params", "slots", "evidence"}
)
_MANIFEST_CAPABILITY_KEYS = frozenset(
    {"think", "use_skill", "recall", "remember", "call_agent", "sub_run", "commit"}
)
_RESUME_VALUES = frozenset({"journal", "none"})
_PARAM_TYPES = frozenset({"int", "bool", "str"})


class LocalManifestError(PipelineError):
    """A local manifest cannot be loaded: malformed or off-contract.

    Raised at load, before anything the manifest describes has run —
    the same cheap failure the built-in declarations and the overlay
    loader enforce.
    """

    wire_code = -32012


class PipelineSelectionError(PipelineError):
    """A selection cannot be made: unknown pipeline, or out of scope.

    The refusal names the pipeline, the scope (when one bounds the run)
    and the registered names — the operator should never have to guess
    which of the three went wrong.
    """

    wire_code = -32013


@dataclass(frozen=True, slots=True)
class RegisteredPipeline:
    """One registry row: a declaration, where it came from, its trust."""

    declaration: PipelineDeclaration
    source: str
    trust: str
    runnable: bool

    @property
    def name(self) -> str:
        return self.declaration.name

    @property
    def version(self) -> str:
        return self.declaration.version


def _param_spec(name: str, raw: Any) -> ParamSpec:
    if not isinstance(raw, dict):
        raise LocalManifestError(
            f"params entry {name!r} must be a table",
            manifest=name,
        )
    ptype = raw.get("type")
    if ptype not in _PARAM_TYPES:
        raise LocalManifestError(
            f"params entry {name!r} has type {ptype!r}; "
            f"expected one of {sorted(_PARAM_TYPES)}",
            manifest=name,
        )
    minimum = raw.get("min")
    maximum = raw.get("max")
    for bound_name, bound in (("min", minimum), ("max", maximum)):
        if bound is not None and (isinstance(bound, bool) or not isinstance(bound, int)):
            raise LocalManifestError(
                f"params entry {name!r} has a non-int {bound_name!r}",
                manifest=name,
            )
    return ParamSpec(
        type=ptype,
        default=raw.get("default"),
        minimum=minimum,
        maximum=maximum,
        description=str(raw.get("description") or ""),
    )


def _slot_declaration(name: str, raw: Any) -> SlotDeclaration:
    if not isinstance(raw, dict):
        raise LocalManifestError(f"slots entry {name!r} must be a table")
    interface = raw.get("interface")
    default = raw.get("default")
    if not isinstance(interface, str) or not interface.strip():
        raise LocalManifestError(
            f"slots entry {name!r} needs a non-empty 'interface'"
        )
    if not isinstance(default, str) or not default.strip():
        raise LocalManifestError(f"slots entry {name!r} needs a non-empty 'default'")
    return SlotDeclaration(
        name=name,
        interface=interface,
        default=default,
        description=str(raw.get("description") or ""),
    )


def declaration_from_manifest(path: Path) -> PipelineDeclaration:
    """Parse one local ``pipeline.toml`` into a declaration, or refuse.

    The closed contract: the section set, the ``[pipeline]`` identity
    keys (``name``/``version``/``requires_runtime`` required), the
    declarable capability names, ``run.resume`` in its closed set, and
    the params/slots entry shapes. An unparseable or unknown-keyed
    manifest fails here — before any code is launched, the whole value
    of declaring.
    """
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise LocalManifestError(
            f"manifest unreadable: {exc}", manifest=str(path)
        ) from exc
    try:
        parsed: dict[str, Any] = tomllib.loads(text)
    except tomllib.TOMLDecodeError as exc:
        raise LocalManifestError(
            f"manifest is not valid TOML: {exc}", manifest=str(path)
        ) from exc

    unknown = set(parsed) - _MANIFEST_SECTIONS
    if unknown:
        raise LocalManifestError(
            f"manifest carries sections {sorted(unknown)!r} outside "
            f"{sorted(_MANIFEST_SECTIONS)!r}",
            manifest=str(path),
        )

    pipeline = parsed.get("pipeline")
    if not isinstance(pipeline, dict):
        raise LocalManifestError("manifest needs a [pipeline] table")
    identity: dict[str, str] = {}
    for key in ("name", "version", "requires_runtime"):
        value = pipeline.get(key)
        if not isinstance(value, str) or not value.strip():
            raise LocalManifestError(
                f"[pipeline].{key} must be a non-empty string"
            )
        identity[key] = value

    run = parsed.get("run", {})
    if not isinstance(run, dict):
        raise LocalManifestError("[run] must be a table")
    resume = run.get("resume", "journal")
    if resume not in _RESUME_VALUES:
        raise LocalManifestError(
            f"run.resume must be one of {sorted(_RESUME_VALUES)}, got {resume!r}"
        )

    capabilities = parsed.get("capabilities", {})
    if not isinstance(capabilities, dict):
        raise LocalManifestError("[capabilities] must be a table")
    off_contract = set(capabilities) - _MANIFEST_CAPABILITY_KEYS
    if off_contract:
        raise LocalManifestError(
            f"[capabilities] carries {sorted(off_contract)!r} outside the "
            "declarable set",
        )

    limits_raw = parsed.get("limits", {})
    if not isinstance(limits_raw, dict):
        raise LocalManifestError("[limits] must be a table")
    limits_kwargs: dict[str, int] = {}
    for key, value in limits_raw.items():
        if key not in RunLimits.__dataclass_fields__:
            raise LocalManifestError(f"[limits] key {key!r} is not a RunLimits key")
        if isinstance(value, bool) or not isinstance(value, int) or value < 1:
            raise LocalManifestError(f"[limits] {key!r} must be a positive int")
        limits_kwargs[key] = value
    limits = RunLimits(**limits_kwargs)

    params_raw = parsed.get("params", {})
    if not isinstance(params_raw, dict):
        raise LocalManifestError("[params] must be a table")
    params = {key: _param_spec(key, raw) for key, raw in params_raw.items()}

    slots_raw = parsed.get("slots", {})
    if not isinstance(slots_raw, dict):
        raise LocalManifestError("[slots] must be a table")
    slots = {key: _slot_declaration(key, raw) for key, raw in slots_raw.items()}

    return PipelineDeclaration(
        name=identity["name"],
        version=identity["version"],
        requires_runtime=identity["requires_runtime"],
        description=str(pipeline.get("description") or ""),
        capabilities=dict(capabilities),
        limits=limits,
        params=params,
        slots=slots,
        trust="local",
    )


class PipelineRegistry:
    """The registered pipelines: builtins plus the local directory.

    Built once per process (or per ``refresh``): the local directory is
    read at construction, not on every lookup — a manifest that changes
    mid-process is a new registry, the same rule the settings cache and
    the model registry follow.
    """

    def __init__(self, *, local_dir: Path | None = None) -> None:
        self._local_dir = local_dir
        self._local: dict[str, RegisteredPipeline] = {}
        self._local_errors: dict[str, str] = {}
        self._scan_local()

    @classmethod
    def default(cls, *, home: Path | None = None) -> PipelineRegistry:
        """The registry the runtime uses: ``PAA_HOME/pipelines`` as local dir."""
        if home is None:
            from paa.config import get_settings

            home = get_settings().home
        return cls(local_dir=home / "pipelines")

    def _scan_local(self) -> None:
        self._local = {}
        self._local_errors = {}
        if self._local_dir is None or not self._local_dir.is_dir():
            return
        for entry in sorted(self._local_dir.iterdir()):
            if not entry.is_dir():
                continue
            manifest = entry / "pipeline.toml"
            if not manifest.is_file():
                continue
            try:
                declaration = declaration_from_manifest(manifest)
            except LocalManifestError as exc:
                # A bad manifest is named, not silently dropped: the
                # operator asked for it by creating it, and a registry
                # that hides it would be lying about what exists.
                self._local_errors[entry.name] = exc.message
                continue
            if declaration.name != entry.name:
                self._local_errors[entry.name] = (
                    f"manifest name {declaration.name!r} does not match its "
                    f"directory {entry.name!r}"
                )
                continue
            self._local[declaration.name] = RegisteredPipeline(
                declaration=declaration,
                source=str(manifest),
                trust="local",
                runnable=False,
            )

    def refresh(self) -> None:
        """Rescan the local directory (builtins never move)."""
        self._scan_local()

    def list(self) -> list[RegisteredPipeline]:
        """Every registered pipeline, builtins first, then local by name."""
        rows = [
            RegisteredPipeline(
                declaration=declaration,
                source="in-process",
                trust="builtin",
                runnable=True,
            )
            for declaration in BUILTIN_DECLARATIONS.values()
        ]
        rows.extend(self._local.values())
        return rows

    def get(self, name: str) -> RegisteredPipeline | None:
        for row in self.list():
            if row.name == name:
                return row
        return None

    @property
    def local_errors(self) -> dict[str, str]:
        """Off-contract local manifests, by directory name, with reasons."""
        return dict(self._local_errors)


def select(
    name: str | None,
    *,
    registry: PipelineRegistry,
    allowed: tuple[str, ...] | list[str] | None = None,
) -> RegisteredPipeline:
    """Explicit, mandate-scoped selection. Never scored.

    The order the plan fixes: explicit name (validated against the
    registry, then against the mandate's pipeline scope when one bounds
    the run) → a single-pipeline scope with no name → the default. A
    multi-pipeline scope with no name refuses: choosing among them is
    the operator's word, and any automatic tiebreak would be the
    auto-score this design deleted, wearing a smaller hat.
    """
    allowed_tuple = tuple(allowed) if allowed is not None else None
    if allowed_tuple is not None and not allowed_tuple:
        raise PipelineSelectionError(
            "the mandate's pipeline scope is empty — nothing may run under it",
            scope=[],
        )

    if name is not None:
        row = registry.get(name)
        if row is None:
            known = ", ".join(sorted(r.name for r in registry.list())) or "(none)"
            raise PipelineSelectionError(
                f"no registered pipeline named {name!r}; registered: {known}",
                pipeline=name,
            )
        if allowed_tuple is not None and name not in allowed_tuple:
            raise PipelineSelectionError(
                f"pipeline {name!r} is outside this mandate's scope "
                f"{list(allowed_tuple)}; the mandate lists what may run, "
                "and it is not widened here",
                pipeline=name,
                scope=list(allowed_tuple),
            )
        return row

    if allowed_tuple is not None:
        if len(allowed_tuple) == 1:
            return select(
                allowed_tuple[0], registry=registry, allowed=allowed_tuple
            )
        raise PipelineSelectionError(
            "the mandate's scope lists several pipelines "
            f"({list(allowed_tuple)}) and none was named; selection is "
            "explicit — name one",
            scope=list(allowed_tuple),
        )

    row = registry.get(DEFAULT_PIPELINE)
    if row is None:  # unreachable while the built-ins ship; kept honest anyway
        raise PipelineSelectionError(
            f"the default pipeline {DEFAULT_PIPELINE!r} is not registered",
        )
    return row
