"""The model layer's error contract: a broken schema, and a broken secret.

Two findings live in this file, and they are the same shape of defect — a
boundary that was supposed to hold a rule held it for one caller and not for
the other.

**F-2 — a malformed schema keyword is a schema error, in every validator.**
:func:`paa.models.base.validate_against_schema` used to compare an ``int``
with a ``str`` and let a raw ``TypeError`` escape
:meth:`paa.models.base.ModelProvider.complete_structured`. A ``TypeError`` is
neither a :class:`~paa.core.errors.PaaError` nor a routable failure: the
orchestrator's ``except PaaError`` did not catch it, so no ``EXECUTION_FAILED``
was written and the run died unrecorded. The rule — *a malformed keyword is a
schema error, reported and never raised as a ``TypeError``* — now reaches
this module, :mod:`paa.skills.contracts` and
:mod:`paa.validation.schema_validator` alike, because all three read the one
table of keyword shapes.

**F-3 — a free-form detail is redacted at the construction boundary.**
:class:`~paa.models.base.ModelUnavailableError` scrubbed its *message* and
carried its ``**details`` verbatim, and ``PaaError`` merges details into a
ledger payload verbatim. So a provider's own error body — the one place a
credential lands — reached the ledger, the log aggregator and whatever the
operator pastes into a bug report. The sibling,
:class:`~paa.models.base.StructuredOutputError`, already scrubbed its own
details; the rule is now applied to both, and the policy for *what* "redacted"
means for a free-form mapping is written down in
:func:`paa.models.base.redact_detail` rather than left to each raise site.

The names of the keywords below are **enumerated from the source**, not from a
docstring: a fix for the one keyword that was found is not a fix.
"""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest

from paa.core.errors import PaaError
from paa.models.anthropic_provider import AnthropicProvider
from paa.models.base import (
    CompletionRequest,
    MalformedSchemaError,
    Message,
    ModelUnavailableError,
    StructuredOutputError,
    minimal_instance_for_schema,
    redact_detail,
    validate_against_schema,
)
from paa.models.echo_provider import EchoProvider

from .conftest import FAKE_KEY, RequestRecorder, mock_client

# ---------------------------------------------------------------------------
# The keyword inventory
# ---------------------------------------------------------------------------

#: Every keyword :func:`validate_against_schema` reads, with a value the
#: keyword cannot use.
#:
#: Read out of the module's own source: ``type``, ``enum``, ``const``,
#: ``properties``, ``required``, ``additionalProperties``, ``items``,
#: ``minItems``/``maxItems``, ``minLength``/``maxLength``, ``minimum``/
#: ``maximum`` and the three combinators.
#:
#: ``const`` is absent **on purpose**: any JSON value is a legal constant, so
#: there is no malformed value for it, and a keyword with no malformed value
#: cannot be given a defect.
MALFORMED_KEYWORDS: tuple[tuple[str, Any], ...] = (
    ("type", 5),
    ("type", ["string", 5]),
    ("enum", "abc"),
    ("properties", "not-a-mapping"),
    ("properties", {"a": "not-a-schema"}),
    ("required", "a"),
    ("required", ["a", 5]),
    ("additionalProperties", 5),
    ("items", "not-a-schema"),
    ("minItems", "2"),
    ("maxItems", "2"),
    ("minLength", "2"),
    ("maxLength", "2"),
    ("minimum", "2"),
    ("maximum", "2"),
    ("anyOf", "not-a-list"),
    ("anyOf", ["not-a-schema"]),
    ("oneOf", "not-a-list"),
    ("allOf", "not-a-list"),
)


def _schema_with(keyword: str, value: Any) -> dict[str, Any]:
    """A schema whose ``keyword`` carries ``value``.

    ``type`` gets the value on its own, because a schema cannot say
    ``"type": "object"`` and ``"type": 5`` at the same time.
    """
    if keyword == "type":
        return {"type": value}
    return {"type": "object", keyword: value}


