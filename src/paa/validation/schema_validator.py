"""Structural validation of payloads against declared schemas. RFC §13.

Used at two boundaries:

* **Skill output** — the Unified Skill Adapter declares an output schema per
  skill, and :meth:`SchemaValidator.validate_tool_output` is what makes that
  declaration load-bearing instead of documentation. A skill whose output drifts
  from its contract must fail *here*, not three layers downstream where the
  KeyError has lost all context about which skill lied.
* **Agent JSON** — a model asked for structured output produces something
  shaped almost right, and "almost" is the expensive case.

SPEC DEVIATION (docs/adr/0010): the JSON Schema subset below is implemented in
pure Python rather than delegating to ``jsonschema``.

The reason is determinism, which RFC §13 requires of every host-side check.
``jsonschema`` is not a declared dependency, so a conditional import would make
validation strictness depend on whether an *optional, unrelated* extra happened
to be installed — the same artifact passing on one machine and failing on
another, with no configuration difference to point at. For a gate that decides
whether agent output is committed, that is not acceptable, and "it's stricter
when the library is present" is the worst of both worlds because the lenient
path is the one that ships.

The supported subset is stated in :func:`validate_instance` and covers what
skill contracts actually use. Unsupported keywords are **ignored, not guessed**
— see :attr:`SchemaValidator.strict_keywords` for making that loud.
"""

from __future__ import annotations

import re
from typing import Any

import structlog
from pydantic import BaseModel
from pydantic import ValidationError as PydanticValidationError

from paa.core.errors import SchemaValidationError

__all__ = ["SchemaError", "SchemaValidator", "validate_instance"]

log = structlog.get_logger(__name__)

#: JSON Schema keywords this implementation understands. Anything outside this
#: set is ignored (or raises, under ``strict_keywords``) rather than silently
#: treated as satisfied — an unimplemented keyword that reads as "valid" is a
#: validator that lies.
SUPPORTED_KEYWORDS: frozenset[str] = frozenset(
    {
        "type",
        "properties",
        "required",
        "additionalProperties",
        "items",
        "prefixItems",
        "enum",
        "const",
        "minimum",
        "maximum",
        "exclusiveMinimum",
        "exclusiveMaximum",
        "multipleOf",
        "minLength",
        "maxLength",
        "pattern",
        "minItems",
        "maxItems",
        "uniqueItems",
        "minProperties",
        "maxProperties",
        "anyOf",
        "oneOf",
        "allOf",
        "not",
        "nullable",
        "default",
        "title",
        "description",
        "examples",
        "$schema",
        "$id",
        "$comment",
        "definitions",
        "$defs",
    }
)

_TYPE_MAP: dict[str, tuple[type, ...]] = {
    "object": (dict,),
    "array": (list, tuple),
    "string": (str,),
    "integer": (int,),
    "number": (int, float),
    "boolean": (bool,),
    "null": (type(None),),
}


class SchemaError(dict[str, Any]):
    """One structural failure, as a plain dict for direct ledger embedding.

    A dict subclass rather than a dataclass so it serialises with no adapter —
    :class:`~paa.core.errors.SchemaValidationError` takes ``list[dict]`` and
    the ledger payload wants exactly this shape.
    """

    def __init__(self, path: str, message: str, **extra: Any) -> None:
        super().__init__(path=path or "$", message=message, **extra)

    @property
    def path(self) -> str:
        return str(self["path"])

    @property
    def message(self) -> str:
        return str(self["message"])

    def __str__(self) -> str:
        return f"{self.path}: {self.message}"


def _type_name(value: Any) -> str:
    """JSON type name for a Python value.

    ``bool`` is checked before ``int`` because ``isinstance(True, int)`` is
    ``True`` in Python. Without the guard, ``{"type": "integer"}`` would accept
    ``true``, which is exactly the sort of near-miss this module exists to
    catch.
    """
    if isinstance(value, bool):
        return "boolean"
    if value is None:
        return "null"
    if isinstance(value, int):
        return "integer"
    if isinstance(value, float):
        return "number"
    if isinstance(value, str):
        return "string"
    if isinstance(value, (list, tuple)):
        return "array"
    if isinstance(value, dict):
        return "object"
    return type(value).__name__


