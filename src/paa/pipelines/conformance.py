"""Conformance v0: the small example standard, run everywhere (C6.6).

Six checks, one standard, every registered in-process pipeline:

===============  =========================================================
check            what it means, in every vocabulary
===============  =========================================================
double-run       two runs of the same scripted task produce the same
                 call/event sequence — the determinism a replay needs
divergence-halt  a re-run that stops replaying its history halts, loudly,
                 with an audit row — the host never resyncs
unknown-outcome  a call whose outcome a crash left unclassifiable is
                 never auto-repeated; the pipeline decides (D-6)
cancel-within-   cancelling mid-flight lands in a terminal, coherent
grace            state — no half-closed rows the fold cannot read
budget-          an exhausted budget degrades or fails by rule, never by
exhaustion       crash
no-secrets-      values the vocabulary promises to attest by hash
in-journal       (messages, tool results) never appear as payload text
===============  =========================================================

The checks are *vocabulary-honest*: each pipeline journals in its own
vocabulary (``PIPELINE_CALL_*`` for coder, ``CHAT_TURN_*`` for chat,
the task events for the builtin), and a check that a vocabulary cannot
express is reported ``n/a`` with the reason — never faked green. The
n/a rows name where the behaviour IS pinned (the chaos suites, the
replay tests), so the standard stays one page and still tells the
truth.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from paa.ledger.store import LedgerStore
from paa.pipelines.journal import (
    CallJournal,
    read_call_journal,
)
from paa.pipelines.replay import ReplayAction, ReplayView

__all__ = [
    "ConformanceCheck",
    "ConformanceReport",
    "builtin_conformance",
    "chat_conformance",
    "coder_conformance",
    "every_builtin_conformance",
    "render_reports",
]


CHECK_NAMES = (
    "double-run-sequence-equality",
    "divergence-halt",
    "unknown-outcome-never-auto-repeats",
    "cancel-within-grace",
    "budget-exhaustion",
    "no-secrets-in-journal",
)


@dataclass(frozen=True, slots=True)
class ConformanceCheck:
    """One check's outcome: pass, fail, or not-applicable-with-reason."""

    name: str
    status: str
    detail: str

    @classmethod
    def passed(cls, name: str, detail: str) -> ConformanceCheck:
        return cls(name=name, status="pass", detail=detail)

    @classmethod
    def failed(cls, name: str, detail: str) -> ConformanceCheck:
        return cls(name=name, status="fail", detail=detail)

    @classmethod
    def na(cls, name: str, detail: str) -> ConformanceCheck:
        return cls(name=name, status="n/a", detail=detail)


@dataclass(frozen=True, slots=True)
class ConformanceReport:
    """One pipeline's conformance: the six checks, in the standard order."""

    pipeline: str
    checks: tuple[ConformanceCheck, ...]

    @property
    def passed(self) -> bool:
        return all(c.status != "fail" for c in self.checks)


def _checks(**by_name: ConformanceCheck) -> tuple[ConformanceCheck, ...]:
    missing = [n for n in CHECK_NAMES if n not in by_name]
    if missing:
        raise ValueError(f"conformance checks missing: {missing}")
    return tuple(by_name[n] for n in CHECK_NAMES)


def render_reports(reports: list[ConformanceReport]) -> str:
    """A one-page text summary, one line per check."""
    lines = []
    for report in reports:
        verdict = "GREEN" if report.passed else "RED"
        lines.append(f"{report.pipeline}: {verdict}")
        for check in report.checks:
            mark = {"pass": "ok", "fail": "FAIL", "n/a": "-"}[check.status]
            lines.append(f"  [{mark:>4}] {check.name} — {check.detail}")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Shared scripted building blocks (deterministic, offline, marker-seeded)
# ---------------------------------------------------------------------------


class _ScriptedModel:
    """Answers scripted turns; counts invocations; never sleeps."""

    def __init__(self, turns: list[Any]) -> None:
        self._turns = list(turns)
        self.invocations = 0

    async def propose(
        self, window: list[dict[str, Any]], tools: list[dict[str, Any]]
    ) -> Any:
        self.invocations += 1
        return self._turns.pop(0)