#: A payload that **reaches** the keyword, so the keyword's value is actually
#: used.
#:
#: The distinction matters and was measured. On the unfixed source a malformed
#: keyword was only a crash when the instance reached it — ``minLength: "2"``
#: against a *dict* was silently accepted, and against a *string* it raised a
#: raw ``TypeError``. The rule has to hold in both cases: a broken contract
#: is broken whether or not this payload happens to walk into it.
REACHING_PAYLOADS: dict[str, Any] = {
    "type": "a string",
    "enum": "a string",
    "properties": {"a": 1},
    "required": {"a": 1},
    "additionalProperties": {"a": 1},
    "items": [1],
    "minItems": [1],
    "maxItems": [1],
    "minLength": "abc",
    "maxLength": "abc",
    "minimum": 5,
    "maximum": 5,
    "anyOf": "a string",
    "oneOf": "a string",
    "allOf": "a string",
}

#: Every malformed keyword, against a payload that does **not** reach it and
#: against one that does.
MALFORMED_CASES: tuple[tuple[str, Any, Any], ...] = tuple(
    (keyword, value, payload)
    for keyword, value in MALFORMED_KEYWORDS
    for payload in ({"a": "x"}, REACHING_PAYLOADS[keyword])
)


# ---------------------------------------------------------------------------
# F-2 — the model path
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(("keyword", "value", "payload"), MALFORMED_CASES)
def test_a_malformed_keyword_is_reported_and_never_raised_as_a_type_error(
    keyword: str, value: Any, payload: Any
) -> None:
    """Every keyword the validator reads is checked for shape.

    Before the fix each of these reached a comparison the keyword's value
    could not survive: ``len(instance) < "2"`` is a ``TypeError``,
    ``"not-a-mapping".items()`` is an ``AttributeError``, and
    ``int("2")`` in the minimal-instance builder is a ``ValueError``. None of
    them is a :class:`~paa.core.errors.PaaError`, so none of them was
    recorded by the run that hit it. And when the payload did *not* reach the
    keyword, the same defect was answered by silently accepting the instance
    against a schema whose constraint had been dropped.
    """
    schema = _schema_with(keyword, value)

    with pytest.raises(MalformedSchemaError) as caught:
        validate_against_schema(payload, schema)

    assert not isinstance(caught.value, TypeError)
    assert not isinstance(caught.value, AttributeError)
    assert not isinstance(caught.value, ValueError)


def test_the_malformed_keyword_is_located_in_the_schema_not_in_the_payload() -> None:
    """The defect names the **schema** node it sits at.

    A schema error that points at the payload is a payload error, and the
    author would go looking in the wrong place. The path convention is the one
    the runtime's output gate already uses: ``$.properties.a.minLength``.
    """
    schema = {"type": "object", "properties": {"a": {"type": "string", "minLength": "2"}}}

    with pytest.raises(MalformedSchemaError) as caught:
        validate_against_schema({"a": "x"}, schema)

    defect = caught.value.malformed_keywords[0]
    assert defect["path"] == "$.properties.a.minLength"
    assert defect["keyword"] == "minLength"
    assert defect["schema_error"] is True


def test_a_defect_in_a_branch_the_payload_never_reaches_is_still_reported() -> None:
    """The walk covers the whole schema.

    A broken constraint in a branch this payload never touches is still a
    broken contract: the author has to hear about it before the payload that
    does touch it arrives.
    """
    schema = {
        "type": "object",
        "properties": {"a": {"type": "string"}, "b": {"type": "string", "minLength": "2"}},
    }

    with pytest.raises(MalformedSchemaError) as caught:
        validate_against_schema({"a": "x"}, schema)

    assert [defect["path"] for defect in caught.value.malformed_keywords] == [
        "$.properties.b.minLength"
    ]


