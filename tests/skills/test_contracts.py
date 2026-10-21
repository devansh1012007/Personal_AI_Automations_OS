"""The contract-side schema subset: ``schema_errors`` and the shape check.

``schema_errors`` is the validator the Unified Skill Adapter runs against a
skill's *declared* ``input_schema`` before the skill runs (RFC §8.2 step 2),
so it is a control rather than a convenience. The cases pinned here are the
ones where a permissive reading is a hole: a schema that forbids a value must
forbid it, and a schema that the runtime cannot interpret must not be silently
read as "anything goes" in the direction that accepts.
"""

from __future__ import annotations

import pytest

from paa.skills.contracts import schema_errors, validate_json_schema_shape

# -- boolean schemas --------------------------------------------------------


def test_false_schema_forbids_every_value() -> None:
    """``false`` forbids the value it governs.

    The shape guard used to swallow this: ``isinstance(False, dict)`` is
    ``False``, so the "is this a mapping?" branch returned "no errors" and the
    payload the forbidding schema was written to reject was accepted.
    """
    errors = schema_errors({"a": 1}, False)

    assert len(errors) == 1
    assert errors[0] == "$: schema forbids any value here"


def test_true_schema_accepts_every_value() -> None:
    assert schema_errors({"a": 1}, True) == []
    assert schema_errors("anything at all", True) == []


def test_false_subschema_forbids_the_property() -> None:
    schema = {"type": "object", "properties": {"a": False}}

    errors = schema_errors({"a": 1}, schema)

    assert errors == ["$.a: schema forbids any value here"]


def test_false_item_schema_forbids_every_item() -> None:
    schema = {"type": "array", "items": False}

    errors = schema_errors([1, 2], schema)

    assert errors == [
        "$[0]: schema forbids any value here",
        "$[1]: schema forbids any value here",
    ]


def test_non_schema_node_is_not_interpreted() -> None:
    """A node that is neither a mapping nor a boolean is not ours to read."""
    assert schema_errors({"a": 1}, "object") == []


# -- the supported subset ---------------------------------------------------


def test_type_mismatch_is_reported() -> None:
    errors = schema_errors("text", {"type": "integer"})

    assert errors == ["$: expected type 'integer', got str"]


def test_enum_mismatch_is_reported() -> None:
    assert schema_errors("a", {"enum": ["a", "b"]}) == []
    assert schema_errors("c", {"enum": ["a", "b"]}) == [
        "$: value is not one of the 2 permitted values"
    ]


def test_required_property_is_reported() -> None:
    errors = schema_errors({}, {"type": "object", "required": ["a"]})

    assert errors == ["$: missing required property 'a'"]


def test_additional_properties_are_reported() -> None:
    schema = {"type": "object", "properties": {"a": {}}, "additionalProperties": False}

    errors = schema_errors({"a": 1, "b": 2}, schema)

    assert errors == ["$: unexpected property 'b'"]


def test_string_bounds_are_reported() -> None:
    assert schema_errors("abc", {"type": "string", "minLength": 5}) == [
        "$: shorter than minLength 5"
    ]
    assert schema_errors("abcdef", {"type": "string", "maxLength": 3}) == [
        "$: longer than maxLength 3"
    ]


def test_pattern_mismatch_is_reported() -> None:
    """``pattern`` is part of the subset the contract validator implements."""
    assert schema_errors("bcd", {"type": "string", "pattern": "^a"}) == [
        "$: does not match the declared pattern"
    ]
    assert schema_errors("abc", {"type": "string", "pattern": "^a"}) == []


def test_number_bounds_are_reported() -> None:
    assert schema_errors(1, {"type": "number", "minimum": 5}) == ["$: below minimum 5"]
    assert schema_errors(9, {"type": "number", "maximum": 5}) == ["$: above maximum 5"]


def test_array_bounds_are_reported() -> None:
    assert schema_errors([1], {"type": "array", "minItems": 2}) == [
        "$: expected at least 2 items, got 1"
    ]
    assert schema_errors([1, 2, 3], {"type": "array", "maxItems": 2}) == [
        "$: expected at most 2 items, got 3"
    ]


# -- the registration-time shape check --------------------------------------


def test_shape_check_rejects_a_non_object_root() -> None:
    with pytest.raises(ValueError, match="must be a JSON Schema object"):
        validate_json_schema_shape(True, field_name="output_schema")


def test_shape_check_rejects_a_non_object_root_type() -> None:
    with pytest.raises(ValueError, match="must declare 'type': 'object'"):
        validate_json_schema_shape({"type": "array"}, field_name="output_schema")


def test_shape_check_accepts_an_object_schema() -> None:
    schema = {"type": "object", "properties": {"a": {"type": "string"}}}

    assert validate_json_schema_shape(schema, field_name="output_schema") == schema
