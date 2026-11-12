"""N2.4: mandate v0 — the authority record for tasks (ADR-0046).

What is pinned here
-------------------
* The v1 ``MANDATE_GRANTED`` / ``MANDATE_REFUSED`` payloads produced by
  the mandate module (golden dicts, field-for-field against ADR-0046).
* The effective policy: content-addressed, canonical, deterministic; a
  different mode hashes differently; LOCKDOWN forces strictness.
* Basis honesty: explicit / inferred / implicit are counted separately
  and never folded into one coverage number.
* Submission behaviour: an explicit mandate lands as the lineage head;
  no mandate lands as implicit; strict mode refuses non-explicit
  mandates with a ``MANDATE_REFUSED`` event; a stale policy version
  (policy changed after the mandate was created) refuses as well.
* The refusal event carries the correlation lineage so the caller can
  find it again.

``ScriptedModel`` mirrors ``tests/api/conftest.py`` (and the pattern in
``tests/coder/test_agent.py``) so the runtime can be built without a
model server.
"""

from __future__ import annotations

import hashlib
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from paa.agents.orchestrator import ChiefOrchestrator, TaskRequest
from paa.config import PolicySettings, Settings, reset_settings_cache
from paa.core.errors import PaaError
from paa.core.types import EventType, PermissionMode, new_correlation_id
from paa.ledger.events import LedgerEvent, canonical_json
from paa.ledger.mandate import (
    MANDATE_SCHEMA_VERSION,
    Mandate,
    MandateBasis,
    MandateCounts,
    PolicyVersion,
    RequesterAsserted,
    Scope,
    count_bases,
    effective_policy_version,
    explicit_mandate,
    head_mandate_summary,
    implicit_mandate,
    require_explicit_mandate,
)
from paa.ledger.store import LedgerStore
from paa.runtime import Runtime


class ScriptedModel:
    """Deterministic ModelLike returning valid structured outputs."""

    def __init__(self) -> None:
        self.calls = 0

    async def complete_structured(
        self, prompt: str, schema: dict[str, Any], **kw: Any
    ) -> dict[str, Any]:
        self.calls += 1
        props = schema.get("properties", {})
        if "execution_steps" in props:
            return {
                "execution_steps": [{"index": 0, "action": "do it", "agent": "worker"}],
                "step_requirements": {},
            }
        if "verdict" in props:
            return {"verdict": "PASS", "findings": []}
        if "sub_requests" in props:
            return {"modality": "STANDARD", "sub_requests": [prompt], "candidate_agents": []}
        return {}