def test_the_malformed_schema_error_is_a_paa_error_and_survives_a_ledger_payload() -> None:
    """Being a :class:`~paa.core.errors.PaaError` is what makes the run record it.

    The whole point of the class: the orchestrator's ``except PaaError`` does
    not catch a ``TypeError``, so the failure was invisible to the run. And
    the payload has to survive ``json.dumps``, because that is how it reaches
    the ledger.
    """
    with pytest.raises(MalformedSchemaError) as caught:
        validate_against_schema({}, {"type": "string", "minLength": "2"})

    error = caught.value
    assert isinstance(error, PaaError)
    payload = error.to_payload()
    assert payload["error_type"] == "MalformedSchemaError"
    assert json.loads(json.dumps(payload))["malformed_keywords"]


def test_the_malformed_schema_error_is_not_a_routable_failure() -> None:
    """It must not travel the escalation path.

    :class:`~paa.models.base.StructuredOutputError` means "the provider
    answered, and the answer was wrong", and the escalation router reads it
    as exactly that: it escalates, and the ``MODEL_ESCALATED`` audit record
    then says reasoning material left the machine. A malformed schema is the
    *caller's* mistake, a better model cannot repair it, and nothing left the
    machine — so it has to be a class of its own.
    """
    with pytest.raises(MalformedSchemaError) as caught:
        validate_against_schema({}, {"type": "string", "minLength": "2"})

    assert not isinstance(caught.value, StructuredOutputError)
    assert not isinstance(caught.value, ModelUnavailableError)


@pytest.mark.parametrize(("keyword", "value"), MALFORMED_KEYWORDS)
async def test_no_raw_type_error_escapes_complete_structured(
    keyword: str, value: Any
) -> None:
    """The gate that matters is the one the run goes through.

    ``complete_structured`` is the entry point every structured call uses, and
    a ``TypeError`` out of it is neither recorded by the ledger nor routable
    by the escalation policy. It is now a
    :class:`~paa.models.base.MalformedSchemaError`, which is both.

    The refusal is on the **schema**, so it does not depend on the payload:
    the provider is never asked, because no answer could satisfy a schema
    whose constraint cannot be read.
    """
    provider = EchoProvider()
    request = CompletionRequest(messages=(Message.user("hello"),))

    with pytest.raises(MalformedSchemaError):
        await provider.complete_structured(request, _schema_with(keyword, value))

    assert provider.calls == [], "the provider was called with a schema it could never satisfy"


async def test_minimal_instance_for_schema_reports_a_malformed_keyword() -> None:
    """The minimal-instance builder reads keywords too, and it used to crash.

    ``int(schema["minItems"])`` on a string is a ``ValueError`` and
    ``value[:schema["maxLength"]]`` on a string is a ``TypeError`` — neither of
    which a caller could act on.
    """
    with pytest.raises(MalformedSchemaError):
        minimal_instance_for_schema({"type": "array", "minItems": "2"})

    with pytest.raises(MalformedSchemaError):
        minimal_instance_for_schema({"type": "object", "properties": "not-a-mapping"})


# ---------------------------------------------------------------------------
# The rule is the same in all three validators
# ---------------------------------------------------------------------------

#: Every keyword the **shared table** constrains, with a value the keyword
#: cannot use.
#:
#: This is the **whole** table, not the part the model validator happens to
#: read. The three validators in this tree ask one question about a keyword's
#: value — *can this keyword use this value?* — and they now get the answer
#: from one place, so a keyword that one of them does not implement is still
#: constrained by the same table. That is what makes the rule *consistent*
#: rather than three rules that happen to agree today: a validator that
#: ignores ``pattern`` cannot quietly accept a schema whose ``pattern`` is not
#: a regex, because the table says a broken pattern is a broken contract.
#:
#: Enumerated from :data:`paa.validation.schema_validator.SUPPORTED_KEYWORDS`
#: and the shape table beside it. ``const`` is absent on purpose: any JSON
#: value is a legal constant, so the keyword has no malformed value.
SHARED_TABLE_CASES: tuple[tuple[str, Any], ...] = (
    ("type", 5),
    ("properties", "not-a-mapping"),
    ("required", "a"),
    ("additionalProperties", 5),
    ("items", "not-a-schema"),
    ("prefixItems", "not-a-list"),
    ("enum", "abc"),
    ("minimum", "abc"),
    ("maximum", "abc"),
    ("exclusiveMinimum", "abc"),
    ("exclusiveMaximum", "abc"),
    ("multipleOf", "abc"),
    ("minLength", "2"),
    ("maxLength", "2"),
    ("pattern", "["),
    ("minItems", "2"),
    ("maxItems", "2"),
    ("uniqueItems", "yes"),
    ("minProperties", "2"),
    ("maxProperties", "2"),
    ("anyOf", "not-a-list"),
    ("oneOf", "not-a-list"),
    ("allOf", "not-a-list"),
    ("not", "not-a-schema"),
    ("nullable", "yes"),
    ("definitions", "not-a-mapping"),
    ("$defs", "not-a-mapping"),
)