def _matches_type(value: Any, expected: str) -> bool:
    if expected == "integer":
        # 3.0 is an integer in JSON's data model; 3.5 is not.
        if isinstance(value, bool):
            return False
        if isinstance(value, int):
            return True
        return isinstance(value, float) and value.is_integer()
    if expected == "number":
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    if expected == "boolean":
        return isinstance(value, bool)
    if expected == "null":
        return value is None
    types = _TYPE_MAP.get(expected)
    if types is None:
        return True  # unknown type keyword — not our place to guess
    if expected in {"string", "object", "array"} and isinstance(value, bool):
        return False
    return isinstance(value, types)


def validate_instance(
    payload: Any,
    schema: dict[str, Any],
    *,
    path: str = "$",
    errors: list[SchemaError] | None = None,
) -> list[SchemaError]:
    """Validate ``payload`` against ``schema``, returning every error found.

    Collects *all* failures rather than stopping at the first. A skill author
    fixing one field at a time across five round trips is the failure mode of
    fail-fast validation, and each round trip here costs an LLM call.

    Supported subset: ``type``, ``properties``, ``required``,
    ``additionalProperties``, ``items``, ``prefixItems``, ``enum``, ``const``,
    numeric bounds, ``multipleOf``, string ``minLength``/``maxLength``/
    ``pattern``, array ``minItems``/``maxItems``/``uniqueItems``, object
    ``minProperties``/``maxProperties``, and the ``anyOf``/``oneOf``/``allOf``/
    ``not`` combinators. ``$ref`` is deliberately **not** supported — see
    :meth:`SchemaValidator.validate`.
    """
    if errors is None:
        errors = []
    if not isinstance(schema, dict):
        return errors
    # `True`/`{}` accept anything; `False` accepts nothing.
    if schema is False:  # pragma: no cover - defensive
        errors.append(SchemaError(path, "schema forbids any value here"))
        return errors

    if schema.get("nullable") and payload is None:
        return errors

    # -- type ------------------------------------------------------------
    if (expected := schema.get("type")) is not None:
        candidates = expected if isinstance(expected, list) else [expected]
        if not any(_matches_type(payload, str(c)) for c in candidates):
            errors.append(
                SchemaError(
                    path,
                    f"expected type {'/'.join(map(str, candidates))}, got {_type_name(payload)}",
                    expected=candidates,
                    actual=_type_name(payload),
                )
            )
            # A type mismatch makes every downstream keyword meaningless —
            # reporting "minLength failed" on an int is noise, not signal.
            return errors

    # -- const / enum ------------------------------------------------------
    if "const" in schema and payload != schema["const"]:
        errors.append(
            SchemaError(path, f"expected constant {schema['const']!r}", expected=schema["const"])
        )
    if (allowed := schema.get("enum")) is not None and payload not in allowed:
        errors.append(
            SchemaError(
                path,
                f"value {payload!r} is not one of the permitted values",
                permitted=list(allowed),
            )
        )

    # -- combinators -------------------------------------------------------
    if (subschemas := schema.get("allOf")) is not None:
        for index, sub in enumerate(subschemas):
            validate_instance(payload, sub, path=f"{path}/allOf[{index}]", errors=errors)
    if (subschemas := schema.get("anyOf")) is not None:
        if not any(not validate_instance(payload, sub, path=path) for sub in subschemas):
            errors.append(SchemaError(path, "value matched none of the anyOf alternatives"))
    if (subschemas := schema.get("oneOf")) is not None:
        matched = sum(1 for sub in subschemas if not validate_instance(payload, sub, path=path))
        if matched != 1:
            errors.append(
                SchemaError(path, f"expected exactly one oneOf match, got {matched}", matched=matched)
            )
    if (negated := schema.get("not")) is not None and not validate_instance(
        payload, negated, path=path
    ):
        errors.append(SchemaError(path, "value matched a schema it must not match"))

    # -- objects -----------------------------------------------------------
    if isinstance(payload, dict):
        properties: dict[str, Any] = schema.get("properties", {}) or {}
        for field in schema.get("required", []) or []:
            if field not in payload:
                errors.append(
                    SchemaError(f"{path}.{field}", "required property is missing", field=field)
                )
        for key, value in payload.items():
            if key in properties:
                validate_instance(value, properties[key], path=f"{path}.{key}", errors=errors)

        additional = schema.get("additionalProperties")
        if additional is False:
            for key in payload:
                if key not in properties:
                    errors.append(
                        SchemaError(
                            f"{path}.{key}",
                            "additional properties are not permitted",
                            field=key,
                        )
                    )
        elif isinstance(additional, dict):
            for key, value in payload.items():
                if key not in properties:
                    validate_instance(value, additional, path=f"{path}.{key}", errors=errors)

        if (limit := schema.get("minProperties")) is not None and len(payload) < limit:
            errors.append(SchemaError(path, f"expected at least {limit} properties"))
        if (limit := schema.get("maxProperties")) is not None and len(payload) > limit:
            errors.append(SchemaError(path, f"expected at most {limit} properties"))

    # -- arrays ------------------------------------------------------------
    if isinstance(payload, (list, tuple)):
        if (item_schema := schema.get("items")) is not None and isinstance(item_schema, dict):
            for index, item in enumerate(payload):
                validate_instance(item, item_schema, path=f"{path}[{index}]", errors=errors)
        if (prefix := schema.get("prefixItems")) is not None:
            for index, sub in enumerate(prefix):
                if index < len(payload):
                    validate_instance(payload[index], sub, path=f"{path}[{index}]", errors=errors)
        if (limit := schema.get("minItems")) is not None and len(payload) < limit:
            errors.append(SchemaError(path, f"expected at least {limit} items, got {len(payload)}"))
        if (limit := schema.get("maxItems")) is not None and len(payload) > limit:
            errors.append(SchemaError(path, f"expected at most {limit} items, got {len(payload)}"))
        if schema.get("uniqueItems"):
            seen: list[Any] = []
            for item in payload:
                # Linear scan rather than a set: JSON values include dicts and
                # lists, which are unhashable. Correctness over speed on
                # payloads that are small by construction.
                if item in seen:
                    errors.append(SchemaError(path, "array items must be unique"))
                    break
                seen.append(item)

    # -- strings -----------------------------------------------------------
    if isinstance(payload, str):
        if (limit := schema.get("minLength")) is not None and len(payload) < limit:
            errors.append(SchemaError(path, f"string shorter than minLength {limit}"))
        if (limit := schema.get("maxLength")) is not None and len(payload) > limit:
            errors.append(SchemaError(path, f"string longer than maxLength {limit}"))
        if (pattern := schema.get("pattern")) is not None:
            try:
                if re.search(pattern, payload) is None:
                    errors.append(
                        SchemaError(path, f"string does not match pattern {pattern!r}")
                    )
            except re.error as exc:
                errors.append(SchemaError(path, f"schema pattern is not a valid regex: {exc}"))

    # -- numbers -----------------------------------------------------------
    if isinstance(payload, (int, float)) and not isinstance(payload, bool):
        if (bound := schema.get("minimum")) is not None and payload < bound:
            errors.append(SchemaError(path, f"{payload} is below minimum {bound}"))
        if (bound := schema.get("maximum")) is not None and payload > bound:
            errors.append(SchemaError(path, f"{payload} is above maximum {bound}"))
        if (bound := schema.get("exclusiveMinimum")) is not None and payload <= bound:
            errors.append(SchemaError(path, f"{payload} must be greater than {bound}"))
        if (bound := schema.get("exclusiveMaximum")) is not None and payload >= bound:
            errors.append(SchemaError(path, f"{payload} must be less than {bound}"))
        if (divisor := schema.get("multipleOf")) is not None and divisor:
            if abs(payload / divisor - round(payload / divisor)) > 1e-9:
                errors.append(SchemaError(path, f"{payload} is not a multiple of {divisor}"))

    return errors


