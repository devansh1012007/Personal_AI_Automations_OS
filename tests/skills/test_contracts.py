"""The contract-side schema subset: ``schema_errors`` and the shape check.

``schema_errors`` is the validator the Unified Skill Adapter runs against a
skill's *declared* ``input_schema`` before the skill runs (RFC §8.2 step 2),
so it is a control rather than a convenience. The cases pinned here are the
ones where a permissive reading is a hole: a schema that forbids a value must
forbid it, and a schema that the runtime cannot interpret must not be silently
read as "anything goes" in the direction that accepts.

**A malformed keyword is a schema error here too.** The validator used to
*ignore* a keyword whose value it could not use — ``isinstance(minimum := \
schema.get("minimum"), int)`` drops the constraint when ``minimum`` is a
string — so the payload came back "valid" against a schema whose constraint
had been quietly removed. That is worse than a crash: the instance was judged
against a schema nobody wrote. The rule is now the same one the model layer's
validator and the runtime's output gate apply: a malformed keyword is
*reported*, never silently ignored, and the way this module reports it is the
``ValueError`` it already raises for a bad schema *shape*.
"""

from __future__ import annotations

from typing import Any

import pytest

from paa.core.errors import SkillContractError
from paa.skills.contracts import (
    SkillContract,
    schema_errors,
    validate_json_schema_shape,
)

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


# -- a malformed keyword is a schema error -----------------------------------

#: Every keyword :func:`schema_errors` reads, with a value the keyword cannot
#: use.
#:
#: Enumerated from the module's own source: ``type``, ``enum``,
#: ``properties``, ``required``, ``additionalProperties``, ``items``,
#: ``minItems``/``maxItems``, ``minLength``/``maxLength``, ``pattern``,
#: ``minimum``/``maximum``.
MALFORMED_KEYWORDS: tuple[tuple[str, Any], ...] = (
    ("type", 5),
    ("enum", "abc"),
    ("properties", "not-a-mapping"),
    ("required", "a"),
    ("additionalProperties", 5),
    ("items", "not-a-schema"),
    ("minItems", "2"),
    ("maxItems", "2"),
    ("minLength", "2"),
    ("maxLength", "2"),
    ("pattern", "["),
    ("minimum", "2"),
    ("maximum", "2"),
)


def _schema_with(keyword: str, value: Any) -> dict[str, Any]:
    """A schema whose ``keyword`` carries ``value``.

    ``type`` gets the value on its own, because a schema cannot say
    ``"type": "object"`` and ``"type": 5`` at the same time.
    """
    if keyword == "type":
        return {"type": value}
    return {"type": "object", keyword: value}


@pytest.mark.parametrize(("keyword", "value"), MALFORMED_KEYWORDS)
def test_a_malformed_keyword_is_reported_and_not_ignored(keyword: str, value: Any) -> None:
    """The instance is never judged against a schema whose constraint was dropped.

    Before the fix every one of these was *silently* ignored: the guards are
    ``isinstance(...)`` checks, so a value of the wrong shape simply turned
    the constraint off, and the payload came back "valid". A validator that
    answers a question about a schema it could not read is worse than one that
    refuses.
    """
    with pytest.raises(ValueError, match="keyword whose value cannot be used"):
        schema_errors({"a": "x"}, _schema_with(keyword, value))


def test_a_malformed_keyword_the_module_does_not_read_is_still_refused() -> None:
    """The shared table constrains the combinators too.

    This module does not implement ``anyOf``, so it never applied it — but a
    value the keyword cannot use is a broken contract whether or not *this*
    validator would have applied it, and the rule is the same in all three
    validators.
    """
    with pytest.raises(ValueError, match="keyword whose value cannot be used"):
        schema_errors({"a": "x"}, {"type": "object", "anyOf": "not-a-list"})


def test_the_malformed_keyword_is_named_in_the_error() -> None:
    """The author has to be able to see which keyword was wrong."""
    with pytest.raises(ValueError) as caught:
        schema_errors("abc", {"type": "string", "minLength": "5"})

    message = str(caught.value)
    assert "minLength" in message
    assert "non-negative integer" in message


def test_the_shape_check_refuses_a_malformed_keyword() -> None:
    """Registration is where a broken contract is caught.

    A contract that parses is a contract every downstream layer may rely on,
    so the shape check walks the whole schema — a broken constraint in a
    branch this payload never touches is still a broken contract.
    """
    with pytest.raises(ValueError, match="keyword whose value cannot be used"):
        validate_json_schema_shape(
            {"type": "object", "properties": {"a": {"type": "string", "minLength": "5"}}},
            field_name="output_schema",
        )


