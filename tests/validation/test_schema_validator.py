"""The JSON Schema subset validator — RFC §13's deterministic gate.

``tests/validation/`` was an empty package, so this is the first coverage of
the subsystem. The cases collected here are the ones where a permissive
reading is a security hole rather than a convenience: a schema that *forbids* a
value has to actually forbid it, at every depth of the schema, and a validator
that cannot represent a boolean schema cannot enforce one.
"""

from __future__ import annotations

import pytest

from paa.core.errors import SchemaValidationError
from paa.validation.schema_validator import (
    SchemaError,
    SchemaValidator,
    validate_instance,
)

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
    """
    assert validate_instance({"a": 1}, "object") == []
    assert validate_instance({"a": 1}, 42) == []


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
    # A zero divisor is a no-op, not a ZeroDivisionError.
    assert validate_instance(7, {"type": "number", "multipleOf": 0}) == []


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
