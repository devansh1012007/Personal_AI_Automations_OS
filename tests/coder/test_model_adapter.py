"""The router-backed ``ToolCallingModel``: rendering, parsing, and degradation.

Every test here runs against a fake router, so the suite never touches a
provider. That is the point of the narrow protocol — the interesting behaviour
(a model that returns a malformed turn, or a provider that ignores
``response_format``) is reachable without a network, and a test that needed one
would be a test that stops running.
"""

from __future__ import annotations

import json
from typing import Any, cast

from paa.coder.model_adapter import RouterToolCallingModel, turn_schema
from paa.core.types import ComplexityModality, PermissionMode


class FakeRouter:
    """Stands in for ``EscalatingModelRouter``, recording how it was called."""

    def __init__(self, *turns: dict[str, Any]) -> None:
        self._turns = list(turns)
        self.calls: list[dict[str, Any]] = []

    async def complete_structured(
        self,
        request: Any,
        schema: dict[str, Any],
        **kwargs: Any,
    ) -> dict[str, Any]:
        self.calls.append(
            {
                "messages": [(m.role, m.content) for m in request.messages],
                "schema": schema,
                "kwargs": kwargs,
                "max_tokens": request.max_tokens,
                "temperature": request.temperature,
            }
        )
        return self._turns.pop(0) if self._turns else {"text": "done", "tool_calls": []}


TOOLS = [
    {"name": "read_ledger", "description": "read events", "input_schema": {"type": "object"}},
    {"name": "run_task", "description": "drive a task", "input_schema": {"type": "object"}},
]


def _model(*turns: dict[str, Any], **kw: Any) -> tuple[RouterToolCallingModel, FakeRouter]:
    router = FakeRouter(*turns)
    return RouterToolCallingModel(cast(Any, router), **kw), router


class TestTurnParsing:
    async def test_tool_call_becomes_a_model_turn(self) -> None:
        model, _ = _model(
            {
                "text": "looking now",
                "tool_calls": [
                    {"name": "read_ledger", "arguments": {"correlation_id": "abc"}}
                ],
            }
        )
        turn = await model.propose([], TOOLS)

        assert turn.text == "looking now"
        assert not turn.is_final
        assert len(turn.tool_calls) == 1
        assert turn.tool_calls[0].name == "read_ledger"
        assert turn.tool_calls[0].arguments == {"correlation_id": "abc"}

    async def test_no_tool_calls_is_final(self) -> None:
        model, _ = _model({"text": "all committed", "tool_calls": []})
        turn = await model.propose([], TOOLS)

        assert turn.is_final
        assert turn.tool_calls == []

    async def test_arguments_emitted_as_a_json_string_are_decoded(self) -> None:
        """Models routinely stringify object arguments even under a schema.

        Left as a string, the permission policy would stringify them again and
        every argument-pattern rule would silently fail to match, so the decode
        has to happen here.
        """

        model, _ = _model(
            {
                "text": "",
                "tool_calls": [{"name": "run_task", "arguments": '{"correlation_id": "xyz"}'}],
            }
        )
        turn = await model.propose([], TOOLS)

        assert turn.tool_calls[0].arguments == {"correlation_id": "xyz"}

    async def test_unparseable_argument_string_is_preserved_not_dropped(self) -> None:
        model, _ = _model(
            {"text": "", "tool_calls": [{"name": "submit_task", "arguments": "not json"}]}
        )
        turn = await model.propose([], TOOLS)

        assert turn.tool_calls[0].arguments == {"value": "not json"}

    async def test_entries_without_a_usable_name_are_dropped(self) -> None:
        model, _ = _model(
            {
                "text": "giving up",
                "tool_calls": [
                    "not even a dict",
                    {"arguments": {}},
                    {"name": "", "arguments": {}},
                    {"name": "read_ledger", "arguments": {}},
                ],
            }
        )
        turn = await model.propose([], TOOLS)

        assert [c.name for c in turn.tool_calls] == ["read_ledger"]

    async def test_every_entry_dropped_reads_as_finished(self) -> None:
        """The loop treats "no tool calls" as done, which is the right direction.

        An unreadable response should stop an unattended run and report, not
        spin: the loop returns immediately on a final turn.
        """

        model, _ = _model({"text": "could not proceed", "tool_calls": [{}, 7]})
        turn = await model.propose([], TOOLS)

        assert turn.is_final
        assert turn.text == "could not proceed"

    async def test_missing_or_non_string_text_becomes_empty(self) -> None:
        model, _ = _model({"tool_calls": []})
        turn = await model.propose([], TOOLS)

        assert turn.text == ""

    async def test_missing_tool_calls_key_is_tolerated(self) -> None:
        model, _ = _model({"text": "hello"})
        turn = await model.propose([], TOOLS)

        assert turn.is_final