@pytest.mark.parametrize(("keyword", "value"), SHARED_TABLE_CASES)
def test_all_three_validators_refuse_the_same_malformed_keyword(
    keyword: str, value: Any
) -> None:
    """One table, three validators, one verdict.

    The finding was that the rule reached **one** of the three validators and
    the other two each failed in their own way — one raised a raw
    ``TypeError``, the other silently dropped the constraint. A per-validator
    list of malformed keywords cannot hold that line, because the *next* one
    is added to whichever table someone was looking at.

    So this test asks all three at once, for every constrained keyword:

    * the runtime's output gate (:func:`paa.validation.schema_validator.validate_instance`)
      **reports** the defect, marked as a schema error and located at the
      schema node;
    * the model layer (:func:`paa.models.base.validate_against_schema`) raises
      a :class:`~paa.models.base.MalformedSchemaError` — a
      :class:`~paa.core.errors.PaaError`, so the run records it;
    * the skills layer (:func:`paa.skills.contracts.schema_errors`) raises a
      ``ValueError``, the way that module already refuses a bad schema shape.

    Only the *way* it is reported differs, because each module has its own
    convention. Whether it is reported at all does not.
    """
    from paa.skills.contracts import schema_errors
    from paa.validation.schema_validator import validate_instance

    schema = _schema_with(keyword, value)

    defects = [error for error in validate_instance(None, schema) if error.get("schema_error")]
    assert defects, f"the shared table does not see a malformed {keyword!r}"

    with pytest.raises(MalformedSchemaError):
        validate_against_schema({"a": "x"}, schema)

    with pytest.raises(ValueError, match="keyword whose value cannot be used"):
        schema_errors({"a": "x"}, schema)


# ---------------------------------------------------------------------------
# The second implementation of ``complete_structured``
# ---------------------------------------------------------------------------

#: A schema with a broken constraint, written the way a caller would write
#: one: the defect is nested in a property, not at the root.
_BROKEN_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {"a": {"type": "integer", "minimum": "5"}},
}


def _anthropic_tool_response(_request: httpx.Request) -> httpx.Response:
    """A well-formed Anthropic answer, so only the *schema* can be at fault."""
    return httpx.Response(
        200,
        json={
            "model": "claude-stub",
            "content": [{"type": "tool_use", "name": "emit_result", "input": {"a": 1}}],
            "usage": {"input_tokens": 3, "output_tokens": 3},
            "stop_reason": "tool_use",
        },
    )


