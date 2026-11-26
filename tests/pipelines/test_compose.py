"""The layer-4 composition contract (C8.2): halving, guards, isolation, replay.

What these tests pin, in the house style of ``tests/pipelines/test_journal.py``:

* **budget halving, measured** — a child's limits are half of the parent's
  *remaining* budget per key (1000 → 500 → 250), the grant leaves the
  wallet, ``max(1, n // 2)`` floors the metered keys, ``max_sub_runs``
  halves with no floor, and ``None`` stays ``None``;
* **the recursion guards** — a direct cycle (A→A) and an indirect cycle
  (A→B→A) refuse by name-stack membership, the depth ceiling
  (``MAX_COMPOSITION_DEPTH``) refuses a chain of distinct names, and a
  run that spent its ``max_sub_runs`` count refuses before the ledger;
* **capability discipline** — a facade without the SUB_RUN grant refuses
  with ``CapabilityNotGrantedError``; the child's grants come from its
  own declaration, never the parent's (the anti-laundering rule);
* **child isolation** — a worker's failure (result or crash) is the
  child's outcome, returned to the parent without crashing the run,
  while the composition's own control errors propagate to the root;
  ``CancelledError`` is journalled then re-raised;
* **the manager demo** — ``builtin.manager`` delegates to two different
  workers through the real facade door and aggregates their results;
* **replay** — a re-run is served the recorded child results with zero
  re-execution, and a changed call diverges the run;
* **journal row shapes** — ``sub_run`` rows carry the v=1 golden shape:
  params only as sha + shape preview, results attested by sha, FAILED
  rows never attesting a result.
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import Mapping
from typing import Any

import pytest

from paa.core.errors import BudgetExceededError, RecursionGuardError
from paa.ledger.store import LedgerStore
from paa.pipelines.base import (
    Capability,
    CapabilityNotGrantedError,
    PipelineDivergedError,
    PipelineResult,
    PipelineStatus,
    PipelineTask,
    RunLimits,
)
from paa.pipelines.builtin import (
    BuiltinPipelineFacade,
    PipelineHost,
    StageAgents,
    SubRunExecutor,
)
from paa.pipelines.compose import (
    MAX_COMPOSITION_DEPTH,
    ChildRun,
    SubRunComposer,
    declared_capabilities,
    halve_budget,
    run_composition,
)
from paa.pipelines.journal import CallJournal, CallOutcome, read_call_journal
from paa.pipelines.registry import PipelineRegistry
from paa.pipelines.replay import ReplayView
from paa.pipelines.slots import PipelineDeclaration

# ---------------------------------------------------------------------------
# Shared scripted building blocks
# ---------------------------------------------------------------------------


class ScriptedWorker:
    """A host-minted runner: records the ChildRun it received, answers fixed.

    The counts and the received grants/limits are what the halving and the
    anti-laundering assertions read; ``raise_exc`` / ``fail`` script the
    isolation cases; ``slow`` parks inside the runner so a cancel lands
    mid-child.
    """

    def __init__(
        self,
        summary: str = "worker done",
        *,
        fail: bool = False,
        crash: type[Exception] | None = None,
        slow: bool = False,
        needs_human: bool = False,
    ) -> None:
        self.summary = summary
        self.fail = fail
        self.crash = crash
        self.slow = slow
        self.needs_human = needs_human
        self.executions = 0
        self.seen: list[ChildRun] = []

    async def __call__(self, run: ChildRun) -> PipelineResult:
        self.executions += 1
        self.seen.append(run)
        if self.crash is not None:
            raise self.crash("worker exploded")
        if self.slow:
            await asyncio.sleep(0.05)
        status = (
            PipelineStatus.NEEDS_HUMAN
            if self.needs_human
            else PipelineStatus.FAILED
            if self.fail
            else PipelineStatus.COMPLETED
        )
        return PipelineResult(
            status=status,
            summary=self.summary,
            metrics={"worker": run.name, "depth": run.depth},
            artifacts=[],
        )


def _declaration(
    name: str,
    *,
    capabilities: Mapping[str, Any] | None = None,
    limits: RunLimits | None = None,
) -> PipelineDeclaration:
    return PipelineDeclaration(
        name=name,
        version="4.1.0",
        requires_runtime=">=4.1,<5",
        description=f"scripted worker {name}",
        capabilities=dict(capabilities or {}),
        limits=limits if limits is not None else RunLimits(),
    )


def _task(request: Mapping[str, Any] | None = None) -> PipelineTask:
    return PipelineTask(
        correlation_id=uuid.uuid4(),
        session_id=None,
        goal="compose",
        request=dict(request or {}),
    )


class _Runner:
    """A ChildRunner whose behaviour a test scripts per pipeline name."""

    def __init__(self) -> None:
        self.workers: dict[str, ScriptedWorker] = {}

    def add(self, name: str, worker: ScriptedWorker) -> ScriptedWorker:
        self.workers[name] = worker
        return worker

    async def __call__(self, run: ChildRun) -> PipelineResult:
        worker = self.workers[run.name]
        return await worker(run)


# ---------------------------------------------------------------------------
# 1. Budget halving, measured per key
# ---------------------------------------------------------------------------


class TestHalving:
    def test_meters_floor_at_one_and_leave_the_wallet(self) -> None:
        parent = RunLimits(max_tokens=1000, max_wall_s=100, max_calls=100)
        child, rest = halve_budget(parent)
        assert child.max_tokens == 500
        assert child.max_wall_s == 50
        assert child.max_calls == 50
        assert (rest.max_tokens, rest.max_wall_s, rest.max_calls) == (500, 50, 50)

    def test_odd_grants_round_down_parent_keeps_the_rest(self) -> None:
        child, rest = halve_budget(RunLimits(max_tokens=7, max_calls=7))
        assert child.max_tokens == 3
        assert rest.max_tokens == 4
        assert child.max_calls == 3
        assert rest.max_calls == 4

    def test_one_unit_floors_at_one_and_empties_the_wallet(self) -> None:
        child, rest = halve_budget(RunLimits(max_tokens=1))
        assert child.max_tokens == 1
        assert rest.max_tokens == 0

    def test_sub_run_count_halves_with_no_floor(self) -> None:
        child, rest = halve_budget(RunLimits(max_sub_runs=2))
        assert child.max_sub_runs == 1
        assert rest.max_sub_runs == 2  # the count is spent, not halved away
        child1, _ = halve_budget(RunLimits(max_sub_runs=1))
        assert child1.max_sub_runs == 0  # zero is "may not compose"
        child0, _ = halve_budget(RunLimits(max_sub_runs=0))
        assert child0.max_sub_runs == 0

    def test_unbounded_keys_stay_unbounded(self) -> None:
        child, rest = halve_budget(RunLimits())
        assert child.max_tokens is None
        assert child.max_wall_s is None
        assert child.max_calls is None
        assert child.max_sub_runs is None
        assert rest.max_tokens is None

    async def test_measured_through_the_composition(self, ledger: LedgerStore) -> None:
        """Parent 1000 → child 500 → grandchild 250, read off the ChildRuns.

        A manager delegates to a middle pipeline that delegates once more;
        both delegations go through the real composer, so the halving is
        measured at the seam the host actually uses. The middle/grandchild
        names enter through a monkeypatched ``registry.BUILTIN_DECLARATIONS``
        (the same seam ``test_conformance.py`` patches — the registry reads
        its own module binding, not ``slots``'), so the registry answers
        them while the shipped builtins stay untouched.
        """
        from paa.pipelines import registry as registry_module
        from paa.pipelines.compose import _CompositionState

        monkeypatch = pytest.MonkeyPatch()
        monkeypatch.setattr(
            registry_module,
            "BUILTIN_DECLARATIONS",
            {
                **registry_module.BUILTIN_DECLARATIONS,
                "builtin.middle": _declaration(
                    "builtin.middle", limits=RunLimits(max_sub_runs=2)
                ),
                "builtin.grandchild": _declaration("builtin.grandchild"),
            },
        )
        try:
            journal = CallJournal(ledger, uuid.uuid4())
            grandchild = ScriptedWorker("grandchild done")
            middle_runs: list[ChildRun] = []

            async def middle_runner(run: ChildRun) -> PipelineResult:
                middle_runs.append(run)
                if run.name == "builtin.middle" and len(middle_runs) == 1:
                    # The middle pipeline immediately delegates once more.
                    return await run.composer.run_child(
                        "builtin.grandchild", {"goal": "go deeper"}
                    )
                return PipelineResult(
                    status=PipelineStatus.COMPLETED,
                    summary="middle done",
                    metrics={"worker": run.name},
                )

            runners: dict[str, Any] = {
                "builtin.middle": middle_runner,
                "builtin.grandchild": grandchild,
            }
            state = _CompositionState(
                journal=journal,
                registry=PipelineRegistry(local_dir=None),
                runners=runners,
            )
            composer = SubRunComposer(
                state,
                name="builtin.manager",
                stack=("builtin.manager",),
                limits=RunLimits(max_tokens=1000, max_sub_runs=2),
            )
            child_result = await composer.run_child(
                "builtin.middle", {"goal": "halve me"}
            )
            assert child_result.status is PipelineStatus.COMPLETED
            assert grandchild.seen[0].limits.max_tokens == 250
            # The middle run's own limits were the halved grant (500); its
            # delegation halved that again out of its wallet (250).
            assert middle_runs[0].limits.max_tokens == 500
            assert grandchild.seen[0].stack == (
                "builtin.manager",
                "builtin.middle",
                "builtin.grandchild",
            )
        finally:
            monkeypatch.undo()


# ---------------------------------------------------------------------------
# 2. The recursion guards
# ---------------------------------------------------------------------------


class TestRecursionGuards:
    def _composer(
        self,
        ledger: LedgerStore,
        runners: Mapping[str, Any],
        *,
        stack: tuple[str, ...],
        sub_runs_left: int | None = 8,
    ) -> SubRunComposer:
        from paa.pipelines.compose import _CompositionState

        state = _CompositionState(
            journal=CallJournal(ledger, uuid.uuid4()),
            registry=PipelineRegistry(local_dir=None),
            runners=dict(runners),
        )
        return SubRunComposer(
            state,
            name=stack[-1],
            stack=stack,
            limits=RunLimits(max_sub_runs=sub_runs_left),
            sub_runs_left=sub_runs_left,
        )

    async def test_direct_cycle_refuses(self, ledger: LedgerStore) -> None:
        runner = _Runner()
        runner.add("builtin.chat", ScriptedWorker())
        composer = self._composer(ledger, runner.workers, stack=("builtin.chat",))
        with pytest.raises(RecursionGuardError, match="cycle") as excinfo:
            await composer.run_child("builtin.chat", {"goal": "self"})
        assert excinfo.value.cycle == ["builtin.chat", "builtin.chat"]
        assert all(w.executions == 0 for w in runner.workers.values())

    async def test_indirect_cycle_refuses(self, ledger: LedgerStore) -> None:
        """A→B→A is refused by stack membership, not by depth."""

        async def b_runner(run: ChildRun) -> PipelineResult:
            return await run.composer.run_child("builtin.chat", {"goal": "back"})

        runner = _Runner()
        runner.add("builtin.chat", ScriptedWorker())
        composer = self._composer(
            ledger,
            {"builtin.chat": runner.workers["builtin.chat"], "coder.tool-loop": b_runner},
            stack=("builtin.chat",),
        )
        with pytest.raises(RecursionGuardError, match="cycle"):
            await composer.run_child("coder.tool-loop", {"goal": "to B"})
        assert all(w.executions == 0 for w in runner.workers.values())

    async def test_depth_ceiling_refuses_a_chain_of_distinct_names(
        self, ledger: LedgerStore, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Distinct names dodge the cycle guard; the ceiling still stops it."""

        async def chain_runner(run: ChildRun) -> PipelineResult:
            seen.append(run)
            return await run.composer.run_child(
                f"builtin.p{run.depth + 1}", {"goal": "deeper"}
            )

        seen: list[ChildRun] = []

        from paa.pipelines import registry as registry_module

        monkeypatch.setattr(
            registry_module,
            "BUILTIN_DECLARATIONS",
            {
                **registry_module.BUILTIN_DECLARATIONS,
                **{
                    f"builtin.p{i}": _declaration(f"builtin.p{i}")
                    for i in range(1, MAX_COMPOSITION_DEPTH + 2)
                },
            },
        )
        composer = self._composer(
            ledger,
            {f"builtin.p{i}": chain_runner for i in range(1, MAX_COMPOSITION_DEPTH + 2)},
            stack=("builtin.manager",),
            sub_runs_left=None,
        )
        with pytest.raises(RecursionGuardError, match="ceiling") as excinfo:
            await composer.run_child("builtin.p1", {"goal": "descend"})
        assert excinfo.value.ceiling == MAX_COMPOSITION_DEPTH

    async def test_spent_sub_run_count_refuses_before_the_ledger(
        self, ledger: LedgerStore
    ) -> None:
        runner = _Runner()
        worker = runner.add("builtin.chat", ScriptedWorker())
        run_id = uuid.uuid4()
        from paa.pipelines.compose import _CompositionState

        state = _CompositionState(
            journal=CallJournal(ledger, run_id),
            registry=PipelineRegistry(local_dir=None),
            runners=dict(runner.workers),
        )
        composer = SubRunComposer(
            state,
            name="builtin.manager",
            stack=("builtin.manager",),
            limits=RunLimits(max_sub_runs=1),
            sub_runs_left=0,
        )
        before = await ledger.read_correlation(run_id)
        with pytest.raises(BudgetExceededError) as excinfo:
            await composer.run_child("builtin.chat", {"goal": "one too many"})
        assert excinfo.value.budget_kind == "sub_run"
        assert worker.executions == 0
        assert await ledger.read_correlation(run_id) == before


# ---------------------------------------------------------------------------
# 3. Capability discipline
# ---------------------------------------------------------------------------


class TestCapabilityDiscipline:
    async def test_ungranted_sub_run_refuses(self, ledger: LedgerStore) -> None:
        """The default facade (no SUB_RUN in its ask) refuses with -32006."""

        from paa.agents.base import AgentContext

        class _Host(PipelineHost):
            pass

        facade = BuiltinPipelineFacade(
            _Host(), StageAgents(), AgentContext(correlation_id=uuid.uuid4())
        )
        with pytest.raises(CapabilityNotGrantedError) as excinfo:
            await facade.sub_run("builtin.chat", {"goal": "no"})
        assert excinfo.value.wire_code == -32006

    async def test_granted_facade_without_executor_still_refuses(
        self, ledger: LedgerStore
    ) -> None:
        """The door without the key: a SUB_RUN grant with no host executor."""

        from paa.agents.base import AgentContext

        class _Host(PipelineHost):
            pass

        class _Executor(SubRunExecutor):
            async def run_child(
                self, name: str, task_input: Mapping[str, Any]
            ) -> PipelineResult:  # pragma: no cover - never reached
                raise AssertionError("must not be called")

        facade = BuiltinPipelineFacade(
            _Host(),
            StageAgents(),
            AgentContext(correlation_id=uuid.uuid4()),
            capabilities=frozenset({Capability.SUB_RUN}),
        )
        with pytest.raises(CapabilityNotGrantedError):
            await facade.sub_run("builtin.chat", {"goal": "no executor"})

    def test_child_grants_come_from_its_own_declaration(self) -> None:
        """A parent cannot launder a capability into a non-asking child."""

        asks_nothing = _declaration("asks.nothing")
        asks_everything = _declaration(
            "asks.everything",
            capabilities={
                "think": {"roles": ["planner"]},
                "use_skill": ["read_file"],
                "sub_run": [],
                "recall": True,
                "commit": True,
            },
        )
        asks_empty_sub_run = _declaration(
            "asks.empty", capabilities={"sub_run": []}
        )
        assert declared_capabilities(asks_nothing) == frozenset()
        # sub_run=[] is a falsy ask: present in the manifest, absent from
        # the grant — the spec's own example shape.
        assert Capability.SUB_RUN not in declared_capabilities(asks_everything)
        assert Capability.THINK in declared_capabilities(asks_everything)
        assert Capability.USE_SKILL in declared_capabilities(asks_everything)
        assert Capability.COMMIT in declared_capabilities(asks_everything)
        assert declared_capabilities(asks_empty_sub_run) == frozenset()


# ---------------------------------------------------------------------------
# 4. Child isolation
# ---------------------------------------------------------------------------


class TestChildIsolation:
    async def _manager_run(
        self,
        ledger: LedgerStore,
        runners: Mapping[str, Any],
        request: Mapping[str, Any] | None = None,
    ) -> tuple[uuid.UUID, PipelineResult]:
        return await run_composition(
            ledger, _task(request), runners=runners, registry=PipelineRegistry(local_dir=None)
        )

    async def test_failed_child_result_is_returned_not_raised(
        self, ledger: LedgerStore
    ) -> None:
        runner = _Runner()
        runner.add("builtin.chat", ScriptedWorker("child failed", fail=True))
        _, result = await self._manager_run(
            ledger, runner.workers, {"delegations": [{"pipeline": "builtin.chat", "goal": "g"}]}
        )
        assert result.status is PipelineStatus.FAILED
        assert result.metrics["children"][0]["status"] == "failed"
        assert result.metrics["children"][0]["summary"] == "child failed"

    async def test_crashed_child_is_isolated_into_a_failed_result(
        self, ledger: LedgerStore
    ) -> None:
        runner = _Runner()
        runner.add(
            "builtin.chat", ScriptedWorker(crash=RuntimeError)
        )
        runner.add("coder.tool-loop", ScriptedWorker("healthy sibling"))
        _, result = await self._manager_run(
            ledger,
            runner.workers,
            {
                "delegations": [
                    {"pipeline": "builtin.chat", "goal": "explode"},
                    {"pipeline": "coder.tool-loop", "goal": "survive"},
                ]
            },
        )
        children = result.metrics["children"]
        assert children[0]["status"] == "failed"
        assert "RuntimeError" in children[0]["summary"]
        # The healthy sibling still ran and completed.
        assert children[1]["status"] == "completed"
        assert result.metrics["completed"] == 1

    async def test_needs_human_child_aggregates_to_needs_human(
        self, ledger: LedgerStore
    ) -> None:
        runner = _Runner()
        runner.add("builtin.chat", ScriptedWorker(needs_human=True))
        _, result = await self._manager_run(
            ledger, runner.workers, {"delegations": [{"pipeline": "builtin.chat", "goal": "g"}]}
        )
        assert result.status is PipelineStatus.NEEDS_HUMAN

    async def test_control_errors_propagate_to_the_root(self, ledger: LedgerStore) -> None:
        """A guard refusal is the composition's, not the child's."""

        async def cycling(run: ChildRun) -> PipelineResult:
            return await run.composer.run_child("builtin.manager", {"goal": "cycle"})

        runner = _Runner()
        runner.add("builtin.chat", cycling)
        with pytest.raises(RecursionGuardError):
            await self._manager_run(
                ledger,
                runner.workers,
                {"delegations": [{"pipeline": "builtin.chat", "goal": "g"}]},
            )

    async def test_cancel_is_journalled_then_reraised(self, ledger: LedgerStore) -> None:
        runner = _Runner()
        runner.add("builtin.chat", ScriptedWorker(slow=True))
        run_id = _task().correlation_id
        task = asyncio.create_task(
            run_composition(
                ledger,
                PipelineTask(
                    correlation_id=run_id, session_id=None, goal="compose", request={
                        "delegations": [{"pipeline": "builtin.chat", "goal": "slow"}]
                    },
                ),
                runners=runner.workers,
                registry=PipelineRegistry(local_dir=None),
            )
        )
        await asyncio.sleep(0.02)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        rows = await ledger.read_correlation(run_id)
        kinds = [e.event_type.value for e in rows]
        assert "PIPELINE_CALL_FAILED" in kinds
        assert "PIPELINE_RUN_FAILED" in kinds
        assert "PIPELINE_RUN_COMPLETED" not in kinds


# ---------------------------------------------------------------------------
# 5. The manager demo: two different workers, aggregated
# ---------------------------------------------------------------------------


class TestTheManagerDemo:
    async def test_two_different_workers_aggregate(self, ledger: LedgerStore) -> None:
        runner = _Runner()
        chat = runner.add("builtin.chat", ScriptedWorker("chat says hello"))
        coder = runner.add("coder.tool-loop", ScriptedWorker("coder closed the test"))
        run_id, result = await run_composition(
            ledger,
            _task(
                {
                    "delegations": [
                        {"pipeline": "builtin.chat", "goal": "greet"},
                        {
                            "pipeline": "coder.tool-loop",
                            "goal": "fix",
                            "input": {"focus": "tests"},
                        },
                    ]
                }
            ),
            runners=runner.workers,
            registry=PipelineRegistry(local_dir=None),
        )
        assert result.status is PipelineStatus.COMPLETED
        assert result.metrics["delegations"] == 2
        assert result.metrics["completed"] == 2
        assert [c["pipeline"] for c in result.metrics["children"]] == [
            "builtin.chat",
            "coder.tool-loop",
        ]
        # Both workers executed exactly once, through the facade door.
        assert chat.executions == 1 and coder.executions == 1
        # The child inputs carry the operator's goal (and input table).
        assert chat.seen[0].task_input == {"goal": "greet"}
        assert coder.seen[0].task_input == {"goal": "fix", "focus": "tests"}
        # The run's own journal rows: started, two sub_run calls, completed.
        fold = await read_call_journal(ledger, run_id)
        assert [r.method for r in fold] == ["sub_run", "sub_run"]
        assert all(r.outcome is CallOutcome.COMPLETED for r in fold)

    async def test_malformed_decomposition_fails_by_result(self, ledger: LedgerStore) -> None:
        runner = _Runner()
        _, result = await run_composition(
            ledger,
            _task({"delegations": "not-a-list"}),
            runners=runner.workers,
            registry=PipelineRegistry(local_dir=None),
        )
        assert result.status is PipelineStatus.FAILED
        assert "delegations" in result.summary
        assert all(w.executions == 0 for w in runner.workers.values())


# ---------------------------------------------------------------------------
# 6. Replay: serve with zero re-execution, diverge on change
# ---------------------------------------------------------------------------


class TestReplay:
    async def test_replay_serves_recorded_results_with_zero_reexecution(
        self, ledger: LedgerStore
    ) -> None:
        runner = _Runner()
        runner.add("builtin.chat", ScriptedWorker("served chat"))
        runner.add("coder.tool-loop", ScriptedWorker("served coder"))
        request = {
            "delegations": [
                {"pipeline": "builtin.chat", "goal": "greet"},
                {"pipeline": "coder.tool-loop", "goal": "fix"},
            ]
        }
        run_id, first = await run_composition(
            ledger, _task(request), runners=runner.workers,
            registry=PipelineRegistry(local_dir=None),
        )
        assert first.status is PipelineStatus.COMPLETED

        fresh = _Runner()
        fresh.add("builtin.chat", ScriptedWorker("would re-execute"))
        fresh.add("coder.tool-loop", ScriptedWorker("would re-execute"))
        replay_task = PipelineTask(correlation_id=run_id, session_id=None, goal="compose")
        replay_task.request.update(request)
        _, second = await run_composition(
            ledger,
            replay_task,
            runners=fresh.workers,
            registry=PipelineRegistry(local_dir=None),
            replay=await ReplayView.load(ledger, run_id),
        )
        assert second.status is PipelineStatus.COMPLETED
        assert second.metrics == first.metrics
        assert all(w.executions == 0 for w in fresh.workers.values())
        # No new call rows on the lineage: the served run appended nothing.
        fold_after_first = await read_call_journal(ledger, run_id)
        rows_after_second = await ledger.read_correlation(run_id)
        call_rows = [
            e
            for e in rows_after_second
            if e.event_type.value.startswith("PIPELINE_CALL_")
        ]
        assert len(call_rows) == 2 * len(fold_after_first)

    async def test_changed_call_diverges_the_run(self, ledger: LedgerStore) -> None:
        runner = _Runner()
        runner.add("builtin.chat", ScriptedWorker("first"))
        request = {"delegations": [{"pipeline": "builtin.chat", "goal": "original"}]}
        run_id, _ = await run_composition(
            ledger, _task(request), runners=runner.workers,
            registry=PipelineRegistry(local_dir=None),
        )
        changed = PipelineTask(correlation_id=run_id, session_id=None, goal="compose")
        changed.request.update(
            {"delegations": [{"pipeline": "builtin.chat", "goal": "rewritten"}]}
        )
        fresh = _Runner()
        fresh.add("builtin.chat", ScriptedWorker("never reached"))
        with pytest.raises(PipelineDivergedError):
            await run_composition(
                ledger,
                changed,
                runners=fresh.workers,
                registry=PipelineRegistry(local_dir=None),
                replay=await ReplayView.load(ledger, run_id),
            )
        assert all(w.executions == 0 for w in fresh.workers.values())
        rows = await ledger.read_correlation(run_id)
        assert any(e.event_type.value == "PIPELINE_RUN_DIVERGED" for e in rows)


# ---------------------------------------------------------------------------
# 7. Journal row shapes (v=1, method="sub_run")
# ---------------------------------------------------------------------------


class TestJournalRows:
    async def test_proposed_and_completed_shapes(self, ledger: LedgerStore) -> None:
        import hashlib

        from paa.ledger.events import canonical_json

        runner = _Runner()
        runner.add("builtin.chat", ScriptedWorker("shaped"))
        params = {"pipeline": "builtin.chat", "task_input": {"goal": "shape me"}}
        run_id, _ = await run_composition(
            ledger,
            _task({"delegations": [{"pipeline": "builtin.chat", "goal": "shape me"}]}),
            runners=runner.workers,
            registry=PipelineRegistry(local_dir=None),
        )
        rows = await ledger.read_correlation(run_id)
        proposed = [
            e for e in rows if e.event_type.value == "PIPELINE_CALL_PROPOSED"
        ]
        completed = [
            e for e in rows if e.event_type.value == "PIPELINE_CALL_COMPLETED"
        ]
        assert len(proposed) == 1 and len(completed) == 1
        p = proposed[0].payload
        assert p["v"] == 1
        assert p["seq"] == 1
        assert p["method"] == "sub_run"
        assert p["run_id"] == str(run_id)
        # Params never enter the payload: only the sha and the shape.
        assert "params" not in p
        assert p["params_sha256"] == hashlib.sha256(
            canonical_json(params).encode("utf-8")
        ).hexdigest()
        # The shape renders the goal only as length+hash, never as text —
        # the key name appears, its value does not.
        assert f"{params['task_input']['goal']}" not in str(p["params_shape"])
        c = completed[0].payload
        assert c["v"] == 1
        assert c["seq"] == 1
        assert c["method"] == "sub_run"
        # The attestation is over the stored canonical text, exactly as
        # the row wrote it (canonical_json of the child result).
        assert c["result_sha256"] == hashlib.sha256(
            c["result_json"].encode("utf-8")
        ).hexdigest()
        assert c["result_json"] == canonical_json(
            {
                "status": "completed",
                "summary": "shaped",
                "metrics": {"worker": "builtin.chat", "depth": 2},
                "artifacts": [],
            }
        )
        served = __import__("json").loads(c["result_json"])
        assert served["status"] == "completed"
        assert served["summary"] == "shaped"

    async def test_failed_row_never_attests_a_result(self, ledger: LedgerStore) -> None:
        """A control error mid-composition closes the row without a result."""

        async def cycling(run: ChildRun) -> PipelineResult:
            return await run.composer.run_child("builtin.manager", {"goal": "cycle"})

        runner = _Runner()
        runner.add("builtin.chat", cycling)
        run_id = _task().correlation_id
        with pytest.raises(RecursionGuardError):
            await run_composition(
                ledger,
                PipelineTask(
                    correlation_id=run_id,
                    session_id=None,
                    goal="compose",
                    request={"delegations": [{"pipeline": "builtin.chat", "goal": "g"}]},
                ),
                runners=runner.workers,
                registry=PipelineRegistry(local_dir=None),
            )
        rows = await ledger.read_correlation(run_id)
        failed = [e for e in rows if e.event_type.value == "PIPELINE_CALL_FAILED"]
        assert len(failed) == 1
        f = failed[0].payload
        assert f["v"] == 1
        assert f["method"] == "sub_run"
        assert "result_json" not in f
        assert "result_sha256" not in f
        assert "cycle" in f["reason"]
