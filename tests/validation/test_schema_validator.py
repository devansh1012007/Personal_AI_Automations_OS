"""The JSON Schema subset validator — RFC §13's deterministic gate.

``tests/validation/`` was an empty package, so this is the first coverage of
the subsystem. The cases collected here are the ones where a permissive
reading is a security hole rather than a convenience: a schema that *forbids* a
value has to actually forbid it, at every depth of the schema, and a validator
that cannot represent a boolean schema cannot enforce one.
"""

from __future__ import annotations

import ast
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

from paa.core import json_schema as json_schema_module
from paa.core.errors import SchemaValidationError
from paa.validation import schema_validator as schema_validator_module
from paa.validation.schema_validator import (
    SUPPORTED_KEYWORDS,
    SchemaError,
    SchemaValidator,
    validate_instance,
)

# ---------------------------------------------------------------------------
# The keyword inventory, derived from the module's own source
# ---------------------------------------------------------------------------


def _module_source(module: ModuleType) -> ast.Module:
    """The module's source, parsed.

    The inventory of keywords is read from the source rather than imported
    from the module, so the check below is about what the module *says* it
    supports — and so this file still imports against a source that does
    not have the shape table at all, which is what makes the reversal
    measurable.

    **Two modules are read, because the table has two homes.** The *shapes*
    live in :mod:`paa.core.json_schema` — the leaf the three validators
    share — and the *supported keyword set* lives in
    :mod:`paa.validation.schema_validator`, which is the module that says
    which keywords it implements. The check below asserts that the two
    halves are the same set, so reading them from two sources is the point:
    it is the join that matters.
    """
    path = Path(str(module.__file__))
    return ast.parse(path.read_text(encoding="utf-8"))


def _module_level_value(module: ModuleType, name: str) -> ast.expr | None:
    """The value assigned to a module-level ``name``."""
    for node in _module_source(module).body:
        if isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            if node.target.id == name:
                return node.value
        elif (
            isinstance(node, ast.Assign)
            and len(node.targets) == 1
            and isinstance(node.targets[0], ast.Name)
            and node.targets[0].id == name
        ):
            return node.value
    return None


def _string_constants(node: ast.AST | None) -> set[str]:
    """The string literals in a set/list/tuple/dict literal, or in a call's argument."""
    if node is None:
        return set()
    if isinstance(node, (ast.Set, ast.List, ast.Tuple)):
        return {
            element.value
            for element in node.elts
            if isinstance(element, ast.Constant) and isinstance(element.value, str)
        }
    if isinstance(node, ast.Dict):
        return {
            key.value
            for key in node.keys
            if isinstance(key, ast.Constant) and isinstance(key.value, str)
        }
    if isinstance(node, ast.Call) and node.args:
        return _string_constants(node.args[0])
    return set()


def _constrained_keywords() -> set[str]:
    """The keywords the shared table declares a *shape* for."""
    return _string_constants(_module_level_value(json_schema_module, "KEYWORD_SPECS"))


def _annotation_keywords() -> set[str]:
    """The keywords the shared table declares to constrain nothing."""
    return _string_constants(_module_level_value(json_schema_module, "ANNOTATION_KEYWORDS"))

# -- boolean schemas --------------------------------------------------------


def test_false_schema_forbids_every_value() -> None:
    """``false`` is a schema that forbids the value it governs.

    This is the case the shape guard used to swallow: ``isinstance(False,
    dict)`` is ``False``, so a "is this a mapping?" check ahead of the boolean
    check returned "no errors" — the validator accepted the payload the
    forbidding schema was written to reject.
    """
    errors = validate_instance({"a": 1}, False)

    assert len(errors) == 1
    assert errors[0].message == "schema forbids any value here"
    assert errors[0].path == "$"


def test_true_schema_accepts_every_value() -> None:
    """``true`` is a schema that permits every value."""
    assert validate_instance({"a": 1}, True) == []
    assert validate_instance("anything at all", True) == []
    assert validate_instance(None, True) == []


def test_false_subschema_forbids_the_property() -> None:
    """A ``false`` nested under ``properties`` forbids that property only."""
    schema = {"type": "object", "properties": {"a": False, "b": {"type": "string"}}}

    errors = validate_instance({"a": 1, "b": "fine"}, schema)

    assert [error.path for error in errors] == ["$.a"]
    assert errors[0].message == "schema forbids any value here"