async def test_the_anthropic_path_also_refuses_a_malformed_schema(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``complete_structured`` has **two** implementations, and the rule reaches both.

    :class:`~paa.models.anthropic_provider.AnthropicProvider` overrides the
    generic parse-and-retry loop with a forced tool call, and it calls
    :func:`paa.models.base.validate_against_schema` itself. Before the fix
    that call was the *other* place a raw ``TypeError`` escaped
    ``complete_structured`` — the reviewer's probe reached it end to end
    through the real provider.

    The rule is the same here: a malformed keyword is a
    :class:`~paa.models.base.MalformedSchemaError`, never a ``TypeError``, so
    the failure is recorded by the run and is not routable.
    """
    monkeypatch.setenv("PAA_TEST_ANTHROPIC_KEY", FAKE_KEY)
    recorder = RequestRecorder(_anthropic_tool_response)
    provider = AnthropicProvider(
        model="claude-stub",
        api_key_env="PAA_TEST_ANTHROPIC_KEY",
        client=mock_client(recorder),
    )
    request = CompletionRequest(messages=(Message.user("hello"),))

    with pytest.raises(MalformedSchemaError) as caught:
        await provider.complete_structured(request, _BROKEN_SCHEMA)

    assert not isinstance(caught.value, TypeError)
    assert not isinstance(caught.value, AttributeError)


@pytest.mark.xfail(
    strict=True,
    reason=(
        "AnthropicProvider.complete_structured (src/paa/models/anthropic_provider.py:152) "
        "overrides the generic loop and does not refuse a malformed schema before it "
        "calls the provider: the broken schema is sent as the tool's input_schema and "
        "the frontier model is asked about it, and only the response is then refused. "
        "The base implementation refuses pre-flight for exactly this reason "
        "(src/paa/models/base.py:884). anthropic_provider.py is read-only for W2-IMPR-02; "
        "the fix is the same three lines the base uses — call "
        "paa.models.base._malformed_keywords(schema) and raise MalformedSchemaError "
        "before self._send(...)."
    ),
)
async def test_the_anthropic_path_does_not_ask_the_frontier_model_about_a_broken_schema(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A broken contract must not leave the machine.

    The base implementation refuses a malformed schema *before* the provider
    is called, and says why: a frontier model cannot repair a contract whose
    constraint cannot be read, so asking it only carries the broken contract
    off the machine and records an escalation that never happened.

    The Anthropic override does not do that yet. The request is made, the
    broken schema goes out as the tool's ``input_schema``, and the answer is
    refused afterwards. ``strict=True`` means this suite goes **red** the day
    the override refuses pre-flight, so the marker cannot outlive the fix.
    """
    monkeypatch.setenv("PAA_TEST_ANTHROPIC_KEY", FAKE_KEY)
    recorder = RequestRecorder(_anthropic_tool_response)
    provider = AnthropicProvider(
        model="claude-stub",
        api_key_env="PAA_TEST_ANTHROPIC_KEY",
        client=mock_client(recorder),
    )
    request = CompletionRequest(messages=(Message.user("hello"),))

    with pytest.raises(MalformedSchemaError):
        await provider.complete_structured(request, _BROKEN_SCHEMA)

    assert recorder.request is None, (
        "the frontier model was asked to satisfy a schema whose constraint cannot be read"
    )


# ---------------------------------------------------------------------------
# A valid schema behaves exactly as before
# ---------------------------------------------------------------------------

#: What the validators answered **before** the change, for a corpus of valid
#: schemas. The rows were captured from ``4c9300d``'s source and are the
#: "a valid schema must behave exactly as before" half of the finding,
#: frozen: a change that alters one of these answers has changed a contract
#: nobody asked it to change.
#:
#: Each row is ``(schema, payload, errors)``.
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
        ['$: expected type object, got list'],
    ),
    (
        {'type': 'object'},
        {},
        [],
    ),
    (
        {'type': 'object', 'properties': {}},
        [],
        ['$: expected type object, got list'],
    ),
    (
        {'type': 'object', 'properties': {}},
        {},
        [],
    ),
    (
        {'type': 'object', 'properties': {'a': {'type': 'string'}}},
        {'a': {'b': 'c'}},
        ['$.a: expected type string, got dict'],
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
        ['$.a: expected type string, got dict'],
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
        ['$.a: expected type object, got list'],
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
        ['$.a: expected type array, got dict'],
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
        ['$: expected type array, got dict'],
    ),
    (
        {'type': 'array'},
        [],
        [],
    ),
    (
        {'type': 'array', 'items': {'type': 'string'}},
        [1, 2],
        ['$[0]: expected type string, got int', '$[1]: expected type string, got int'],
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
        ['$[0]: expected type string, got int', '$[1]: expected type string, got int'],
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
        ['$[0]: expected type integer, got str',
         '$[1]: expected type integer, got str',
         '$[2]: expected type integer, got str'],
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
        ['$: expected type string, got dict'],
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
        {},
        ['$: expected type string, got dict'],
    ),
    (
        {'type': 'string', 'pattern': '^a'},
        '',
        [],
    ),
    (
        {'type': 'string', 'pattern': 'z$'},
        {},
        ['$: expected type string, got dict'],
    ),
    (
        {'type': 'string', 'pattern': 'z$'},
        '',
        [],
    ),
    (
        {'type': 'string', 'enum': ['a', 'b']},
        'abcdef',
        ["$: 'abcdef' is not one of ['a', 'b']"],
    ),
    (
        {'type': 'string', 'const': 'x'},
        'abcdef',
        ["$: expected the constant 'x', got 'abcdef'"],
    ),
    (
        {'type': 'integer'},
        {},
        ['$: expected type integer, got dict'],
    ),
    (
        {'type': 'integer'},
        0,
        [],
    ),
    (
        {'type': 'integer', 'minimum': 3},
        1,
        ['$: 1 is below minimum 3'],
    ),
    (
        {'type': 'integer', 'minimum': 3},
        3,
        [],
    ),
    (
        {'type': 'integer', 'maximum': 7},
        10,
        ['$: 10 is above maximum 7'],
    ),
    (
        {'type': 'integer', 'maximum': 7},
        0,
        [],
    ),
    (
        {'type': 'integer', 'minimum': 3, 'maximum': 7},
        10,
        ['$: 10 is above maximum 7'],
    ),
    (
        {'type': 'integer', 'minimum': 3, 'maximum': 7},
        3,
        [],
    ),
    (
        {'type': 'number'},
        {},
        ['$: expected type number, got dict'],
    ),
    (
        {'type': 'number'},
        0,
        [],
    ),
    (
        {'type': 'number', 'minimum': 1.5},
        1,
        ['$: 1 is below minimum 1.5'],
    ),
    (
        {'type': 'number', 'minimum': 1.5},
        3,
        [],
    ),
    (
        {'type': 'number', 'maximum': 2.5},
        5,
        ['$: 5 is above maximum 2.5'],
    ),
    (
        {'type': 'number', 'maximum': 2.5},
        0,
        [],
    ),
    (
        {'type': 'number', 'minimum': 1.5, 'maximum': 2.5},
        5,
        ['$: 5 is above maximum 2.5'],
    ),
    (
        {'type': 'number', 'minimum': 1.5, 'maximum': 2.5},
        1.5,
        [],
    ),
    (
        {'type': 'boolean'},
        {},
        ['$: expected type boolean, got dict'],
    ),
    (
        {'type': 'boolean'},
        True,
        [],
    ),
    (
        {'type': 'null'},
        {},
        ['$: expected type null, got dict'],
    ),
    (
        {'type': 'null'},
        None,
        [],
    ),
    (
        {'type': ['string', 'null']},
        {},
        ['$: expected type string|null, got dict'],
    ),
    (
        {'type': ['string', 'null']},
        '',
        [],
    ),
    (
        {'type': ['string', 'integer']},
        {},
        ['$: expected type string|integer, got dict'],
    ),
    (
        {'type': ['string', 'integer']},
        '',
        [],
    ),
    (
        {'anyOf': [{'type': 'string'}, {'type': 'integer'}]},
        {},
        ['$: matched none of the 2 anyOf branches'],
    ),
    (
        {'anyOf': [{'type': 'string'}, {'type': 'integer'}]},
        '',
        [],
    ),
    (
        {'oneOf': [{'type': 'string'}, {'type': 'integer'}]},
        {},
        ['$: matched 0 oneOf branches, expected exactly 1'],
    ),
    (
        {'oneOf': [{'type': 'string'}, {'type': 'integer'}]},
        '',
        [],
    ),
    (
        {'allOf': [{'type': 'string'}, {'minLength': 2}]},
        '',
        ['$: shorter than minLength 2'],
    ),
    (
        {'allOf': [{'type': 'string'}, {'minLength': 2}]},
        'abc',
        [],
    ),
    (
        {'enum': [1, 2, 3]},
        {},
        ['$: {} is not one of [1, 2, 3]'],
    ),
    (
        {'enum': [1, 2, 3]},
        1,
        [],
    ),
    (
        {'const': 'only'},
        {},
        ["$: expected the constant 'only', got {}"],
    ),
)

