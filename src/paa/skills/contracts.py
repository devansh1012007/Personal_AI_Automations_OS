"""Skill contracts — the declaration a capability makes before it may run.

RFC §8.1. A contract is the *only* thing the runtime trusts about a skill
before execution. Everything the security layer decides — which permissions to
demand, whether a human gate fires, what the sandbox mounts, whether the output
is believable — is read off this model. So the model validates aggressively:
a contract that parses is a contract every downstream layer may rely on.

SPEC CORRECTION (RFC §8.1 code sketch)
--------------------------------------
The RFC's ``contracts.py`` sketch is written against **Pydantic v1** and does
not execute on the v2 runtime this project pins (``pydantic>=2.9``):

* ``Field(..., regex=...)`` was renamed to ``pattern=`` in v2. In v2 ``regex``
  is not silently ignored — it raises ``PydanticUserError`` at class-definition
  time, so the RFC's module would fail on import, not at first use.
* ``@validator`` was replaced by ``@field_validator``, whose callable must be a
  classmethod and whose signature drops the implicit ``values`` argument in
  favour of ``ValidationInfo``.

Both are corrected here. This is a transcription fix, not a design change: the
validation *intent* in the RFC is preserved exactly.

Why validation errors are re-wrapped
------------------------------------
Pydantic's ``ValidationError`` is excellent for a developer and useless in a
ledger payload — it carries a URL, a nested location tuple and the offending
input echoed back. The last part matters: a contract can be authored by a
downloaded marketplace package, so echoing its input into an exception that
gets logged is an injection surface. :meth:`SkillContract.parse` therefore
converts to :class:`~paa.core.errors.SkillContractError` with a flat, bounded,
JSON-serialisable finding list.
"""

from __future__ import annotations

import re
from typing import Any, Literal

import structlog
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from paa.core.errors import SkillContractError
from paa.core.types import Permission

__all__ = [
    "SEMVER_PATTERN",
    "SKILL_NAME_PATTERN",
    "SkillContract",
    "SkillInvocation",
    "SkillResult",
    "schema_errors",
    "validate_json_schema_shape",
]

log = structlog.get_logger(__name__)

#: Skill names are lower-case, dotted/dashed/underscored identifiers.
#:
#: Deliberately narrow. A skill name is interpolated into log lines, ledger
#: payloads, SQL parameters and — via the Claw-Hub adapter — filesystem paths.
#: Forbidding uppercase additionally sidesteps the case-insensitive-filesystem
#: collision where ``Deploy`` and ``deploy`` are two registry rows but one
#: directory on Windows and macOS.
SKILL_NAME_PATTERN = r"^[a-z0-9_\-\.]+$"

#: Semantic version, per semver.org's official recommended regex, anchored.
#: Contracts are matched by ``(skill_name, version)`` and marketplace upgrades
#: compare versions, so "1.0" or "v1.2.3" must be rejected rather than sorted
#: unpredictably.
SEMVER_PATTERN = (
    r"^(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)"
    r"(?:-((?:0|[1-9]\d*|\d*[a-zA-Z-][0-9a-zA-Z-]*)"
    r"(?:\.(?:0|[1-9]\d*|\d*[a-zA-Z-][0-9a-zA-Z-]*))*))?"
    r"(?:\+([0-9a-zA-Z-]+(?:\.[0-9a-zA-Z-]+)*))?$"
)

_SEMVER_RE = re.compile(SEMVER_PATTERN)


# ---------------------------------------------------------------------------
# JSON Schema shape checking
# ---------------------------------------------------------------------------


def validate_json_schema_shape(schema: Any, *, field_name: str) -> dict[str, Any]:
    """Assert that ``schema`` is a JSON Schema *object* schema.

    The runtime hands arguments to a skill as a mapping and reads results back
    as a mapping. A top-level ``{"type": "array"}`` or a bare ``true`` schema
    is legal JSON Schema but cannot describe that contract, so it is rejected
    at registration rather than producing a confusing failure at invocation.

    :raises ValueError: with a message naming the field and the problem.
    """
    if not isinstance(schema, dict):
        raise ValueError(
            f"{field_name} must be a JSON Schema object (a mapping), "
            f"got {type(schema).__name__}"
        )
    declared = schema.get("type")
    if declared != "object":
        raise ValueError(
            f"{field_name} must declare 'type': 'object' at the top level "
            f"(got {declared!r}); the runtime passes arguments and reads results "
            "as mappings, so no other root type can be honoured"
        )
    properties = schema.get("properties")
    if properties is not None and not isinstance(properties, dict):
        raise ValueError(f"{field_name}.properties must be a mapping when present")
    required = schema.get("required")
    if required is not None and not (
        isinstance(required, list) and all(isinstance(item, str) for item in required)
    ):
        raise ValueError(f"{field_name}.required must be a list of strings when present")
    return schema