def _contract_payload(output_schema: dict[str, Any]) -> dict[str, Any]:
    """A contract that is valid apart from its ``output_schema``."""
    return {
        "skill_name": "example.skill",
        "provider": "native",
        "version": "1.0.0",
        "description": "A skill that exists to be validated by this suite.",
        "input_schema": {"type": "object", "properties": {}},
        "output_schema": output_schema,
        "risk_profile": 0.1,
        "invocation": {"kind": "native_callable", "target": "example.skill:run"},
    }


def test_a_contract_with_a_malformed_keyword_is_not_registrable() -> None:
    """The broken contract never reaches the registry.

    A contract is refused at registration rather than producing a confusing
    failure at invocation — and once it is refused here, the adapter can
    never judge a payload against it.
    """
    with pytest.raises(SkillContractError) as caught:
        SkillContract.parse(
            _contract_payload({"type": "object", "properties": {"a": {"minLength": "5"}}})
        )

    findings = caught.value.details.get("findings", [])
    assert any("keyword" in str(finding) for finding in findings), findings


def test_a_contract_turns_a_malformed_keyword_into_a_contract_error() -> None:
    """The invocation path reports it the way this module reports everything.

    A defect in the *contract* is not the payload's fault, and reporting it as
    one would punish the skill for a schema the runtime wrote.
    """
    contract = SkillContract.model_construct(
        **_contract_payload({"type": "object", "properties": {"a": {"minLength": "5"}}})
    )

    with pytest.raises(SkillContractError) as caught:
        contract.validate_output({"a": "x"})

    assert "output_schema" in str(caught.value)
    assert caught.value.details["skill_name"] == "example.skill"

    other = SkillContract.model_construct(
        **{
            **_contract_payload({"type": "object", "properties": {}}),
            "input_schema": {"type": "object", "properties": {"a": {"minLength": "5"}}},
        }
    )

    with pytest.raises(SkillContractError) as caught:
        other.validate_input({"a": "x"})

    assert "input_schema" in str(caught.value)


# -- a valid schema answers exactly as it did before -------------------------

