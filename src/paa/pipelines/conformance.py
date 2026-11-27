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
import hashlib
import json
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from paa.core.errors import SandboxError
from paa.core.types import EventType
from paa.ledger.events import LedgerEvent
from paa.ledger.store import LedgerStore
from paa.pipelines.journal import (
    CallJournal,
    CallOutcome,
    read_call_journal,
)
from paa.pipelines.replay import ReplayView
from paa.pipelines.slots import PipelineDeclaration
from paa.sandbox.base import (
    IsolationLevel,
    Sandbox,
    SandboxResult,
    SandboxSession,
    SandboxSpec,
)

__all__ = [
    "ConformanceCheck",
    "ConformanceReport",
    "builtin_conformance",
    "chat_conformance",
    "coder_conformance",
    "every_builtin_conformance",
    "improve_conformance",
    "installed_conformance",
    "manager_conformance",
    "mcp_host_conformance",
    "observe_conformance",
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
    from paa.coder.journal import LedgerToolJournal
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
            tool_journal=LedgerToolJournal(ledger, run_id),
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
    # No registered in-process pipeline journals a *mutating* call through
    # CallJournal: coder journals only `think`, which is pure, and `use_skill`
    # rides TOOL_CALL_*. A hand-written `commit` row would prove the fold in
    # isolation and then be reported as a pipeline pass — the exact masking
    # this standard exists to stop. The behaviour is pinned where the
    # vocabulary exists; say so instead of claiming a green here.
    unknown = ConformanceCheck.na(
        CHECK_NAMES[2],
        "no registered in-process pipeline journals a mutating call through "
        "CallJournal: coder journals only `think` (coder/pipeline.py:340,359) "
        "and it is pure (pipelines/replay.py:90), while `use_skill` rides "
        "TOOL_CALL_* (coder/pipeline.py:451-506); -32008 is pinned in "
        "tests/pipelines/test_replay.py::TestDecisionTable::"
        "test_interrupted_mutating_call_never_auto_repeats and the kill-9 "
        "sweep in tests/pipelines/test_replay_chaos.py:214-224",
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
        tool_journal=LedgerToolJournal(ledger, run_d),
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
    rows_d = await ledger.read_correlation(run_d)
    failed_seqs: set[int] = set()
    for e in rows_d:
        seq = e.payload.get("seq")
        if (
            e.event_type is EventType.PIPELINE_CALL_FAILED
            and e.payload.get("reason") == "cancelled"
            and isinstance(seq, int)
            and not isinstance(seq, bool)
        ):
            failed_seqs.add(seq)
    dangling = [r.seq for r in fold_d if r.outcome is CallOutcome.PROPOSED]
    closed = bool(failed_seqs) and failed_seqs <= {
        r.seq for r in fold_d if r.outcome is CallOutcome.FAILED
    }
    no_run_completed = not any(
        e.event_type is EventType.PIPELINE_RUN_COMPLETED for e in rows_d
    )
    if cancelled_cleanly and closed and not dangling and no_run_completed:
        cancel = ConformanceCheck.passed(
            CHECK_NAMES[3],
            f"the cancel propagated; seq(s) {sorted(failed_seqs)} closed as "
            "PIPELINE_CALL_FAILED(reason='cancelled') with no dangling "
            "PROPOSED and no PIPELINE_RUN_COMPLETED",
        )
    else:
        cancel = ConformanceCheck.failed(
            CHECK_NAMES[3],
            f"cancelled_cleanly={cancelled_cleanly} failed={sorted(failed_seqs)} "
            f"dangling={dangling} run_completed={not no_run_completed}",
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
    # A negative scan alone can pass on a vocabulary the runner never wrote.
    # Prove both halves: the marker is absent from every payload, AND the
    # two tool results it rides in are attested by hash in TOOL_CALL_COMPLETED.
    texts = await _payload_texts(ledger, run_a)
    leaked = [t for t in texts if secret in t]
    tool_rows = [
        e
        for e in await ledger.read_correlation(run_a)
        if e.event_type is EventType.TOOL_CALL_COMPLETED
    ]
    attested = [e.payload.get("result_sha256") for e in tool_rows]
    expected = [
        hashlib.sha256(f"{secret}-{name}".encode()).hexdigest()
        for name in ("alpha", "beta")
    ]
    if not leaked and attested == expected:
        secrets = ConformanceCheck.passed(
            CHECK_NAMES[5],
            "the seeded tool-result marker appears in no payload; both "
            "TOOL_CALL_COMPLETED rows attest it by result_sha256 only",
        )
    else:
        secrets = ConformanceCheck.failed(
            CHECK_NAMES[5],
            f"leaked={len(leaked)} attested={attested} expected={expected}",
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
    from paa.chat.context_bundle import ChatContextBundle, build_context_bundle
    from paa.chat.loop import ChatLoop, ChatReply
    from paa.chat.service import ChatService
    from paa.chat.session import text_sha256
    from paa.config import ChatSettings, ContextSettings
    from paa.core.types import PermissionMode
    from paa.memory.creator import MemoryCreator
    from paa.pipelines.slots import ContextProvider
    from paa.storage.coldlake.cas import ContentAddressedStore
    from paa.storage.coldlake.signals import SignalRepository
    from paa.storage.relational.database import Database

    async def service(
        tmp: Path,
        *,
        settings: ChatSettings | None = None,
        context_provider: ContextProvider | None = None,
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
            context_provider=context_provider,
        )

    dbs: list[Any] = []
    ledgers: list[LedgerStore] = []

    def attested(
        events: list[LedgerEvent], session_id: uuid.UUID
    ) -> tuple[tuple[str, tuple[tuple[str, str], ...]], ...]:
        """The hash attestations only: ids and timings are deliberately random.

        The episodic consult hashes ``episodic:{session_id}`` (chat/loop.py:566),
        so that one value is per-run by design. Normalise it to a sentinel;
        every other attestation — the message, its recall, the reply — stays
        byte-exact across the two sessions.
        """
        episodic_sha = text_sha256(f"episodic:{session_id}")
        return tuple(
            (
                e.event_type.value,
                tuple(
                    sorted(
                        (
                            k,
                            "<episodic-session>"
                            if k == "query_sha256" and v == episodic_sha
                            else v,
                        )
                        for k, v in e.payload.items()
                        if k.endswith("_sha256") and isinstance(v, str)
                    )
                ),
            )
            for e in events
        )

    try:
        # -- 1. double-run sequence equality --------------------------------
        secret = _marker()
        dir_one, dir_two = tmp_path / "one", tmp_path / "two"
        dir_one.mkdir()
        dir_two.mkdir()
        s1 = await service(dir_one)
        sid1 = await s1.create_session()
        first = await s1.send(sid1, f"{secret} first")
        s2 = await service(dir_two)
        sid2 = await s2.create_session()
        second = await s2.send(sid2, f"{secret} first")
        events1 = await ledgers[0].read_correlation(sid1)
        events2 = await ledgers[1].read_correlation(sid2)
        kinds1 = [e.event_type.value for e in events1]
        kinds2 = [e.event_type.value for e in events2]
        hashes1 = attested(events1, sid1)
        hashes2 = attested(events2, sid2)
        if (
            kinds1 == kinds2
            and events1
            and hashes1 == hashes2
            and first.status == second.status
            and first.reply == second.reply
        ):
            double_run = ConformanceCheck.passed(
                CHECK_NAMES[0],
                f"two sessions walked the same {len(events1)}-event turn "
                "sequence with the same attested hashes (episodic session "
                "id normalised) and terminal reply",
            )
        else:
            double_run = ConformanceCheck.failed(
                CHECK_NAMES[0],
                f"{kinds1} vs {kinds2}; hashes={hashes1} vs "
                f"{hashes2}; status={first.status}/{second.status}",
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
        observed_ceilings: list[int] = []

        def recording_provider(
            *,
            persona: str,
            recalled_facts: list[dict[str, Any]],
            recent_turns: list[dict[str, Any]],
            token_ceiling: int,
        ) -> ChatContextBundle:
            observed_ceilings.append(token_ceiling)
            return build_context_bundle(
                persona=persona,
                recalled_facts=recalled_facts,
                recent_turns=recent_turns,
                token_ceiling=token_ceiling,
            )

        dir_four = tmp_path / "four"
        dir_four.mkdir()
        s4 = await service(
            dir_four,
            settings=ChatSettings(token_budget=1),
            context_provider=recording_provider,
        )
        sid4 = await s4.create_session()
        result4 = await s4.send(sid4, "tight budget turn")
        if (
            result4.status == "completed"
            and observed_ceilings == [1]
            and result4.token_ceiling == 1
            and 0 <= result4.bundle_tokens <= 1
        ):
            budget = ConformanceCheck.passed(
                CHECK_NAMES[4],
                "token_budget=1 reached the context provider as "
                f"token_ceiling=1; the turn stayed completed with "
                f"{result4.bundle_tokens} bundle tokens (an empty packet)",
            )
        else:
            budget = ConformanceCheck.failed(
                CHECK_NAMES[4],
                f"status={result4.status} ceilings={observed_ceilings} "
                f"result_ceiling={result4.token_ceiling} "
                f"bundle_tokens={result4.bundle_tokens} error={result4.error}",
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
    from paa.ledger.replay import TaskPhase
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
    phases: list[TaskPhase] = []
    kinds: list[list[str]] = []
    for _ in range(2):
        orch = orchestrator()
        cid = await orch.submit(TaskRequest(goal=f"{secret} conform"))
        outcome = await orch.run(cid)
        phases.append(outcome.phase)
        events = await ledger.read_correlation(cid)
        kinds.append([e.event_type.value for e in events])
    # A failed stage is a RED row, not an assertion escaping the library
    # function: `render_reports` must always see all six checks.
    if (
        all(p is TaskPhase.COMMITTED for p in phases)
        and kinds[0] == kinds[1]
        and kinds[0]
    ):
        double_run = ConformanceCheck.passed(
            CHECK_NAMES[0],
            f"two submissions walked the same {len(kinds[0])}-event task sequence",
        )
    else:
        double_run = ConformanceCheck.failed(
            CHECK_NAMES[0],
            f"phases={[p.value for p in phases]} kinds={kinds[0]} vs {kinds[1]}",
        )

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
        "the task vocabulary has no CANCELLED event type and "
        "ChiefOrchestrator has no cancel path; "
        "tests/agents/test_orchestrator.py pins event sequences, gates, "
        "retries and resume, not cancellation. Cancellation is pinned where "
        "a vocabulary exists: tests/chat/test_loop.py:212-243 and "
        "tests/pipelines/test_coder_pipeline.py:510-560",
    )
    budget = ConformanceCheck.na(
        CHECK_NAMES[4],
        "the builtin ships unbounded RunLimits; budget enforcement is "
        "the facade's checks (pinned there) and the mandate's step budget",
    )
    # A seeded secret lands in TASK_REQUESTED verbatim (the orchestrator
    # records the goal), and no builtin stage value is hash-attested. Calling
    # that a pass would green-wash the exact property being checked; the
    # honest row is n/a with a pointer to where attestation is pinned.
    secrets = ConformanceCheck.na(
        CHECK_NAMES[5],
        "the task vocabulary carries the goal verbatim in TASK_REQUESTED "
        "(agents/orchestrator.py:317-324) and attests no stage value by hash "
        "(pipelines/builtin.py:463-473); the hash-attestation promise is "
        "pinned in tests/chat/test_chat_events.py:249-275 and "
        "tests/coder/test_secret_hygiene.py:626-652",
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
# builtin.observe — the T1 adapter, pipeline #4 (C7.1)
# ---------------------------------------------------------------------------


async def observe_conformance(ledger: LedgerStore, tmp_path: Path) -> ConformanceReport:
    """The six checks over ``builtin.observe``, in the observer's vocabulary.

    The observer's honesty limits what can be green here, and the rows say
    so: a run whose subject is a foreign mind cannot promise the subject's
    determinism, and an adapter that declares ``resume = "none"`` has no
    replay to diverge. What the vocabulary *can* promise for real — the
    observer's own call sequence, the no-auto-retry discipline around a
    killed child, the wall-clock cancel, the hash-attested transcript —
    is checked for real against a scripted fake harness on the real
    subprocess backend.
    """
    import shlex
    import sys

    from paa.observe.pipeline import ObserveFacade, ObservePipeline
    from paa.pipelines.base import PipelineStatus, PipelineTask
    from paa.pipelines.journal import CallOutcome
    from paa.sandbox.subprocess_backend import SubprocessSandbox

    sandbox = SubprocessSandbox()
    bundle_home = tmp_path / "observe-bundles"
    bundle_home.mkdir(exist_ok=True)

    async def drive(
        run_id: uuid.UUID,
        harness_argv: list[str],
        *,
        workspace: Path,
        timeout_s: int = 30,
    ) -> Any:
        journal = CallJournal(ledger, run_id)
        await journal.run_started(
            ObservePipeline.name,
            ObservePipeline.version,
            run_kind="observe",
            resume="none",
            evidence_tier="T1",
        )
        facade = ObserveFacade(
            sandbox=sandbox,
            journal=journal,
            workspace=workspace,
            bundle_home=bundle_home,
        )
        task = PipelineTask(
            correlation_id=run_id,
            session_id=None,
            # Quoted so an argv word with spaces (a ``-c`` payload) survives
            # the pipeline's own shlex round trip exactly.
            goal=" ".join(shlex.quote(part) for part in harness_argv),
            params={"timeout_s": timeout_s, "memory_mb": 256},
        )
        result = await ObservePipeline().run(task, facade)
        await journal.run_completed(result.status.value, metrics=dict(result.metrics))
        return result

    def fresh_workspace(name: str) -> Path:
        ws = tmp_path / f"ws-{name}-{uuid.uuid4().hex[:6]}"
        ws.mkdir()
        (ws / "seed.txt").write_text("seed\n", encoding="utf-8")
        return ws

    # -- 1. double-run call-sequence equality -----------------------------
    # The same scripted harness, the same workspace *path* (re-seeded
    # between runs so the manifest effects match), the same knobs: the
    # observer's own sequence — what it proposes, what resolves — must be
    # identical. The child's behaviour is not the observer's to promise.
    harness = [
        sys.executable,
        "-c",
        "import pathlib; pathlib.Path('made.txt').write_text('by harness'); "
        "print('harness done')",
    ]
    ws_shared = tmp_path / f"ws-shared-{uuid.uuid4().hex[:6]}"

    def reseed() -> Path:
        import shutil

        if ws_shared.exists():
            shutil.rmtree(ws_shared)
        ws_shared.mkdir()
        (ws_shared / "seed.txt").write_text("seed\n", encoding="utf-8")
        return ws_shared

    run_a, run_b = uuid.uuid4(), uuid.uuid4()
    await drive(run_a, harness, workspace=reseed())
    await drive(run_b, harness, workspace=reseed())
    fold_a = await read_call_journal(ledger, run_a)
    fold_b = await read_call_journal(ledger, run_b)

    def shape(records: list[Any]) -> list[tuple[Any, ...]]:
        return [(r.seq, r.method, r.fingerprint, r.outcome) for r in records]

    if shape(fold_a) == shape(fold_b) and len(fold_a) == 1:
        double_run = ConformanceCheck.passed(
            CHECK_NAMES[0],
            "two observations of the same scripted harness folded to the "
            "same 1-call sequence — the observer's sequence is the promise, "
            "not the foreign mind's behaviour",
        )
    else:
        double_run = ConformanceCheck.failed(
            CHECK_NAMES[0], f"sequences differ: {shape(fold_a)} vs {shape(fold_b)}"
        )

    # -- 2. divergence halt -----------------------------------------------
    divergence = ConformanceCheck.na(
        CHECK_NAMES[1],
        "the adapter declares resume='none' (spec §6): there is no replay "
        "to diverge, and the recovery sweep refuses to re-drive the "
        "lineage — the halt-on-divergence property itself is pinned on "
        "coder.tool-loop (tests/pipelines/test_replay.py)",
    )

    # -- 3. unknown outcome never auto-repeats ----------------------------
    # The observe-vocabulary reading of D-6: a child killed by SIGKILL
    # mid-run is a *resolved* observation with a named state, not an
    # unknown outcome to retry — exactly one PROPOSED row exists, and
    # nothing re-ran the command.
    killed_id = uuid.uuid4()
    killed = await drive(
        killed_id,
        [
            sys.executable,
            "-c",
            "import os, pathlib, signal; "
            "pathlib.Path('half.txt').write_text('half'); "
            "os.kill(os.getpid(), signal.SIGKILL)",
        ],
        workspace=fresh_workspace("killed"),
    )
    killed_rows = await read_call_journal(ledger, killed_id)
    if (
        killed.status is PipelineStatus.FAILED
        and killed.metrics["state"] == "signalled"
        and len(killed_rows) == 1
        and killed_rows[0].outcome is CallOutcome.COMPLETED
    ):
        unknown = ConformanceCheck.passed(
            CHECK_NAMES[2],
            "a SIGKILLed child resolved to exactly one COMPLETED call with "
            "state='signalled' — classified, never re-run",
        )
    else:
        unknown = ConformanceCheck.failed(
            CHECK_NAMES[2],
            f"killed child left status={killed.status.value} "
            f"state={killed.metrics.get('state')} rows={len(killed_rows)}",
        )

    # -- 4. cancel within grace -------------------------------------------
    # The observer's wall clock is the only cancel this vocabulary has,
    # and it is real: the whole tree dies, the call resolves TIMED_OUT,
    # the run lands in a terminal failed state with coherent rows.
    timed_id = uuid.uuid4()
    timed = await drive(
        timed_id,
        [sys.executable, "-c", "import time; time.sleep(30)"],
        workspace=fresh_workspace("timed"),
        timeout_s=1,
    )
    timed_rows = await read_call_journal(ledger, timed_id)
    if (
        timed.status is PipelineStatus.FAILED
        and timed.metrics["state"] == "timed_out"
        and len(timed_rows) == 1
        and timed_rows[0].outcome is CallOutcome.COMPLETED
    ):
        cancel = ConformanceCheck.passed(
            CHECK_NAMES[3],
            "the 1 s wall clock killed the tree mid-run; the call resolved "
            "TIMED_OUT with terminal rows, nothing half-closed",
        )
    else:
        cancel = ConformanceCheck.failed(
            CHECK_NAMES[3],
            f"timeout left status={timed.status.value} "
            f"state={timed.metrics.get('state')} rows={len(timed_rows)}",
        )

    # -- 5. budget exhaustion ---------------------------------------------
    budget = ConformanceCheck.na(
        CHECK_NAMES[4],
        "the observed child's budgets (wall clock, memory ceiling) are the "
        "sandbox backend's own contract, pinned in tests/sandbox; the "
        "adapter itself ships unbounded RunLimits like the builtin",
    )

    # -- 6. no secrets in journal -----------------------------------------
    secret = _marker()
    secret_id = uuid.uuid4()
    observed = await drive(
        secret_id,
        [
            sys.executable,
            "-c",
            f"print('{secret}')",
        ],
        workspace=fresh_workspace("secret"),
    )
    texts = await _payload_texts(ledger, secret_id)
    leaked = [t for t in texts if secret in t]
    from paa.observe.bundle import read_bundle

    bundle = read_bundle(Path(observed.metrics["bundle_path"]))
    positive_control = (
        bundle.transcript_stdout is not None and secret in bundle.transcript_stdout.text
    )
    if not leaked and positive_control:
        secrets = ConformanceCheck.passed(
            CHECK_NAMES[5],
            "the harness's stdout secret rode the bundle (attested by "
            "SHA-256) and never appeared as payload text on the lineage",
        )
    else:
        secrets = ConformanceCheck.failed(
            CHECK_NAMES[5],
            f"leaked={bool(leaked)} positive_control={positive_control}",
        )

    return ConformanceReport(
        pipeline=ObservePipeline.name,
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
# improve.reflection-loop — the guarded self-improvement cycle (C8.1)
# ---------------------------------------------------------------------------


async def improve_conformance(ledger: LedgerStore, tmp_path: Path) -> ConformanceReport:
    """The six checks over ``improve.reflection-loop``, in its own vocabulary.

    This is the first registered pipeline that journals a **mutating**
    call through the call journal (``improve.commit``), so the
    unknown-outcome check — an ``n/a`` row with a pointer for every other
    pipeline — is a real green here: a crashed commit is never
    auto-repeated, and the repository is left exactly as the crash left
    it. The other five are real against a fixture git repository with a
    seeded high-friction ledger, a scripted approver and gate commands
    that are real subprocesses with real exit codes.
    """
    import shutil
    import sys

    from paa.core.types import EventType, new_correlation_id
    from paa.improve.gate import Approver, HumanDecision
    from paa.improve.pipeline import ImproveFacade, ImproveReflectionLoop
    from paa.ledger.events import LedgerEvent
    from paa.pipelines.base import PipelineStatus, PipelineTask

    green = [[sys.executable, "-c", "raise SystemExit(0)"]]

    async def _git(root: Path, *args: str) -> str:
        """One fixture git call — setup, never the machinery under test.

        The coder suite's rule: the tools being tested are never their own
        fixture. Same discipline as ``gitcommit._run_git`` (absolute
        executable, literal argv), but plain: this is scaffolding.
        """
        git = shutil.which("git")
        if git is None:
            raise RuntimeError("conformance needs git on the host")
        proc = await asyncio.create_subprocess_exec(
            git,
            *args,
            cwd=str(root),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, _ = await proc.communicate()
        if proc.returncode != 0:
            raise RuntimeError(f"fixture git {args[0]} failed (exit {proc.returncode})")
        return stdout.decode("utf-8", errors="replace").strip()

    async def fixture_repo(root: Path) -> Path:
        """A real git repository with identity and a seed playbook."""
        root.mkdir(parents=True, exist_ok=True)
        (root / "playbooks.md").write_text("# ops playbooks\n", encoding="utf-8")
        await _git(root, "init", "-q", "-b", "main")
        await _git(root, "config", "user.email", "conformance@example.invalid")
        await _git(root, "config", "user.name", "Conformance")
        await _git(root, "add", "--", "playbooks.md")
        await _git(root, "commit", "-q", "-m", "seed")
        return root.resolve()

    async def reseed(repo: Path, sha: str) -> None:
        """Restore the fixture to its seed commit (the observe pattern)."""
        await _git(repo, "reset", "--hard", sha)
        await _git(repo, "clean", "-fdx")

    # The reflection input: one deploy task with a correction and a
    # rollback against zero clean commits — friction 4.5, the same seed
    # the daemon's own tests use. The pipeline journal's own rows are
    # not in the engine's event set, so the two runs below see the same
    # friction from the same ledger.
    seed_cid = new_correlation_id()
    await ledger.append(
        LedgerEvent.create(
            seed_cid,
            EventType.TASK_REQUESTED,
            payload={"request": {"goal": "deploy the service"}},
        )
    )
    await ledger.append(LedgerEvent.create(seed_cid, EventType.USER_CORRECTION))
    await ledger.append(
        LedgerEvent.create(
            seed_cid, EventType.STATE_ROLLBACK_TRIGGERED, discriminator="r1"
        )
    )

    shared_repo = await fixture_repo(tmp_path / "improve-shared-repo")
    seed = await _git(shared_repo, "rev-parse", "HEAD")
    artifact_home = tmp_path / "improve-artifacts"
    artifact_home.mkdir(exist_ok=True)

    async def approve(
        proposal: object, verdict: object
    ) -> HumanDecision:
        return HumanDecision(
            decision="approved",
            approver_kind="human",
            approver_id="conformance",
            note="scripted approval",
        )

    async def drive(
        run_id: uuid.UUID,
        *,
        validators: list[list[str]] | None = None,
        approver: Approver | None = None,
        replay: ReplayView | None = None,
        timeout_s: int | None = None,
    ) -> Any:
        journal = CallJournal(ledger, run_id)
        await journal.run_started(
            ImproveReflectionLoop.name, ImproveReflectionLoop.version
        )
        facade = ImproveFacade(
            ledger=ledger,
            repo_root=shared_repo,
            artifact_home=artifact_home,
            journal=journal,
            validators=green if validators is None else validators,
            approver=approve if approver is None else approver,
            replay=replay,
            allow_insecure_signing=True,
        )
        # Only pass the timeout when it is asked for: the default (the
        # facade's own 300) must stay identical between a `drive` run and
        # a hand-driven facade, or the fingerprints diverge.
        params: dict[str, Any] = {}
        if timeout_s is not None:
            params["validation_timeout_s"] = timeout_s
        task = PipelineTask(
            correlation_id=run_id, session_id=None, goal="self-improve", params=params
        )
        result = await ImproveReflectionLoop().run(task, facade)
        await journal.run_completed(result.status.value, metrics=dict(result.metrics))
        return result

    # -- 1. double-run call-sequence equality -----------------------------
    # Same ledger, same repo *path* re-seeded to the seed commit between
    # runs (so the proposal's prior content matches), same validators:
    # the five journalled calls must fold to the same fingerprints.
    run_a, run_b = uuid.uuid4(), uuid.uuid4()
    await drive(run_a)
    await reseed(shared_repo, seed)
    await drive(run_b)
    fold_a = await read_call_journal(ledger, run_a)
    fold_b = await read_call_journal(ledger, run_b)

    def shape(records: list[Any]) -> list[tuple[Any, ...]]:
        return [(r.seq, r.method, r.fingerprint, r.outcome) for r in records]

    if shape(fold_a) == shape(fold_b) and len(fold_a) == 5:
        double_run = ConformanceCheck.passed(
            CHECK_NAMES[0],
            "two guarded cycles over the same seeded ledger folded to the "
            "same 5-call sequence (reflect, propose, validate, gate, commit)",
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
    await reseed(shared_repo, seed)
    try:
        # The same lineage replayed with a different validator set: the
        # validate call's fingerprint changes, and the run must stop there
        # — the record can no longer speak for this run.
        await drive(
            run_a,
            validators=[[sys.executable, "-c", "raise SystemExit(1)"]],
            replay=await ReplayView.load(ledger, run_a),
        )
    except PipelineDivergedError:
        halted = True
        diverged_row = any(
            e.event_type.value == "PIPELINE_RUN_DIVERGED"
            for e in await ledger.read_correlation(run_a)
        )
    except Exception as exc:  # a halt of the wrong kind is a finding
        wrong_exc = f"{type(exc).__name__}: {exc}"
    if halted and diverged_row:
        divergence = ConformanceCheck.passed(
            CHECK_NAMES[1],
            "a re-run with a changed validator set diverged at improve.validate "
            "and halted with PIPELINE_RUN_DIVERGED; the host never resynced",
        )
    else:
        divergence = ConformanceCheck.failed(
            CHECK_NAMES[1],
            f"halted={halted} diverged_row={diverged_row} wrong_exc={wrong_exc}",
        )

    # -- 3. unknown-outcome never auto-repeats (a real row, at last) ------
    # improve.commit is a mutating call journalled through CallJournal —
    # the first registered pipeline with one. Build the crash state the
    # sweep would find: stages 1-4 really ran, and the commit's PROPOSED
    # row exists with no outcome. The re-run must refuse with -32008 and
    # leave the repository exactly as the crash left it.
    from paa.pipelines.base import OutcomeUnknownError

    crashed = uuid.uuid4()
    journal_c = CallJournal(ledger, crashed)
    await journal_c.run_started(ImproveReflectionLoop.name, ImproveReflectionLoop.version)
    facade_c = ImproveFacade(
        ledger=ledger,
        repo_root=shared_repo,
        artifact_home=artifact_home,
        journal=journal_c,
        validators=green,
        approver=approve,
        allow_insecure_signing=True,
    )
    report_c = await facade_c.reflect(7)
    proposal_c = await facade_c.propose(report_c.high_friction_domains[0])
    verdict_c = await facade_c.validate(proposal_c)
    decision_c = await facade_c.ask_human(proposal_c, verdict_c)
    facade_c.carry_approval(decision_c)
    # The crash: the PROPOSED row landed, the effects never did.
    await journal_c.call_proposed(
        5,
        "improve.commit",
        {"repo_root": str(shared_repo), "proposal_sha256": proposal_c.sha256},
    )
    playbook_before = (shared_repo / "playbooks.md").read_text(encoding="utf-8")
    refused = False
    wrong_exc = None
    try:
        await drive(
            crashed,
            replay=await ReplayView.load(ledger, crashed),
        )
    except OutcomeUnknownError:
        refused = True
    except Exception as exc:  # any other outcome is a finding
        wrong_exc = f"{type(exc).__name__}: {exc}"
    rows_c = await read_call_journal(ledger, crashed)
    commit_rows = [r for r in rows_c if r.method == "improve.commit"]
    still_proposed = len(commit_rows) == 1 and commit_rows[0].outcome is CallOutcome.PROPOSED
    nothing_reapplied = (
        (shared_repo / "playbooks.md").read_text(encoding="utf-8") == playbook_before
    )
    if refused and still_proposed and nothing_reapplied:
        unknown = ConformanceCheck.passed(
            CHECK_NAMES[2],
            "a crashed improve.commit (PROPOSED, no outcome) refused the "
            "re-run with -32008; exactly one PROPOSED row remains and the "
            "repository is untouched — the pipeline decides, never the host",
        )
    else:
        unknown = ConformanceCheck.failed(
            CHECK_NAMES[2],
            f"refused={refused} still_proposed={still_proposed} "
            f"reapplied={not nothing_reapplied} wrong_exc={wrong_exc}",
        )

    # -- 4. cancel within grace --------------------------------------------
    # The honest place a cancel lands in this vocabulary: the human gate.
    # The approver signals when it is asked, then takes its time — a
    # human reading the evidence — so the cancel deterministically lands
    # inside the gate call, never wherever the faster stages happen to be.
    run_d = uuid.uuid4()
    journal_d = CallJournal(ledger, run_d)
    await journal_d.run_started(ImproveReflectionLoop.name, ImproveReflectionLoop.version)
    asked = asyncio.Event()

    async def slow_approver(proposal: object, verdict: object) -> HumanDecision:
        asked.set()
        await asyncio.sleep(30)
        return HumanDecision(
            decision="approved", approver_kind="human", approver_id="slow"
        )

    facade_d = ImproveFacade(
        ledger=ledger,
        repo_root=shared_repo,
        artifact_home=artifact_home,
        journal=journal_d,
        validators=green,
        approver=slow_approver,
        allow_insecure_signing=True,
    )
    task_d = asyncio.create_task(
        ImproveReflectionLoop().run(
            PipelineTask(
                correlation_id=run_d, session_id=None, goal="self-improve", params={}
            ),
            facade_d,
        )
    )
    await asyncio.wait_for(asked.wait(), timeout=30)
    task_d.cancel()
    cancelled_cleanly = False
    try:
        await task_d
    except asyncio.CancelledError:
        cancelled_cleanly = True
    fold_d = await read_call_journal(ledger, run_d)
    rows_d = await ledger.read_correlation(run_d)
    failed_seqs: set[int] = set()
    for e in rows_d:
        seq = e.payload.get("seq")
        if (
            e.event_type is EventType.PIPELINE_CALL_FAILED
            and e.payload.get("reason") == "cancelled"
            and isinstance(seq, int)
            and not isinstance(seq, bool)
        ):
            failed_seqs.add(seq)
    dangling = [r.seq for r in fold_d if r.outcome is CallOutcome.PROPOSED]
    closed = bool(failed_seqs) and failed_seqs <= {
        r.seq for r in fold_d if r.outcome is CallOutcome.FAILED
    }
    gate_was_asked = any(r.method == "improve.gate" for r in fold_d)
    no_run_completed = not any(
        e.event_type is EventType.PIPELINE_RUN_COMPLETED for e in rows_d
    )
    if cancelled_cleanly and closed and gate_was_asked and not dangling and no_run_completed:
        cancel = ConformanceCheck.passed(
            CHECK_NAMES[3],
            f"the cancel landed inside the human gate; seq(s) {sorted(failed_seqs)} "
            "closed as PIPELINE_CALL_FAILED(reason='cancelled') with no dangling "
            "PROPOSED and no PIPELINE_RUN_COMPLETED",
        )
    else:
        cancel = ConformanceCheck.failed(
            CHECK_NAMES[3],
            f"cancelled_cleanly={cancelled_cleanly} failed={sorted(failed_seqs)} "
            f"gate_was_asked={gate_was_asked} dangling={dangling} "
            f"run_completed={not no_run_completed}",
        )

    # -- 5. budget exhaustion ------------------------------------------------
    # The loop's own budget rule: a gate command past its wall budget is a
    # resolved red (timed_out), never a hang — the run fails by rule.
    run_e = uuid.uuid4()
    result_e = await drive(
        run_e,
        validators=[[sys.executable, "-c", "import time; time.sleep(30)"]],
        timeout_s=1,
    )
    fold_e = await read_call_journal(ledger, run_e)
    timed_out = (
        len(fold_e) == 3
        and fold_e[2].method == "improve.validate"
        and fold_e[2].outcome is CallOutcome.COMPLETED
    )
    if (
        result_e.status is PipelineStatus.FAILED
        and result_e.metrics.get("stopped_reason") == "validation_failed"
        and timed_out
    ):
        budget = ConformanceCheck.passed(
            CHECK_NAMES[4],
            "validation_timeout_s=1 resolved the sleeping gate command as a "
            "timed-out red; the run failed by rule (validation_failed), "
            "not by crash or hang",
        )
    else:
        budget = ConformanceCheck.failed(
            CHECK_NAMES[4],
            f"status={result_e.status} metrics={result_e.metrics} "
            f"rows={[(r.method, r.outcome) for r in fold_e]}",
        )

    # -- 6. no secrets in the journal ---------------------------------------
    # The marker rides the gate command twice over: in its argv (an
    # operator can type a credential into a command) and on its stdout.
    # Neither may reach a payload: the argv is operator text the record
    # replaces with a command index, the stdout is attested by digest
    # only. The evidence file carries the stdout in full — the positive
    # control that the scan is not vacuous.
    secret = _marker()
    run_f = uuid.uuid4()
    result_f = await drive(
        run_f,
        validators=[[sys.executable, "-c", f"print('{secret}')"]],
    )
    texts = await _payload_texts(ledger, run_f)
    leaked = [t for t in texts if secret in t]
    evidence = artifact_home / f"{run_f.hex}" / "evidence" / "validation-01.json"
    positive_control = evidence.is_file() and secret in evidence.read_text(
        encoding="utf-8"
    )
    if (
        not leaked
        and positive_control
        and result_f.status is PipelineStatus.COMPLETED
    ):
        secrets = ConformanceCheck.passed(
            CHECK_NAMES[5],
            "the gate command's argv and stdout markers rode the evidence "
            "file and never appeared as payload text on the lineage "
            "(argv recorded as an index, stdout attested by digest only)",
        )
    else:
        secrets = ConformanceCheck.failed(
            CHECK_NAMES[5],
            f"leaked={len(leaked)} positive_control={positive_control} "
            f"status={result_f.status}",
        )

    return ConformanceReport(
        pipeline=ImproveReflectionLoop.name,
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
# builtin.manager — the composition manager, pipeline #5 (C8.2)
# ---------------------------------------------------------------------------


async def manager_conformance(ledger: LedgerStore) -> ConformanceReport:
    """The six checks over ``builtin.manager``, in the composition vocabulary.

    What the vocabulary can promise for real, checked for real against
    scripted workers (the registered builtins stand in as delegation
    targets; the runner is the host's act of trusting one, so a scripted
    runner exercises exactly the composition contract): the delegation
    call sequence, the divergence halt on a changed decomposition, the
    D-6 refusal around an interrupted delegation, the mid-child cancel,
    the ``max_sub_runs`` rule, and the hash-attested delegation inputs.
    """
    from paa.core.errors import BudgetExceededError
    from paa.ledger.events import canonical_json
    from paa.pipelines.base import (
        OutcomeUnknownError,
        PipelineDivergedError,
        PipelineResult,
        PipelineStatus,
        PipelineTask,
        RunLimits,
    )
    from paa.pipelines.compose import ChildRun, ManagerPipeline, run_composition
    from paa.pipelines.registry import PipelineRegistry

    registry = PipelineRegistry(local_dir=None)

    class _Worker:
        """A scripted worker: counts executions, returns a fixed result."""

        def __init__(self, summary: str, *, slow: bool = False) -> None:
            self._summary = summary
            self._slow = slow
            self.executions = 0

        async def __call__(self, run: ChildRun) -> PipelineResult:
            self.executions += 1
            if self._slow:
                await asyncio.sleep(0.05)
            return PipelineResult(
                status=PipelineStatus.COMPLETED,
                summary=self._summary,
                metrics={"worker": run.name},
            )

    def plan() -> list[dict[str, Any]]:
        return [
            {"pipeline": "builtin.chat", "goal": "greet the operator"},
            {"pipeline": "coder.tool-loop", "goal": "close the failing test"},
        ]

    def workers(*, slow_chat: bool = False) -> dict[str, _Worker]:
        return {
            "builtin.chat": _Worker("chat done", slow=slow_chat),
            "coder.tool-loop": _Worker("coder done"),
        }

    async def drive(
        run_id: uuid.UUID,
        request: dict[str, Any],
        runners: dict[str, _Worker],
        *,
        limits: RunLimits | None = None,
        replay: ReplayView | None = None,
    ) -> Any:
        task = PipelineTask(
            correlation_id=run_id, session_id=None, goal="conform", request=request
        )
        _, result = await run_composition(
            ledger, task, runners=runners, registry=registry, limits=limits, replay=replay
        )
        return result

    def shape(records: list[Any]) -> list[tuple[Any, ...]]:
        return [(r.seq, r.method, r.fingerprint, r.outcome) for r in records]

    # -- 1. double-run call-sequence equality -----------------------------
    run_a, run_b = uuid.uuid4(), uuid.uuid4()
    result_a = await drive(run_a, {"delegations": plan()}, runners=workers())
    result_b = await drive(run_b, {"delegations": plan()}, runners=workers())
    fold_a = await read_call_journal(ledger, run_a)
    fold_b = await read_call_journal(ledger, run_b)
    if (
        shape(fold_a) == shape(fold_b)
        and len(fold_a) == 2
        and result_a.status is PipelineStatus.COMPLETED
        and result_b.status is PipelineStatus.COMPLETED
        and result_a.metrics.get("delegations") == 2
    ):
        double_run = ConformanceCheck.passed(
            CHECK_NAMES[0],
            "two fresh composition runs over the same two-delegation plan "
            "folded to the same 2-call sequence (sub_run x2, COMPLETED)",
        )
    else:
        double_run = ConformanceCheck.failed(
            CHECK_NAMES[0],
            f"sequences differ: {shape(fold_a)} vs {shape(fold_b)} "
            f"status={result_a.status.value}/{result_b.status.value}",
        )

    # -- 2. divergence halt ------------------------------------------------
    fresh = workers()
    halted = False
    diverged_row = False
    wrong_exc: str | None = None
    changed = [
        {"pipeline": "builtin.chat", "goal": "changed mid-replay"},
        {"pipeline": "coder.tool-loop", "goal": "close the failing test"},
    ]
    try:
        await drive(
            run_a,
            {"delegations": changed},
            runners=fresh,
            replay=await ReplayView.load(ledger, run_a),
        )
    except PipelineDivergedError:
        halted = True
        rows = await ledger.read_correlation(run_a)
        diverged_row = any(
            e.event_type.value == "PIPELINE_RUN_DIVERGED" for e in rows
        )
    except Exception as exc:  # a halt of the wrong kind is a finding
        wrong_exc = f"{type(exc).__name__}: {exc}"
    untouched = all(w.executions == 0 for w in fresh.values())
    if halted and diverged_row and untouched:
        divergence = ConformanceCheck.passed(
            CHECK_NAMES[1],
            "a re-run whose first delegation changed diverged the replay before "
            "any worker executed; the run halted with PIPELINE_RUN_DIVERGED",
        )
    else:
        divergence = ConformanceCheck.failed(
            CHECK_NAMES[1],
            f"halted={halted} diverged_row={diverged_row} "
            f"untouched={untouched} wrong_exc={wrong_exc}",
        )

    # -- 3. unknown-outcome never auto-repeats ------------------------------
    # The manager's own vocabulary journals a mutating call (``sub_run``),
    # so D-6 is checkable here for real: the crash prefix below is exactly
    # the row set a killed process leaves — run_started plus the first
    # delegation's PROPOSED row, no outcome — written through the journal's
    # own front door, not a hand-forged shape.
    run_u = uuid.uuid4()
    goal_u = "interrupted delegation"
    journal_u = CallJournal(ledger, run_u)
    await journal_u.run_started(
        ManagerPipeline.name, ManagerPipeline.version, run_kind="composition"
    )
    await journal_u.call_proposed(
        1, "sub_run", {"pipeline": "builtin.chat", "task_input": {"goal": goal_u}}
    )
    refused_u = False
    wrong_exc_u: str | None = None
    workers_u = workers()
    try:
        await drive(
            run_u,
            {"delegations": [{"pipeline": "builtin.chat", "goal": goal_u}]},
            runners=workers_u,
            replay=await ReplayView.load(ledger, run_u),
        )
    except OutcomeUnknownError:
        refused_u = True
    except Exception as exc:
        wrong_exc_u = f"{type(exc).__name__}: {exc}"
    untouched_u = all(w.executions == 0 for w in workers_u.values())
    rows_u = await ledger.read_correlation(run_u)
    failed_row_u = any(e.event_type is EventType.PIPELINE_RUN_FAILED for e in rows_u)
    if refused_u and untouched_u and failed_row_u:
        unknown = ConformanceCheck.passed(
            CHECK_NAMES[2],
            "a crash that left the first sub_run PROPOSED refused the re-run "
            "with OutcomeUnknownError before any worker executed — sub_run can "
            "mutate, so it is never auto-repeated (D-6)",
        )
    else:
        unknown = ConformanceCheck.failed(
            CHECK_NAMES[2],
            f"refused={refused_u} untouched={untouched_u} "
            f"failed_row={failed_row_u} wrong_exc={wrong_exc_u}",
        )

    # -- 4. cancel within grace ---------------------------------------------
    run_c = uuid.uuid4()
    slow = workers(slow_chat=True)
    task_c = asyncio.create_task(drive(run_c, {"delegations": plan()}, runners=slow))
    await asyncio.sleep(0.02)
    task_c.cancel()
    cancelled_cleanly = False
    try:
        await task_c
    except asyncio.CancelledError:
        cancelled_cleanly = True
    fold_c = await read_call_journal(ledger, run_c)
    rows_c = await ledger.read_correlation(run_c)
    failed_seqs: set[int] = set()
    for e in rows_c:
        seq = e.payload.get("seq")
        if (
            e.event_type is EventType.PIPELINE_CALL_FAILED
            and e.payload.get("reason") == "cancelled"
            and isinstance(seq, int)
            and not isinstance(seq, bool)
        ):
            failed_seqs.add(seq)
    dangling_c = [r.seq for r in fold_c if r.outcome is CallOutcome.PROPOSED]
    no_run_completed_c = not any(
        e.event_type is EventType.PIPELINE_RUN_COMPLETED for e in rows_c
    )
    if cancelled_cleanly and failed_seqs and not dangling_c and no_run_completed_c:
        cancel = ConformanceCheck.passed(
            CHECK_NAMES[3],
            f"the cancel landed inside the slow child; seq(s) {sorted(failed_seqs)} "
            "closed as PIPELINE_CALL_FAILED(reason='cancelled') with no dangling "
            "PROPOSED and no PIPELINE_RUN_COMPLETED",
        )
    else:
        cancel = ConformanceCheck.failed(
            CHECK_NAMES[3],
            f"cancelled_cleanly={cancelled_cleanly} failed={sorted(failed_seqs)} "
            f"dangling={dangling_c} run_completed={not no_run_completed_c}",
        )

    # -- 5. budget exhaustion ------------------------------------------------
    run_e = uuid.uuid4()
    workers_e = workers()
    budget_kind: str | None = None
    wrong_exc_e: str | None = None
    try:
        await drive(
            run_e,
            {"delegations": plan()},
            runners=workers_e,
            limits=RunLimits(
                max_tokens=1000, max_wall_s=60, max_calls=100, max_sub_runs=1
            ),
        )
    except BudgetExceededError as exc:
        budget_kind = exc.budget_kind
    except Exception as exc:
        wrong_exc_e = f"{type(exc).__name__}: {exc}"
    fold_e = await read_call_journal(ledger, run_e)
    rows_e = await ledger.read_correlation(run_e)
    first_completed = any(
        r.seq == 1 and r.outcome is CallOutcome.COMPLETED for r in fold_e
    )
    dangling_e = [r.seq for r in fold_e if r.outcome is CallOutcome.PROPOSED]
    second_never_proposed = not any(
        e.event_type is EventType.PIPELINE_CALL_PROPOSED and e.payload.get("seq") == 2
        for e in rows_e
    )
    if (
        budget_kind == "sub_run"
        and first_completed
        and not dangling_e
        and second_never_proposed
        and workers_e["builtin.chat"].executions == 1
        and workers_e["coder.tool-loop"].executions == 0
    ):
        budget = ConformanceCheck.passed(
            CHECK_NAMES[4],
            "max_sub_runs=1: the first delegation completed and ran, the "
            "second was refused by BudgetExceededError(sub_run) before its "
            "PROPOSED row and never executed — by rule, not by crash",
        )
    else:
        budget = ConformanceCheck.failed(
            CHECK_NAMES[4],
            f"budget_kind={budget_kind} first_completed={first_completed} "
            f"dangling={dangling_e} second_proposed={not second_never_proposed} "
            f"first_ran={workers_e['builtin.chat'].executions} "
            f"second_ran={workers_e['coder.tool-loop'].executions} "
            f"wrong_exc={wrong_exc_e}",
        )

    # -- 6. no secrets in the journal ----------------------------------------
    # The composition vocabulary's promise: delegation *inputs* are attested
    # by hash, never stored as text — the PROPOSED row carries the params'
    # canonical-JSON SHA-256 and a shape-only preview. Child results ride
    # the COMPLETED row by value (that is what replay serves), so the
    # seeded marker rides the input side, where the promise lives.
    secret = _marker()
    run_s = uuid.uuid4()
    goal_s = f"summarize {secret}"
    await drive(
        run_s,
        {"delegations": [{"pipeline": "builtin.chat", "goal": goal_s}]},
        runners=workers(),
    )
    texts = await _payload_texts(ledger, run_s)
    leaked = [t for t in texts if secret in t]
    rows_s = await ledger.read_correlation(run_s)
    proposed_s = [
        e for e in rows_s if e.event_type is EventType.PIPELINE_CALL_PROPOSED
    ]
    expected_sha = hashlib.sha256(
        canonical_json(
            {"pipeline": "builtin.chat", "task_input": {"goal": goal_s}}
        ).encode("utf-8")
    ).hexdigest()
    goal_hash = hashlib.sha256(goal_s.encode("utf-8")).hexdigest()[:8]
    attested = [e.payload.get("params_sha256") for e in proposed_s]
    shape_text = str([e.payload.get("params_shape") for e in proposed_s])
    if not leaked and attested == [expected_sha] and goal_hash in shape_text:
        secrets = ConformanceCheck.passed(
            CHECK_NAMES[5],
            "the seeded delegation-goal marker appears in no payload text; the "
            "PROPOSED row attests the params by params_sha256 and renders the "
            "goal only as its length+hash prefix",
        )
    else:
        secrets = ConformanceCheck.failed(
            CHECK_NAMES[5],
            f"leaked={len(leaked)} attested={attested} "
            f"expected={expected_sha} shape_has_hash={goal_hash in shape_text}",
        )

    return ConformanceReport(
        pipeline=ManagerPipeline.name,
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
# builtin.mcp-host — the externally-driven session, pipeline #7 (W9-C9.4)
# ---------------------------------------------------------------------------


async def mcp_host_conformance(tmp_path: Path) -> ConformanceReport:
    """The six checks over ``builtin.mcp-host``, in the server's vocabulary.

    Driven the way ``tests/mcp_server`` drives the server: in-process,
    over the server's own framing (a fed ``StreamReader`` playing the
    external harness, an in-memory sink collecting its replies), on a
    throwaway offline stack — the echo chat model, so the one tool call
    that thinks (``chat_send``) is byte-stable. The honesty limits are
    the adapter's own: a session whose client is a foreign mind cannot
    promise the client's behaviour (the wrapper's sequence is the
    promise, and it is one call), and an adapter declaring
    ``resume = "none"`` has no replay to diverge. What the vocabulary
    *can* promise for real — the deterministic session call, the
    classified death of a broken transport, the between-frames cancel,
    the canary rule over the operator's harness label — is checked for
    real against the unchanged server.
    """
    from datetime import UTC, datetime

    from paa.chat.service import ChatService
    from paa.coder.journal import args_sha256_of
    from paa.config import ChatSettings, ContextSettings
    from paa.core.types import PermissionMode
    from paa.mcp.host import McpHostPipeline, run_mcp_host_session
    from paa.mcp.protocol import PROTOCOL_VERSION, encode_frame
    from paa.mcp.server import MCPServer
    from paa.memory.creator import MemoryCreator
    from paa.storage.coldlake.cas import ContentAddressedStore
    from paa.storage.coldlake.signals import SignalRepository
    from paa.storage.relational.database import Database

    db = Database(tmp_path / "mcp-host.db")
    await db.connect()
    session_ledger = LedgerStore(db)
    signals = SignalRepository(db, ContentAddressedStore(tmp_path / "mcp-cold"))

    def _server() -> MCPServer:
        creator = MemoryCreator(db)
        chat_settings = ChatSettings()  # the offline echo model
        return MCPServer(
            chat=ChatService(
                ledger=session_ledger,
                db=db,
                creator=creator,
                settings=chat_settings,
                permission_mode=PermissionMode.ASK,
                context_settings=ContextSettings(),
                signals=signals,
            ),
            ledger=session_ledger,
            db=db,
            creator=creator,
            signals=signals,
            chat_settings=chat_settings,
        )

    def _client(*messages: dict[str, Any]) -> Any:
        reader = asyncio.StreamReader()
        for message in messages:
            reader.feed_data(encode_frame(message))
        reader.feed_eof()
        return reader

    def _initialize(id_: int = 0) -> dict[str, Any]:
        return {
            "jsonrpc": "2.0",
            "id": id_,
            "method": "initialize",
            "params": {
                "protocolVersion": PROTOCOL_VERSION,
                "capabilities": {},
                "clientInfo": {"name": "conformance-harness", "version": "0"},
            },
        }

    class _Recorder:
        def __init__(self) -> None:
            self.frames = 0

        def write(self, data: bytes) -> None:
            self.frames += 1

        async def drain(self) -> None:
            return None

    class _Exploding(_Recorder):
        def write(self, data: bytes) -> None:
            raise RuntimeError("broken pipe")

    class _Evented(_Recorder):
        def __init__(self) -> None:
            super().__init__()
            self.answered = asyncio.Event()

        def write(self, data: bytes) -> None:
            super().write(data)
            self.answered.set()

    async def drive(
        frames: Any, sink: _Recorder | None = None, *, harness: str = "conformance"
    ) -> Any:
        return await run_mcp_host_session(
            _server(),
            session_ledger,
            reader=frames,
            sink=sink if sink is not None else _Recorder(),
            harness=harness,
        )

    async def last_run_id() -> uuid.UUID:
        events = await session_ledger.events_since(
            datetime(1970, 1, 1, tzinfo=UTC),
            event_types=[EventType.PIPELINE_RUN_STARTED],
            descending=True,
            limit=1,
        )
        assert events
        return events[0].correlation_id

    try:
        # -- 1. double-run call-sequence equality -------------------------
        # The same scripted harness session, twice: the wrapper's own
        # sequence — one mcp.session call, proposed and resolved — must
        # fold identically. The client's behaviour is not the wrapper's
        # to promise; its identity (the catalogue + label) is.
        scripted = _client(
            _initialize(),
            {"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
            {"jsonrpc": "2.0", "method": "notifications/initialized"},
            {
                "jsonrpc": "2.0",
                "id": 2,
                "method": "tools/call",
                "params": {"name": "sessions_list", "arguments": {}},
            },
        )
        run_a, result_a = await drive(scripted)
        run_b, result_b = await drive(
            _client(
                _initialize(),
                {"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
                {"jsonrpc": "2.0", "method": "notifications/initialized"},
                {
                    "jsonrpc": "2.0",
                    "id": 2,
                    "method": "tools/call",
                    "params": {"name": "sessions_list", "arguments": {}},
                },
            )
        )
        fold_a = await read_call_journal(session_ledger, run_a)
        fold_b = await read_call_journal(session_ledger, run_b)
        shape_a = [(r.seq, r.method, r.fingerprint, r.outcome) for r in fold_a]
        shape_b = [(r.seq, r.method, r.fingerprint, r.outcome) for r in fold_b]
        if (
            shape_a == shape_b
            and len(fold_a) == 1
            and result_a.metrics["requests_answered"]
            == result_b.metrics["requests_answered"]
            == 3
        ):
            double_run = ConformanceCheck.passed(
                CHECK_NAMES[0],
                "two scripted sessions of the same harness folded to the "
                "same 1-call sequence and answered the same 3 requests — "
                "the wrapper's sequence is the promise, not the foreign "
                "client's behaviour",
            )
        else:
            double_run = ConformanceCheck.failed(
                CHECK_NAMES[0], f"{shape_a} vs {shape_b}"
            )

        # -- 2. divergence halt ---------------------------------------------
        divergence = ConformanceCheck.na(
            CHECK_NAMES[1],
            "the adapter declares resume='none' (spec §6): an MCP session "
            "is not journalled for replay — each turn is its own "
            "request/response — so there is no replay to diverge, and the "
            "pipeline-lineage sweep exemption means a crashed session is "
            "never re-queued as task work (the halt-on-divergence property "
            "itself is pinned on coder.tool-loop, tests/pipelines/"
            "test_replay.py)",
        )

        # -- 3. unknown outcome never auto-repeats --------------------------
        # The mcp-host reading of D-6: a session whose transport died
        # mid-serve is a *resolved* session call with a named state —
        # exactly one PROPOSED-then-FAILED row pair and a FAILED run, the
        # fault named by type only — never an open PROPOSED the host might
        # be tempted to re-drive.
        broken = _client(_initialize())
        died = False
        try:
            await drive(broken, _Exploding())
        except RuntimeError:
            died = True
        died_id = await last_run_id()
        died_rows = await read_call_journal(session_ledger, died_id)
        died_events = await session_ledger.read_correlation(died_id)
        run_failed_rows = [
            e for e in died_events if e.event_type is EventType.PIPELINE_RUN_FAILED
        ]
        if (
            died
            and len(died_rows) == 1
            and died_rows[0].outcome is CallOutcome.FAILED
            and died_rows[0].error == "session did not resolve (RuntimeError)"
            and len(run_failed_rows) == 1
            and run_failed_rows[0].payload["reason"] == "RuntimeError"
        ):
            unknown = ConformanceCheck.passed(
                CHECK_NAMES[2],
                "a session whose transport died mid-serve resolved to one "
                "FAILED mcp.session call and one FAILED run naming the "
                "fault's type — classified, never re-driven",
            )
        else:
            unknown = ConformanceCheck.failed(
                CHECK_NAMES[2],
                f"died={died} rows={[(r.outcome, r.error) for r in died_rows]} "
                f"run_failed={len(run_failed_rows)}",
            )

        # -- 4. cancel within grace ------------------------------------------
        # The real cancel this vocabulary has: the session task killed
        # between frames. Both rows close FAILED('cancelled') before the
        # CancelledError propagates — nothing half-closed.
        parked = asyncio.StreamReader()
        parked.feed_data(encode_frame(_initialize()))  # answered, then parked
        evented = _Evented()
        cancel_task = asyncio.create_task(drive(parked, evented, harness="cancel-me"))
        await evented.answered.wait()
        cancel_task.cancel()
        cancelled = False
        try:
            await cancel_task
        except asyncio.CancelledError:
            cancelled = True
        cancel_id = await last_run_id()
        cancel_rows = await read_call_journal(session_ledger, cancel_id)
        coherently_closed = (
            len(cancel_rows) == 1
            and cancel_rows[0].outcome is CallOutcome.FAILED
            and cancel_rows[0].error == "cancelled"
        )
        cancel_events = await session_ledger.read_correlation(cancel_id)
        run_cancelled = any(
            e.event_type is EventType.PIPELINE_RUN_FAILED
            and e.payload["reason"] == "cancelled"
            for e in cancel_events
        )
        if cancelled and coherently_closed and run_cancelled:
            cancel = ConformanceCheck.passed(
                CHECK_NAMES[3],
                "the cancel landed between frames and the session closed "
                "FAILED('cancelled') at both the call and the run before "
                "the CancelledError propagated — nothing half-closed",
            )
        else:
            cancel = ConformanceCheck.failed(
                CHECK_NAMES[3],
                f"cancelled={cancelled} rows={[(r.outcome, r.error) for r in cancel_rows]} "
                f"run_cancelled={run_cancelled}",
            )

        # -- 5. budget exhaustion ---------------------------------------------
        budget = ConformanceCheck.na(
            CHECK_NAMES[4],
            "the session is externally paced and the adapter ships "
            "unbounded RunLimits — nothing at this layer to exhaust; the "
            "limits that do bind are the server's own per-call "
            "disciplines (the LOCKDOWN policy ceiling, the search and "
            "list caps), pinned in tests/mcp_server",
        )

        # -- 6. no secrets in journal -----------------------------------------
        secret = _marker()
        secret_id: uuid.UUID | None = None
        try:
            secret_id, _ = await drive(
                _client(
                    _initialize(),
                    {
                        "jsonrpc": "2.0",
                        "id": 1,
                        "method": "tools/call",
                        "params": {
                            "name": "chat_send",
                            "arguments": {"message": f"conformance probe {secret}"},
                        },
                    },
                )
            )
        except Exception as exc:  # the chat turn failing is a RED row, not a crash
            secrets = ConformanceCheck.failed(CHECK_NAMES[5], f"probe failed: {exc}")
        else:
            assert secret_id is not None
            texts = await _payload_texts(session_ledger, secret_id)
            leaked = [t for t in texts if secret in t]
            # The positive control: the marked input really crossed the
            # session — the server's per-call PROPOSED row attests the
            # very arguments that carried it, by their canonical SHA-256.
            tool_rows = await session_ledger.events_since(
                datetime(1970, 1, 1, tzinfo=UTC),
                event_types=[EventType.TOOL_CALL_PROPOSED],
                descending=True,
                limit=1,
            )
            expected_sha = args_sha256_of(
                {"message": f"conformance probe {secret}"}
            )
            positive_control = bool(tool_rows) and (
                tool_rows[0].payload["args_sha256"] == expected_sha
            )
            if not leaked and positive_control:
                secrets = ConformanceCheck.passed(
                    CHECK_NAMES[5],
                    "the chat_send marker rode the server's own per-call "
                    "lineage (attested by args_sha256) and never appeared "
                    "in a payload the session wrapper wrote",
                )
            else:
                secrets = ConformanceCheck.failed(
                    CHECK_NAMES[5],
                    f"leaked={bool(leaked)} positive_control={positive_control}",
                )

        return ConformanceReport(
            pipeline=McpHostPipeline.name,
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
    finally:
        with contextlib.suppress(Exception):  # closing never masks the report
            await db.close()


# ---------------------------------------------------------------------------
# installed pipeline packages — the sandboxed adapter's own vocabulary (C8.6)
# ---------------------------------------------------------------------------


class _HeldSandbox(Sandbox):
    """A sandbox wrapper that holds the run window open, once.

    The installed vocabulary's ``_SlowModel``: the seam the runner drives
    (the sandbox) is made to wait a beat before it launches the child, so
    a cancel can land *inside* the entrypoint call deterministically —
    the same trick the coder standard plays on the model seam. The inner
    sandbox is the caller's own (the real one, in an install), and it
    still runs everything the hold does not intercept.
    """

    def __init__(self, inner: Sandbox) -> None:
        self._inner = inner
        self.entered = asyncio.Event()

    @property
    def name(self) -> str:
        return self._inner.name

    @property
    def isolation_level(self) -> IsolationLevel:
        return self._inner.isolation_level

    async def run(self, spec: SandboxSpec) -> SandboxResult:
        self.entered.set()
        await asyncio.sleep(1.0)
        return await self._inner.run(spec)

    async def open(self, spec: SandboxSpec) -> SandboxSession:
        return await self._inner.open(spec)

    async def healthcheck(self) -> bool:
        return await self._inner.healthcheck()


class _ExhaustedSandbox(Sandbox):
    """A sandbox that answers with the shape a fired clock leaves.

    The budget check pins the *runner's* rule — an exhausted containment
    budget folds to a failed run by rule, never a crash — and the shape
    it must fold is the one every real backend's watchdog returns
    (``timed_out`` with no exit code). The backends' own clocks are
    pinned in ``tests/sandbox``; this double is how the standard says so
    without depending on a child slow enough to exceed a real one.
    """

    @property
    def name(self) -> str:
        return "scripted-exhausted"

    @property
    def isolation_level(self) -> IsolationLevel:
        return IsolationLevel.NONE

    async def run(self, spec: SandboxSpec) -> SandboxResult:
        return SandboxResult(
            exit_code=None,
            stdout="",
            stderr="",
            timed_out=True,
            killed_reason="timeout",
            backend=self.name,
        )

    async def open(self, spec: SandboxSpec) -> SandboxSession:
        raise SandboxError(
            "the scripted exhausted double cannot open sessions",
            backend=self.name,
        )

    async def healthcheck(self) -> bool:
        return True


async def installed_conformance(
    ledger: LedgerStore,
    tmp_path: Path,
    *,
    staged: Path,
    declaration: PipelineDeclaration,
    sandbox: Sandbox,
) -> ConformanceReport:
    """The six checks over an *installed* pipeline, in its own vocabulary.

    What the installed vocabulary can promise for real — and what it
    honestly cannot:

    * the host's own call sequence (the runner journals one entrypoint
      call per run; two runs of the same goal must fold identically);
    * the replay discipline (a re-drive that stops matching its journal
      halts loudly with a divergence row; a call whose outcome a crash
      left unclassifiable is never auto-repeated — the child is a
      mutating call by definition, so D-6 applies in full);
    * the cancel (a cancelled run closes its call and propagates, with
      nothing half-closed at the call level) and the containment clock
      (an exhausted budget fails the run by rule);
    * the no-secrets rule (the goal rides the args file and the
      fingerprint, never a payload text).

    What it cannot promise is the *child's* behaviour, and the rows say
    so rather than faking it: the child's own determinism beyond the
    call sequence is the package's claim, verified by its own tests, and
    a package declaring ``resume = "none"`` has no replay to diverge.
    """
    from paa.pipelines.base import (
        OutcomeUnknownError,
        PipelineDivergedError,
        PipelineStatus,
    )
    from paa.pipelines.journal import fingerprint
    from paa.pipelines.sandboxed import (
        ENTRYPOINT_METHOD,
        SandboxedPipelineRunner,
    )

    async def drive(
        run_id: uuid.UUID,
        goal: str,
        *,
        runner_sandbox: Sandbox | None = None,
        timeout_s: float | None = None,
    ) -> Any:
        journal = CallJournal(ledger, run_id)
        await journal.run_started(
            declaration.name,
            declaration.version,
            run_kind="installed",
            resume=declaration.resume,
            evidence_tier=declaration.evidence_tier or None,
        )
        runner = SandboxedPipelineRunner(
            sandbox=runner_sandbox if runner_sandbox is not None else sandbox,
            journal=journal,
            staged=staged,
            declaration=declaration,
            timeout_s=timeout_s,
        )
        result = await runner.run(goal)
        await journal.run_completed(result.status.value, metrics=dict(result.metrics))
        return result

    # -- 1. double-run call-sequence equality -----------------------------
    probe = "conformance: the sequence probe"
    run_a, run_b = uuid.uuid4(), uuid.uuid4()
    result_a = await drive(run_a, probe)
    result_b = await drive(run_b, probe)
    fold_a = await read_call_journal(ledger, run_a)
    fold_b = await read_call_journal(ledger, run_b)

    def shape(records: list[Any]) -> list[tuple[Any, ...]]:
        return [(r.seq, r.method, r.fingerprint, r.outcome) for r in records]

    if (
        shape(fold_a) == shape(fold_b)
        and len(fold_a) == 1
        and result_a.status is PipelineStatus.COMPLETED
        and result_b.status is PipelineStatus.COMPLETED
    ):
        double_run = ConformanceCheck.passed(
            CHECK_NAMES[0],
            "two runs of the same goal folded to the same 1-call sequence "
            "and completed — the runner's sequence is the promise, not the "
            "package's inner behaviour",
        )
    else:
        double_run = ConformanceCheck.failed(
            CHECK_NAMES[0],
            f"sequences differ or a probe failed: {shape(fold_a)} vs "
            f"{shape(fold_b)} ({result_a.status.value}/{result_b.status.value})",
        )

    # -- 2. divergence halt -----------------------------------------------
    if declaration.resume != "journal":
        divergence = ConformanceCheck.na(
            CHECK_NAMES[1],
            "the declaration says resume='none' (spec §6): the host refuses "
            "to mark such a run recoverable, so there is no replay to "
            "diverge — the halt-on-divergence property itself is pinned on "
            "coder.tool-loop (tests/pipelines/test_replay.py)",
        )
    else:
        div_id = uuid.uuid4()
        journal = CallJournal(ledger, div_id)
        await journal.run_started(
            declaration.name,
            declaration.version,
            run_kind="installed",
            resume=declaration.resume,
            evidence_tier=declaration.evidence_tier or None,
        )
        runner = SandboxedPipelineRunner(
            sandbox=sandbox,
            journal=journal,
            staged=staged,
            declaration=declaration,
        )
        await runner.run(probe)
        view = await ReplayView.load(ledger, div_id)
        halted = False
        try:
            await runner.run("conformance: a DIFFERENT goal", replay=view)
        except PipelineDivergedError:
            halted = True
        folded = await read_call_journal(ledger, div_id)
        events = await ledger.read_correlation(div_id)
        diverged_rows = [
            e for e in events if e.event_type is EventType.PIPELINE_RUN_DIVERGED
        ]
        if halted and len(folded) == 1 and len(diverged_rows) == 1:
            divergence = ConformanceCheck.passed(
                CHECK_NAMES[1],
                "a re-drive with a changed goal halted at seq 1 with one "
                "RUN_DIVERGED row and no second call — the host never "
                "resyncs",
            )
        else:
            divergence = ConformanceCheck.failed(
                CHECK_NAMES[1],
                f"halted={halted} calls={len(folded)} "
                f"divergence_rows={len(diverged_rows)}",
            )

    # -- 3. unknown outcome never auto-repeats ----------------------------
    # One PROPOSED row with no outcome — the shape a crash between the
    # proposal and the child's answer leaves — and the re-drive must
    # refuse to execute it again: the entrypoint is a mutating call by
    # definition, so D-6 applies in full.
    unknown_id = uuid.uuid4()
    unknown_journal = CallJournal(ledger, unknown_id)
    await unknown_journal.call_proposed(
        1, ENTRYPOINT_METHOD, {"goal": probe, "params": {}}
    )
    unknown_view = await ReplayView.load(ledger, unknown_id)
    unknown_runner = SandboxedPipelineRunner(
        sandbox=sandbox,
        journal=unknown_journal,
        staged=staged,
        declaration=declaration,
    )
    refused = False
    try:
        await unknown_runner.run(probe, replay=unknown_view)
    except OutcomeUnknownError:
        refused = True
    unknown_rows = await read_call_journal(ledger, unknown_id)
    if refused and len(unknown_rows) == 1:
        unknown = ConformanceCheck.passed(
            CHECK_NAMES[2],
            "the interrupted call was reported as the unknown outcome it is "
            "and never re-executed — exactly one PROPOSED row stands",
        )
    else:
        unknown = ConformanceCheck.failed(
            CHECK_NAMES[2],
            f"refused={refused} rows={len(unknown_rows)}",
        )

    # -- 4. cancel within grace -------------------------------------------
    cancel_id = uuid.uuid4()
    held = _HeldSandbox(sandbox)
    cancel_task = asyncio.create_task(drive(cancel_id, probe, runner_sandbox=held))
    await held.entered.wait()
    cancel_task.cancel()
    cancelled = False
    try:
        await cancel_task
    except asyncio.CancelledError:
        cancelled = True
    cancel_rows = await read_call_journal(ledger, cancel_id)
    coherently_closed = (
        len(cancel_rows) == 1
        and cancel_rows[0].outcome is CallOutcome.FAILED
        and cancel_rows[0].error == "cancelled"
    )
    if cancelled and coherently_closed:
        cancel = ConformanceCheck.passed(
            CHECK_NAMES[3],
            "the cancel landed inside the entrypoint call and the call "
            "closed FAILED('cancelled') before the CancelledError "
            "propagated — nothing half-closed at the call level",
        )
    else:
        cancel = ConformanceCheck.failed(
            CHECK_NAMES[3],
            f"cancelled={cancelled} rows={[(r.outcome, r.error) for r in cancel_rows]}",
        )

    # -- 5. budget exhaustion ---------------------------------------------
    budget_id = uuid.uuid4()
    budget_result = await drive(
        budget_id,
        probe,
        runner_sandbox=_ExhaustedSandbox(),
        timeout_s=0.5,
    )
    budget_rows = await read_call_journal(ledger, budget_id)
    if (
        budget_result.status is PipelineStatus.FAILED
        and budget_result.metrics.get("state") == "timeout"
        and len(budget_rows) == 1
        and budget_rows[0].outcome is CallOutcome.COMPLETED
    ):
        budget = ConformanceCheck.passed(
            CHECK_NAMES[4],
            "an exhausted containment clock folded to a FAILED run by rule — "
            "a classified outcome row, not a crash (the sandbox double plays "
            "the watchdog's answer; the backends' own clocks are pinned in "
            "tests/sandbox)",
        )
    else:
        budget = ConformanceCheck.failed(
            CHECK_NAMES[4],
            f"status={budget_result.status.value} "
            f"state={budget_result.metrics.get('state')} rows={len(budget_rows)}",
        )

    # -- 6. no secrets in journal -----------------------------------------
    # The goal is the one input that can carry a secret (a prompt is the
    # coder vocabulary's own example), so it rides this probe. The scan
    # covers every payload the host renders, with one stated exclusion:
    # the child's own answer. ``CALL_COMPLETED``'s result_json and
    # ``RUN_COMPLETED``'s metrics are the child's words carried for
    # replay (ADR-0050 serves the recorded value verbatim — the same
    # property the coder vocabulary gives the model's own think text),
    # and a package that echoes its input is a package the capability
    # review reads, not a leak the host made. The host's promise is the
    # input side, and the positive control attests exactly that: the
    # fingerprint — the SHA-256 the journal holds over the very params
    # that carried the marker.
    secret = _marker()
    secret_id = uuid.uuid4()
    await drive(secret_id, f"conformance probe with {secret}")
    leaked: list[str] = []
    for event in await ledger.read_correlation(secret_id):
        payload = dict(event.payload)
        if event.event_type is EventType.PIPELINE_CALL_COMPLETED:
            payload.pop("result_json", None)
        if event.event_type is EventType.PIPELINE_RUN_COMPLETED:
            payload.pop("metrics", None)
        if secret in json.dumps(payload, sort_keys=True):
            leaked.append(event.event_type.value)
    secret_rows = await read_call_journal(ledger, secret_id)
    expected_fp = fingerprint(
        ENTRYPOINT_METHOD, {"goal": f"conformance probe with {secret}", "params": {}}
    )
    positive_control = (
        len(secret_rows) == 1 and secret_rows[0].fingerprint == expected_fp
    )
    if not leaked and positive_control:
        secrets = ConformanceCheck.passed(
            CHECK_NAMES[5],
            "the goal's marker rode the call (attested by the param "
            "fingerprint) and never appeared in a payload the host wrote; "
            "the child's own answer rides the recorded result ADR-0050 "
            "serves back — the child's words, not the host's leak",
        )
    else:
        secrets = ConformanceCheck.failed(
            CHECK_NAMES[5],
            f"leaked={leaked} positive_control={positive_control}",
        )

    return ConformanceReport(
        pipeline=declaration.name,
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
    """Run the standard over every runnable in-process pipeline.

    A builtin added to the registry without a runner is not silently skipped:
    it gets a RED report naming the gap, so the gate fails until the six
    checks are expressed in that pipeline's own vocabulary.
    """
    from paa.pipelines.registry import PipelineRegistry

    reports = [
        await builtin_conformance(ledger),
        await chat_conformance(tmp_path),
        await coder_conformance(ledger, tmp_path),
        await observe_conformance(ledger, tmp_path),
        await manager_conformance(ledger),
        await improve_conformance(ledger, tmp_path),
        await mcp_host_conformance(tmp_path),
    ]
    covered = {report.pipeline for report in reports}
    missing = [
        row.name
        for row in PipelineRegistry(local_dir=None).list()
        if row.runnable and row.name not in covered
    ]
    for name in missing:
        reports.append(
            ConformanceReport(
                pipeline=name,
                checks=_checks(
                    **{
                        check: ConformanceCheck.failed(
                            check, "no conformance runner registered for this builtin"
                        )
                        for check in CHECK_NAMES
                    }
                ),
            )
        )
    return reports