@pytest.fixture
async def runtime(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[Runtime]:
    home = tmp_path / "paa_home"
    home.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("PAA_HOME", str(home))
    reset_settings_cache()
    settings = Settings(home=home)
    settings.policy.mode = PermissionMode.AUTO
    settings.sandbox.backend = "subprocess"
    rt = await Runtime.build(
        settings,
        model_adapter=ScriptedModel(),
        run_recovery=False,
        enable_optional_backends=False,
    )
    try:
        yield rt
    finally:
        await rt.close()
        reset_settings_cache()


@pytest.fixture
async def lockdown_runtime(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> AsyncIterator[Runtime]:
    """A runtime whose effective policy is LOCKDOWN (strict forced on)."""
    home = tmp_path / "paa_home_lockdown"
    home.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("PAA_HOME", str(home))
    reset_settings_cache()
    settings = Settings(home=home)
    settings.policy.mode = PermissionMode.LOCKDOWN
    settings.sandbox.backend = "subprocess"
    rt = await Runtime.build(
        settings,
        model_adapter=ScriptedModel(),
        run_recovery=False,
        enable_optional_backends=False,
    )
    try:
        yield rt
    finally:
        await rt.close()
        reset_settings_cache()


def _event(etype: EventType, **payload: Any) -> LedgerEvent:
    return LedgerEvent(
        correlation_id=new_correlation_id(), event_type=etype, payload=dict(payload)
    )


class TestGoldenV1Payloads:
    """The producer's payloads match the ADR-0046 v1 contract exactly."""

    def test_granted_payload_is_golden_v1(self) -> None:
        mandate = explicit_mandate(
            kind="cli",
            requester_id="operator",
            policy=PolicySettings(mode=PermissionMode.ASK),
            workspace="/tmp/ws",
            budget=10,
        )
        payload = mandate.granted_payload(session_id=None)
        assert payload == {
            "v": 1,
            "session_id": None,
            "requester_asserted": {"kind": "cli", "id": "operator"},
            "identity_assurance": "asserted",
            "basis": "explicit",
            "policy_version": mandate.policy_version.sha256,
            "policy_label": "ASK",
            "scope": {"workspace": "/tmp/ws", "permission_mode": "ASK", "budget": 10},
        }
        assert payload["v"] == MANDATE_SCHEMA_VERSION == 1

    def test_granted_payload_session_id_is_a_string_for_coder_sessions(self) -> None:
        mandate = explicit_mandate(
            kind="coder",
            requester_id="operator",
            policy=PolicySettings(mode=PermissionMode.ASK),
        )
        payload = mandate.granted_payload(session_id="sess-1")
        assert payload["session_id"] == "sess-1"

    def test_refused_payload_is_golden_v1(self) -> None:
        mandate = Mandate(
            requester_asserted=RequesterAsserted(kind="api", id="alice"),
            basis=MandateBasis.EXPLICIT,
            policy_version=PolicyVersion(sha256="f" * 64, label="ask"),
        )
        payload = mandate.refused_payload(session_id=None, reason="require_explicit_mandate")
        assert payload == {
            "v": 1,
            "session_id": None,
            "requester_asserted": {"kind": "api", "id": "alice"},
            "reason": "require_explicit_mandate",
            "policy_version": "f" * 64,
        }

    def test_payloads_carry_no_fact_id(self) -> None:
        """Mandate payloads must not leak into tier state (W2-REV-01 MINOR-2)."""
        mandate = explicit_mandate(
            kind="cli", requester_id="operator", policy=PolicySettings()
        )
        assert "fact_id" not in mandate.granted_payload()
        assert "fact_id" not in mandate.refused_payload(
            session_id=None, reason="require_explicit_mandate"
        )


class TestEffectivePolicy:
    """The policy version is the hash of the canonical effective policy."""

    def test_version_is_sha256_of_canonical_effective_policy(self) -> None:
        policy = PolicySettings(mode=PermissionMode.ASK)
        version = effective_policy_version(policy)
        canonical = {
            "mode": "ASK",
            "anti_goal_threshold": policy.anti_goal_threshold,
            "auto_confidence_floor": policy.auto_confidence_floor,
            "always_gate_risk_profile": policy.always_gate_risk_profile,
            "latency_budget_ms": policy.latency_budget_ms,
            "require_explicit_mandate": False,
        }
        assert version.sha256 == hashlib.sha256(
            canonical_json(canonical).encode()
        ).hexdigest()
        assert version.label == "ASK"

    def test_version_is_deterministic(self) -> None:
        policy = PolicySettings(mode=PermissionMode.ASK)
        assert effective_policy_version(policy) == effective_policy_version(policy)

    def test_different_mode_hashes_differently(self) -> None:
        ask = effective_policy_version(PolicySettings(mode=PermissionMode.ASK))
        auto = effective_policy_version(PolicySettings(mode=PermissionMode.AUTO))
        assert ask.sha256 != auto.sha256
        assert ask.label != auto.label

    def test_lockdown_forces_strict(self) -> None:
        policy = PolicySettings(mode=PermissionMode.LOCKDOWN)
        assert require_explicit_mandate(policy) is True
        # The forced value is what the hash sees: a LOCKDOWN policy whose
        # flag was never set still hashes with strictness on.
        version = effective_policy_version(policy)
        canonical = {
            "mode": "LOCKDOWN",
            "anti_goal_threshold": policy.anti_goal_threshold,
            "auto_confidence_floor": policy.auto_confidence_floor,
            "always_gate_risk_profile": policy.always_gate_risk_profile,
            "latency_budget_ms": policy.latency_budget_ms,
            "require_explicit_mandate": True,
        }
        assert version.sha256 == hashlib.sha256(
            canonical_json(canonical).encode()
        ).hexdigest()

    def test_default_policy_is_not_strict(self) -> None:
        assert require_explicit_mandate(PolicySettings()) is False


class TestMandateValidation:
    """Construction refuses values that would make the record a lie."""

    def test_empty_requester_is_rejected(self) -> None:
        with pytest.raises(ValueError, match="kind and id"):
            RequesterAsserted(kind="", id="alice")
        with pytest.raises(ValueError, match="kind and id"):
            RequesterAsserted(kind="cli", id="  ")

    def test_negative_budget_is_rejected(self) -> None:
        with pytest.raises(ValueError, match="budget"):
            Scope(budget=-1)

    def test_non_asserted_assurance_is_rejected(self) -> None:
        kwargs: dict[str, Any] = {
            "requester_asserted": RequesterAsserted(kind="cli", id="operator"),
            "basis": MandateBasis.EXPLICIT,
            "policy_version": PolicyVersion(sha256="f" * 64, label="ask"),
            # Not a v0 value: the check lives in __post_init__, and
            # construction must fail even though the field's type says
            # otherwise (the dict unpacks past the Literal on purpose).
            "identity_assurance": "authenticated",
        }
        with pytest.raises(ValueError, match="asserted"):
            Mandate(**kwargs)

    def test_secret_shaped_requester_id_is_redacted(self) -> None:
        mandate = explicit_mandate(
            kind="api",
            requester_id="sk-proj-aaaaaaaaaaaaaaaaaaaa",
            policy=PolicySettings(),
        )
        payload = mandate.granted_payload()
        assert payload["requester_asserted"]["id"] != "sk-proj-aaaaaaaaaaaaaaaaaaaa"
        assert "sk-proj" not in payload["requester_asserted"]["id"]


class TestImplicitMandate:
    def test_implicit_mandate_shape(self) -> None:
        policy = PolicySettings(mode=PermissionMode.ASK)
        mandate = implicit_mandate(policy)
        assert mandate.basis is MandateBasis.IMPLICIT
        assert mandate.requester_asserted == RequesterAsserted(
            kind="runtime", id="implicit"
        )
        assert mandate.policy_version == effective_policy_version(policy)
        payload = mandate.granted_payload(session_id=None)
        assert payload["basis"] == "implicit"
        assert payload["identity_assurance"] == "asserted"


class TestCountBases:
    """Basis counts are three facts, never one coverage number."""

    def test_counts_are_separate(self) -> None:
        def granted(basis: str, requester: dict[str, str]) -> LedgerEvent:
            return _event(
                EventType.MANDATE_GRANTED,
                v=1,
                session_id=None,
                requester_asserted=requester,
                identity_assurance="asserted",
                basis=basis,
                policy_version="0" * 64,
                policy_label="ask",
                scope={"workspace": None, "permission_mode": "ask", "budget": None},
            )

        events: list[LedgerEvent] = [
            granted("explicit", {"kind": "cli", "id": "operator"}),
            granted("inferred", {"kind": "api", "id": "alice"}),
            granted("implicit", {"kind": "runtime", "id": "implicit"}),
            granted("implicit", {"kind": "runtime", "id": "implicit"}),
            _event(
                EventType.MANDATE_REFUSED,
                v=1,
                session_id=None,
                requester_asserted={"kind": "api", "id": "alice"},
                reason="require_explicit_mandate",
                policy_version="0" * 64,
            ),
        ]
        counts = count_bases(events)
        assert counts.explicit == 1
        assert counts.inferred == 1
        assert counts.implicit == 2
        assert counts.refused == 1
        # Implicit is not coverage: folding it in would claim 4 grants
        # where only one mandate was ever explicitly asserted.
        assert counts.implicit != 0

    def test_grant_without_basis_is_counted_nowhere(self) -> None:
        events = [
            _event(
                EventType.MANDATE_GRANTED,
                v=1,
                session_id=None,
                requester_asserted={"kind": "cli", "id": "operator"},
                policy_version="0" * 64,
                # No basis: not evidence under v1.
            )
        ]
        assert count_bases(events) == MandateCounts()

    def test_non_mandate_events_are_ignored(self) -> None:
        events = [_event(EventType.TASK_REQUESTED, request={"goal": "x"})]
        assert count_bases(events) == MandateCounts()


class TestHeadMandateSummary:
    def test_summary_names_basis_requester_policy_workspace(self) -> None:
        event = _event(
            EventType.MANDATE_GRANTED,
            v=1,
            session_id=None,
            requester_asserted={"kind": "cli", "id": "operator"},
            identity_assurance="asserted",
            basis="explicit",
            policy_version="0" * 64,
            policy_label="ask",
            scope={"workspace": "/tmp/ws", "permission_mode": "ask", "budget": 10},
        )
        summary = head_mandate_summary([event])
        assert summary is not None
        assert "basis=explicit" in summary
        assert "requester=cli:operator" in summary
        assert "policy=ask" in summary
        assert "workspace=/tmp/ws" in summary

    def test_no_grant_means_no_summary(self) -> None:
        event = _event(EventType.TASK_REQUESTED, request={"goal": "x"})
        assert head_mandate_summary([event]) is None
        assert head_mandate_summary([]) is None


class TestSubmitBehaviour:
    """End-to-end submit: head grant, implicit default, strict refusal."""

    async def test_explicit_mandate_lands_as_head(self, runtime: Runtime) -> None:
        mandate = explicit_mandate(
            kind="cli", requester_id="operator", policy=runtime.settings.policy
        )
        cid = await runtime.submit("do something", mandate=mandate)
        events = await runtime.ledger.read_correlation(cid)
        assert events[0].event_type is EventType.MANDATE_GRANTED
        assert events[0].state_version == 1
        payload = events[0].payload
        assert payload["basis"] == "explicit"
        assert payload["requester_asserted"] == {"kind": "cli", "id": "operator"}
        assert payload["policy_version"] == effective_policy_version(
            runtime.settings.policy
        ).sha256
        assert events[1].event_type is EventType.TASK_REQUESTED

    async def test_no_mandate_submits_implicit(self, runtime: Runtime) -> None:
        """Backward compatibility: every old call site submits as implicit."""
        cid = await runtime.submit("do something")
        events = await runtime.ledger.read_correlation(cid)
        assert events[0].event_type is EventType.MANDATE_GRANTED
        assert events[0].payload["basis"] == "implicit"
        assert events[1].event_type is EventType.TASK_REQUESTED

    async def test_strict_mode_refuses_implicit_in_lockdown(
        self, lockdown_runtime: Runtime
    ) -> None:
        with pytest.raises(PaaError) as excinfo:
            await lockdown_runtime.submit("do something")
        assert excinfo.value.details["reason"] == "require_explicit_mandate"
        refused = await lockdown_runtime.ledger.events_since(
            datetime(1970, 1, 1, tzinfo=UTC),
            event_types=[EventType.MANDATE_REFUSED],
        )
        assert len(refused) == 1
        payload = refused[0].payload
        assert payload["reason"] == "require_explicit_mandate"
        assert payload["v"] == 1
        assert payload["requester_asserted"] == {"kind": "runtime", "id": "implicit"}
        # The lineage the exception points at is the refusal lineage.
        assert (
            str(refused[0].correlation_id)
            == excinfo.value.details["refusal_correlation_id"]
        )

    async def test_strict_mode_accepts_explicit_in_lockdown(
        self, lockdown_runtime: Runtime
    ) -> None:
        mandate = explicit_mandate(
            kind="cli", requester_id="operator", policy=lockdown_runtime.settings.policy
        )
        cid = await lockdown_runtime.submit("do something", mandate=mandate)
        events = await lockdown_runtime.ledger.read_correlation(cid)
        assert events[0].event_type is EventType.MANDATE_GRANTED
        assert events[0].payload["basis"] == "explicit"

    async def test_stale_policy_version_refuses(self, runtime: Runtime) -> None:
        """Policy changed after the mandate was created -> event, not silent."""
        stale = explicit_mandate(
            kind="cli",
            requester_id="operator",
            policy=PolicySettings(mode=PermissionMode.ASK),
        )
        with pytest.raises(PaaError) as excinfo:
            await runtime.submit("do something", mandate=stale)
        assert excinfo.value.details["reason"] == "policy_changed"
        refused = await runtime.ledger.events_since(
            datetime(1970, 1, 1, tzinfo=UTC),
            event_types=[EventType.MANDATE_REFUSED],
        )
        assert len(refused) == 1
        assert refused[0].payload["reason"] == "policy_changed"
        assert refused[0].payload["policy_version"] == stale.policy_version.sha256

    async def test_refusal_lineage_is_not_a_task(self, runtime: Runtime) -> None:
        """A refused lineage carries no TASK_REQUESTED — no work proceeded."""
        stale = explicit_mandate(
            kind="cli",
            requester_id="operator",
            policy=PolicySettings(mode=PermissionMode.ASK),
        )
        with pytest.raises(PaaError) as exc:
            await runtime.submit("do something", mandate=stale)
        refused_cid = exc.value.details["refusal_correlation_id"]
        events = await runtime.ledger.read_correlation(refused_cid)
        assert [e.event_type for e in events] == [EventType.MANDATE_REFUSED]

    async def test_inferred_basis_counts_separately_after_submit(
        self, runtime: Runtime
    ) -> None:
        mandate = Mandate(
            requester_asserted=RequesterAsserted(kind="api", id="alice"),
            basis=MandateBasis.INFERRED,
            policy_version=effective_policy_version(runtime.settings.policy),
        )
        cid = await runtime.submit("do something", mandate=mandate)
        events = await runtime.ledger.read_correlation(cid)
        counts = count_bases(events)
        assert counts.inferred == 1
        assert counts.explicit == 0
        assert counts.implicit == 0


class TestOrchestratorHeadEmission:
    """The orchestrator alone writes the grant before the request."""

    async def test_mandate_emitted_before_task_requested(
        self, ledger: LedgerStore
    ) -> None:
        orchestrator = ChiefOrchestrator(ledger)
        mandate = explicit_mandate(
            kind="cli",
            requester_id="operator",
            policy=PolicySettings(mode=PermissionMode.ASK),
        )
        cid = await orchestrator.submit(TaskRequest(goal="do something", mandate=mandate))
        events = await ledger.read_correlation(cid)
        assert [e.event_type for e in events[:2]] == [
            EventType.MANDATE_GRANTED,
            EventType.TASK_REQUESTED,
        ]
        assert events[0].payload["basis"] == "explicit"

    async def test_no_mandate_means_no_head_event(self, ledger: LedgerStore) -> None:
        """A request that bypassed the runtime carries no mandate event."""
        orchestrator = ChiefOrchestrator(ledger)
        cid = await orchestrator.submit(TaskRequest(goal="do something"))
        events = await ledger.read_correlation(cid)
        assert events[0].event_type is EventType.TASK_REQUESTED
