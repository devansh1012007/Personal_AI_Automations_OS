"""The adapter registration surface — RFC §8's modalities, open to all comers.

The adapter layer used to be closed by construction: the package's
``__init__`` imported the three shipped adapters by name and every dispatch
site constructed them by class, so a fourth modality needed a pull request
against the host. This module is the open seam: a name → factory table with
one registration door, plus an ``importlib.metadata`` entry-point scan so a
third-party distribution can join without touching host code at all.

Factories, not instances
------------------------
A registration holds a *factory* — the adapter class itself, or any callable
that builds one — never a ready-made instance, because the modalities'
constructors genuinely differ:
:class:`~paa.skills.adapters.claw_hub.ClawHubAdapter` takes the skills root
it reads, :class:`~paa.skills.adapters.mcp.McpServerAdapter` takes its
server command and policy binding, and
:class:`~paa.skills.adapters.native.NativeAdapter` takes nothing. A registry
that wanted one uniform signature would have to invent a config object every
adapter then unpacked — a second, weaker contract beside the ones the
adapters already carry. So the *name* is the registry's business and the
*construction* stays the caller's: each call site passes the config its
modality needs and the registry never guesses arguments.

Duplicate registration refuses loudly
-------------------------------------
:func:`register_adapter` never overrides an existing name. An adapter is the
thing a dispatch hands a sandbox and a secret proxy to; a silent override
would let whatever registered last shadow the contained path without a
trace, and "behaves differently because an unrelated package was imported"
is not a failure mode this runtime wants to debug. (Contrast
:func:`paa.pipelines.slots.register_context_provider`, where the operator's
later word wins — a context provider packs a prompt, an adapter runs code.)
The builtins register first, on first use, so their names are reserved by
precedent rather than by special case: a plugin that reaches for the
``claw_hub`` name is told no, a plugin with its own name is welcome.

Lazy, once
----------
Nothing here runs at import time — import-time side effects are against the
house style. The three builtin adapters enter through the same
:func:`register_adapter` door a plugin uses, the first time anything touches
the registry, and the entry-point scan of the default group runs at most
once per process, on the first miss: a host that only ever dispatches
builtin kinds never pays the ``importlib.metadata`` walk at all.

One broken plugin must not take the host down
---------------------------------------------
Every entry point is loaded inside its own ``try``: an import error, a value
that does not resolve to a callable, a name another plugin already took —
each is logged as a structlog warning naming the entry and skipped, and the
rest of the group still registers. The scan itself (the
``importlib.metadata`` call) is *not* swallowed: a broken plugin is the
plugin's problem, a broken installation is worth raising.

Names are the contract's provider vocabulary
--------------------------------------------
The registration keys are the ``SkillContract.provider`` values
(``claw_hub``, ``mcp_server``, ``native`` — see
:data:`paa.skills.contracts.SkillContract.provider`), because the
kind-driven dispatch sites resolve ``adapter_for(contract.provider)``. A
name nobody's contracts can carry is still registrable — the table is
name → factory, and who asks for a name is the caller's business.
"""

from __future__ import annotations

import importlib.metadata
from collections.abc import Callable
from typing import TypeAlias

import structlog

from paa.core.errors import PaaError
from paa.skills.adapters.base import SkillAdapter
from paa.skills.adapters.claw_hub import ClawHubAdapter
from paa.skills.adapters.mcp import McpServerAdapter
from paa.skills.adapters.native import NativeAdapter

__all__ = [
    "ADAPTER_ENTRY_POINT_GROUP",
    "AdapterFactory",
    "adapter_for",
    "discover_entry_point_adapters",
    "register_adapter",
]

log = structlog.get_logger(__name__)

#: The ``importlib.metadata`` group a third-party distribution advertises its
#: adapter factory under — the joining instruction in three lines of TOML::
#:
#:     [project.entry-points."paa.skill_adapters"]
#:     acme = "acme_skills.adapter:factory"
#:
#: The value is a dotted import path that resolves to a callable factory,
#: the same shape :func:`register_adapter` takes in-process.
ADAPTER_ENTRY_POINT_GROUP = "paa.skill_adapters"

#: What a registration holds: a callable that builds an adapter. Deliberately
#: not ``() -> SkillAdapter`` — see the module docstring: the modalities'
#: constructors differ, so the factory *is* the class (or an equivalent
#: callable) and each call site supplies the config its modality needs.
AdapterFactory: TypeAlias = Callable[..., SkillAdapter]

#: The table itself. Module-level (not class-level): one registry per
#: process is the point — the builtins and the plugins share it.
_FACTORIES: dict[str, AdapterFactory] = {}

_seeded_builtins = False
_default_group_scanned = False


