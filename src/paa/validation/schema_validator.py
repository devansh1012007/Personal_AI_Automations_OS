"""Structural validation of payloads against declared schemas. RFC §13.

Used at two boundaries:

* **Skill output** — the Unified Skill Adapter declares an output schema per
  skill, and :meth:`SchemaValidator.validate_tool_output` is what makes that
  declaration load-bearing instead of documentation. A skill whose output drifts
  from its contract must fail *here*, not three layers downstream where the
  KeyError has lost all context about which skill lied.
* **Agent JSON** — a model asked for structured output produces something
  shaped almost right, and "almost" is the expensive case.

SPEC DEVIATION (docs/adr/0028): the JSON Schema subset below is implemented in
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

A malformed keyword *value* is a different failure from an unsupported
keyword, and it is treated differently on purpose. An unsupported keyword is a
question this implementation cannot answer; a malformed value is a broken
contract the author can fix. So a keyword whose value has the wrong shape
(``minLength: "abc"``, ``multipleOf: "2"``, a ``pattern`` that is not a valid
regex) is **reported as a schema error and never applied** — and never
raised. The rule is the same for every keyword, it does not depend on
``strict_keywords``, and a valid schema validates exactly what it validated
before.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

import structlog
from pydantic import BaseModel
from pydantic import ValidationError as PydanticValidationError

from paa.core.errors import SchemaValidationError

__all__ = ["JsonSchema", "SchemaError", "SchemaValidator", "validate_instance"]

log = structlog.get_logger(__name__)

#: A JSON Schema node: a mapping of keywords, or one of the two boolean
#: schemas — ``true`` accepts every value, ``false`` accepts none.
#:
#: Both forms are legal JSON Schema, so a validator whose schema parameter is
#: typed as ``dict`` alone describes a strict subset of the language it claims
#: to implement — and the boolean form is the half that carries the
#: security-relevant meaning. ``false`` is a schema that *forbids* a value, and
#: a validator that cannot even represent it will accept the payload it was
#: written to reject.
JsonSchema = dict[str, Any] | bool

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


# ---------------------------------------------------------------------------
# The shape a keyword's value must have
# ---------------------------------------------------------------------------

#: The kinds of value a keyword may carry.
#:
#: A *kind* is a shape, not a range: a value that does not have the shape is
#: malformed, and a malformed value is reported rather than applied. The
#: ranges that JSON Schema itself imposes (``minLength`` is a non-negative
#: integer, ``multipleOf`` is strictly positive) are part of the shape, because
#: a value outside them cannot be used by the keyword either.
_KIND_TYPE = "type"
_KIND_NUMBER = "number"
_KIND_POSITIVE_NUMBER = "positive_number"
_KIND_NON_NEGATIVE_INT = "non_negative_int"
_KIND_BOOLEAN = "boolean"
_KIND_STRING = "string"
_KIND_REGEX = "regex"
_KIND_ARRAY = "array"
_KIND_STRING_ARRAY = "string_array"
_KIND_SCHEMA = "schema"
_KIND_SCHEMA_ARRAY = "schema_array"
_KIND_SCHEMA_MAP = "schema_map"


@dataclass(frozen=True, slots=True)
class _KeywordSpec:
    """How one supported keyword's value must look."""

    kind: str
    description: str
    """What the keyword's own value must be — "expects {description}"."""

    element_description: str = ""
    """What each element of a container keyword must be, where it has one."""