def schema_errors(instance: Any, schema: dict[str, Any], *, path: str = "$") -> list[str]:
    """Validate ``instance`` against a **deliberate subset** of JSON Schema.

    Supported keywords: ``type``, ``properties``, ``required``,
    ``additionalProperties`` (bool form), ``enum``, ``items``, ``minimum``,
    ``maximum``, ``minLength``, ``maxLength``, ``minItems``, ``maxItems``.

    Why a subset rather than the ``jsonschema`` package: ``jsonschema`` is not
    a declared dependency, and this validator sits on the *output* path of
    every skill invocation, where it is a security control (RFC §8.2 step 6) —
    a hard dependency that might be absent would make that control optional,
    and an optional security control is not one. Unknown keywords are ignored
    rather than treated as failures, so a richer schema still validates on the
    parts we do understand instead of rejecting everything.

    :returns: human-readable error strings; empty means valid.
    """
    errors: list[str] = []
    if not isinstance(schema, dict):
        return errors

    expected = schema.get("type")
    if expected is not None and not _type_matches(instance, expected):
        actual = "null" if instance is None else type(instance).__name__
        return [f"{path}: expected type {expected!r}, got {actual}"]

    if (allowed := schema.get("enum")) is not None and isinstance(allowed, list):
        if instance not in allowed:
            errors.append(f"{path}: value is not one of the {len(allowed)} permitted values")

    if isinstance(instance, dict):
        errors.extend(_object_errors(instance, schema, path))
    elif isinstance(instance, list):
        errors.extend(_array_errors(instance, schema, path))
    elif isinstance(instance, str):
        errors.extend(_string_errors(instance, schema, path))
    elif isinstance(instance, (int, float)) and not isinstance(instance, bool):
        errors.extend(_number_errors(instance, schema, path))

    return errors


def _type_matches(instance: Any, expected: Any) -> bool:
    if isinstance(expected, list):
        return any(_type_matches(instance, item) for item in expected)
    match expected:
        case "object":
            return isinstance(instance, dict)
        case "array":
            return isinstance(instance, list)
        case "string":
            return isinstance(instance, str)
        # bool is a subclass of int in Python but a distinct JSON type, so the
        # isinstance check must exclude it explicitly or `true` validates as an
        # integer and a schema-typed field silently accepts the wrong thing.
        case "integer":
            return isinstance(instance, int) and not isinstance(instance, bool)
        case "number":
            return isinstance(instance, (int, float)) and not isinstance(instance, bool)
        case "boolean":
            return isinstance(instance, bool)
        case "null":
            return instance is None
        case _:
            return True  # unknown type keyword: do not invent a failure


def _object_errors(instance: dict[str, Any], schema: dict[str, Any], path: str) -> list[str]:
    errors: list[str] = []
    properties = schema.get("properties")
    properties = properties if isinstance(properties, dict) else {}

    for key in schema.get("required", []) or []:
        if key not in instance:
            errors.append(f"{path}: missing required property {key!r}")

    for key, value in instance.items():
        if key in properties:
            errors.extend(schema_errors(value, properties[key], path=f"{path}.{key}"))
        elif schema.get("additionalProperties") is False:
            errors.append(f"{path}: unexpected property {key!r}")
    return errors


def _array_errors(instance: list[Any], schema: dict[str, Any], path: str) -> list[str]:
    errors: list[str] = []
    if isinstance(min_items := schema.get("minItems"), int) and len(instance) < min_items:
        errors.append(f"{path}: expected at least {min_items} items, got {len(instance)}")
    if isinstance(max_items := schema.get("maxItems"), int) and len(instance) > max_items:
        errors.append(f"{path}: expected at most {max_items} items, got {len(instance)}")
    if isinstance(item_schema := schema.get("items"), dict):
        for index, item in enumerate(instance):
            errors.extend(schema_errors(item, item_schema, path=f"{path}[{index}]"))
    return errors


def _string_errors(instance: str, schema: dict[str, Any], path: str) -> list[str]:
    errors: list[str] = []
    if isinstance(low := schema.get("minLength"), int) and len(instance) < low:
        errors.append(f"{path}: shorter than minLength {low}")
    if isinstance(high := schema.get("maxLength"), int) and len(instance) > high:
        errors.append(f"{path}: longer than maxLength {high}")
    if isinstance(pattern := schema.get("pattern"), str):
        try:
            if re.search(pattern, instance) is None:
                errors.append(f"{path}: does not match the declared pattern")
        except re.error:
            # A malformed pattern in a downloaded contract must not crash the
            # output gate; treat it as unconstrained and say so.
            log.warning("skills.schema.bad_pattern", path=path)
    return errors


