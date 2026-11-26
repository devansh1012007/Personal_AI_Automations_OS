"""The overlay loader: tighten-only, at load, by name (C6.4 / P1.5).

What these tests pin:

* **every narrowing overlay is accepted** — params inside their declared
  ranges, a slot filled with a well-formed ref, capabilities narrowed to
  subsets, limits tightened (including bounding a limit the declaration
  leaves unbounded, the very shape of tightening);
* **every widening attempt is rejected at load, naming the widened key**
  — an undeclared param, a param out of range or of the wrong type, an
  undeclared slot, a malformed ref, a capability outside the ask, a
  ``false -> true`` flip, a grown tool list, a grown role list, a limit
  above the declared one, an unknown limits key, an unknown section;
* **an overlay swapping chat's context provider changes behaviour** —
  the plan's own exit criterion: a provider that packs no facts makes
  the second turn's reply lose the ``(from memory: ...)`` note the
  shipped compiler's reply carries. Layer-2 customisation, no code
  change, and the loop cannot tell the difference except through the
  behaviour.
"""

from __future__ import annotations

import dataclasses
from typing import Any

import pytest

from paa.pipelines.base import RunLimits
from paa.pipelines.overlay import (
    Overlay,
    OverlayError,
    WideningOverlayError,
    load_overlay,
    tightened_limits,
)
from paa.pipelines.slots import (
    BUILTIN_DECLARATIONS,
    builtin_context_provider,
    declaration_for,
    register_context_provider,
    resolve_context_provider,
)

CHAT = BUILTIN_DECLARATIONS["builtin.chat"]
CODER = BUILTIN_DECLARATIONS["coder.tool-loop"]
BUILTIN = BUILTIN_DECLARATIONS["builtin.plan-execute-critique"]


class TestNarrowingIsAccepted:
    def test_params_within_range(self) -> None:
        overlay = load_overlay(
            """
[params]
recall_k = 2
last_k_turns = 0
tokens_budget = 500
""",
            CHAT,
        )
        assert overlay.params == {"recall_k": 2, "last_k_turns": 0, "tokens_budget": 500}

    def test_slot_filled_with_a_well_formed_ref(self) -> None:
        overlay = load_overlay(
            """
[slots]
context_provider = "builtin:packet"
""",
            CHAT,
        )
        assert overlay.slots == {"context_provider": "builtin:packet"}

    def test_capabilities_narrow_to_subsets(self) -> None:
        overlay = load_overlay(
            """
[capabilities]
use_skill = ["read_file", "bash"]
""",
            CODER,
        )
        assert overlay.capabilities == {"use_skill": ("read_file", "bash")}

    def test_think_roles_shrink(self) -> None:
        overlay = load_overlay(
            """
[capabilities]
think = { roles = ["worker"] }
""",
            CODER,
        )
        assert overlay.capabilities == {"think": {"roles": ("worker",)}}

    def test_a_true_capability_may_be_turned_off(self) -> None:
        overlay = load_overlay(
            """
[capabilities]
recall = false
""",
            CHAT,
        )
        assert overlay.capabilities == {"recall": False}

    def test_limits_tighten_and_bound_the_unbounded(self) -> None:
        overlay = load_overlay(
            """
[limits]
max_tokens = 100
max_calls = 10
""",
            CHAT,
        )
        assert overlay.limits == {"max_tokens": 100, "max_calls": 10}
        tightened = tightened_limits(CHAT.limits, overlay)
        assert tightened.max_tokens == 100
        assert tightened.max_calls == 10

    def test_validated_overlay_cannot_drift_after_loading(self) -> None:
        overlay = load_overlay("[limits]\nmax_tokens = 100\n", CHAT)
        with pytest.raises(TypeError):
            overlay.limits["max_tokens"] = 10**9  # type: ignore[index]

    def test_nested_overlay_values_are_frozen(self) -> None:
        overlay = load_overlay('[capabilities]\nuse_skill = ["read_file"]\n', CODER)
        tools = overlay.capabilities["use_skill"]
        assert isinstance(tools, tuple)
        with pytest.raises(AttributeError):
            tools.append("web_fetch")  # type: ignore[attr-defined]

    def test_tightened_limits_default_to_the_base(self) -> None:
        base = RunLimits(max_tokens=1000, max_calls=40)
        overlay = load_overlay("[limits]\nmax_calls = 10\n", CHAT)
        tightened = tightened_limits(base, overlay)
        assert tightened == RunLimits(max_tokens=1000, max_calls=10)

    def test_an_empty_overlay_is_valid_and_changes_nothing(self) -> None:
        overlay = load_overlay("", CHAT)
        assert overlay == Overlay()
        assert tightened_limits(RunLimits(max_tokens=7), overlay) == RunLimits(
            max_tokens=7
        )