def register_adapter(name: str, factory: AdapterFactory) -> None:
    """Register ``factory`` under ``name`` — explicit, in-process, loud.

    Duplicate registration of a name that is already taken refuses with a
    :class:`~paa.core.errors.PaaError` naming both the name and the factory
    that holds it; it never silently overrides, for the reason the module
    docstring states: an adapter is dispatched code, not a preference. The
    first registration of a builtin name is the builtins' own, made lazily
    on first use — a plugin reaching for ``"claw_hub"`` therefore meets the
    refusal, not a shadowed host adapter.
    """
    _ensure_builtin_adapters()
    if not name:
        raise PaaError("a skill adapter name must not be empty", name=name)
    existing = _FACTORIES.get(name)
    if existing is not None:
        raise PaaError(
            f"skill adapter name is already registered: {name!r}",
            name=name,
            registered_to=_factory_name(existing),
        )
    _FACTORIES[name] = factory


def adapter_for(name: str) -> AdapterFactory:
    """The factory registered under ``name`` — the kind-driven dispatch door.

    Unknown names refuse with a :class:`~paa.core.errors.PaaError` naming
    the registered adapters, the same shape
    :func:`paa.pipelines.slots.resolve_context_provider` gives its refs. A
    miss against the builtins triggers the one lazy entry-point scan of the
    default group (see the module docstring) before the refusal is raised,
    so a plugin distribution is reachable without anyone remembering to
    warm it — and a name that is genuinely unknown still refuses loudly.
    """
    _ensure_builtin_adapters()
    factory = _FACTORIES.get(name)
    if factory is None and not _default_group_scanned:
        discover_entry_point_adapters()
        factory = _FACTORIES.get(name)
    if factory is None:
        known = ", ".join(sorted(_FACTORIES)) or "(none)"
        raise PaaError(
            f"unknown skill adapter name {name!r}; registered: {known}",
            name=name,
        )
    return factory


def discover_entry_point_adapters(
    group: str = ADAPTER_ENTRY_POINT_GROUP,
) -> list[str]:
    """Register every adapter factory advertised in ``group``; return the names.

    Each entry point's value must resolve to a callable factory — the same
    shape :func:`register_adapter` takes — and registers under the entry
    point's own name. Discovery failures of **one** plugin are caught,
    logged as a structlog warning, and skipped: a broken third-party import
    must not take the host down, and neither must a plugin that reaches for
    a name another entry (or a builtin) already holds. Entries are visited
    sorted by name, so two passes over the same environment register in the
    same order and a duplicate is refused by whichever entry sorts second,
    not by whichever the filesystem happened to yield first.

    What discovery validates is that the value *resolves to a callable*.
    Whether the adapter it builds honours :class:`~paa.skills.adapters.base.SkillAdapter`
    is proven by use — a factory's signature is per-modality by design, so
    there is no argument list discovery could probe it with; the honest
    limit is stated here rather than papered over with an isinstance that
    would only check the product of a call nobody can make.
    """
    global _default_group_scanned
    _ensure_builtin_adapters()
    registered: list[str] = []
    entries = sorted(importlib.metadata.entry_points(group=group), key=lambda e: e.name)
    for entry in entries:
        try:
            loaded = entry.load()
        except Exception as exc:  # a plugin's import may fail in any way at all
            log.warning(
                "skills.adapters.entry_point_skipped", entry=entry.name, error=str(exc)
            )
            continue
        if not callable(loaded):
            log.warning(
                "skills.adapters.entry_point_skipped",
                entry=entry.name,
                error="entry point did not resolve to a callable factory",
            )
            continue
        try:
            register_adapter(entry.name, loaded)
        except PaaError as exc:
            # One plugin's refusal (a taken name, an empty name) is that
            # plugin's problem: named in the log, skipped, never fatal.
            log.warning(
                "skills.adapters.entry_point_skipped", entry=entry.name, error=str(exc)
            )
            continue
        registered.append(entry.name)
    if group == ADAPTER_ENTRY_POINT_GROUP:
        # Only a scan of the default group satisfies the lazy-once miss in
        # adapter_for; a caller probing a custom group has not warmed it.
        _default_group_scanned = True
    return registered


def _ensure_builtin_adapters() -> None:
    """Register the three shipped adapters — once, lazily, through the front door.

    The flag is set *before* registering because :func:`register_adapter`
    re-enters here; with it set, the re-entry is a no-op and the builtins
    land as plain registrations, not a special case. Should a builtin name
    already be taken at this point — only reachable by poking the private
    table — the duplicate refusal fires and says so, which is the loudest
    honest option available.
    """
    global _seeded_builtins
    if _seeded_builtins:
        return
    _seeded_builtins = True
    register_adapter("claw_hub", ClawHubAdapter)
    register_adapter("mcp_server", McpServerAdapter)
    register_adapter("native", NativeAdapter)


def _factory_name(factory: AdapterFactory) -> str:
    """The most identifying name a factory can offer, for refusal messages."""
    return getattr(factory, "__qualname__", None) or repr(factory)
