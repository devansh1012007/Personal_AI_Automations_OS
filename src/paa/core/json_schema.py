"""The shape of a JSON Schema's keywords — one table, in one leaf module.

Three modules in this tree judge a payload against a JSON Schema:
:mod:`paa.models.base` (the model layer's structured output),
:mod:`paa.skills.contracts` (a skill's declared contract) and
:mod:`paa.validation.schema_validator` (the runtime's output gate). They all
ask one question about a keyword's value — *can this keyword use this value?*
— and the answer has to be the same in all three, or a schema that one of them
refuses is a schema the other two accept.

**Why this module exists, and why it is here.** The table used to live in
:mod:`paa.validation.schema_validator`, and the model layer read it from
there. That put the whole :mod:`paa.validation` package — and, through it,
the whole :mod:`paa.sandbox` package — into the model layer's import graph:
**16 extra modules and about 20 ms**, paid once per process by everything
that imports :mod:`paa.models`, which is nearly everything. The table is data,
not behaviour, and it has no business dragging a subsystem along with it.

**The milliseconds are a measurement of this edge and of nothing else.** The
edge was measured **once**, here: the tree as it was *before* the table moved
(extracted whole, so the comparison is the two trees and not two versions of
one file) against the tree as it is now, in **60 pairs of fresh interpreters**,
the two trees interleaved so that a drift in machine load falls on both sides
alike. The difference in the wall-clock of ``import paa.models.base``:
**+20.6 ms** as the median of the 60 pairwise differences, **+20.4 ms** as the
difference of the two medians (213.8 ms against 193.4 ms). The per-pair
spread ran from -6 ms to +49 ms — the host was running the wave in parallel,
and one pair is not a measurement — which is why the figure quoted is the
median and not the minimum.

The **16 modules** are exact, and do not move between hosts: the pre-move
tree loaded 32 ``paa.*`` modules and this one loads 17, and the difference is
the seven :mod:`paa.validation` modules and the nine :mod:`paa.sandbox`
modules the table used to drag in, with :mod:`paa.core.json_schema` added.

**Two older figures measured this same edge, and neither is quoted any more.**
W2-IMPR-02 measured **+26.4 ms** minimum / **+35.1 ms** median, on an earlier
host; W2-FIX-02 measured the same edge as the move's saving — **-19.6 /
-20.4 / -20.4 ms** — on this one. Three numbers for one edge is a record
that cannot be checked, so the figure is quoted **once**, here, and the two
other places that mention the cost (:mod:`paa.models.base` and the import
lock's own docstring) quote this one rather than taking a second measurement
of their own.

So the table lives here, in a **leaf**: this module imports nothing from this
project at all — only the standard library — so a consumer of the table pays
for the table and nothing else. The three validators import it from here and
**not from each other**, which is what lets the model layer and the skills
layer use the same rule without either of them depending on the other.

**What "the same rule" means is stated here, once.** A keyword whose value has
the wrong shape is a **broken contract**: it is reported, never applied, and
never raised as a ``TypeError``. The *kind* of a keyword's value is what says
whether a value is usable — a shape, not a range, except where the range is
part of the shape (``minLength`` is a non-negative integer, ``multipleOf`` is
strictly positive, because a value outside those ranges cannot be used by the
keyword either).

What stays in each validator is its own **convention**: the runtime's output
gate *reports* the defect alongside the instance's own errors, the model layer
*raises* :class:`~paa.models.base.MalformedSchemaError`, and the skills layer
*raises* ``ValueError``. The data is shared; the reporting is not.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

__all__ = [
    "ANNOTATION_KEYWORDS",
    "KEYWORD_SPECS",
    "JsonSchema",
    "KeywordSpec",
    "SchemaError",
    "json_type_name",
    "schema_shape_errors",
    "value_defects",
]

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


# ---------------------------------------------------------------------------
# The kinds of value a keyword may carry
# ---------------------------------------------------------------------------

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
class KeywordSpec:
    """How one supported keyword's value must look."""

    kind: str
    description: str
    """What the keyword's own value must be — "expects {description}"."""

    element_description: str = ""
    """What each element of a container keyword must be, where it has one."""