def test_false_item_schema_forbids_every_item() -> None:
    schema = {"type": "array", "items": False}

    errors = validate_instance([1, 2], schema)

    assert [error.path for error in errors] == ["$[0]", "$[1]"]


def test_non_schema_node_is_not_interpreted() -> None:
    """A node that is neither a mapping nor a boolean is not ours to read.

    Unchanged behaviour, pinned so the "not a schema" branch stays a decision
    rather than an accident.

    The two values are annotated as ``Any`` because they are deliberately
    *not* schemas: ``JsonSchema`` is ``dict[str, Any] | bool``, so the
    argument is a type error by construction, and the point of the test is
    what the validator does with it.
    """
    not_a_schema: Any = "object"
    also_not_a_schema: Any = 42

    assert validate_instance({"a": 1}, not_a_schema) == []
    assert validate_instance({"a": 1}, also_not_a_schema) == []


# -- the raising and collecting surfaces ------------------------------------


def test_validate_raises_on_a_false_schema() -> None:
    validator = SchemaValidator()

    with pytest.raises(SchemaValidationError) as excinfo:
        validator.validate({"a": 1}, False, schema_name="payload")

    assert excinfo.value.errors[0]["message"] == "schema forbids any value here"


def test_check_collects_a_false_schema_error() -> None:
    errors = SchemaValidator().check({"a": 1}, False)

    assert len(errors) == 1
    assert isinstance(errors[0], SchemaError)
    assert errors[0].path == "$"


def test_validate_tool_output_rejects_a_false_output_schema() -> None:
    """A skill whose output schema forbids everything produces no valid output."""
    with pytest.raises(SchemaValidationError):
        SchemaValidator().validate_tool_output(False, {"a": 1}, skill_name="demo")


def test_validate_tool_output_accepts_a_true_output_schema() -> None:
    payload = {"a": 1}

    assert SchemaValidator().validate_tool_output(True, payload, skill_name="demo") is payload


# -- combinators ------------------------------------------------------------


def test_any_of_accepts_one_matching_alternative() -> None:
    schema = {"anyOf": [{"type": "string"}, {"type": "integer"}]}

    assert validate_instance("text", schema) == []
    assert validate_instance(7, schema) == []


def test_any_of_rejects_when_no_alternative_matches() -> None:
    schema = {"anyOf": [{"type": "string"}, {"type": "integer"}]}

    errors = validate_instance([], schema)

    assert len(errors) == 1
    assert errors[0].message == "value matched none of the anyOf alternatives"


def test_one_of_requires_exactly_one_match() -> None:
    schema = {"oneOf": [{"type": "integer"}, {"type": "number"}]}

    # An integer matches both alternatives, so "exactly one" fails.
    errors = validate_instance(7, schema)
    assert len(errors) == 1
    assert errors[0].message == "expected exactly one oneOf match, got 2"

    # A string matches neither.
    errors = validate_instance("seven", schema)
    assert len(errors) == 1
    assert errors[0].message == "expected exactly one oneOf match, got 0"


def test_multiple_of_is_enforced() -> None:
    schema = {"type": "number", "multipleOf": 2}

    assert validate_instance(8, schema) == []
    assert len(validate_instance(7, schema)) == 1


def test_multiple_of_zero_is_reported_not_ignored() -> None:
    """A zero divisor is a *broken contract*, not a constraint to ignore.

    It used to be a silent no-op — ``multipleOf: 0`` was read as "no
    multiple-of constraint at all" — which is the failure mode this module
    exists to prevent elsewhere: the author believes a constraint is in force
    and it is not. It is now reported as a schema error, and the divisor is
    never applied, so it is still not a ``ZeroDivisionError``.

    The change is deliberate and it is the *same* rule every other keyword
    gets: a value the keyword cannot use is a schema error, and a keyword
    whose value is malformed is reported rather than applied.
    """
    errors = validate_instance(7, {"type": "number", "multipleOf": 0})

    assert len(errors) == 1
    assert errors[0].path == "$.multipleOf"
    assert errors[0]["keyword"] == "multipleOf"