def _number_errors(instance: float, schema: dict[str, Any], path: str) -> list[str]:
    errors: list[str] = []
    if isinstance(low := schema.get("minimum"), (int, float)) and instance < low:
        errors.append(f"{path}: below minimum {low}")
    if isinstance(high := schema.get("maximum"), (int, float)) and instance > high:
        errors.append(f"{path}: above maximum {high}")
    return errors


# ---------------------------------------------------------------------------
# Invocation
# ---------------------------------------------------------------------------


class SkillInvocation(BaseModel):
    """*How* to actually run a skill once policy has cleared it.

    Kept separate from :class:`SkillContract` because the contract is the
    security-relevant declaration and this is the mechanical recipe. They have
    different audiences: policy reads the contract, the adapter reads this.

    ``kind`` selects the adapter; the remaining fields are the union of what
    the four adapters need. A union rather than a discriminated hierarchy
    because this struct is persisted as one JSON column
    (``hot_serving_skill_registry.invocation``) and round-trips more
    predictably without polymorphic deserialisation.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    kind: Literal["python_entrypoint", "mcp_tool", "native_callable", "shell"]

    target: str = Field(min_length=1)
    """Interpretation depends on ``kind``: a ``module:function`` path, an MCP
    tool name, a registered callable's key, or an argv[0]."""

    args: tuple[str, ...] = ()
    """Fixed leading arguments. Skill arguments are appended by the adapter."""

    working_dir: str | None = None
    """Skill root. Mounted **read-only** — a skill's own scripts are code, and
    code that can rewrite itself between the security scan and execution
    defeats the scan (a TOCTOU the read-only mount closes)."""

    entrypoint_file: str | None = None
    """Path, relative to ``working_dir``, of the script the sandbox executes."""

    server_command: tuple[str, ...] = ()
    """argv of the MCP stdio server. Only meaningful for ``kind='mcp_tool'``."""

    system_prompt: str | None = None
    """Markdown body from ``SKILL.md``, wrapped around the worker's prompt."""

    timeout_seconds: float | None = Field(default=None, gt=0)
    """``None`` defers to the modality profile rather than meaning "forever"."""

    env_allowlist: tuple[str, ...] = ()
    """Names the skill may read via the secret broker. Note this is a list of
    *names*, never values — see :mod:`paa.skills.secrets`."""

    @field_validator("target")
    @classmethod
    def _no_control_characters(cls, value: str) -> str:
        """A target is interpolated into argv and log lines.

        Control characters there are either a mistake or an attempt to forge
        log records by injecting newlines.
        """
        if any(ord(char) < 32 for char in value):
            raise ValueError("target must not contain control characters")
        return value


# ---------------------------------------------------------------------------
# Result
# ---------------------------------------------------------------------------


