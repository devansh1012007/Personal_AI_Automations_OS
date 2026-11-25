"""The overlay loader: customise a pipeline, tighten-only, at load (C6.4 / P1.5).

ADR-0052's layer-2 rule made executable: an overlay is a small TOML file
that may set ``[params]``, fill ``[slots]``, narrow ``[capabilities]``
and tighten ``[limits]`` — and may never widen. Every widening attempt
is rejected **at load time**, with a message naming the widened key,
before any code runs: a widenable overlay is a privilege-escalation
primitive in a config file, which is why the rejection is the guarantee
and a warning is an audit trail nobody reads.

What "tighten" means, row by row (the same table the throwaway
``scripts/pipeline_manifest_check.py`` proved over the manifest; this is
the production loader over :mod:`paa.pipelines.slots` declarations):

===============  =========================================================
section          rule
===============  =========================================================
``[params]``     key declared; type matches the spec; int values inside
                 ``[min, max]``. A value outside the declared range is a
                 widening attempt.
``[slots]``      key declared; the ref's interface matches the slot's.
                 Filling a slot the pipeline never declared is a widening
                 attempt.
``[capabilities]``  narrow only: no key outside the declared ask;
                 ``false -> true`` refused; a list shorter than or equal
                 to the declared one (subset) accepted, longer refused;
                 ``think.roles`` shrinks, never grows.
``[limits]``     ``RunLimits`` keys only; a value above the declared
                 limit refused; setting a limit the declaration leaves
                 unbounded is allowed (unbounded -> bounded is the very
                 shape of tightening).
===============  =========================================================

Applying an overlay is the host's job, explicitly: the loader returns a
validated :class:`Overlay`, and the host resolves slot refs and threads
params/limits into the pipeline it builds. The loader never instantiates
anything — validation and application are separate acts on purpose, the
same separation the installer's five gates enforce for skills.

The one application helper that lives here, :func:`tightened_limits`,
exists because limit arithmetic is fiddly enough to deserve one tested
implementation.
"""

from __future__ import annotations

import tomllib
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from types import MappingProxyType
from typing import Any

from paa.pipelines.base import PipelineError, RunLimits
from paa.pipelines.slots import PipelineDeclaration, interface_for_ref

__all__ = [
    "Overlay",
    "OverlayError",
    "WideningOverlayError",
    "load_overlay",
    "tightened_limits",
]

#: The closed section set an overlay may carry (ADR-0052's "may set /
#: fill / narrow / tighten" list, nothing else).
_OVERLAY_SECTIONS = frozenset({"params", "slots", "capabilities", "limits"})

_LIMIT_KEYS = ("max_tokens", "max_wall_s", "max_calls", "max_sub_runs")


def _frozen(value: Any) -> Any:
    """Freeze a validated TOML value, including nested mappings and lists."""
    if isinstance(value, Mapping):
        return MappingProxyType({key: _frozen(item) for key, item in value.items()})
    if isinstance(value, list):
        return tuple(_frozen(item) for item in value)
    return value


class OverlayError(PipelineError):
    """An overlay cannot be loaded: malformed, unknown-shaped, unresolvable.

    Raised at load time, before any code the overlay would configure has
    run — the cheap failure ADR-0052's parse contract exists for.
    """

    wire_code = -32009


class WideningOverlayError(OverlayError):
    """An overlay tried to widen; the widened key is named in the message.

    The tighten-only rule is the vision's constraint 2 enforced by
    configuration instead of code, and a widening attempt is the same
    violation either way — refused loudly, never warned through.
    """

    wire_code = -32010


@dataclass(frozen=True, slots=True)
class Overlay:
    """A validated overlay: params set, slots filled, ask narrowed, limits tightened.

    Frozen because a validated configuration drifting after validation
    would make the load-time guarantee a one-time check; a host that
    wants to combine overlays validates the combination, it does not
    mutate a validated one.
    """

    params: Mapping[str, Any] = field(default_factory=dict)
    slots: Mapping[str, str] = field(default_factory=dict)
    capabilities: Mapping[str, Any] = field(default_factory=dict)
    limits: Mapping[str, int] = field(default_factory=dict)

    def __post_init__(self) -> None:
        for name in ("params", "slots", "capabilities", "limits"):
            object.__setattr__(self, name, _frozen(getattr(self, name)))


