"""P0.3 throwaway check: the ADR-0052 manifest parses, and overlays only tighten.

Parses the exact ``pipeline.toml`` recorded in ADR-0052 (embedded verbatim
below) with ``tomllib``, validates it against the closed contract ADR-0052
fixes (closed key set, the manifest's declarable capability names, limits
present, a parseable ``requires_runtime`` range, ``run.resume`` in its closed
value set, slot defaults that resolve), and exercises the tighten-only overlay
rule in both directions:

* a **narrowing** overlay (fewer tools, no commit, a smaller think-role set,
  a smaller token limit, a param inside its declared range) is accepted;
* every **widening** attempt is rejected at load: a capability the manifest
  does not ask for, a false capability turned true, a widened tool list, a
  widened role list, a limit above the manifest's, a param outside its
  declared range or of the wrong type, a slot the manifest never declared.

This is the throwaway proof for the P0.3 exit criterion, not the P4 loader:
the registry, CLI and ``paa pipeline fork`` are later phases. It imports
nothing from ``paa`` because the contract it checks is data, not code.

Usage (from the repo root):

    .venv/bin/python scripts/pipeline_manifest_check.py

Exit codes: 0 = the manifest parses, narrowing is accepted and every widening
attempt is rejected; 1 = a check failed.
"""

from __future__ import annotations

import sys
import tomllib
from typing import Any

MANIFEST_CAPABILITY_KEYS = frozenset(
    # The manifest's declarable capability subset of ADR-0051's facade vocabulary:
    # the seven capabilities a pipeline asks for in [capabilities]. `gate`,
    # `plan.submit`, `policy.check` and `progress` are host-side operations of
    # the facade itself (ADR-0051) and are not manifest-requestable in v1.
    {"think", "use_skill", "recall", "remember", "call_agent", "sub_run", "commit"}
)
SECTIONS = frozenset({"pipeline", "run", "capabilities", "limits", "params", "slots", "evidence"})
RESUME_VALUES = frozenset({"journal", "none"})

MANIFEST = """
[pipeline]
name        = "tdd-loop"
version     = "0.1.0"
description = "Write a failing test, make it pass, refactor."
requires_runtime = ">=4.2,<5"
protocol    = "paa-pipeline/1"

[run]
command = ["python", "-m", "tdd_loop"]
cwd     = "."
transport = "stdio"
isolation = "sandboxed"
resume = "journal"

[capabilities]
think        = { roles = ["planner", "worker"] }
use_skill    = ["read_file", "edit_file", "bash", "git_commit"]
recall       = true
remember     = false
call_agent   = ["critic"]
sub_run      = []
commit       = true

[limits]
max_tokens   = 200000
max_wall_s   = 1800
max_calls    = 400
max_sub_runs = 2

[params]
max_cycles   = { type = "int",  default = 5, min = 1, max = 20 }
run_linter   = { type = "bool", default = true }

[slots]
critic    = { interface = "critic/1",    default = "agent:critic" }
validator = { interface = "validator/1", default = "builtin:tests" }

[evidence]
declares_tier = "T2"
"""


def _parse_runtime_range(text: str) -> tuple[tuple[str, tuple[int, ...]], ...]:
    """Parse a comma-separated semver-range clause like ``">=4.2,<5"``."""
    clauses = []
    for clause in text.split(","):
        clause = clause.strip()
        for op in (">=", "<=", ">", "<", "=="):
            if clause.startswith(op):
                version = tuple(int(p) for p in clause[len(op):].split("."))
                clauses.append((op, version))
                break
        else:
            raise ValueError(f"bad requires_runtime clause: {clause!r}")
    return tuple(clauses)


def parse_manifest(text: str) -> dict[str, Any]:
    data: dict[str, Any] = tomllib.loads(text)
    unknown = set(data) - SECTIONS
    if unknown:
        raise ValueError(f"unknown manifest sections: {sorted(unknown)}")
    if data["pipeline"]["protocol"] != "paa-pipeline/1":
        raise ValueError("protocol must be paa-pipeline/1")
    _parse_runtime_range(data["pipeline"]["requires_runtime"])  # must parse, not just equal
    if data["run"].get("resume") not in RESUME_VALUES:
        raise ValueError(f"run.resume must be one of {sorted(RESUME_VALUES)}")
    if not set(data["capabilities"]) <= MANIFEST_CAPABILITY_KEYS:
        raise ValueError(
            f"unknown capabilities: {sorted(set(data['capabilities']) - MANIFEST_CAPABILITY_KEYS)}"
        )
    for key in ("max_tokens", "max_wall_s", "max_calls", "max_sub_runs"):
        if not isinstance(data["limits"].get(key), int):
            raise ValueError(f"limits.{key} missing or not an int")
    for slot, spec in data["slots"].items():  # slot defaults must resolve to a provider
        if not isinstance(spec.get("interface"), str) or not isinstance(spec.get("default"), str):
            raise ValueError(f"slot {slot}: interface and default must be strings")
        if "/" not in spec["interface"] or not spec["interface"].rsplit("/", 1)[1].isdigit():
            raise ValueError(f"slot {slot}: interface must be '<name>/<major>'")
        if not any(spec["default"].startswith(p) for p in ("agent:", "builtin:", "skill:", "out:")):
            raise ValueError(f"slot {slot}: default does not resolve to agent/builtin/skill/out")
    return data