def test_a_valid_schema_answers_exactly_as_it_did_before() -> None:
    """The rule is about *malformed* schemas. A valid one must not move.

    The rows above were captured from the source before the change. Every one
    of them is a keyword the validator understands, carrying a value the
    keyword can use — so the shape check has nothing to report and the
    validator's own answers must be untouched.
    """
    for index, (schema, payload, expected) in enumerate(VALID_SCHEMA_ANSWERS):
        assert validate_against_schema(payload, schema) == expected, (
            f"row {index}: {schema!r} against {payload!r} answered differently"
        )


VALID_MINIMAL_INSTANCES: tuple[tuple[dict[str, Any], Any], ...] = (
    (
        {'type': 'object', 'properties': {'a': {'type': 'string'}}, 'required': ['a']},
        {'a': 'a'},
    ),
    (
        {'type': 'object', 'properties': {'a': {'type': 'string'}}},
        {'a': 'a'},
    ),
    (
        {'type': 'array', 'items': {'type': 'string'}, 'minItems': 2},
        ['echo', 'echo'],
    ),
    (
        {'type': 'string', 'minLength': 3},
        'echo',
    ),
    (
        {'type': 'string', 'minLength': 3, 'maxLength': 4},
        'echo',
    ),
    (
        {'type': 'string', 'maxLength': 2},
        'ec',
    ),
    (
        {'type': 'integer', 'minimum': 5},
        5,
    ),
    (
        {'type': 'number', 'minimum': 1.5},
        1.5,
    ),
    (
        {'anyOf': [{'type': 'string'}, {'type': 'integer'}]},
        'echo',
    ),
    (
        {'const': 'only'},
        'only',
    ),
    (
        {'enum': ['a', 'b']},
        'a',
    ),
)