#: Every supported keyword whose value is constrained.
#:
#: The union of this table and :data:`ANNOTATION_KEYWORDS` is exactly the
#: validator's supported-keyword set, and a test asserts it — so a keyword
#: added to the supported set without a shape here is a loud failure rather
#: than a keyword whose value is never checked.
KEYWORD_SPECS: dict[str, KeywordSpec] = {
    "type": KeywordSpec(
        _KIND_TYPE, "a type name or a list of type names", "a type name"
    ),
    "properties": KeywordSpec(
        _KIND_SCHEMA_MAP, "a mapping of property names to schemas", "a schema"
    ),
    "required": KeywordSpec(_KIND_STRING_ARRAY, "a list of property names", "a string"),
    "additionalProperties": KeywordSpec(_KIND_SCHEMA, "a boolean or a schema"),
    "items": KeywordSpec(_KIND_SCHEMA, "a schema"),
    "prefixItems": KeywordSpec(_KIND_SCHEMA_ARRAY, "a list of schemas", "a schema"),
    "enum": KeywordSpec(_KIND_ARRAY, "a list of permitted values"),
    "minimum": KeywordSpec(_KIND_NUMBER, "a number"),
    "maximum": KeywordSpec(_KIND_NUMBER, "a number"),
    "exclusiveMinimum": KeywordSpec(_KIND_NUMBER, "a number"),
    "exclusiveMaximum": KeywordSpec(_KIND_NUMBER, "a number"),
    "multipleOf": KeywordSpec(_KIND_POSITIVE_NUMBER, "a number greater than zero"),
    "minLength": KeywordSpec(_KIND_NON_NEGATIVE_INT, "a non-negative integer"),
    "maxLength": KeywordSpec(_KIND_NON_NEGATIVE_INT, "a non-negative integer"),
    "pattern": KeywordSpec(_KIND_REGEX, "a valid regular expression"),
    "minItems": KeywordSpec(_KIND_NON_NEGATIVE_INT, "a non-negative integer"),
    "maxItems": KeywordSpec(_KIND_NON_NEGATIVE_INT, "a non-negative integer"),
    "uniqueItems": KeywordSpec(_KIND_BOOLEAN, "a boolean"),
    "minProperties": KeywordSpec(_KIND_NON_NEGATIVE_INT, "a non-negative integer"),
    "maxProperties": KeywordSpec(_KIND_NON_NEGATIVE_INT, "a non-negative integer"),
    "anyOf": KeywordSpec(_KIND_SCHEMA_ARRAY, "a list of schemas", "a schema"),
    "oneOf": KeywordSpec(_KIND_SCHEMA_ARRAY, "a list of schemas", "a schema"),
    "allOf": KeywordSpec(_KIND_SCHEMA_ARRAY, "a list of schemas", "a schema"),
    "not": KeywordSpec(_KIND_SCHEMA, "a schema"),
    "nullable": KeywordSpec(_KIND_BOOLEAN, "a boolean"),
    "definitions": KeywordSpec(
        _KIND_SCHEMA_MAP, "a mapping of names to schemas", "a schema"
    ),
    "$defs": KeywordSpec(_KIND_SCHEMA_MAP, "a mapping of names to schemas", "a schema"),
}

#: Supported keywords that constrain nothing: their value may be anything.
#:
#: ``const`` is here rather than in the constrained set because any JSON
#: value — including ``null`` — is a legal constant.
ANNOTATION_KEYWORDS: frozenset[str] = frozenset(
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


# ---------------------------------------------------------------------------
# Reading a value against a shape
# ---------------------------------------------------------------------------


def json_type_name(value: Any) -> str:
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


def _is_number(value: Any) -> bool:
    """A JSON number — and not a boolean, which is an ``int`` in Python."""
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _is_schema(value: Any) -> bool:
    """A schema node: a mapping, or one of the two boolean schemas."""
    return isinstance(value, (dict, bool))


def value_defects(spec: KeywordSpec, value: Any) -> list[tuple[str, str]]:
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
        return [("", f"expects {spec.description}, got {json_type_name(value)}")]

    if kind == _KIND_TYPE:
        if isinstance(value, str):
            return []
        if isinstance(value, list):
            return [
                (f"[{index}]", f"expects {spec.element_description}, got {json_type_name(item)}")
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
            (f"[{index}]", f"expects {spec.element_description}, got {json_type_name(item)}")
            for index, item in enumerate(value)
            if not isinstance(item, str)
        ]
    if kind == _KIND_SCHEMA:
        return [] if _is_schema(value) else whole()
    if kind == _KIND_SCHEMA_ARRAY:
        if not isinstance(value, list):
            return whole()
        return [
            (f"[{index}]", f"expects {spec.element_description}, got {json_type_name(item)}")
            for index, item in enumerate(value)
            if not _is_schema(item)
        ]
    if kind == _KIND_SCHEMA_MAP:
        if not isinstance(value, dict):
            return whole()
        return [
            (f".{key}", f"expects {spec.element_description}, got {json_type_name(item)}")
            for key, item in value.items()
            if not _is_schema(item)
        ]
    raise AssertionError(f"unknown keyword kind {kind!r}")  # pragma: no cover


def _sub_schemas(spec: KeywordSpec, value: Any, path: str) -> list[tuple[str, Any]]:
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


# ---------------------------------------------------------------------------
# Walking a schema
# ---------------------------------------------------------------------------


def schema_shape_errors(schema: Any, path: str = "$") -> list[SchemaError]:
    """Every way ``schema`` breaks the shape the supported keywords require.

    The walk descends into the schema's sub-schemas, so a malformed keyword
    is reported wherever it sits — not only where the instance happens to
    reach it. A schema is a contract in its own right: a broken constraint
    in a branch this payload never touches is still a broken contract, and
    the author needs to hear about it before the payload that does touch it
    arrives.

    Every defect it returns carries ``schema_error=True`` and is located by
    the path of the **schema** node it sits at (``$.properties.a.minLength``).
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
        spec = KEYWORD_SPECS.get(keyword)
        if spec is None:
            # An annotation, or a keyword this implementation does not
            # support — the latter is `SchemaValidator`'s business, not this
            # walk's, and it has its own rule for it.
            continue
        keyword_path = f"{path}.{keyword}"
        for suffix, message in value_defects(spec, value):
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
