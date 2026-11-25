"""The overlay behaviour proof, chat half (C6.4 / P1.5).

The loader's tighten-only table is pinned in
``tests/pipelines/test_overlay.py``; this file pins the plan's exit
criterion where the ``chat`` fixture lives: **an overlay swapping
chat's context provider changes behaviour**. Turn 1 teaches the session
a fact. Turn 2 with the shipped provider recalls it, packs it, and the
echo model says so — ``(from memory: ...)``. The same turn 2 under an
overlay that fills the ``context_provider`` slot with a terse provider
(packs nothing) loses the note. Same session shape, same message, same
model: only the slot differs, and the reply is the proof — layer-2
customisation with no code change.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from paa.chat.context_bundle import build_context_bundle
from paa.chat.service import ChatService
from paa.core.types import PermissionMode
from paa.pipelines.overlay import load_overlay
from paa.pipelines.slots import (
    BUILTIN_DECLARATIONS,
    register_context_provider,
    resolve_context_provider,
)
from tests.chat.conftest import ChatHarness

CHAT = BUILTIN_DECLARATIONS["builtin.chat"]


def _terse(
    *,
    persona: str,
    recalled_facts: list[dict[str, Any]],
    recent_turns: list[dict[str, Any]],
    token_ceiling: int,
) -> Any:
    """A provider that packs no facts, whatever recall served."""
    return build_context_bundle(
        persona="",
        recalled_facts=[],
        recent_turns=recent_turns,
        token_ceiling=token_ceiling,
    )


class TestSwappingTheContextProviderChangesBehaviour:
    async def test_the_reply_changes_when_the_slot_changes(
        self, chat: Callable[..., ChatHarness]
    ) -> None:
        # -- the shipped provider: turn 2's reply carries the memory note
        default_harness = chat()
        sid = await default_harness.service.create_session()
        await default_harness.service.send(sid, "Apollo is blocked until Friday.")

        second = await default_harness.service.send(sid, "What happens on Friday?")
        assert "(from memory:" in second.reply

        # -- the overlay: fill the slot, tighten a knob — accepted at load.
        # Registration sits inside the guarded block so a failed load
        # cannot leak a provider into a later test in the same process.
        try:
            register_context_provider("test:terse", _terse)
            overlay = load_overlay(
                """
[slots]
context_provider = "test:terse"

[params]
recall_k = 1
""",
                CHAT,
            )
            assert overlay.slots == {"context_provider": "test:terse"}
            provider = resolve_context_provider(overlay.slots["context_provider"])
            # The host threads the resolved provider into the service it
            # builds — validation at load, application here, never inside
            # the loader.
            terse_harness = chat()
            terse_service = ChatService(
                ledger=terse_harness.ledger,
                db=terse_harness.db,
                creator=terse_harness.creator,
                settings=terse_harness.settings,
                permission_mode=PermissionMode.ASK,
                context_settings=terse_harness.context_settings,
                signals=terse_harness.signals,
                context_provider=provider,
            )
            sid2 = await terse_service.create_session()
            await terse_service.send(sid2, "Apollo is blocked until Friday.")
            terse_second = await terse_service.send(sid2, "What happens on Friday?")
            assert "(from memory:" not in terse_second.reply
            assert terse_second.recalled_fact_ids, (
                "recall must still serve the fact; only rendering changed"
            )
        finally:
            from paa.pipelines import slots as slots_module

            slots_module._PROVIDERS.pop("test:terse", None)
            slots_module._REF_INTERFACES.pop("test:terse", None)