#: Every supported keyword whose value is constrained.
#:
#: The union of this table and :data:`_ANNOTATION_KEYWORDS` is exactly
#: :data:`SUPPORTED_KEYWORDS`, and a test asserts it — so a keyword added to
#: the supported set without a shape here is a loud failure rather than a
#: keyword whose value is never checked.
_KEYWORD_SPECS: dict[str, _KeywordSpec] = {
    "type": _KeywordSpec(
        _KIND_TYPE, "a type name or a list of type names", "a type name"
    ),
    "properties": _KeywordSpec(
        _KIND_SCHEMA_MAP, "a mapping of property names to schemas", "a schema"
    ),
    "required": _KeywordSpec(_KIND_STRING_ARRAY, "a list of property names", "a string"),
    "additionalProperties": _KeywordSpec(_KIND_SCHEMA, "a boolean or a schema"),
    "items": _KeywordSpec(_KIND_SCHEMA, "a schema"),
    "prefixItems": _KeywordSpec(_KIND_SCHEMA_ARRAY, "a list of schemas", "a schema"),
    "enum": _KeywordSpec(_KIND_ARRAY, "a list of permitted values"),
    "minimum": _KeywordSpec(_KIND_NUMBER, "a number"),
    "maximum": _KeywordSpec(_KIND_NUMBER, "a number"),
    "exclusiveMinimum": _KeywordSpec(_KIND_NUMBER, "a number"),
    "exclusiveMaximum": _KeywordSpec(_KIND_NUMBER, "a number"),
    "multipleOf": _KeywordSpec(_KIND_POSITIVE_NUMBER, "a number greater than zero"),
    "minLength": _KeywordSpec(_KIND_NON_NEGATIVE_INT, "a non-negative integer"),
    "maxLength": _KeywordSpec(_KIND_NON_NEGATIVE_INT, "a non-negative integer"),
    "pattern": _KeywordSpec(_KIND_REGEX, "a valid regular expression"),
    "minItems": _KeywordSpec(_KIND_NON_NEGATIVE_INT, "a non-negative integer"),
    "maxItems": _KeywordSpec(_KIND_NON_NEGATIVE_INT, "a non-negative integer"),
    "uniqueItems": _KeywordSpec(_KIND_BOOLEAN, "a boolean"),
    "minProperties": _KeywordSpec(_KIND_NON_NEGATIVE_INT, "a non-negative integer"),
    "maxProperties": _KeywordSpec(_KIND_NON_NEGATIVE_INT, "a non-negative integer"),
    "anyOf": _KeywordSpec(_KIND_SCHEMA_ARRAY, "a list of schemas", "a schema"),
    "oneOf": _KeywordSpec(_KIND_SCHEMA_ARRAY, "a list of schemas", "a schema"),
    "allOf": _KeywordSpec(_KIND_SCHEMA_ARRAY, "a list of schemas", "a schema"),
    "not": _KeywordSpec(_KIND_SCHEMA, "a schema"),
    "nullable": _KeywordSpec(_KIND_BOOLEAN, "a boolean"),
    "definitions": _KeywordSpec(
        _KIND_SCHEMA_MAP, "a mapping of names to schemas", "a schema"
    ),
    "$defs": _KeywordSpec(_KIND_SCHEMA_MAP, "a mapping of names to schemas", "a schema"),
}

#: Supported keywords that constrain nothing: their value may be anything.
#:
#: ``const`` is here rather than in the constrained set because any JSON
#: value — including ``null`` — is a legal constant.
_ANNOTATION_KEYWORDS: frozenset[str] = frozenset(
    {
        "const",
        "default",
        "title",
        "description",
        "examples",
        "$schema",
        "$id",
        "$comment",
    }
)


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


def _is_number(value: Any) -> bool:
    """A JSON number — and not a boolean, which is an ``int`` in Python."""
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _is_schema(value: Any) -> bool:
    """A schema node: a mapping, or one of the two boolean schemas."""
    return isinstance(value, (dict, bool))