def test_enum_rejects_an_unlisted_value() -> None:
    schema = {"enum": ["a", "b"]}

    assert validate_instance("a", schema) == []
    assert len(validate_instance("c", schema)) == 1


def test_not_rejects_a_matching_value() -> None:
    schema = {"not": {"type": "string"}}

    assert validate_instance(7, schema) == []
    assert len(validate_instance("text", schema)) == 1


def test_not_false_accepts_every_value() -> None:
    """``not: false`` is satisfied by every instance.

    ``not`` requires the instance to *fail* the subschema, and ``false``
    fails every instance, so every instance satisfies ``not: false``. Before
    the boolean branch was honoured, ``false`` read as "matches everything",
    which made ``not: false`` reject everything — the opposite of JSON
    Schema's meaning. This is the one place where honouring ``false`` made
    the validator *more* permissive, so it is pinned rather than left to the
    order of two branches.
    """
    assert validate_instance(7, {"not": False}) == []
    assert validate_instance({"a": 1}, {"not": False}) == []


def test_not_true_rejects_every_value() -> None:
    """``not: true`` is satisfied by no instance — the complement."""
    errors = validate_instance(7, {"not": True})

    assert len(errors) == 1
    assert errors[0].message == "value matched a schema it must not match"


# -- the keyword the validator refuses to guess ------------------------------


def test_ref_is_rejected_rather_than_ignored() -> None:
    with pytest.raises(SchemaValidationError):
        SchemaValidator().validate({"a": 1}, {"$ref": "#/definitions/x"})


def test_unsupported_keyword_under_strict_mode_is_rejected() -> None:
    with pytest.raises(SchemaValidationError):
        SchemaValidator(strict_keywords=True).validate({"a": 1}, {"if": {"a": 1}})


# -- a malformed keyword value is a schema error, never a crash ---------------
#
# The gate that decides whether an artifact may be committed used to answer
# `{"type": "string", "minLength": "abc"}` with a raw ``TypeError``: '<' not
# supported between instances of 'int' and 'str'``. A crash is neither a pass
# nor a fail — it is the gate absent — and the value that caused it is
# *model-supplied*, so the crash was reachable by the very party the gate
# exists to hold back.
#
# The policy, stated once and applied to every keyword:
#
#   * a keyword whose value has the wrong shape is a **schema error**, reported
#     in the same shape as any other invalid schema, and never raised;
#   * the malformed keyword is reported **and not applied** — half a constraint
#     is not a constraint;
#   * the rule does not depend on ``strict_keywords``: that flag answers
#     "does this implementation *understand* this keyword", and a malformed
#     value is a broken contract, which is a different question. A keyword
#     that is understood and given a nonsense value is the case where the
#     author's belief and the gate's behaviour diverge, so it is an error in
#     both modes.