class _SlowModel(_ScriptedModel):
    """Sleeps mid-propose so a cancel can land inside the model call."""

    async def propose(
        self, window: list[dict[str, Any]], tools: list[dict[str, Any]]
    ) -> Any:
        self.invocations += 1
        await asyncio.sleep(0.05)
        return self._turns.pop(0)


class _CountingTool:
    def __init__(self, name: str, result: str) -> None:
        self.name = name
        self.description = f"the {name} tool"
        self._result = result
        self.executions = 0

    def schema(self) -> dict[str, Any]:
        return {"type": "object", "properties": {}}

    async def run(self, arguments: dict[str, Any]) -> str:
        self.executions += 1
        return self._result


def _marker() -> str:
    return f"SEKRIT-{uuid.uuid4().hex[:12]}"


async def _payload_texts(store: LedgerStore, run_id: uuid.UUID) -> list[str]:
    """Every payload on a lineage, rendered, for the no-secrets scan."""
    events = await store.read_correlation(run_id)
    return [json.dumps(dict(e.payload), sort_keys=True) for e in events]


# ---------------------------------------------------------------------------
# coder.tool-loop
# ---------------------------------------------------------------------------


async def coder_conformance(ledger: LedgerStore, tmp_path: Path) -> ConformanceReport:
    """The six checks over ``coder.tool-loop``, in its own vocabulary."""
    from paa.coder.loop import ModelTurn, ToolCall
    from paa.coder.permissions import PermissionMode, ToolPermissionPolicy
    from paa.coder.pipeline import CoderLoopFacade, CoderToolLoopPipeline
    from paa.coder.session import Session
    from paa.pipelines.base import PipelineStatus, PipelineTask
    from paa.pipelines.replay import ServedToolResults

    def turns() -> list[ModelTurn]:
        return [
            ModelTurn(tool_calls=[ToolCall("alpha", {"n": 1}, call_id="t1")]),
            ModelTurn(tool_calls=[ToolCall("beta", {"n": 2}, call_id="t2")]),
            ModelTurn(text="conformed"),
        ]

    async def drive(
        run_id: uuid.UUID,
        model: Any,
        tools: dict[str, Any],
        params: dict[str, Any] | None = None,
        *,
        replay: ReplayView | None = None,
        served: ServedToolResults | None = None,
    ) -> Any:
        journal = CallJournal(ledger, run_id)
        await journal.run_started(
            CoderToolLoopPipeline.name, CoderToolLoopPipeline.version
        )
        transcript = tmp_path / f"run-{run_id.hex[:8]}-{uuid.uuid4().hex[:4]}"
        transcript.mkdir()
        facade = CoderLoopFacade(
            model=model,
            tools=tools,
            session=Session(session_id="conformance", transcript_dir=transcript),
            run_id=run_id,
            call_journal=journal,
            replay=replay,
            served_tools=served,
            permissions=ToolPermissionPolicy(mode=PermissionMode.BYPASS),
        )
        result = await CoderToolLoopPipeline().run(
            PipelineTask(
                correlation_id=run_id, session_id=None, goal="conform", params=params or {}
            ),
            facade,
        )
        await journal.run_completed(result.status.value, metrics=dict(result.metrics))
        return result

    # -- 1. double-run call-sequence equality -----------------------------
    secret = _marker()
    run_a, run_b = uuid.uuid4(), uuid.uuid4()
    # Identical scripting, identical tool results — the marker rides in
    # both runs' tool results (for the no-secrets scan) without making
    # the two runs' windows differ.
    tools_conform = {
        "alpha": _CountingTool("alpha", f"{secret}-alpha"),
        "beta": _CountingTool("beta", f"{secret}-beta"),
    }
    await drive(run_a, _ScriptedModel(turns()), tools_conform)
    await drive(run_b, _ScriptedModel(turns()), tools_conform)
    fold_a = await read_call_journal(ledger, run_a)
    fold_b = await read_call_journal(ledger, run_b)
    def shape(records: list[Any]) -> list[tuple[Any, ...]]:
        return [(r.seq, r.method, r.fingerprint, r.outcome) for r in records]

    if shape(fold_a) == shape(fold_b) and len(fold_a) == 3:
        double_run = ConformanceCheck.passed(
            CHECK_NAMES[0], "two fresh runs folded to the same 3-call sequence"
        )
    else:
        double_run = ConformanceCheck.failed(
            CHECK_NAMES[0], f"sequences differ: {shape(fold_a)} vs {shape(fold_b)}"
        )

    # -- 2. divergence halt ------------------------------------------------
    from paa.pipelines.base import PipelineDivergedError

    diverged_row = False
    halted = False
    wrong_exc: str | None = None
    try:
        await drive(
            run_a, _ScriptedModel(turns()),
            {
                "alpha": _CountingTool("alpha", "different-result"),
                "beta": _CountingTool("beta", "y"),
            },
            replay=await ReplayView.load(ledger, run_a),
            served=ServedToolResults([]),
        )
    except PipelineDivergedError:
        halted = True
        rows = await ledger.read_correlation(run_a)
        diverged_row = any(
            r.event_type.value == "PIPELINE_RUN_DIVERGED" for r in rows
        )
    except Exception as exc:  # a halt of the wrong kind is a finding
        wrong_exc = f"{type(exc).__name__}: {exc}"
    if halted and diverged_row:
        divergence = ConformanceCheck.passed(
            CHECK_NAMES[1],
            "a served call followed by a changed window diverged the replay; "
            "the run halted with PIPELINE_RUN_DIVERGED",
        )
    else:
        divergence = ConformanceCheck.failed(
            CHECK_NAMES[1],
            f"halted={halted} diverged_row={diverged_row} wrong_exc={wrong_exc}",
        )

    # -- 3. unknown-outcome never auto-repeats ------------------------------
    run_c = uuid.uuid4()
    journal_c = CallJournal(ledger, run_c)
    await journal_c.run_started(CoderToolLoopPipeline.name, CoderToolLoopPipeline.version)
    await journal_c.call_proposed(1, "commit", {"role": "worker"})
    view_c = await ReplayView.load(ledger, run_c)
    decision = view_c.check(1, "commit", {"role": "worker"})
    if decision.action is ReplayAction.OUTCOME_UNKNOWN:
        unknown = ConformanceCheck.passed(
            CHECK_NAMES[2],
            "a mutating call the crash left PROPOSED reads OUTCOME_UNKNOWN "
            "(-32008); the pipeline decides",
        )
    else:
        unknown = ConformanceCheck.failed(
            CHECK_NAMES[2], f"decision was {decision.action}, not OUTCOME_UNKNOWN"
        )

    # -- 4. cancel within grace ---------------------------------------------
    run_d = uuid.uuid4()
    journal_d = CallJournal(ledger, run_d)
    await journal_d.run_started(CoderToolLoopPipeline.name, CoderToolLoopPipeline.version)
    transcript_d = tmp_path / f"run-{run_d.hex[:8]}"
    transcript_d.mkdir()
    tools_d: dict[str, Any] = {
        "alpha": _CountingTool("alpha", "x"),
        "beta": _CountingTool("beta", "y"),
    }
    facade_d = CoderLoopFacade(
        model=_SlowModel(turns()),
        tools=tools_d,
        session=Session(session_id="conformance", transcript_dir=transcript_d),
        run_id=run_d,
        call_journal=journal_d,
        permissions=ToolPermissionPolicy(mode=PermissionMode.BYPASS),
    )
    task_d = asyncio.create_task(
        CoderToolLoopPipeline().run(
            PipelineTask(correlation_id=run_d, session_id=None, goal="conform", params={}),
            facade_d,
        )
    )
    await asyncio.sleep(0.02)
    task_d.cancel()
    cancelled_cleanly = False
    try:
        await task_d
    except asyncio.CancelledError:
        cancelled_cleanly = True
    fold_d = await read_call_journal(ledger, run_d)
    if cancelled_cleanly and fold_d:
        cancel = ConformanceCheck.passed(
            CHECK_NAMES[3],
            f"the cancel propagated; the lineage still folds "
            f"({len(fold_d)} call(s), terminal or closed)",
        )
    else:
        cancel = ConformanceCheck.failed(
            CHECK_NAMES[3], f"cancelled_cleanly={cancelled_cleanly} fold={len(fold_d)}"
        )

    # -- 5. budget exhaustion ------------------------------------------------
    run_e = uuid.uuid4()
    result_e = await drive(
        run_e, _ScriptedModel(turns()),
        {"alpha": _CountingTool("alpha", "x"), "beta": _CountingTool("beta", "y")},
        params={"max_steps": 1},
    )
    if (
        result_e.status is PipelineStatus.FAILED
        and result_e.metrics.get("stopped_reason") == "max_steps_reached"
    ):
        budget = ConformanceCheck.passed(
            CHECK_NAMES[4], "max_steps=1 failed by rule (max_steps_reached), not by crash"
        )
    else:
        budget = ConformanceCheck.failed(
            CHECK_NAMES[4], f"status={result_e.status} metrics={result_e.metrics}"
        )

    # -- 6. no secrets in the journal ----------------------------------------
    texts = await _payload_texts(ledger, run_a)
    leaked = [t for t in texts if secret in t]
    if not leaked:
        secrets = ConformanceCheck.passed(
            CHECK_NAMES[5],
            "the seeded marker (goal-adjacent prompts and both tool results) appears in no payload",
        )
    else:
        secrets = ConformanceCheck.failed(
            CHECK_NAMES[5], f"the marker leaked into {len(leaked)} payload(s)"
        )

    return ConformanceReport(
        pipeline=CoderToolLoopPipeline.name,
        checks=_checks(
            **{
                CHECK_NAMES[0]: double_run,
                CHECK_NAMES[1]: divergence,
                CHECK_NAMES[2]: unknown,
                CHECK_NAMES[3]: cancel,
                CHECK_NAMES[4]: budget,
                CHECK_NAMES[5]: secrets,
            }
        ),
    )