def _value_defects(spec: _KeywordSpec, value: Any) -> list[tuple[str, str]]:
    """Every way ``value`` breaks the shape ``spec`` requires.

    Returns ``(path suffix, message)`` pairs; an empty list means the value is
    usable. The suffix is relative to the keyword's own path, so a defect
    inside a container keyword points at the element that is wrong.

    The check is deliberately about the *shape* and not about the meaning: a
    value that has the right shape is applied, and one that does not is
    reported. Which is why the same function serves both purposes — the
    report and the decision to apply — and the two cannot drift apart.
    """
    kind = spec.kind

    def whole() -> list[tuple[str, str]]:
        return [("", f"expects {spec.description}, got {_type_name(value)}")]

    if kind == _KIND_TYPE:
        if isinstance(value, str):
            return []
        if isinstance(value, list):
            return [
                (f"[{index}]", f"expects {spec.element_description}, got {_type_name(item)}")
                for index, item in enumerate(value)
                if not isinstance(item, str)
            ]
        return whole()
    if kind == _KIND_NUMBER:
        return [] if _is_number(value) else whole()
    if kind == _KIND_POSITIVE_NUMBER:
        return [] if _is_number(value) and value > 0 else whole()
    if kind == _KIND_NON_NEGATIVE_INT:
        if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
            return []
        return whole()
    if kind == _KIND_BOOLEAN:
        return [] if isinstance(value, bool) else whole()
    if kind == _KIND_STRING:
        return [] if isinstance(value, str) else whole()
    if kind == _KIND_REGEX:
        if not isinstance(value, str):
            return whole()
        try:
            re.compile(value)
        except (re.error, OverflowError, RecursionError) as exc:
            # ``re`` raises three different things for three different
            # malformed patterns: ``re.error`` for a bad syntax,
            # ``OverflowError`` for an unrepresentable repetition count, and
            # ``RecursionError`` for a pattern nested past the compiler's
            # limit. A pattern that cannot be compiled is a broken contract,
            # not a reason for the gate to raise.
            return [("", f"expects {spec.description}: {exc}")]
        return []
    if kind == _KIND_ARRAY:
        return [] if isinstance(value, list) else whole()
    if kind == _KIND_STRING_ARRAY:
        if not isinstance(value, list):
            return whole()
        return [
            (f"[{index}]", f"expects {spec.element_description}, got {_type_name(item)}")
            for index, item in enumerate(value)
            if not isinstance(item, str)
        ]
    if kind == _KIND_SCHEMA:
        return [] if _is_schema(value) else whole()
    if kind == _KIND_SCHEMA_ARRAY:
        if not isinstance(value, list):
            return whole()
        return [
            (f"[{index}]", f"expects {spec.element_description}, got {_type_name(item)}")
            for index, item in enumerate(value)
            if not _is_schema(item)
        ]
    if kind == _KIND_SCHEMA_MAP:
        if not isinstance(value, dict):
            return whole()
        return [
            (f".{key}", f"expects {spec.element_description}, got {_type_name(item)}")
            for key, item in value.items()
            if not _is_schema(item)
        ]
    raise AssertionError(f"unknown keyword kind {kind!r}")  # pragma: no cover


def _sub_schemas(spec: _KeywordSpec, value: Any, path: str) -> list[tuple[str, Any]]:
    """The sub-schemas of ``value``, with the path each one sits at.

    Empty for a keyword that holds no sub-schema, and empty for a *malformed*
    value: there is nothing to descend into, and descending into a value of
    the wrong type is how a validator starts raising.
    """
    kind = spec.kind
    if kind == _KIND_SCHEMA:
        return [(path, value)] if _is_schema(value) else []
    if kind == _KIND_SCHEMA_ARRAY:
        if not isinstance(value, list):
            return []
        return [(f"{path}[{index}]", item) for index, item in enumerate(value)]
    if kind == _KIND_SCHEMA_MAP:
        if not isinstance(value, dict):
            return []
        return [(f"{path}.{key}", item) for key, item in value.items()]
    return []


def _schema_shape_errors(schema: Any, path: str) -> list[SchemaError]:
    """Every way ``schema`` breaks the shape the supported keywords require.

    The walk descends into the schema's sub-schemas, so a malformed keyword
    is reported wherever it sits — not only where the instance happens to
    reach it. A schema is a contract in its own right: a broken constraint
    in a branch this payload never touches is still a broken contract, and
    the author needs to hear about it before the payload that does touch it
    arrives.
    """
    errors: list[SchemaError] = []
    _walk_schema_shape(schema, path, errors)
    return errors


def _walk_schema_shape(node: Any, path: str, errors: list[SchemaError]) -> None:
    if not isinstance(node, dict):
        return
    for keyword, value in node.items():
        if not isinstance(keyword, str):
            # A JSON object always has string keys. A mapping with another
            # kind of key cannot have come from JSON, and it must not take
            # the gate down on its way to being reported.
            errors.append(
                SchemaError(
                    path,
                    "schema keys must be strings",
                    keyword=repr(keyword),
                    schema_error=True,
                )
            )
            continue
        spec = _KEYWORD_SPECS.get(keyword)
        if spec is None:
            # An annotation, or a keyword this implementation does not
            # support — the latter is `SchemaValidator`'s business, not this
            # walk's, and it has its own rule for it.
            continue
        keyword_path = f"{path}.{keyword}"
        for suffix, message in _value_defects(spec, value):
            errors.append(
                SchemaError(
                    f"{keyword_path}{suffix}",
                    f"schema keyword {keyword!r} {message}",
                    keyword=keyword,
                    schema_error=True,
                )
            )
        for child_path, child in _sub_schemas(spec, value, keyword_path):
            _walk_schema_shape(child, child_path, errors)