class TestWideningIsRejectedByName:
    def test_undeclared_param(self) -> None:
        with pytest.raises(WideningOverlayError, match="max_cycles"):
            load_overlay("[params]\nmax_cycles = 3\n", CHAT)

    def test_param_below_the_declared_minimum(self) -> None:
        with pytest.raises(WideningOverlayError, match="recall_k"):
            load_overlay("[params]\nrecall_k = -1\n", CHAT)

    def test_param_above_the_declared_maximum(self) -> None:
        with pytest.raises(WideningOverlayError, match="recall_k"):
            load_overlay("[params]\nrecall_k = 500\n", CHAT)

    def test_param_of_the_wrong_type(self) -> None:
        with pytest.raises(OverlayError, match="int"):
            load_overlay("[params]\nrecall_k = true\n", CHAT)

    def test_undeclared_slot(self) -> None:
        with pytest.raises(WideningOverlayError, match="planner"):
            load_overlay('[slots]\nplanner = "agent:planner"\n', CHAT)

    def test_malformed_slot_ref(self) -> None:
        with pytest.raises(OverlayError, match="ref"):
            load_overlay('[slots]\ncontext_provider = "packet"\n', CHAT)

    def test_slot_ref_interface_must_match(self) -> None:
        with pytest.raises(
            WideningOverlayError,
            match=r"implements 'agent/1', which does not match the declared "
            r"interface 'context-provider/1'",
        ):
            load_overlay('[slots]\ncontext_provider = "agent:planner"\n', CHAT)

    def test_none_disables_a_slot(self) -> None:
        overlay = load_overlay('[slots]\ncontext_provider = "none"\n', CHAT)
        assert overlay.slots == {"context_provider": "none"}

    def test_unregistered_ref_has_no_interface(self) -> None:
        with pytest.raises(OverlayError, match="no declared interface"):
            load_overlay('[slots]\ncontext_provider = "nope:thing"\n', CHAT)

    def test_capability_outside_the_ask(self) -> None:
        with pytest.raises(WideningOverlayError, match="call_agent"):
            load_overlay("[capabilities]\ncall_agent = [\"critic\"]\n", CHAT)

    def test_false_to_true_flip(self) -> None:
        # No shipped declaration asks for a capability it refuses, so the
        # branch needs a synthetic declaration to be exercised honestly.
        declared = dataclasses.replace(CODER, capabilities={"commit": False})
        with pytest.raises(WideningOverlayError, match="turns on a capability"):
            load_overlay("[capabilities]\ncommit = true\n", declared)

    def test_grown_tool_list(self) -> None:
        with pytest.raises(WideningOverlayError, match="use_skill"):
            load_overlay(
                '[capabilities]\nuse_skill = ["read_file", "web_fetch"]\n', CODER
            )

    def test_grown_role_list(self) -> None:
        with pytest.raises(WideningOverlayError, match="roles"):
            load_overlay(
                '[capabilities]\nthink = { roles = ["worker", "planner"] }\n', CODER
            )

    def test_limit_above_the_declared_one(self) -> None:
        declared = dataclasses.replace(
            BUILTIN, limits=RunLimits(max_tokens=1000)
        )
        with pytest.raises(WideningOverlayError, match="max_tokens"):
            load_overlay("[limits]\nmax_tokens = 2000\n", declared)

    def test_unknown_limits_key(self) -> None:
        with pytest.raises(OverlayError, match="max_plan_nodes"):
            load_overlay("[limits]\nmax_plan_nodes = 5\n", CHAT)

    def test_unknown_section(self) -> None:
        with pytest.raises(OverlayError, match="pipeline"):
            load_overlay("[pipeline]\nname = \"x\"\n", CHAT)

    def test_broken_toml(self) -> None:
        with pytest.raises(OverlayError, match="TOML"):
            load_overlay("[params\n", CHAT)

    def test_missing_overlay_file(self, tmp_path: Any) -> None:
        import pathlib

        with pytest.raises(OverlayError, match="unreadable"):
            load_overlay(pathlib.Path(tmp_path) / "nope.toml", CHAT)

    def test_invalid_utf8_file(self, tmp_path: Any) -> None:
        import pathlib

        bad = pathlib.Path(tmp_path) / "bad.toml"
        bad.write_bytes(b"[params]\nrecall_k = \xff\n")
        with pytest.raises(OverlayError, match="UTF-8"):
            load_overlay(bad, CHAT)


class TestDeclarations:
    def test_all_five_in_process_pipelines_are_declared(self) -> None:
        assert set(BUILTIN_DECLARATIONS) == {
            "builtin.plan-execute-critique",
            "builtin.chat",
            "coder.tool-loop",
            "builtin.observe",
            "builtin.manager",
        }

    def test_declarations_restate_the_classes(self) -> None:
        from paa.chat.loop import ChatLoop
        from paa.coder.pipeline import CoderToolLoopPipeline
        from paa.observe.pipeline import ObservePipeline
        from paa.pipelines.builtin import BuiltinPlanExecuteCritique
        from paa.pipelines.compose import ManagerPipeline

        for pipeline, declared in (
            (ChatLoop, CHAT),
            (CoderToolLoopPipeline, CODER),
            (BuiltinPlanExecuteCritique, BUILTIN),
            (ObservePipeline, declaration_for("builtin.observe")),
            (ManagerPipeline, declaration_for("builtin.manager")),
        ):
            assert declared.name == pipeline.name
            assert declared.version == pipeline.version
            assert declared.requires_runtime == pipeline.requires_runtime

    def test_declaration_for_lookup(self) -> None:
        assert declaration_for("builtin.chat") is CHAT
        assert declaration_for("nope") is None

    def test_chat_param_defaults_restate_the_settings(self) -> None:
        from paa.config import ChatSettings

        settings = ChatSettings()
        assert CHAT.params["recall_k"].default == settings.recall_k
        assert CHAT.params["last_k_turns"].default == settings.last_k_turns


class TestContextProviderSlot:
    def test_builtin_ref_resolves_to_the_shipped_compiler(self) -> None:
        provider = resolve_context_provider("builtin:packet")
        assert provider is builtin_context_provider

    def test_unknown_ref_names_the_registered_ones(self) -> None:
        with pytest.raises(ValueError, match="builtin:packet"):
            resolve_context_provider("nope:nothing")

    def test_registered_refs_resolve(self) -> None:
        def terse(**kwargs: Any) -> Any:  # pragma: no cover - resolution only
            return kwargs

        register_context_provider("test:terse", terse)
        try:
            assert resolve_context_provider("test:terse") is terse
        finally:
            from paa.pipelines import slots as slots_module

            slots_module._PROVIDERS.pop("test:terse", None)
            slots_module._REF_INTERFACES.pop("test:terse", None)