# ---------------------------------------------------------------------------
# Validation helpers — one section each, each naming its widened key
# ---------------------------------------------------------------------------


def _validate_params(
    section: Mapping[str, Any], declared: PipelineDeclaration
) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for key, value in section.items():
        spec = declared.params.get(key)
        if spec is None:
            raise WideningOverlayError(
                f"overlay sets param {key!r}, which the pipeline does not declare",
                key=key,
            )
        if spec.type == "int":
            if isinstance(value, bool) or not isinstance(value, int):
                raise OverlayError(
                    f"overlay param {key!r} must be an int, got {type(value).__name__}",
                    key=key,
                )
            if spec.minimum is not None and value < spec.minimum:
                raise WideningOverlayError(
                    f"overlay param {key!r} = {value} is below the declared "
                    f"minimum {spec.minimum}",
                    key=key,
                )
            if spec.maximum is not None and value > spec.maximum:
                raise WideningOverlayError(
                    f"overlay param {key!r} = {value} is above the declared "
                    f"maximum {spec.maximum}",
                    key=key,
                )
        elif spec.type == "bool":
            if not isinstance(value, bool):
                raise OverlayError(
                    f"overlay param {key!r} must be a bool, got {type(value).__name__}",
                    key=key,
                )
        else:  # str
            if not isinstance(value, str):
                raise OverlayError(
                    f"overlay param {key!r} must be a str, got {type(value).__name__}",
                    key=key,
                )
        out[key] = value
    return out


def _validate_slots(
    section: Mapping[str, Any], declared: PipelineDeclaration
) -> dict[str, str]:
    out: dict[str, str] = {}
    for key, value in section.items():
        slot = declared.slots.get(key)
        if slot is None:
            raise WideningOverlayError(
                f"overlay fills slot {key!r}, which the pipeline does not declare",
                key=key,
            )
        if not isinstance(value, str) or (value != "none" and ":" not in value):
            raise OverlayError(
                f"overlay slot {key!r} must be 'none' or a ref ('prefix:name'), "
                f"got {value!r}",
                key=key,
            )
        if value == "none":
            out[key] = value
            continue
        interface = interface_for_ref(value)
        if interface is None:
            raise OverlayError(
                f"overlay slot {key!r} ref {value!r} has no declared interface",
                key=key,
            )
        if interface != slot.interface:
            raise WideningOverlayError(
                f"overlay slot {key!r} ref {value!r} implements {interface!r}, "
                f"which does not match the declared interface {slot.interface!r}",
                key=key,
            )
        out[key] = value
    return out


def _narrow_list(
    declared_list: list[Any], overlay_value: Any, key: str
) -> list[Any]:
    """A list overlay value must be a subset of the declared list."""
    if not isinstance(overlay_value, list) or not all(
        isinstance(item, str) for item in overlay_value
    ):
        raise OverlayError(
            f"overlay capability {key!r} must be a list of names", key=key
        )
    declared_set = set(declared_list)
    added = [item for item in overlay_value if item not in declared_set]
    if added:
        raise WideningOverlayError(
            f"overlay capability {key!r} adds {added!r} the pipeline does not ask for",
            key=key,
        )
    return list(overlay_value)


