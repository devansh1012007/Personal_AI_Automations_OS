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

from paa.core.types import EventType
from paa.ledger.events import LedgerEvent
from paa.ledger.store import LedgerStore
from paa.pipelines.journal import (
    CallJournal,
    CallOutcome,
    read_call_journal,
)
from paa.pipelines.replay import ReplayView

__all__ = [
    "ConformanceCheck",
    "ConformanceReport",
    "builtin_conformance",
    "chat_conformance",
    "coder_conformance",
    "every_builtin_conformance",
    "manager_conformance",
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