#: ``(schema, payload, keyword)`` — every way a keyword's value can be wrong.
#:
#: The table is the *whole* policy, so it is written out rather than
#: generated: a reader must be able to see that the same rule covers
#: ``minLength`` and ``maxItems`` and ``pattern`` without reading the
#: implementation.
MALFORMED_KEYWORD_VALUES: list[tuple[dict[str, object], object, str]] = [
    ({"type": "string", "minLength": "abc"}, "x", "minLength"),
    ({"type": "string", "maxLength": "abc"}, "x", "maxLength"),
    ({"type": "string", "minLength": -1}, "x", "minLength"),
    ({"type": "array", "minItems": "2"}, [1], "minItems"),
    ({"type": "array", "maxItems": "2"}, [1], "maxItems"),
    ({"type": "array", "minItems": -1}, [1], "minItems"),
    ({"type": "object", "minProperties": "1"}, {}, "minProperties"),
    ({"type": "object", "maxProperties": "1"}, {}, "maxProperties"),
    ({"type": "number", "minimum": "x"}, 1, "minimum"),
    ({"type": "number", "maximum": "x"}, 1, "maximum"),
    ({"type": "number", "exclusiveMinimum": "x"}, 1, "exclusiveMinimum"),
    ({"type": "number", "exclusiveMaximum": "x"}, 1, "exclusiveMaximum"),
    ({"type": "number", "multipleOf": "2"}, 4, "multipleOf"),
    ({"type": "number", "multipleOf": 0}, 4, "multipleOf"),
    ({"type": "string", "pattern": 5}, "x", "pattern"),
    ({"type": "string", "pattern": "["}, "x", "pattern"),
    ({"type": "string", "pattern": "a{99999999999}"}, "x", "pattern"),
    ({"enum": 5}, "x", "enum"),
    ({"required": 5}, {}, "required"),
    ({"required": ["a", 5]}, {}, "required"),
    ({"properties": 5}, {}, "properties"),
    ({"properties": {"a": 5}}, {"a": 1}, "properties"),
    ({"additionalProperties": 5}, {"a": 1}, "additionalProperties"),
    ({"items": 5}, [1], "items"),
    ({"prefixItems": 5}, [1], "prefixItems"),
    ({"prefixItems": [5]}, [1], "prefixItems"),
    ({"anyOf": 5}, 1, "anyOf"),
    ({"anyOf": [5]}, 1, "anyOf"),
    ({"oneOf": 5}, 1, "oneOf"),
    ({"allOf": 5}, 1, "allOf"),
    ({"not": 5}, 1, "not"),
    ({"uniqueItems": "yes"}, [1, 2], "uniqueItems"),
    ({"nullable": "yes"}, None, "nullable"),
    ({"type": 5}, 1, "type"),
    ({"type": ["string", 5]}, 1, "type"),
    ({"definitions": {"x": 5}}, {}, "definitions"),
    ({"$defs": {"x": 5}}, {}, "$defs"),
]


@pytest.mark.parametrize(("schema", "payload", "keyword"), MALFORMED_KEYWORD_VALUES)
def test_malformed_keyword_value_is_a_schema_error(
    schema: dict[str, object], payload: object, keyword: str
) -> None:
    """Every malformed keyword value is reported, and none of them raises."""
    errors = validate_instance(payload, schema)

    assert errors, f"{schema} should have been reported as an invalid schema"
    schema_errors = [error for error in errors if error.get("schema_error")]
    assert schema_errors, f"{schema} produced no schema error: {errors}"
    assert keyword in {error["keyword"] for error in schema_errors}


@pytest.mark.parametrize(("schema", "payload", "keyword"), MALFORMED_KEYWORD_VALUES)
def test_malformed_keyword_value_never_raises(
    schema: dict[str, object], payload: object, keyword: str
) -> None:
    """The gate answers with a verdict for a malformed schema, never a traceback.

    Asserted through the *collecting* surface and the *raising* one, because
    the crash this replaces escaped from both.
    """
    validate_instance(payload, schema)

    with pytest.raises(SchemaValidationError) as excinfo:
        SchemaValidator().validate(payload, schema)

    assert excinfo.value.errors


def test_malformed_keyword_value_is_rejected_in_both_keyword_modes() -> None:
    """``strict_keywords`` is about coverage; a broken value is an error either way.

    ``strict_keywords`` answers "does this implementation understand this
    keyword?" — a *coverage* question, and the lenient default exists so a
    schema carrying documentation-only extensions still works. A keyword that
    *is* understood and given a nonsense value is a different failure: the
    author believes a constraint is in force and it cannot be. Leaving that
    to the lenient mode would mean the default silently ignores a broken
    constraint — which is the failure the whole module exists to prevent.
    """
    schema = {"type": "string", "minLength": "abc"}

    for validator in (SchemaValidator(), SchemaValidator(strict_keywords=True)):
        with pytest.raises(SchemaValidationError) as excinfo:
            validator.validate("x", schema)
        assert excinfo.value.errors[0]["keyword"] == "minLength"


def test_a_malformed_keyword_is_reported_and_not_applied() -> None:
    """The malformed keyword is skipped, not half-applied.

    ``minLength: "abc"`` cannot be applied, and applying the *other* half of
    a broken constraint is not what "the schema is broken" means. The
    payload therefore produces the schema error and nothing else.
    """
    errors = validate_instance("x", {"type": "string", "minLength": "abc"})

    assert len(errors) == 1
    assert errors[0]["schema_error"] is True
    assert errors[0]["path"] == "$.minLength"


