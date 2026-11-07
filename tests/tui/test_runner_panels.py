"""TREV-W3 P1: panel builders must surface real events, not empty forever.

W3's dashboard tests drove ``build_panel_fields`` with an empty fake
ledger, so a builder that returned ``[]``/``0`` forever passed every test
(the mutation survivor). This file drives the same builder with a real
ledger carrying real events on two open lineages: one parked on a human
gate with a POLICY decision, one executing. The assertions require the
three ledger-backed panels to be non-empty and correctly shaped, so an
empty-forever builder fails them by construction.

Tmp ``db`` + ``ledger`` fixtures only; the runtime is a namespace over
the real ledger (no mock of the unit under test).
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

from paa.core.types import EventType, new_correlation_id
from paa.ledger.events import LedgerEvent
from paa.ledger.store import LedgerStore
from paa.storage.relational.database import Database
from paa.tui.runner import build_panel_fields


def _settings() -> Any:
    return SimpleNamespace(policy=SimpleNamespace(mode=SimpleNamespace(value="ask")))


class TestPanelBuilderHappyPaths:
    async def test_two_open_lineages_populate_all_three_ledger_panels(
        self, db: Database, ledger: LedgerStore
    ) -> None:
        awaiting_cid = new_correlation_id()
        executing_cid = new_correlation_id()

        # Lineage 1: POLICY decision, then parked on the human gate.
        await ledger.append(
            LedgerEvent.create(
                awaiting_cid,
                EventType.TASK_REQUESTED,
                payload={"request": {"goal": "awaiting goal"}},
            )
        )
        await ledger.append(
            LedgerEvent.create(
                awaiting_cid,
                EventType.POLICY_CLEARED,
                payload={"decision": "AUTO_APPROVE", "reason": "low risk"},
            )
        )
        await ledger.append(
            LedgerEvent.create(
                awaiting_cid,
                EventType.AWAITING_HUMAN_ATTESTATION,
                payload={"reason": "needs human"},
                agent_role="planner",
            )
        )

        # Lineage 2: POLICY decision, then executing.
        await ledger.append(
            LedgerEvent.create(
                executing_cid,
                EventType.TASK_REQUESTED,
                payload={"request": {"goal": "executing goal"}},
            )
        )
        await ledger.append(
            LedgerEvent.create(
                executing_cid,
                EventType.POLICY_CLEARED,
                payload={"decision": "STATUS_APPROVED", "reason": "cleared"},
            )
        )
        await ledger.append(
            LedgerEvent.create(
                executing_cid,
                EventType.EXECUTION_STARTED,
                payload={"step_index": 0},
                agent_role="worker",
            )
        )

        rt = SimpleNamespace(
            db=db,
            ledger=ledger,
            agents={"planner": SimpleNamespace(), "worker": SimpleNamespace()},
        )
        panels = await build_panel_fields(_settings(), rt)

        # attention_top stays null this wave (P-07 allocator not landed).
        assert panels["attention_top"] is None
        # Counts come from the one aggregate query over the (empty) fact
        # table: zeros, not a crash or a missing key.
        assert panels["memory_browser_counts"] == {
            "total": 0,
            "by_domain": {},
            "by_status": {},
        }

        # decision_tracker surfaces raw policy_decision + policy_reason.
        tracker = panels["decision_tracker"]
        assert isinstance(tracker, list) and len(tracker) == 2
        by_id = {entry["correlation_id"]: entry for entry in tracker}
        assert set(by_id) == {str(awaiting_cid), str(executing_cid)}
        assert by_id[str(awaiting_cid)] == {
            "correlation_id": str(awaiting_cid),
            "policy_decision": "AUTO_APPROVE",
            "policy_reason": "low risk",
        }
        assert by_id[str(executing_cid)]["policy_decision"] == "STATUS_APPROVED"
        assert by_id[str(executing_cid)]["policy_reason"] == "cleared"

        # autonomy_state counts the AWAITING_HUMAN lineage only.
        assert panels["autonomy_state"] == {
            "mode": "ask",
            "awaiting_gates": 1,
            "awaiting_ids": [str(awaiting_cid)],
        }

        # agent_monitor collects the last event's agent_role per lineage,
        # plus the loaded agent keys.
        monitor = panels["agent_monitor"]
        assert monitor["agents"] == ["planner", "worker"]
        assert set(monitor["recent_roles"]) == {"planner", "worker"}
        assert len(monitor["recent_roles"]) == 2

    async def test_a_lineage_without_a_policy_decision_is_not_a_decision_entry(
        self, db: Database, ledger: LedgerStore
    ) -> None:
        """A POLICY-less lineage is excluded from the tracker, not nulled.

        The tracker lists only lineages whose projection carries a
        decision; a lineage with no decision is skipped rather than
        emitted with null fields or crashing the builder.
        """
        plain_cid = new_correlation_id()
        await ledger.append(
            LedgerEvent.create(
                plain_cid,
                EventType.TASK_REQUESTED,
                payload={"request": {"goal": "plain goal"}},
            )
        )
        rt = SimpleNamespace(db=db, ledger=ledger, agents={})
        panels = await build_panel_fields(_settings(), rt)

        assert panels["decision_tracker"] == []
        assert panels["autonomy_state"]["awaiting_gates"] == 0
        assert panels["agent_monitor"] == {"agents": [], "recent_roles": []}