class SkillResult(BaseModel):
    """Outcome of one skill invocation.

    Total, like :class:`~paa.sandbox.base.SandboxResult`: a failed skill still
    produces a result so the reliability optimiser has a sample to learn from.
    Only refusals *before* execution (permission denied, contract invalid)
    raise, because those produce no evidence about the skill's reliability and
    must not be allowed to drag its weight down.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    skill_name: str
    ok: bool
    output: dict[str, Any] = Field(default_factory=dict)
    error: str | None = None
    duration_ms: float = 0.0

    output_valid: bool = True
    """False when :meth:`SkillContract.validate_output` rejected the payload.
    Distinct from ``ok``: a skill can exit 0 and still return garbage, and that
    is precisely the case the reliability weight must punish."""

    schema_errors: tuple[str, ...] = ()
    exit_code: int | None = None
    adapter: str = "unknown"


# ---------------------------------------------------------------------------
# Contract
# ---------------------------------------------------------------------------


class SkillContract(BaseModel):
    """Everything the runtime must know before letting a capability execute.

    RFC §8.1, transcribed to Pydantic v2 (see the module docstring).
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    skill_name: str = Field(pattern=SKILL_NAME_PATTERN, min_length=1, max_length=128)
    provider: Literal["claw_hub", "mcp_server", "native", "marketplace"]
    version: str = Field(pattern=SEMVER_PATTERN)

    description: str = Field(min_length=20, max_length=4096)
    """At least 20 characters. Not bureaucracy: this string is what semantic
    search embeds and what the planner reads when choosing between skills. A
    description of "does stuff" makes the skill unfindable and the choice
    unexplainable, so the floor is enforced rather than requested."""

    input_schema: dict[str, Any]
    output_schema: dict[str, Any]

    risk_profile: float = Field(ge=0.0, le=1.0)
    """Blast radius of the skill's effects. Compared against
    :attr:`~paa.config.PolicySettings.always_gate_risk_profile`."""

    required_permissions: tuple[Permission, ...] = ()
    """Checked as a subset of the active mode's grants *before* a sandbox boots
    (RFC §8.2 step 3)."""

    reliability_weight: float = Field(default=1.0, ge=0.0, le=1.0)
    """EWMA of observed outcomes, maintained by
    :meth:`paa.skills.registry.SkillRegistry.update_reliability`."""

    invocation: SkillInvocation

    source_uri: str | None = None
    source_checksum: str | None = None
    signature: str | None = None

    @field_validator("input_schema", "output_schema")
    @classmethod
    def _schemas_are_object_schemas(cls, value: Any, info: Any) -> dict[str, Any]:
        return validate_json_schema_shape(value, field_name=str(info.field_name))

    @field_validator("required_permissions", mode="before")
    @classmethod
    def _coerce_permissions(cls, value: Any) -> Any:
        """Accept raw strings but insist every one names a real permission.

        Contracts arrive as JSON from the registry and from marketplace
        manifests, so the wire form is strings. Pydantic would happily coerce
        an unknown string into a validation error, but the default message
        ("Input should be 'PERM_SANDBOX_RUN' or ...") truncates the enum and is
        hard to act on. Listing the valid names makes a typo self-correcting.
        """
        if value is None:
            return ()
        if isinstance(value, (str, bytes)):
            raise ValueError("required_permissions must be a list, not a bare string")

        valid = {p.value for p in Permission} | {p.name for p in Permission}
        resolved: list[Permission] = []
        for item in value:
            if isinstance(item, Permission):
                resolved.append(item)
                continue
            if not isinstance(item, str) or item not in valid:
                raise ValueError(
                    f"unknown permission {item!r}; valid values are "
                    f"{sorted(p.value for p in Permission)}"
                )
            resolved.append(Permission(item) if item.startswith("PERM_") else Permission[item])
        return tuple(resolved)

    # -- derived ----------------------------------------------------------

    @property
    def key(self) -> str:
        """Idempotency key for registration: name plus version."""
        return f"{self.skill_name}@{self.version}"

    @property
    def version_tuple(self) -> tuple[int, int, int]:
        """``(major, minor, patch)`` for ordering. Pre-release tags are dropped."""
        match = _SEMVER_RE.match(self.version)
        if match is None:  # unreachable: the field pattern already enforced it
            return (0, 0, 0)  # pragma: no cover
        return (int(match.group(1)), int(match.group(2)), int(match.group(3)))

    def missing_permissions(self, granted: frozenset[Permission]) -> list[Permission]:
        """Permissions this skill needs that ``granted`` does not supply."""
        return [p for p in self.required_permissions if p not in granted]

    def validate_output(self, payload: Any) -> list[str]:
        """Check a skill's return value against ``output_schema``.

        RFC §8.2 step 6. Returns error strings; empty means the output is
        believable enough to hand back to the caller.
        """
        return schema_errors(payload, self.output_schema)

    def validate_input(self, payload: Any) -> list[str]:
        return schema_errors(payload, self.input_schema)

    # -- construction ------------------------------------------------------

    @classmethod
    def parse(cls, data: dict[str, Any]) -> SkillContract:
        """Build a contract, raising :class:`SkillContractError` on any problem.

        The canonical entry point for untrusted input (registry rows,
        marketplace manifests, adapter discovery). Direct construction is fine
        for trusted, literal, in-repo contracts.
        """
        try:
            return cls.model_validate(data)
        except ValidationError as exc:
            findings = [
                {
                    "field": ".".join(str(part) for part in error["loc"]) or "<root>",
                    "problem": error["msg"],
                }
                for error in exc.errors()
            ]
            # `data` is deliberately not echoed — a contract can originate from
            # a downloaded package, and this message reaches logs and ledger.
            name = data.get("skill_name") if isinstance(data, dict) else None
            raise SkillContractError(
                "skill contract failed validation",
                skill_name=name if isinstance(name, str) else None,
                findings=findings,
            ) from exc

    def to_row(self) -> dict[str, Any]:
        """Column values for ``hot_serving_skill_registry``."""
        return {
            "skill_name": self.skill_name,
            "provider": self.provider,
            "version": self.version,
            "description": self.description,
            "input_schema": self.input_schema,
            "output_schema": self.output_schema,
            "risk_profile": self.risk_profile,
            "required_permissions": [p.value for p in self.required_permissions],
            "reliability_weight": self.reliability_weight,
            "invocation": self.invocation.model_dump(mode="json"),
            "source_uri": self.source_uri,
            "source_checksum": self.source_checksum,
            "signature": self.signature,
        }