def test_a_valid_schema_still_builds_the_same_minimal_instance() -> None:
    """The minimal-instance builder answers the same way for a valid schema.

    It reads keywords of its own — ``minItems`` says how many items to emit,
    ``minLength`` how long a string has to be — so a malformed value there was
    a crash of its own. For a valid schema its answers must not move.
    """
    for schema, expected in VALID_MINIMAL_INSTANCES:
        assert minimal_instance_for_schema(schema) == expected, (
            f"minimal_instance_for_schema({schema!r}) answered differently"
        )


# ---------------------------------------------------------------------------
# F-3 — the details of an error are redacted at construction
# ---------------------------------------------------------------------------


def test_model_unavailable_error_redacts_its_details() -> None:
    """The details are built out of the provider's *own* output.

    A provider that answers "connection refused: key=sk-…" puts the key in a
    detail. ``PaaError`` merges details into a ledger payload **verbatim** and
    renders them in ``str(error)``, so an unscrubbed detail is a copy of the
    key in the ledger, in the log aggregator and in whatever the operator
    pastes into a bug report.
    """
    error = ModelUnavailableError(
        f"connection refused: key={FAKE_KEY}",
        provider="ollama",
        body_excerpt=f"upstream said key={FAKE_KEY}",
        status_code=503,
    )

    assert FAKE_KEY not in str(error)
    assert FAKE_KEY not in repr(error.details)
    assert FAKE_KEY not in json.dumps(error.to_payload())
    assert "REDACTED" in error.details["body_excerpt"]