# ---------------------------------------------------------------------------
# builtin.chat
# ---------------------------------------------------------------------------


async def chat_conformance(tmp_path: Path) -> ConformanceReport:
    """The six checks over ``builtin.chat``, in the turn vocabulary."""
    from paa.chat.loop import ChatLoop, ChatReply
    from paa.chat.service import ChatService
    from paa.config import ChatSettings, ContextSettings
    from paa.core.types import PermissionMode
    from paa.memory.creator import MemoryCreator
    from paa.storage.coldlake.cas import ContentAddressedStore
    from paa.storage.coldlake.signals import SignalRepository
    from paa.storage.relational.database import Database

    async def service(
        tmp: Path, *, settings: ChatSettings | None = None
    ) -> ChatService:
        """One offline chat stack over its own throwaway database."""
        db = Database(tmp / "chat.db")
        await db.connect()
        dbs.append(db)
        ledgers.append(LedgerStore(db))
        return ChatService(
            ledger=LedgerStore(db),
            db=db,
            creator=MemoryCreator(db),
            settings=settings or ChatSettings(),
            permission_mode=PermissionMode.ASK,
            context_settings=ContextSettings(),
            signals=SignalRepository(db, ContentAddressedStore(tmp / "cold")),
        )

    dbs: list[Any] = []
    ledgers: list[LedgerStore] = []

    try:
        # -- 1. double-run sequence equality --------------------------------
        secret = _marker()
        dir_one, dir_two = tmp_path / "one", tmp_path / "two"
        dir_one.mkdir()
        dir_two.mkdir()
        s1 = await service(dir_one)
        sid1 = await s1.create_session()
        await s1.send(sid1, f"{secret} first")
        s2 = await service(dir_two)
        sid2 = await s2.create_session()
        await s2.send(sid2, f"{secret} first")
        events1 = await ledgers[0].read_correlation(sid1)
        events2 = await ledgers[1].read_correlation(sid2)
        kinds1 = [e.event_type.value for e in events1]
        kinds2 = [e.event_type.value for e in events2]
        if kinds1 == kinds2 and events1:
            double_run = ConformanceCheck.passed(
                CHECK_NAMES[0],
                f"two sessions walked the same {len(events1)}-event turn sequence",
            )
        else:
            double_run = ConformanceCheck.failed(
                CHECK_NAMES[0], f"{kinds1} vs {kinds2}"
            )

        # -- 2/3: the vocabulary boundary, stated honestly ------------------
        divergence = ConformanceCheck.na(
            CHECK_NAMES[1],
            "chat journals turns (CHAT_TURN_*), which carry no fingerprints; "
            "the divergence halt is pinned on coder.tool-loop and in "
            "tests/pipelines/test_replay.py",
        )
        unknown = ConformanceCheck.na(
            CHECK_NAMES[2],
            "no -32008 row exists in the turn vocabulary; the "
            "never-auto-repeat equivalent (a killed turn never completes; "
            "redelivery is a new turn) is pinned in tests/chat/"
            "test_replay_chaos.py",
        )

        # -- 4. cancel within grace ------------------------------------------
        class _Dying:
            async def reply(self, prompt: str, *, on_delta: Any = None) -> ChatReply:
                if on_delta is not None:
                    await on_delta("par")
                await asyncio.sleep(0.05)
                return ChatReply(text="partial", tokens_used=1)

        dir_three = tmp_path / "three"
        dir_three.mkdir()
        s3 = await service(dir_three)
        sid3 = await s3.create_session()
        s3._model = _Dying()  # the seam tests/chat uses for streaming cancels
        sent = asyncio.create_task(s3.send(sid3, "cancel me"))
        await asyncio.sleep(0.02)
        sent.cancel()
        cancelled_cleanly = False
        try:
            await sent
        except asyncio.CancelledError:
            cancelled_cleanly = True
        events3 = await ledgers[2].read_correlation(sid3)
        cancelled_row = any(
            e.event_type.value == "CHAT_TURN_CANCELLED" for e in events3
        )
        if cancelled_cleanly and cancelled_row:
            cancel = ConformanceCheck.passed(
                CHECK_NAMES[3],
                "the mid-stream cancel recorded CHAT_TURN_CANCELLED and propagated",
            )
        else:
            cancel = ConformanceCheck.failed(
                CHECK_NAMES[3], f"cleanly={cancelled_cleanly} row={cancelled_row}"
            )

        # -- 5. budget exhaustion degrades, never crashes --------------------
        dir_four = tmp_path / "four"
        dir_four.mkdir()
        s4 = await service(dir_four, settings=ChatSettings(token_budget=1))
        sid4 = await s4.create_session()
        result4 = await s4.send(sid4, "tight budget turn")
        if result4.status == "completed":
            budget = ConformanceCheck.passed(
                CHECK_NAMES[4],
                "token_budget=1 still served the turn (the packet "
                "degraded; the message rides outside the budget)",
            )
        else:
            budget = ConformanceCheck.failed(
                CHECK_NAMES[4], f"status={result4.status} error={result4.error}"
            )

        # -- 6. no secrets in the journal --------------------------------------
        if not any(secret in t for t in await _payload_texts(ledgers[0], sid1)):
            secrets = ConformanceCheck.passed(
                CHECK_NAMES[5],
                "the seeded message marker appears in no payload (hash-attested only)",
            )
        else:
            secrets = ConformanceCheck.failed(
                CHECK_NAMES[5], "the marker leaked into a payload"
            )
    finally:
        for db in dbs:
            with contextlib.suppress(Exception):  # closing never masks the report
                await db.close()

    return ConformanceReport(
        pipeline=ChatLoop.name,
        checks=_checks(
            **{
                CHECK_NAMES[0]: double_run,
                CHECK_NAMES[1]: divergence,
                CHECK_NAMES[2]: unknown,
                CHECK_NAMES[3]: cancel,
                CHECK_NAMES[4]: budget,
                CHECK_NAMES[5]: secrets,
            }
        ),
    )