def apply_overlay(manifest: dict[str, Any], overlay: dict[str, Any]) -> dict[str, Any]:
    """Apply an overlay under ADR-0052's tighten-only rule.

    Accepted: filling a *declared* slot, narrowing a capability, tightening a
    limit, setting a param inside its declared range. Rejected (``ValueError``
    naming the widened key): a capability the manifest never asked for, a
    ``false`` capability turned ``true``, a widened list, a limit above the
    manifest's, a param out of range, an undeclared slot.
    """
    merged: dict[str, Any] = {
        "capabilities": dict(manifest["capabilities"]),
        "limits": dict(manifest["limits"]),
        "params": dict(manifest["params"]),
        "slots": dict(manifest["slots"]),
    }
    for key, value in overlay.get("capabilities", {}).items():
        if key not in manifest["capabilities"]:
            raise ValueError(f"widened capability: {key} (not declared by the manifest)")
        old = manifest["capabilities"][key]
        if isinstance(old, bool) or isinstance(value, bool):
            if value is True and old is not True:
                raise ValueError(f"widened capability: {key} false->true")
            merged["capabilities"][key] = value if value is not None else old
        elif isinstance(old, list) or isinstance(value, list):
            if not set(value) <= set(old):
                raise ValueError(f"widened capability: {key} {sorted(set(value) - set(old))}")
            merged["capabilities"][key] = list(value)
        else:  # think-style role mapping: any new sub-key or wider list is a widening
            for sub, new in value.items():
                if sub not in old:
                    raise ValueError(f"widened capability: {key}.{sub} (undeclared)")
                prior = old[sub]
                if isinstance(new, list) and isinstance(prior, list):
                    if not set(new) <= set(prior):
                        raise ValueError(
                            f"widened capability: {key}.{sub} {sorted(set(new) - set(prior))}"
                        )
                elif new != prior:
                    raise ValueError(f"widened capability: {key}.{sub} {new!r} != {prior!r}")
            merged["capabilities"][key] = dict(value)
    for key, value in overlay.get("limits", {}).items():
        if key not in manifest["limits"]:
            raise ValueError(f"widened limit: {key} (undeclared)")
        if value > manifest["limits"][key]:
            raise ValueError(f"widened limit: {key} {value} > {manifest['limits'][key]}")
        merged["limits"][key] = value
    for key, value in overlay.get("params", {}).items():
        spec = manifest["params"].get(key)
        if not isinstance(spec, dict):
            raise ValueError(f"widened param: {key} (undeclared)")
        expected = {"int": int, "bool": bool}.get(spec["type"])
        if expected is not None and not isinstance(value, expected) or isinstance(value, bool) != (
            spec["type"] == "bool"
        ):
            raise ValueError(f"widened param: {key} {value!r} is not a {spec['type']}")
        if spec["type"] == "int" and not spec["min"] <= value <= spec["max"]:
            raise ValueError(f"widened param: {key} {value} outside [{spec['min']}, {spec['max']}]")
        merged["params"][key] = value
    for key, value in overlay.get("slots", {}).items():
        if key not in manifest["slots"]:
            raise ValueError(f"widened slot: {key} (undeclared)")
        if value["interface"] != manifest["slots"][key]["interface"]:
            raise ValueError(f"widened slot: {key} interface mismatch")
        merged["slots"][key] = dict(value)
    return merged


def main() -> int:
    checks: list[tuple[str, bool]] = []
    try:
        manifest = parse_manifest(MANIFEST)
        checks.append(("ADR-0052 manifest parses with tomllib", True))
        checks.append(
            ("capability keys are the manifest's declarable subset",
             set(manifest["capabilities"]) <= MANIFEST_CAPABILITY_KEYS)
        )
        checks.append(
            ("run.resume declares the resumption model", manifest["run"]["resume"] == "journal")
        )
    except ValueError as exc:
        checks.append((f"ADR-0052 manifest parses with tomllib ({exc})", False))
        print("\n".join(f"{'FAIL'}  {name}" for name, _ in checks))
        return 1

    narrowing = {
        "capabilities": {"use_skill": ["read_file", "edit_file"], "commit": False, "remember": False,
                          "think": {"roles": ["planner"]}},
        "limits": {"max_tokens": 100000, "max_calls": 200},
        "params": {"max_cycles": 3, "run_linter": False},
        "slots": {"critic": {"interface": "critic/1", "default": "agent:my-critic"}},
    }
    try:
        merged = apply_overlay(manifest, narrowing)
        checks.append(("narrowing overlay accepted",
                       merged["limits"]["max_tokens"] == 100000
                       and merged["capabilities"]["commit"] is False))
    except ValueError as exc:
        checks.append((f"narrowing overlay accepted ({exc})", False))

    widenings = [
        {"capabilities": {"remember": True}},                       # false -> true
        {"capabilities": {"use_skill": ["read_file", "bash", "net"]}},  # new tool
        {"capabilities": {"think": {"roles": ["planner", "root"]}}},  # new role
        {"capabilities": {"gate": True}},                            # not manifest-requestable
        {"limits": {"max_tokens": 999999}},                          # above the manifest's
        {"params": {"max_cycles": 99}},                              # outside [1, 20]
        {"params": {"run_linter": "yes-please"}},                   # not a bool
        {"params": {"max_cycles": 5.5}},                             # not an int
        {"slots": {"memory_policy": {"interface": "memory/1"}}},    # undeclared slot
    ]
    for overlay in widenings:
        key = next(iter(next(iter(overlay.values()))))
        try:
            apply_overlay(manifest, overlay)
            checks.append((f"widening rejected: {key}", False))
        except ValueError as exc:
            checks.append((f"widening rejected: {key}", f"widened" in str(exc)))

    for name, ok in checks:
        print(f"{'PASS' if ok else 'FAIL'}  {name}")
    return 0 if all(ok for _, ok in checks) else 1


if __name__ == "__main__":
    sys.exit(main())