class SchemaValidator:
    """Validates payloads against JSON Schemas or pydantic models."""

    def __init__(self, *, strict_keywords: bool = False) -> None:
        self.strict_keywords = strict_keywords
        """Raise when a schema uses a keyword this implementation ignores.

        Off by default so a schema carrying documentation-only extensions still
        works. Turn it **on** for skill contracts, where a silently ignored
        ``$ref`` means a constraint the author believed was enforced never was.
        """

    def unsupported_keywords(self, schema: dict[str, Any]) -> set[str]:
        """Keywords present in ``schema`` that this validator ignores."""
        found: set[str] = set()

        def walk(node: Any) -> None:
            if isinstance(node, dict):
                for key, value in node.items():
                    if key not in SUPPORTED_KEYWORDS and not key.startswith("x-"):
                        found.add(key)
                    walk(value)
            elif isinstance(node, list):
                for item in node:
                    walk(item)

        walk(schema)
        return found

    def validate(
        self,
        payload: Any,
        schema: dict[str, Any],
        *,
        schema_name: str = "payload",
    ) -> Any:
        """Validate and return ``payload``, or raise :class:`SchemaValidationError`.

        ``$ref`` is rejected rather than ignored. A ``$ref`` we silently drop
        would leave the referenced constraints unenforced while the schema
        author reasonably believes they are in force — a validator that reports
        success on an un-checked subtree is worse than no validator, because it
        is trusted.
        """
        if unsupported := self.unsupported_keywords(schema):
            blocking = unsupported & {"$ref", "$dynamicRef", "if", "then", "else"}
            if blocking or self.strict_keywords:
                raise SchemaValidationError(
                    schema_name,
                    [
                        SchemaError(
                            "$",
                            f"schema uses unsupported keyword(s) {sorted(blocking or unsupported)}; "
                            "see docs/adr/0010 for the supported subset",
                        )
                    ],
                )
            log.debug(
                "validation.schema.ignored_keywords",
                schema_name=schema_name,
                keywords=sorted(unsupported),
            )

        if errors := validate_instance(payload, schema):
            log.warning(
                "validation.schema.rejected",
                schema_name=schema_name,
                error_count=len(errors),
                first=str(errors[0]),
            )
            raise SchemaValidationError(schema_name, list(errors))
        return payload

    def check(self, payload: Any, schema: dict[str, Any]) -> list[SchemaError]:
        """Non-raising variant for the engine, which collects rather than aborts."""
        return validate_instance(payload, schema)

    def validate_model(
        self,
        model: type[BaseModel],
        payload: Any,
        *,
        schema_name: str | None = None,
    ) -> BaseModel:
        """Validate against a pydantic model, re-shaping its errors to ours.

        Pydantic's error dicts carry a ``ctx`` that may hold arbitrary
        non-serialisable objects (exception instances, for one), which would
        blow up ledger persistence at the worst possible moment. Only the
        JSON-safe fields are carried across.
        """
        name = schema_name or model.__name__
        try:
            return model.model_validate(payload)
        except PydanticValidationError as exc:
            errors = [
                SchemaError(
                    "$" + "".join(f".{p}" for p in err["loc"]),
                    err["msg"],
                    error_type=err["type"],
                )
                for err in exc.errors()
            ]
            log.warning("validation.schema.model_rejected", schema_name=name, errors=len(errors))
            raise SchemaValidationError(name, list(errors)) from exc

    def validate_tool_output(
        self,
        skill_output_schema: dict[str, Any] | type[BaseModel] | None,
        payload: Any,
        *,
        skill_name: str = "skill",
    ) -> Any:
        """Validate a skill's output against its declared contract.

        Called by the Unified Skill Adapter on every skill return.

        A ``None`` schema means the skill declared no contract, and the payload
        passes through unchecked. That is a deliberate gap and it is logged at
        warning level: an unvalidated skill output is a hole in RFC §13's
        determinism guarantee, and it should be visible in the logs rather than
        discovered later by whoever debugs the resulting KeyError.
        """
        if skill_output_schema is None:
            log.warning(
                "validation.schema.no_contract",
                skill=skill_name,
                detail="skill declared no output schema; output is unvalidated",
            )
            return payload

        if isinstance(skill_output_schema, type) and issubclass(skill_output_schema, BaseModel):
            return self.validate_model(
                skill_output_schema, payload, schema_name=f"{skill_name}.output"
            )
        return self.validate(payload, skill_output_schema, schema_name=f"{skill_name}.output")