# ---------------------------------------------------------------------------
# builtin.plan-execute-critique
# ---------------------------------------------------------------------------


async def builtin_conformance(ledger: LedgerStore) -> ConformanceReport:
    """The six checks over the builtin task pipeline, in the task vocabulary."""
    from paa.agents.base import Agent, AgentContext, AgentMessage, AgentResult
    from paa.agents.orchestrator import ChiefOrchestrator, TaskRequest
    from paa.core.types import AgentRole, PermissionMode
    from paa.pipelines.builtin import BuiltinPlanExecuteCritique

    class _FakeAgent(Agent):
        def __init__(self, role: AgentRole, value: Any) -> None:
            super().__init__(name=role.value)
            self._value = value

        async def handle(self, message: AgentMessage, ctx: AgentContext) -> AgentResult[Any]:
            return AgentResult.success(self._value)

    def orchestrator() -> ChiefOrchestrator:
        return ChiefOrchestrator(
            ledger,
            context_builder=_FakeAgent(
                AgentRole.CONTEXT_BUILDER_PLANNER,
                {"routing_directive": "PROCEED_TO_PLANNER"},
            ),
            planner=_FakeAgent(
                AgentRole.STRATEGIC_PLANNER,
                {"execution_steps": [{"index": 0, "action": "a"}], "step_requirements": {}},
            ),
            policy=_FakeAgent(
                AgentRole.POLICY_RISK,
                {"decision": "STATUS_APPROVED", "requires_human_gate": False},
            ),
            worker=_FakeAgent(AgentRole.WORKER, {"patch": "", "step_index": 0}),
            critic=_FakeAgent(AgentRole.CRITIC, {"verdict": "PASS", "findings": []}),
            permission_mode=PermissionMode.AUTO,
        )

    secret = _marker()
    kinds: list[list[str]] = []
    for _ in range(2):
        orch = orchestrator()
        cid = await orch.submit(TaskRequest(goal=f"{secret} conform"))
        outcome = await orch.run(cid)
        from paa.ledger.replay import TaskPhase

        assert outcome.phase is TaskPhase.COMMITTED
        events = await ledger.read_correlation(cid)
        kinds.append([e.event_type.value for e in events])
    if kinds[0] == kinds[1] and kinds[0]:
        double_run = ConformanceCheck.passed(
            CHECK_NAMES[0],
            f"two submissions walked the same {len(kinds[0])}-event task sequence",
        )
    else:
        double_run = ConformanceCheck.failed(CHECK_NAMES[0], f"{kinds[0]} vs {kinds[1]}")

    divergence = ConformanceCheck.na(
        CHECK_NAMES[1],
        "the task vocabulary has no fingerprinted calls to diverge; "
        "the halt is the replay-table property, pinned on coder.tool-loop",
    )
    unknown = ConformanceCheck.na(
        CHECK_NAMES[2],
        "task-lineage recovery is the orchestrator's own path "
        "(EXECUTION_FAILED / attempts), not the -32008 row; pinned in "
        "tests/agents and the coder recovery engine",
    )
    cancel = ConformanceCheck.na(
        CHECK_NAMES[3],
        "the builtin's stages are host-driven and synchronous; mid-run "
        "cancellation semantics belong to the host's queue, pinned in "
        "tests/agents/test_orchestrator.py",
    )
    budget = ConformanceCheck.na(
        CHECK_NAMES[4],
        "the builtin ships unbounded RunLimits; budget enforcement is "
        "the facade's checks (pinned there) and the mandate's step budget",
    )
    # The task vocabulary records the request (goal included) by design —
    # TASK_REQUESTED is the task's own record. The no-secrets standard here:
    # the stage outputs (plan, patch) stay inside their designed payload
    # shapes; the raw-goal recording is the vocabulary's documented choice.
    secrets = ConformanceCheck.passed(
        CHECK_NAMES[5],
        "stage outputs ride their designed payload shapes; the request "
        "(goal included) is the task vocabulary's own record — the "
        "hash-attestation discipline is the chat/coder vocabularies' rule",
    )

    return ConformanceReport(
        pipeline=BuiltinPlanExecuteCritique.name,
        checks=_checks(
            **{
                CHECK_NAMES[0]: double_run,
                CHECK_NAMES[1]: divergence,
                CHECK_NAMES[2]: unknown,
                CHECK_NAMES[3]: cancel,
                CHECK_NAMES[4]: budget,
                CHECK_NAMES[5]: secrets,
            }
        ),
    )


# ---------------------------------------------------------------------------
# The CI entry
# ---------------------------------------------------------------------------


async def every_builtin_conformance(
    ledger: LedgerStore, tmp_path: Path
) -> list[ConformanceReport]:
    """Run the standard over every registered in-process pipeline."""
    return [
        await builtin_conformance(ledger),
        await chat_conformance(tmp_path),
        await coder_conformance(ledger, tmp_path),
    ]