def test_model_unavailable_error_keeps_the_shape_of_its_details() -> None:
    """Redaction must not cost the operator the diagnosis.

    A payload that says nothing is not an audit record: the scrub replaces
    the secret and keeps the rest. The **keys** are the contract callers and
    ledger payloads are written against, so they are not rewritten; a number
    cannot carry a credential, so it is passed through untouched.
    """
    error = ModelUnavailableError(
        "boom",
        provider="ollama",
        body_excerpt=f"key={FAKE_KEY}",
        status_code=503,
        attempts=2,
        healthy=False,
        missing=None,
    )

    assert set(error.details) == {
        "provider",
        "body_excerpt",
        "status_code",
        "attempts",
        "healthy",
        "missing",
    }
    assert error.details["status_code"] == 503
    assert error.details["attempts"] == 2
    assert error.details["healthy"] is False
    assert error.details["missing"] is None
    assert error.details["provider"] == "ollama"


def test_redact_detail_scrubs_strings_at_any_depth_and_leaves_the_keys_alone() -> None:
    """The policy for a free-form mapping, written down once.

    * every **string value** is scrubbed, at any depth;
    * the **keys** are not — they name a field, and rewriting one would break
      the contract without removing a leak;
    * a value that is not a string, a container or a mapping is passed through
      unchanged, so the details stay JSON-serialisable for the ledger.
    """
    scrubbed = redact_detail(
        {
            "top": f"key={FAKE_KEY}",
            "nested": {"deep": [f"key={FAKE_KEY}", {"deeper": f"key={FAKE_KEY}"}]},
            "count": 3,
            "flag": True,
            "nothing": None,
        }
    )

    assert scrubbed == {
        "top": "key=***REDACTED***",
        "nested": {"deep": ["key=***REDACTED***", {"deeper": "key=***REDACTED***"}]},
        "count": 3,
        "flag": True,
        "nothing": None,
    }
    assert FAKE_KEY not in json.dumps(scrubbed)


def test_structured_output_error_redacts_its_details_too() -> None:
    """The sibling carries the same kind of detail, so it gets the same rule.

    "Remember to redact at the raise site" is a rule that holds right up until
    the one path nobody tested — and the sibling that *was* tested proved it:
    the class's own docstring now says the scrub happens at construction.
    """
    error = StructuredOutputError(
        f"provider echoed {FAKE_KEY}",
        provider="ollama",
        schema_errors=[f"$: {FAKE_KEY!r} is not one of ['ok']"],
        raw_excerpt=f"the model said {FAKE_KEY}",
        upstream=f"key={FAKE_KEY}",
    )

    assert FAKE_KEY not in str(error)
    assert FAKE_KEY not in json.dumps(error.to_payload())
    assert FAKE_KEY not in " ".join(error.schema_errors)
    assert error.schema_errors == ["$: '***REDACTED***' is not one of ['ok']"]


def test_the_model_layer_and_the_router_share_one_redaction_policy() -> None:
    """One policy, two copies of it — and this is the check that says so.

    The escalation router carries a private copy of the same rule for the
    details it copies out of a provider's error. The two are the same rule
    written twice, and the day they drift the router would scrub differently
    from the class that raised the error. If the router's copy has been
    retired in favour of the shared one, that is the same outcome, reached
    the other way.
    """
    from paa.models import router as router_module

    router_redact = getattr(router_module, "_redact_detail", None)
    if router_redact is None:
        assert getattr(router_module, "redact_detail", None) is redact_detail
        return

    for value in (
        f"key={FAKE_KEY}",
        [f"key={FAKE_KEY}", 3, None],
        {"a": f"key={FAKE_KEY}", "b": {"c": [f"key={FAKE_KEY}"]}},
        3,
        None,
    ):
        assert router_redact(value) == redact_detail(value)


# ---------------------------------------------------------------------------
# The provider contract these classes belong to
# ---------------------------------------------------------------------------


def test_the_malformed_schema_error_is_part_of_the_module_s_public_surface() -> None:
    """The class is exported, so a caller can catch it by name.

    ``paa.models.__init__`` re-exports a fixed list of names and is not this
    task's to edit, so the class is reached through the module that owns it —
    but it is exported from *that* module, and the shared redaction helper
    with it.
    """
    from paa.models import base

    assert "MalformedSchemaError" in base.__all__
    assert "redact_detail" in base.__all__
    assert issubclass(MalformedSchemaError, PaaError)