class TestSchema:
    def test_tool_names_are_constrained_by_enum(self) -> None:
        schema = turn_schema(["read_ledger", "run_task"])

        assert schema["properties"]["tool_calls"]["items"]["properties"]["name"]["enum"] == [
            "read_ledger",
            "run_task",
        ]

    def test_no_tools_means_no_enum(self) -> None:
        schema = turn_schema([])

        assert "enum" not in schema["properties"]["tool_calls"]["items"]["properties"]["name"]

    def test_arguments_stay_free_form(self) -> None:
        """Arguments must not be closed, or every tool would need its schema
        inlined here and the model could not pass an unlisted key."""

        schema = turn_schema(["read_ledger"])
        assert "additionalProperties" not in schema["properties"]["tool_calls"]["items"][
            "properties"
        ]["arguments"]

    def test_text_and_tool_calls_are_both_required(self) -> None:
        assert set(turn_schema(["a"])["required"]) == {"text", "tool_calls"}


class TestPromptRendering:
    async def test_system_turn_carries_the_tool_catalogue(self) -> None:
        model, router = _model()
        await model.propose([], TOOLS)

        system = router.calls[0]["messages"][0]
        assert system[0] == "system"
        assert "read_ledger" in system[1]
        assert "run_task" in system[1]

    async def test_window_roles_render_into_messages(self) -> None:
        model, router = _model()
        await model.propose(
            [
                {"role": "user", "content": "why did it fail?"},
                {"role": "assistant", "content": "reading the ledger"},
                {"role": "tool_call", "name": "read_ledger", "arguments": {"x": 1}},
                {"role": "tool_result", "name": "read_ledger", "content": "POLICY_BLOCKED"},
                {"role": "summary", "content": "earlier turns collapsed"},
            ],
            TOOLS,
        )

        body = " ".join(m[1] for m in router.calls[0]["messages"][1:])
        assert "why did it fail?" in body
        assert "reading the ledger" in body
        assert "[tool call] read_ledger" in body
        assert "[tool result] read_ledger" in body
        assert "POLICY_BLOCKED" in body
        assert "earlier turns collapsed" in body

    async def test_custom_instructions_are_appended(self) -> None:
        model, router = _model(instructions="PAA_HOME is /tmp/paa-test.")
        await model.propose([], TOOLS)

        assert "/tmp/paa-test" in router.calls[0]["messages"][0][1]

    async def test_empty_window_still_sends_the_system_turn(self) -> None:
        model, router = _model()
        await model.propose([], TOOLS)

        assert len(router.calls[0]["messages"]) == 1


class TestRouting:
    async def test_configuration_is_forwarded_to_the_router(self) -> None:
        """The adapter must not decide policy — it passes posture through and
        lets the router apply its own rules, so escalation stays auditable."""

        model, router = _model(modality=ComplexityModality.COMPLEX)
        await model.propose([], TOOLS)

        kwargs = router.calls[0]["kwargs"]
        assert kwargs["modality"] is ComplexityModality.COMPLEX
        assert kwargs["permission_mode"] is PermissionMode.ASK
        assert kwargs["force_escalate"] is False
        assert kwargs["reason"]

    async def test_force_escalate_is_forwarded(self) -> None:
        model, router = _model(force_escalate=True)
        await model.propose([], TOOLS)

        assert router.calls[0]["kwargs"]["force_escalate"] is True

    async def test_request_shape(self) -> None:
        model, router = _model(max_tokens=4096, temperature=0.3)
        await model.propose([], TOOLS)

        assert router.calls[0]["max_tokens"] == 4096
        assert router.calls[0]["temperature"] == 0.3

    async def test_temperatures_stays_at_zero_by_default(self) -> None:
        model, router = _model()
        await model.propose([], TOOLS)

        assert router.calls[0]["temperature"] == 0.0


class TestTurnSchemaShape:
    def test_schema_is_json_serialisable(self) -> None:
        """The provider sends this verbatim, so it has to survive encoding."""

        assert json.loads(json.dumps(turn_schema(["a", "b"]))) == turn_schema(["a", "b"])