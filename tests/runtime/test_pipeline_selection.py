"""The runtime's own pipeline selection — one door, recorded in the ledger.

The review round's Medium: ``Runtime.run()`` called ``orchestrator.run``
directly, so a task submitted through the core runtime could not choose a
pipeline — selection existed only behind the chat and CLI surfaces. These
tests pin the door that closed it: selection happens at
:meth:`paa.runtime.Runtime.submit` (explicit, mandate-scoped, zero trace on
refusal), the choice is recorded on the ``TASK_REQUESTED`` payload (the
ledger is the truth), and :meth:`Runtime.run` reads it back from the lineage
to dispatch — the default task pipeline through the orchestrator,
``builtin.manager`` as a composition run on the same lineage, and anything
else refused loudly with its one-shot door named.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest

from paa.config import Settings, reset_settings_cache
from paa.core.errors import PaaError
from paa.core.types import EventType, PermissionMode
from paa.ledger.events import LedgerEvent
from paa.ledger.mandate import explicit_mandate
from paa.pipelines.base import PipelineStatus
from paa.pipelines.registry import PipelineSelectionError
from paa.runtime import Runtime


class _StubModel:
    """The leanest ``ModelLike``: nothing here should ever call it.

    Selection and submission are deterministic doors, and the composition
    path runs host-minted runners that touch no model — so a call arriving
    at this model is itself a defect worth failing on.
    """

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    async def complete_structured(
        self, prompt: str, schema: dict[str, Any], **kwargs: Any
    ) -> dict[str, Any]:
        self.calls.append({"prompt": prompt, "schema": schema, "kwargs": kwargs})
        raise AssertionError("the selection door must not reach a model")


@pytest.fixture
async def runtime(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> AsyncIterator[Runtime]:
    home = tmp_path / "paa_home"
    home.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("PAA_HOME", str(home))
    reset_settings_cache()

    settings = Settings(home=home)
    settings.policy.mode = PermissionMode.AUTO
    settings.sandbox.backend = "subprocess"
    rt = await Runtime.build(
        settings,
        model_adapter=_StubModel(),
        run_recovery=False,
        enable_optional_backends=False,
    )
    try:
        yield rt
    finally:
        await rt.close()
        reset_settings_cache()


def _counting_append(rt: Runtime, monkeypatch: pytest.MonkeyPatch) -> list[LedgerEvent]:
    """Wrap the ledger's append so a test can prove a refusal left no event."""
    appended: list[LedgerEvent] = []
    original = rt.ledger.append

    async def counting(event: LedgerEvent) -> LedgerEvent:
        appended.append(event)
        return await original(event)

    monkeypatch.setattr(rt.ledger, "append", counting)
    return appended


async def test_selection_is_recorded_and_the_manager_runs_as_composition(
    runtime: Runtime,
) -> None:
    decomposition = json.dumps(
        {"delegations": [{"pipeline": "builtin.chat", "goal": "say hi"}]}
    )
    cid = await runtime.submit(decomposition, pipeline="builtin.manager")

    events = await runtime.ledger.read_correlation(cid)
    requested = next(e for e in events if e.event_type is EventType.TASK_REQUESTED)
    assert requested.payload["request"]["pipeline"] == "builtin.manager"
    assert requested.payload["request"]["goal"] == decomposition

    result = await runtime.run(cid)
    assert result.status is PipelineStatus.COMPLETED
    children = result.metrics.get("children", [])
    assert [child["pipeline"] for child in children] == ["builtin.chat"]

    # The composition journaled on the task's own lineage: one door, one
    # truth, traceable with `paa ledger` / `paa pipeline trace`.
    types = {e.event_type for e in await runtime.ledger.read_correlation(cid)}
    assert EventType.PIPELINE_RUN_STARTED in types
    assert EventType.PIPELINE_RUN_COMPLETED in types


async def test_an_unknown_pipeline_refuses_with_zero_trace(
    runtime: Runtime, monkeypatch: pytest.MonkeyPatch
) -> None:
    appended = _counting_append(runtime, monkeypatch)
    with pytest.raises(PipelineSelectionError) as excinfo:
        await runtime.submit("goal", pipeline="nope.nope")
    assert "nope.nope" in excinfo.value.message
    # A caller error is not an authority event: nothing is appended, no
    # lineage exists, and the operator's ledger is unmoved.
    assert appended == []


async def test_selection_respects_the_mandates_pipeline_scope(
    runtime: Runtime, monkeypatch: pytest.MonkeyPatch
) -> None:
    mandate = explicit_mandate(
        kind="test",
        requester_id="operator",
        policy=runtime.settings.policy,
        pipelines=["builtin.chat"],
    )
    appended = _counting_append(runtime, monkeypatch)
    with pytest.raises(PipelineSelectionError) as excinfo:
        await runtime.submit(
            "goal", mandate=mandate, pipeline="builtin.manager"
        )
    assert "scope" in excinfo.value.message
    assert appended == []


async def test_a_one_shot_pipeline_is_refused_loudly_not_run_silently(
    runtime: Runtime,
) -> None:
    """A pipeline whose driver owns its models names its own door.

    The selection is recorded (the operator's word), so the run cannot
    quietly ignore it: the refusal says where that pipeline actually runs.
    """
    cid = await runtime.submit("hello", pipeline="builtin.chat")
    with pytest.raises(PaaError) as excinfo:
        await runtime.run(cid)
    assert "one-shot door" in excinfo.value.message
    assert "builtin.chat" in excinfo.value.message


async def test_a_task_without_selection_records_no_pipeline_key(
    runtime: Runtime,
) -> None:
    """The payload key appears only when a pipeline was named.

    Events from before the field existed must replay identically, so the
    default submission leaves the request payload exactly as it was.
    """
    cid = await runtime.submit("plain goal")
    events = await runtime.ledger.read_correlation(cid)
    requested = next(e for e in events if e.event_type is EventType.TASK_REQUESTED)
    assert "pipeline" not in requested.payload["request"]


def test_default_host_runners_covers_every_runnable_builtin_but_the_manager() -> None:
    """The one runner table both composition doors share.

    Every runnable builtin except the manager itself gets a runner; the
    manager is the composition, not one of its own workers.
    """
    from paa.pipelines.compose import ManagerPipeline, default_host_runners
    from paa.pipelines.registry import PipelineRegistry

    registry = PipelineRegistry(local_dir=None)
    runners = default_host_runners(registry)
    expected = sorted(
        row.name
        for row in registry.list()
        if row.runnable and row.name != ManagerPipeline.name
    )
    assert sorted(runners) == expected
    assert ManagerPipeline.name not in runners