def test_a_malformed_keyword_is_reported_even_when_the_payload_never_reaches_it() -> None:
    """A broken contract is broken whether or not this payload touches it.

    The schema is the contract, not the payload. A keyword that cannot be
    applied is reported whatever the instance looks like — otherwise the
    author only hears about it once a payload happens to reach the branch,
    which is one round trip later than it needs to be.
    """
    errors = validate_instance(5, {"type": "string", "minLength": "abc"})

    schema_errors = [error for error in errors if error.get("schema_error")]
    assert [error["keyword"] for error in schema_errors] == ["minLength"]


def test_a_nested_malformed_keyword_is_reported_at_its_own_path() -> None:
    """The error names the schema node it sits at, not the payload's."""
    schema = {
        "type": "object",
        "properties": {"a": {"type": "string", "maxLength": "abc"}},
    }

    errors = validate_instance({"a": "x"}, schema)

    assert len(errors) == 1
    assert errors[0].path == "$.properties.a.maxLength"
    assert errors[0]["keyword"] == "maxLength"


def test_a_schema_error_and_an_instance_error_are_both_reported() -> None:
    """The module collects failures rather than stopping at the first.

    One round trip per error is the failure mode the module's own docstring
    names, so a broken schema and a broken payload are reported together.
    """
    errors = validate_instance(5, {"type": "string", "minLength": "abc"})

    assert {error["path"] for error in errors} == {"$.minLength", "$"}


def test_a_schema_with_a_non_string_key_is_reported_not_raised() -> None:
    """A mapping with a non-string key cannot have come from JSON.

    It used to raise ``AttributeError`` out of the keyword scan — the same
    "the gate is absent" failure as the ``TypeError`` this file is about,
    one level up: ``str.startswith`` was called on the key.
    """
    malformed: Any = {5: "x"}
    errors = validate_instance({}, malformed)

    assert len(errors) == 1
    assert errors[0]["schema_error"] is True
    assert "keys must be strings" in errors[0].message


# -- the fix must not make a valid schema fail -------------------------------

#: ``(schema, payload, expected error count)`` for schemas that are *valid*.
#:
#: Pinned because the fix is a change to the validator's core, and the only
#: acceptable blast radius is zero: a schema that is correct must still
#: validate exactly the instances it validated before.
VALID_SCHEMA_CASES: list[tuple[dict[str, object], object, int]] = [
    ({"type": "string", "minLength": 2}, "abc", 0),
    ({"type": "string", "minLength": 2}, "a", 1),
    ({"type": "string", "maxLength": 2}, "abc", 1),
    ({"type": "string", "pattern": "^a"}, "abc", 0),
    ({"type": "string", "pattern": "^a"}, "bbc", 1),
    ({"type": "number", "minimum": 0}, 0, 0),
    ({"type": "number", "minimum": 0}, -1, 1),
    ({"type": "number", "maximum": 10}, 11, 1),
    ({"type": "number", "exclusiveMinimum": 0}, 0, 1),
    ({"type": "number", "exclusiveMaximum": 10}, 10, 1),
    ({"type": "number", "multipleOf": 2}, 8, 0),
    ({"type": "number", "multipleOf": 2}, 7, 1),
    ({"type": "array", "minItems": 2}, [1], 1),
    ({"type": "array", "maxItems": 2}, [1, 2, 3], 1),
    ({"type": "array", "uniqueItems": True}, [1, 1], 1),
    ({"type": "array", "uniqueItems": True}, [1, 2], 0),
    ({"type": "object", "minProperties": 1}, {}, 1),
    ({"type": "object", "maxProperties": 1}, {"a": 1, "b": 2}, 1),
    ({"type": "object", "required": ["a"]}, {}, 1),
    ({"type": "object", "required": ["a"]}, {"a": 1}, 0),
    ({"type": "object", "additionalProperties": False}, {"a": 1}, 1),
    ({"type": "object", "additionalProperties": False}, {}, 0),
    ({"type": "object", "properties": {"a": {"type": "string"}}}, {"a": 1}, 1),
    ({"type": "object", "properties": {"a": {"type": "string"}}}, {"a": "x"}, 0),
    ({"type": ["string", "null"]}, None, 0),
    ({"type": ["string", "null"]}, "x", 0),
    ({"type": ["string", "null"]}, 1, 1),
    ({"enum": ["a", "b"]}, "a", 0),
    ({"enum": ["a", "b"]}, "c", 1),
    ({"const": "fixed"}, "fixed", 0),
    ({"const": "fixed"}, "other", 1),
    ({"nullable": True}, None, 0),
    ({"anyOf": [{"type": "string"}, {"type": "integer"}]}, 1, 0),
    ({"anyOf": [{"type": "string"}, {"type": "integer"}]}, 1.5, 1),
    ({"oneOf": [{"type": "integer"}, {"type": "string"}]}, 1, 0),
    ({"oneOf": [{"type": "integer"}, {"type": "string"}]}, 1.5, 1),
    ({"allOf": [{"type": "string"}, {"minLength": 2}]}, "ab", 0),
    ({"allOf": [{"type": "string"}, {"minLength": 2}]}, "a", 1),
    ({"not": {"type": "string"}}, 1, 0),
    ({"not": {"type": "string"}}, "x", 1),
    ({"type": "array", "items": {"type": "integer"}}, [1, 2], 0),
    ({"type": "array", "items": {"type": "integer"}}, [1, "x"], 1),
    ({"type": "array", "prefixItems": [{"type": "string"}]}, ["a"], 0),
    ({"type": "array", "prefixItems": [{"type": "string"}]}, [1], 1),
]