def _keyword_value(schema: dict[str, Any], keyword: str) -> Any | None:
    """The value of ``schema[keyword]`` when the keyword can use it.

    ``None`` means *do not apply this keyword*: either the schema does not
    carry it, or it carries a value the keyword cannot use. A malformed
    keyword is reported by the shape walk; applying half of it would be worse
    than not applying it, and applying it at all is how this gate used to
    answer a question with a traceback.
    """
    if keyword not in schema:
        return None
    spec = _KEYWORD_SPECS.get(keyword)
    if spec is None:
        # An annotation: any value is acceptable and none of them constrain
        # the instance.
        return schema[keyword]
    value = schema[keyword]
    if _value_defects(spec, value):
        return None
    return value


def validate_instance(
    payload: Any,
    schema: JsonSchema,
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

    The two boolean schemas are supported in full: ``true`` accepts any value
    and ``false`` accepts none, so a ``false`` at any depth of the schema
    rejects the payload it governs.

    **A malformed keyword value is a schema error, never a crash.** A schema
    whose ``minLength`` is a string, or whose ``pattern`` is not a regex, is
    a broken contract, and the answer to a broken contract is an error the
    author can act on — not a ``TypeError`` out of the gate that decides
    whether an artifact may be committed. The rule is the same for every
    keyword (see :data:`_KEYWORD_SPECS`), it holds whether or not
    ``strict_keywords`` is set, and a malformed keyword is reported *and* not
    applied.

    Schema errors carry ``schema_error=True`` and are located by the path of
    the **schema** node they sit at (``$.properties.a.minLength``), while
    instance errors are located by the path of the **payload** (``$.a``).
    """
    if errors is None:
        errors = []
    if isinstance(schema, dict):
        # The schema's own shape is checked before the instance is: a keyword
        # whose value the keyword cannot use cannot be applied, and applying
        # it is exactly how this gate used to answer a question with a
        # traceback instead of a verdict.
        errors.extend(_schema_shape_errors(schema, path))
    return _validate_instance(payload, schema, path=path, errors=errors)


def _validate_instance(
    payload: Any,
    schema: Any,
    *,
    path: str,
    errors: list[SchemaError],
) -> list[SchemaError]:
    """Validate one instance against one schema node.

    The recursive half of :func:`validate_instance`. It does not re-check the
    schema's shape: the shape is a property of the schema, and the entry
    point has already walked the whole tree once.
    """
    # `True`/`{}` accept anything; `False` accepts nothing.
    #
    # The boolean check comes *first*, and it has to: `isinstance(False, dict)`
    # is False, so a shape guard ahead of it would swallow the forbidding
    # schema on its way out and accept the very payload the schema exists to
    # reject. The order of these two branches is the whole difference between
    # "a false schema forbids this value" and "a false schema forbids
    # nothing".
    if schema is False:
        errors.append(SchemaError(path, "schema forbids any value here"))
        return errors
    if not isinstance(schema, dict):
        # A boolean `true` schema accepts anything, and a node that is neither
        # a mapping nor a boolean is not ours to interpret.
        return errors

    if _keyword_value(schema, "nullable") and payload is None:
        return errors

    # -- type ------------------------------------------------------------
    if (expected := _keyword_value(schema, "type")) is not None:
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
    if (allowed := _keyword_value(schema, "enum")) is not None and payload not in allowed:
        errors.append(
            SchemaError(
                path,
                f"value {payload!r} is not one of the permitted values",
                permitted=list(allowed),
            )
        )

    # -- combinators -------------------------------------------------------
    if (subschemas := _keyword_value(schema, "allOf")) is not None:
        for index, sub in enumerate(subschemas):
            _validate_instance(payload, sub, path=f"{path}/allOf[{index}]", errors=errors)
    if (subschemas := _keyword_value(schema, "anyOf")) is not None and not any(
        not _validate_instance(payload, sub, path=path, errors=[]) for sub in subschemas
    ):
        errors.append(SchemaError(path, "value matched none of the anyOf alternatives"))
    if (subschemas := _keyword_value(schema, "oneOf")) is not None:
        matched = sum(
            1 for sub in subschemas if not _validate_instance(payload, sub, path=path, errors=[])
        )
        if matched != 1:
            errors.append(
                SchemaError(
                    path,
                    f"expected exactly one oneOf match, got {matched}",
                    matched=matched,
                )
            )
    if (negated := _keyword_value(schema, "not")) is not None and not _validate_instance(
        payload, negated, path=path, errors=[]
    ):
        errors.append(SchemaError(path, "value matched a schema it must not match"))

    # -- objects -----------------------------------------------------------
    if isinstance(payload, dict):
        properties: dict[str, Any] = _keyword_value(schema, "properties") or {}
        for field in _keyword_value(schema, "required") or []:
            if field not in payload:
                errors.append(
                    SchemaError(f"{path}.{field}", "required property is missing", field=field)
                )
        for key, value in payload.items():
            if key in properties:
                _validate_instance(value, properties[key], path=f"{path}.{key}", errors=errors)

        additional = _keyword_value(schema, "additionalProperties")
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
                    _validate_instance(value, additional, path=f"{path}.{key}", errors=errors)

        if (limit := _keyword_value(schema, "minProperties")) is not None and len(payload) < limit:
            errors.append(SchemaError(path, f"expected at least {limit} properties"))
        if (limit := _keyword_value(schema, "maxProperties")) is not None and len(payload) > limit:
            errors.append(SchemaError(path, f"expected at most {limit} properties"))

    # -- arrays ------------------------------------------------------------
    if isinstance(payload, (list, tuple)):
        # The guard is "is there an item schema", not "is it a mapping": a
        # boolean ``items`` schema is a schema, and ``items: false`` forbids
        # every item. Requiring a mapping here would skip it, and the array
        # would validate as though it had no item constraints at all.
        if (item_schema := _keyword_value(schema, "items")) is not None:
            for index, item in enumerate(payload):
                _validate_instance(item, item_schema, path=f"{path}[{index}]", errors=errors)
        if (prefix := _keyword_value(schema, "prefixItems")) is not None:
            for index, sub in enumerate(prefix):
                if index < len(payload):
                    _validate_instance(payload[index], sub, path=f"{path}[{index}]", errors=errors)
        if (limit := _keyword_value(schema, "minItems")) is not None and len(payload) < limit:
            errors.append(SchemaError(path, f"expected at least {limit} items, got {len(payload)}"))
        if (limit := _keyword_value(schema, "maxItems")) is not None and len(payload) > limit:
            errors.append(SchemaError(path, f"expected at most {limit} items, got {len(payload)}"))
        if _keyword_value(schema, "uniqueItems"):
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
        if (limit := _keyword_value(schema, "minLength")) is not None and len(payload) < limit:
            errors.append(SchemaError(path, f"string shorter than minLength {limit}"))
        if (limit := _keyword_value(schema, "maxLength")) is not None and len(payload) > limit:
            errors.append(SchemaError(path, f"string longer than maxLength {limit}"))
        if (pattern := _keyword_value(schema, "pattern")) is not None and (
            re.search(pattern, payload) is None
        ):
            # The pattern compiles: `_keyword_value` only returns a value the
            # shape check accepted, and the shape check compiles it.
            errors.append(SchemaError(path, f"string does not match pattern {pattern!r}"))

    # -- numbers -----------------------------------------------------------
    if isinstance(payload, (int, float)) and not isinstance(payload, bool):
        if (bound := _keyword_value(schema, "minimum")) is not None and payload < bound:
            errors.append(SchemaError(path, f"{payload} is below minimum {bound}"))
        if (bound := _keyword_value(schema, "maximum")) is not None and payload > bound:
            errors.append(SchemaError(path, f"{payload} is above maximum {bound}"))
        if (bound := _keyword_value(schema, "exclusiveMinimum")) is not None and payload <= bound:
            errors.append(SchemaError(path, f"{payload} must be greater than {bound}"))
        if (bound := _keyword_value(schema, "exclusiveMaximum")) is not None and payload >= bound:
            errors.append(SchemaError(path, f"{payload} must be less than {bound}"))
        if (
            (divisor := _keyword_value(schema, "multipleOf")) is not None
            and divisor
            and abs(payload / divisor - round(payload / divisor)) > 1e-9
        ):
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

    def unsupported_keywords(self, schema: JsonSchema) -> set[str]:
        """Keywords present in ``schema`` that this validator ignores."""
        found: set[str] = set()

        def walk(node: Any) -> None:
            if isinstance(node, dict):
                for key, value in node.items():
                    # A key that is not a string is not a keyword at all, and
                    # it is reported by the shape walk; asking ``str`` methods
                    # of it here is how a malformed schema used to take the
                    # gate down before it could answer.
                    if not isinstance(key, str):
                        continue
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
        schema: JsonSchema,
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
                            "schema uses unsupported keyword(s) "
                            f"{sorted(blocking or unsupported)}; "
                            "see docs/adr/0028 for the supported subset",
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

    def check(self, payload: Any, schema: JsonSchema) -> list[SchemaError]:
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
        skill_output_schema: JsonSchema | type[BaseModel] | None,
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