#: What :func:`schema_errors` answered **before** the change, for a corpus of
#: valid schemas. The rows were captured from ``4c9300d``'s source: a valid
#: schema must behave exactly as before, and these are the answers that must
#: not move.
VALID_SCHEMA_ANSWERS: tuple[tuple[dict[str, Any], Any, list[str]], ...] = (
    (
        {},
        {},
        [],
    ),
    (
        {},
        '',
        [],
    ),
    (
        {'type': 'object'},
        [],
        ["$: expected type 'object', got list"],
    ),
    (
        {'type': 'object'},
        {},
        [],
    ),
    (
        {'type': 'object', 'properties': {}},
        [],
        ["$: expected type 'object', got list"],
    ),
    (
        {'type': 'object', 'properties': {}},
        {},
        [],
    ),
    (
        {'type': 'object', 'properties': {'a': {'type': 'string'}}},
        {'a': {'b': 'c'}},
        ["$.a: expected type 'string', got dict"],
    ),
    (
        {'type': 'object', 'properties': {'a': {'type': 'string'}}},
        {},
        [],
    ),
    (
        {'type': 'object', 'properties': {'a': {'type': 'string'}}, 'required': ['a']},
        {},
        ["$: missing required property 'a'"],
    ),
    (
        {'type': 'object', 'properties': {'a': {'type': 'string'}}, 'required': ['a']},
        {'a': 'x'},
        [],
    ),
    (
        {'type': 'object',
         'properties': {'a': {'type': 'integer'}, 'b': {'type': 'number'}},
         'required': ['a', 'b']},
        {},
        ["$: missing required property 'a'", "$: missing required property 'b'"],
    ),
    (
        {'type': 'object',
         'properties': {'a': {'type': 'string'}},
         'additionalProperties': False},
        {'b': 1},
        ["$: unexpected property 'b'"],
    ),
    (
        {'type': 'object',
         'properties': {'a': {'type': 'string'}},
         'additionalProperties': False},
        {},
        [],
    ),
    (
        {'type': 'object',
         'properties': {'a': {'type': 'string'}},
         'additionalProperties': {'type': 'string'}},
        {'a': {'b': 'c'}},
        ["$.a: expected type 'string', got dict"],
    ),
    (
        {'type': 'object',
         'properties': {'a': {'type': 'string'}},
         'additionalProperties': {'type': 'string'}},
        {},
        [],
    ),
    (
        {'type': 'object',
         'properties': {'a': {'type': 'object', 'properties': {'b': {'type': 'string'}}}}},
        {'a': [1, 2]},
        ["$.a: expected type 'object', got list"],
    ),
    (
        {'type': 'object',
         'properties': {'a': {'type': 'object', 'properties': {'b': {'type': 'string'}}}}},
        {},
        [],
    ),
    (
        {'type': 'object',
         'properties': {'a': {'type': 'array', 'items': {'type': 'integer'}}}},
        {'a': {'b': 'c'}},
        ["$.a: expected type 'array', got dict"],
    ),
    (
        {'type': 'object',
         'properties': {'a': {'type': 'array', 'items': {'type': 'integer'}}}},
        {},
        [],
    ),
    (
        {'type': 'array'},
        {},
        ["$: expected type 'array', got dict"],
    ),
    (
        {'type': 'array'},
        [],
        [],
    ),
    (
        {'type': 'array', 'items': {'type': 'string'}},
        [1, 2],
        ["$[0]: expected type 'string', got int", "$[1]: expected type 'string', got int"],
    ),
    (
        {'type': 'array', 'items': {'type': 'string'}},
        [],
        [],
    ),
    (
        {'type': 'array', 'items': {'type': 'string'}, 'minItems': 2},
        [],
        ['$: expected at least 2 items, got 0'],
    ),
    (
        {'type': 'array', 'items': {'type': 'string'}, 'minItems': 2},
        ['a', 'b', 'c'],
        [],
    ),
    (
        {'type': 'array', 'items': {'type': 'string'}, 'maxItems': 3},
        [1, 2],
        ["$[0]: expected type 'string', got int", "$[1]: expected type 'string', got int"],
    ),
    (
        {'type': 'array', 'items': {'type': 'string'}, 'maxItems': 3},
        [],
        [],
    ),
    (
        {'type': 'array', 'items': {'type': 'string'}, 'minItems': 1, 'maxItems': 3},
        [],
        ['$: expected at least 1 items, got 0'],
    ),
    (
        {'type': 'array', 'items': {'type': 'string'}, 'minItems': 1, 'maxItems': 3},
        ['a'],
        [],
    ),
    (
        {'type': 'array', 'items': {'type': 'integer'}},
        ['a', 'b', 'c'],
        ["$[0]: expected type 'integer', got str",
         "$[1]: expected type 'integer', got str",
         "$[2]: expected type 'integer', got str"],
    ),
    (
        {'type': 'array', 'items': {'type': 'integer'}},
        [],
        [],
    ),
    (
        {'type': 'array', 'items': {'type': 'string'}, 'minItems': 1},
        [],
        ['$: expected at least 1 items, got 0'],
    ),
    (
        {'type': 'array', 'items': {'type': 'string'}, 'minItems': 1},
        ['a'],
        [],
    ),
    (
        {'type': 'string'},
        {},
        ["$: expected type 'string', got dict"],
    ),
    (
        {'type': 'string'},
        '',
        [],
    ),
    (
        {'type': 'string', 'minLength': 3},
        '',
        ['$: shorter than minLength 3'],
    ),
    (
        {'type': 'string', 'minLength': 3},
        'abc',
        [],
    ),
    (
        {'type': 'string', 'maxLength': 5},
        'abcdef',
        ['$: longer than maxLength 5'],
    ),
    (
        {'type': 'string', 'maxLength': 5},
        '',
        [],
    ),
    (
        {'type': 'string', 'minLength': 2, 'maxLength': 5},
        'abcdef',
        ['$: longer than maxLength 5'],
    ),
    (
        {'type': 'string', 'minLength': 2, 'maxLength': 5},
        'abc',
        [],
    ),
    (
        {'type': 'string', 'pattern': '^a'},
        '',
        ['$: does not match the declared pattern'],
    ),
    (
        {'type': 'string', 'pattern': '^a'},
        'abc',
        [],
    ),
    (
        {'type': 'string', 'pattern': 'z$'},
        'abcdef',
        ['$: does not match the declared pattern'],
    ),
    (
        {'type': 'string', 'enum': ['a', 'b']},
        'abcdef',
        ['$: value is not one of the 2 permitted values'],
    ),
    (
        {'type': 'string', 'const': 'x'},
        {},
        ["$: expected type 'string', got dict"],
    ),
    (
        {'type': 'string', 'const': 'x'},
        '',
        [],
    ),
    (
        {'type': 'integer'},
        {},
        ["$: expected type 'integer', got dict"],
    ),
    (
        {'type': 'integer'},
        0,
        [],
    ),
    (
        {'type': 'integer', 'minimum': 3},
        1,
        ['$: below minimum 3'],
    ),
    (
        {'type': 'integer', 'minimum': 3},
        3,
        [],
    ),
    (
        {'type': 'integer', 'maximum': 7},
        10,
        ['$: above maximum 7'],
    ),
    (
        {'type': 'integer', 'maximum': 7},
        0,
        [],
    ),
    (
        {'type': 'integer', 'minimum': 3, 'maximum': 7},
        10,
        ['$: above maximum 7'],
    ),
    (
        {'type': 'integer', 'minimum': 3, 'maximum': 7},
        3,
        [],
    ),
    (
        {'type': 'number'},
        {},
        ["$: expected type 'number', got dict"],
    ),
    (
        {'type': 'number'},
        0,
        [],
    ),
    (
        {'type': 'number', 'minimum': 1.5},
        1,
        ['$: below minimum 1.5'],
    ),
    (
        {'type': 'number', 'minimum': 1.5},
        3,
        [],
    ),
    (
        {'type': 'number', 'maximum': 2.5},
        5,
        ['$: above maximum 2.5'],
    ),
    (
        {'type': 'number', 'maximum': 2.5},
        0,
        [],
    ),
    (
        {'type': 'number', 'minimum': 1.5, 'maximum': 2.5},
        5,
        ['$: above maximum 2.5'],
    ),
    (
        {'type': 'number', 'minimum': 1.5, 'maximum': 2.5},
        1.5,
        [],
    ),
    (
        {'type': 'boolean'},
        {},
        ["$: expected type 'boolean', got dict"],
    ),
    (
        {'type': 'boolean'},
        True,
        [],
    ),
    (
        {'type': 'null'},
        {},
        ["$: expected type 'null', got dict"],
    ),
    (
        {'type': 'null'},
        None,
        [],
    ),
    (
        {'type': ['string', 'null']},
        {},
        ["$: expected type ['string', 'null'], got dict"],
    ),
    (
        {'type': ['string', 'null']},
        '',
        [],
    ),
    (
        {'type': ['string', 'integer']},
        {},
        ["$: expected type ['string', 'integer'], got dict"],
    ),
    (
        {'type': ['string', 'integer']},
        '',
        [],
    ),
    (
        {'anyOf': [{'type': 'string'}, {'type': 'integer'}]},
        {},
        [],
    ),
    (
        {'anyOf': [{'type': 'string'}, {'type': 'integer'}]},
        '',
        [],
    ),
    (
        {'oneOf': [{'type': 'string'}, {'type': 'integer'}]},
        {},
        [],
    ),
    (
        {'oneOf': [{'type': 'string'}, {'type': 'integer'}]},
        '',
        [],
    ),
    (
        {'allOf': [{'type': 'string'}, {'minLength': 2}]},
        {},
        [],
    ),
    (
        {'allOf': [{'type': 'string'}, {'minLength': 2}]},
        '',
        [],
    ),
    (
        {'enum': [1, 2, 3]},
        {},
        ['$: value is not one of the 3 permitted values'],
    ),
    (
        {'enum': [1, 2, 3]},
        1,
        [],
    ),
    (
        {'const': 'only'},
        {},
        [],
    ),
    (
        {'const': 'only'},
        '',
        [],
    ),
)


def test_a_valid_schema_answers_exactly_as_it_did_before() -> None:
    """The rule is about *malformed* schemas. A valid one must not move.

    Every row is a keyword the validator understands, carrying a value the
    keyword can use — so the shape check has nothing to report and the
    validator's own answers must be untouched.
    """
    for index, (schema, payload, expected) in enumerate(VALID_SCHEMA_ANSWERS):
        assert schema_errors(payload, schema) == expected, (
            f"row {index}: {schema!r} against {payload!r} answered differently"
        )