def _validate_capabilities(
    section: Mapping[str, Any], declared: PipelineDeclaration
) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for key, value in section.items():
        declared_value = declared.capabilities.get(key)
        if declared_value is None:
            raise WideningOverlayError(
                f"overlay narrows capability {key!r}, which the pipeline does "
                "not ask for — the ask is the ceiling",
                key=key,
            )
        if isinstance(declared_value, bool):
            if not isinstance(value, bool):
                raise OverlayError(
                    f"overlay capability {key!r} must be a bool, got "
                    f"{type(value).__name__}",
                    key=key,
                )
            if declared_value is False and value is True:
                raise WideningOverlayError(
                    f"overlay capability {key!r} turns on a capability the "
                    "pipeline does not ask for",
                    key=key,
                )
            out[key] = value
        elif isinstance(declared_value, list):
            out[key] = _narrow_list(declared_value, value, key)
        elif isinstance(declared_value, Mapping):
            # The ``think`` shape: {"roles": [...]}; roles shrink, never grow.
            if not isinstance(value, Mapping) or set(value) - {"roles"}:
                raise OverlayError(
                    f"overlay capability {key!r} may only narrow 'roles'", key=key
                )
            declared_roles = declared_value.get("roles") or []
            roles = value.get("roles")
            out[key] = {
                "roles": _narrow_list(list(declared_roles), roles, f"{key}.roles")
            }
        else:
            raise OverlayError(
                f"overlay capability {key!r} has an unsupported declared shape",
                key=key,
            )
    return out


def _validate_limits(
    section: Mapping[str, Any], declared: PipelineDeclaration
) -> dict[str, int]:
    out: dict[str, int] = {}
    for key, value in section.items():
        if key not in _LIMIT_KEYS:
            raise OverlayError(
                f"overlay limit {key!r} is not a RunLimits key "
                f"({', '.join(_LIMIT_KEYS)})",
                key=key,
            )
        if isinstance(value, bool) or not isinstance(value, int) or value < 1:
            raise OverlayError(
                f"overlay limit {key!r} must be a positive int, got {value!r}",
                key=key,
            )
        declared_value = getattr(declared.limits, key)
        if declared_value is not None and value > declared_value:
            raise WideningOverlayError(
                f"overlay limit {key!r} = {value} is above the declared "
                f"{declared_value}",
                key=key,
            )
        out[key] = value
    return out


# ---------------------------------------------------------------------------
# The loader
# ---------------------------------------------------------------------------


def load_overlay(
    source: str | Path, declared: PipelineDeclaration
) -> Overlay:
    """Parse and validate one overlay against one pipeline's declaration.

    ``source`` is the TOML text or a path to it. The whole point is that
    this fails **before** any code the overlay would configure has run:
    an unparseable file, an unknown section, an unknown key, a widened
    anything — all refuse here, loudly, by name.
    """
    if isinstance(source, Path):
        try:
            text = source.read_text(encoding="utf-8")
        except OSError as exc:
            raise OverlayError(f"overlay file unreadable: {exc}") from exc
        except UnicodeDecodeError as exc:
            raise OverlayError(f"overlay file is not valid UTF-8: {exc}") from exc
    else:
        text = source
    try:
        parsed: dict[str, Any] = tomllib.loads(text)
    except tomllib.TOMLDecodeError as exc:
        raise OverlayError(f"overlay is not valid TOML: {exc}") from exc

    unknown_sections = set(parsed) - _OVERLAY_SECTIONS
    if unknown_sections:
        raise OverlayError(
            f"overlay carries sections {sorted(unknown_sections)!r} outside "
            f"the overlay vocabulary {sorted(_OVERLAY_SECTIONS)!r}"
        )

    def _section(name: str) -> Mapping[str, Any]:
        value = parsed.get(name, {})
        if not isinstance(value, Mapping):
            raise OverlayError(f"overlay section [{name}] must be a table")
        return value

    return Overlay(
        params=_validate_params(_section("params"), declared),
        slots=_validate_slots(_section("slots"), declared),
        capabilities=_validate_capabilities(_section("capabilities"), declared),
        limits=_validate_limits(_section("limits"), declared),
    )


def tightened_limits(base: RunLimits, overlay: Overlay) -> RunLimits:
    """Apply an overlay's limits to a base: tighten only, by construction.

    The loader already refused widening values, so this is arithmetic,
    not policing — but it is done here, once, so every host applies the
    same rule and a limit nobody tightens stays exactly what it was.
    """
    return RunLimits(
        max_tokens=overlay.limits.get("max_tokens", base.max_tokens),
        max_wall_s=overlay.limits.get("max_wall_s", base.max_wall_s),
        max_calls=overlay.limits.get("max_calls", base.max_calls),
        max_sub_runs=overlay.limits.get("max_sub_runs", base.max_sub_runs),
    )