@pytest.mark.parametrize(("schema", "payload", "expected"), VALID_SCHEMA_CASES)
def test_valid_schemas_validate_exactly_what_they_validated_before(
    schema: dict[str, object], payload: object, expected: int
) -> None:
    """The fix changes the broken case only.

    Every case here is a schema that is *correct*, so it must produce the
    same verdict as before the fix — same errors, same count. A change in
    any of them is a behaviour change on a valid contract, which the fix is
    not allowed to make.
    """
    errors = validate_instance(payload, schema)

    assert len(errors) == expected
    assert not [error for error in errors if error.get("schema_error")]


# -- the shape table cannot drift from the supported set ----------------------


def test_every_supported_keyword_has_a_declared_shape() -> None:
    """A keyword with no declared shape is a keyword whose value is unchecked.

    The shape table and :data:`SUPPORTED_KEYWORDS` are the two halves of one
    promise: every keyword this implementation claims to support has a
    declared shape, and every shape belongs to a keyword it supports. A
    keyword added to the supported set without a shape here would be one
    whose malformed value is never reported — the exact hole this fix
    closed — so the two are asserted to be the same set.

    Both sides are read from the module's *source*, so the check is about
    what the module declares rather than about a copy of the declaration.
    """
    constrained = _constrained_keywords()
    annotations = _annotation_keywords()

    assert constrained & annotations == set()
    assert constrained | annotations == set(SUPPORTED_KEYWORDS)
    assert _string_constants(
        _module_level_value(schema_validator_module, "SUPPORTED_KEYWORDS")
    ) == set(SUPPORTED_KEYWORDS)


def test_the_malformed_table_covers_every_constrained_keyword() -> None:
    """The policy covers every keyword, not the ones that happened to crash.

    A keyword left out of the malformed table is a keyword whose broken value
    is silently accepted — which is how the crash survived in the first
    place, one keyword at a time.
    """
    covered = {keyword for _, _, keyword in MALFORMED_KEYWORD_VALUES}

    assert covered == _constrained_keywords()


def test_no_constrained_keyword_accepts_every_nonsense_value() -> None:
    """The rule is uniform: a value the keyword cannot use is rejected.

    The candidates are values no keyword of the matching kind can use — a
    float where an integer belongs, a string where a number belongs, a list
    where a schema belongs. Every keyword must reject at least one of them,
    which is the "same rule for every keyword" property stated as a test
    rather than as a promise.
    """
    candidates: list[Any] = [1.5, "not a number", ["not", "a", "schema"], {"not": "a schema"}]

    for keyword in sorted(_constrained_keywords()):
        rejected = [
            candidate
            for candidate in candidates
            if validate_instance("x", {keyword: candidate})
        ]
        assert rejected, f"{keyword} accepted every nonsense value"
