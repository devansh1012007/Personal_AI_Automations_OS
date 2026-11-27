"""The RPC pipeline host — the ``paa-pipeline/1`` wire, pinned (W9-C9.3 / P3).

The tests drive a **real child** written **with the child SDK**
(:mod:`paa_pipeline` — the fixture script below imports it and nothing
from ``paa``, which is the SDK's own boundary) through a real
:class:`~paa.sandbox.subprocess_backend.SubprocessSandbox` over a real
ledger — the same shape ``tests/pipelines/test_sandboxed.py`` pinned for
the installed adapter, because the property under test is a wire contract
and a wire contract is only real between two processes.

What is pinned, per the spec's own anchors (§5):

* the happy path — ``pipeline.initialize`` (pinned protocol string,
  granted *names*, limits, resume facts) → ``pipeline.run`` → the child's
  capability calls journalled ``PIPELINE_CALL_*`` in arrival order with
  the transport's ``seq``/``token`` params stripped before the
  fingerprint (a fingerprint that included the token would differ
  run-to-run and ride the ledger, both forbidden) → the final result
  folded under the sandboxed adapter's own bounds and the driver's
  ``PIPELINE_RUN_*`` rows landing around it exactly as they do for every
  other runner;
* cancel within the grace period — ``pipeline.cancel`` lands, the SDK
  cancels the pipeline coroutine, ``pipeline.run`` answers ``-32005``
  and the run resolves :class:`PipelineCancelledError`;
* an ungranted capability is refused ``-32006`` **and the host never
  minted that token**: the child's ``PAA_PIPELINE_TOKENS`` env held no
  ``think`` entry, which is the out-of-band delivery rule making the
  absent-token and forged-token cases one refusal;
* the child's stderr is captured for the transcript and never journalled;
* determinism — two runs of the same child against the same facade shape
  make the identical call sequence (seq, method, fingerprint) and the
  identical recorded results, which is the population replay's divergence
  check feeds on;
* the version pins — the SDK re-states ``paa-pipeline/1`` and the token
  channel *because* it must not import them, so this test holds the two
  copies equal; and the host refuses a declared runtime range that
  excludes the facade version **before any child is launched**.
"""

from __future__ import annotations

import asyncio
import json
import sys
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any

import pytest

import paa_pipeline
from paa.core.types import EventType
from paa.ledger.store import LedgerStore
from paa.pipelines.base import (
    PIPELINE_PROTOCOL,
    Capability,
    CapabilitySet,
    PipelineArtifact,
    PipelineCancelledError,
    PipelineResult,
    PipelineStatus,
)
from paa.pipelines.journal import CallJournal, CallOutcome, fingerprint, read_call_journal
from paa.pipelines.rpc_host import TOKENS_ENV, RpcPipelineError, RpcPipelineHost
from paa.pipelines.slots import PipelineDeclaration
from paa.sandbox.base import SandboxSpec
from paa.sandbox.subprocess_backend import SubprocessSandbox, SubprocessSandboxSession

#: The SDK's own root, handed to the fixture child the way
#: ``tests/acp/serve_child.py`` bootstraps ``src/``: the sandbox's env
#: allowlist does not carry the parent's PYTHONPATH, so the child puts the
#: tree on its own path before importing the SDK.
_SDK_ROOT = Path(paa_pipeline.__file__).resolve().parents[1]

#: The fixture child's declared identity — the initialize handshake
#: refuses a child that answers anything else.
_CHILD_NAME = "fixture.rpc"
_CHILD_VERSION = "1.0.0"

#: One script, three modes: the run's ``params`` pick the behaviour, the
#: way a real pipeline's typed params would. ``happy`` is the full
#: conversation (heartbeat, both determinism aids, one token-carrying
#: capability call, the §3.2 ``use_skill`` exception, one memory write, a
#: stderr line, artifacts in the final result); ``sleep`` parks inside
#: ``pipeline.run`` so a cancel lands mid-flight; ``ungranted`` calls a
#: capability the run was not granted and reports the refusal.
_CHILD = '''\
"""The fixture pipeline child: the SDK's serve loop, nothing from paa."""
from __future__ import annotations

import asyncio
import json
import os
import sys

sys.path.insert(0, "__SRC__")

from paa_pipeline import PipelineCtx, RpcError, serve


async def pipeline(ctx: PipelineCtx):
    mode = ctx.params.get("mode", "happy")
    if mode == "happy":
        await ctx.progress("working")
        now = await ctx.now()
        rng = await ctx.random()
        thought = await ctx.think("what shall we do?", role="worker")
        skill = await ctx.use_skill("greet", {"who": "world"})
        await ctx.remember({"fact": "the fixture ran"})
        print("child stderr: happy path", file=sys.stderr)
        return {
            "status": "completed",
            "summary": "thought: " + str(thought["answer"]),
            "metrics": {
                "now": now,
                "random": rng,
                "skill": skill,
                "recorded_calls": ctx.recorded_calls,
            },
            "artifacts": [{"kind": "report", "sha256": "deadbeef"}],
        }
    if mode == "sleep":
        await ctx.progress("working")
        await asyncio.sleep(30)
        return {"status": "completed", "summary": "slept"}
    if mode == "ungranted":
        try:
            await ctx.think("no token for this")
        except RpcError as exc:
            tokens = json.loads(os.environ.get("__TOKENS_ENV__", "{}"))
            return {
                "status": "completed",
                "summary": "refused",
                "metrics": {
                    "refusal_code": exc.code,
                    "think_token_issued": "think" in tokens,
                },
            }
        return {"status": "failed", "summary": "the call was not refused"}
    return {"status": "failed", "summary": "unknown mode " + str(mode)}


async def main() -> None:
    await serve(pipeline, name="__NAME__", version="__VERSION__")


asyncio.run(main())
'''

#: The happy path's grant set: THINK, USE_SKILL and REMEMBER, so the run
#: exercises one token-carrying call per journaling rule (journalled,
#: the §3.2 exception, journalled) and nothing it was not granted.
_HAPPY_GRANTS = (Capability.THINK, Capability.USE_SKILL, Capability.REMEMBER)


def _child_script(root: Path) -> Path:
    """Write the fixture child and return its path (one per test run)."""
    script = root / f"rpc-child-{uuid.uuid4().hex[:6]}.py"
    script.write_text(
        _CHILD.replace("__SRC__", str(_SDK_ROOT))
        .replace("__TOKENS_ENV__", TOKENS_ENV)
        .replace("__NAME__", _CHILD_NAME)
        .replace("__VERSION__", _CHILD_VERSION),
        encoding="utf-8",
    )
    return script


def _declaration(requires_runtime: str = ">=4.1,<5") -> PipelineDeclaration:
    """The fixture child's declaration, as a registry row would carry it."""
    return PipelineDeclaration(
        name=_CHILD_NAME,
        version=_CHILD_VERSION,
        requires_runtime=requires_runtime,
        description="the RPC wire fixture child",
        trust="installed",
        resume="journal",
        evidence_tier="unrated",
    )


class _DeterministicFacade:
    """The wire-shaped facade double: fixed answers, heartbeats recorded.

    Every answer is a constant (the clock is a fixed datetime, the
    entropy a fixed float) because the facade is the run's source of both
    — a real facade reads the recorded journal on resume — and the
    determinism test's double-run equality is only meaningful against a
    facade that does not invent new answers between runs.
    """

    def __init__(self) -> None:
        self.progresses: list[str] = []

    async def think(
        self,
        prompt: str,
        *,
        schema: dict[str, Any] | None = None,
        role: str = "worker",
        budget: int | None = None,
    ) -> dict[str, Any]:
        return {"answer": "consider: " + prompt, "role": role}

    async def use_skill(self, name: str, args: dict[str, Any]) -> str:
        return "skill-ok"

    async def recall(self, query: str, *, k: int = 10) -> list[dict[str, Any]]:
        return [{"query": query, "k": k}]

    async def remember(self, fact: dict[str, Any]) -> None:
        return None

    async def submit_plan(self, steps: list[dict[str, Any]]) -> None:
        return None

    async def commit(self, *, steps_completed: int, tokens_consumed: int) -> None:
        return None

    async def sub_run(self, name: str, task_input: dict[str, Any]) -> PipelineResult:
        return PipelineResult(
            status=PipelineStatus.COMPLETED, summary="sub: " + name
        )

    async def now(self) -> datetime:
        return datetime(2026, 1, 1, 12, 0, 0)

    async def random(self) -> float:
        return 0.5

    async def progress(self, status: str) -> None:
        self.progresses.append(status)


class _CountingSandbox(SubprocessSandbox):
    """A subprocess sandbox that counts opens — the refusal-before-launch pin."""

    def __init__(self) -> None:
        super().__init__()
        self.opens = 0

    async def open(self, spec: SandboxSpec) -> SubprocessSandboxSession:
        self.opens += 1
        return await super().open(spec)


def _build(
    ledger: LedgerStore,
    script: Path,
    workspace: Path,
    capabilities: tuple[Capability, ...],
    *,
    declaration: PipelineDeclaration | None = None,
    sandbox: SubprocessSandbox | None = None,
) -> tuple[RpcPipelineHost, _DeterministicFacade]:
    """One wired host: real sandbox, real journal, deterministic facade."""
    run_id = uuid.uuid4()
    facade = _DeterministicFacade()
    host = RpcPipelineHost(
        sandbox=sandbox if sandbox is not None else SubprocessSandbox(),
        journal=CallJournal(ledger, run_id),
        facade=facade,
        grants=CapabilitySet(run_id, capabilities),
        declaration=declaration if declaration is not None else _declaration(),
        command=(sys.executable, str(script)),
        workspace=workspace,
    )
    return host, facade


class TestProtocolPins:
    """The two copies of the frozen vocabulary, held equal by test."""

    def test_the_sdk_restates_the_frozen_protocol_and_token_channel(self) -> None:
        """The SDK cannot import these; the test is what keeps them one."""
        assert paa_pipeline.PROTOCOL_VERSION == PIPELINE_PROTOCOL
        assert paa_pipeline.TOKENS_ENV == TOKENS_ENV

    async def test_a_range_excluding_the_facade_version_is_refused_before_launch(
        self, tmp_path: Path, ledger: LedgerStore
    ) -> None:
        """The spec's rule: refuse at run time, before any child is launched."""
        script = _child_script(tmp_path)
        sandbox = _CountingSandbox()
        host, _ = _build(
            ledger,
            script,
            tmp_path,
            (Capability.THINK,),
            declaration=_declaration(">=5,<6"),
            sandbox=sandbox,
        )
        with pytest.raises(RpcPipelineError, match="excludes this facade version"):
            await host.run("go")
        assert sandbox.opens == 0  # no child was ever spawned to be refused


class TestHappyPath:
    """initialize → run → calls journalled → final result folded and journalled."""

    async def test_the_conversation_journals_as_the_pipeline_vocabulary(
        self, tmp_path: Path, ledger: LedgerStore
    ) -> None:
        script = _child_script(tmp_path)
        host, facade = _build(ledger, script, tmp_path, _HAPPY_GRANTS)
        journal = host.journal
        run_id = journal.run_id
        await journal.run_started(_CHILD_NAME, _CHILD_VERSION, run_kind="rpc")
        result = await host.run("say hello")
        await journal.run_completed(result.status.value, metrics=dict(result.metrics))

        assert result.status is PipelineStatus.COMPLETED
        assert result.summary == "thought: consider: what shall we do?"
        assert result.metrics == {
            "now": "2026-01-01T12:00:00",
            "random": 0.5,
            "skill": "skill-ok",
            "recorded_calls": 0,
        }
        assert result.artifacts == [PipelineArtifact(kind="report", sha256="deadbeef")]
        assert host.child_name == _CHILD_NAME
        assert host.child_version == _CHILD_VERSION
        assert facade.progresses == ["working"]

        records = await read_call_journal(ledger, run_id)
        # use_skill is the spec §3.2 exception: served through the facade,
        # journalled by the facade implementation's TOOL_CALL_* vocabulary
        # rather than the transport's — so seq 4 exists on the wire and not
        # in these rows. progress never appears: it carries no seq.
        assert [(r.seq, r.method) for r in records] == [
            (1, "now"),
            (2, "random"),
            (3, "think"),
            (5, "remember"),
        ]
        assert all(r.outcome is CallOutcome.COMPLETED for r in records)
        think = records[2]
        # The fingerprint is over the call's own params — seq and token
        # were stripped before anything was journalled, so the digest a
        # replay compares against never depended on the run's secrets.
        assert think.fingerprint == fingerprint(
            "think", {"prompt": "what shall we do?", "role": "worker"}
        )
        assert think.result == {
            "answer": "consider: what shall we do?",
            "role": "worker",
        }

        events = await ledger.read_correlation(run_id)
        kinds = [event.event_type for event in events]
        assert EventType.PIPELINE_RUN_STARTED in kinds
        assert EventType.PIPELINE_CALL_PROPOSED in kinds
        assert EventType.PIPELINE_CALL_COMPLETED in kinds
        assert EventType.PIPELINE_RUN_COMPLETED in kinds


class TestCancelWithinGrace:
    """``pipeline.cancel`` → the SDK stops itself → the run resolves cancelled."""

    async def test_cancel_lands_mid_run_and_resolves_the_run_cancelled(
        self, tmp_path: Path, ledger: LedgerStore
    ) -> None:
        script = _child_script(tmp_path)
        host, facade = _build(ledger, script, tmp_path, (Capability.THINK,))
        task = asyncio.create_task(host.run("slow goal", params={"mode": "sleep"}))
        # Aim at a run that is visibly mid-flight: the heartbeat is the
        # child's first act inside pipeline.run, so its arrival on the
        # facade proves the run request is live.
        for _ in range(400):  # a 20 s ceiling; the child needs well under 2 s
            if facade.progresses:
                break
            await asyncio.sleep(0.05)
        assert facade.progresses == ["working"]

        await host.cancel("test cancel")

        with pytest.raises(PipelineCancelledError):
            await task


class TestUngrantedCapability:
    """-32006, and the token was never issued to begin with."""

    async def test_an_ungranted_call_is_refused_and_never_minted_a_token(
        self, tmp_path: Path, ledger: LedgerStore
    ) -> None:
        script = _child_script(tmp_path)
        # recall granted, think not: the mint covers the granted set only,
        # so the child's env carries no think token to present or forge.
        host, _ = _build(ledger, script, tmp_path, (Capability.RECALL,))
        result = await host.run("no think for you", params={"mode": "ungranted"})

        assert result.status is PipelineStatus.COMPLETED  # the child folded the refusal
        assert result.metrics["refusal_code"] == -32006
        assert result.metrics["think_token_issued"] is False

        records = await read_call_journal(ledger, host.journal.run_id)
        think = [record for record in records if record.method == "think"]
        assert len(think) == 1
        assert think[0].outcome is CallOutcome.FAILED
        # The PROPOSED row landed before the grant check — a refused call
        # is still a call the record saw — and the failure names the rule.
        assert think[0].error == "capability is not granted to this run"


class TestStderrCapture:
    """The child's stderr reaches the transcript and never the ledger."""

    async def test_stderr_is_captured_and_never_journalled(
        self, tmp_path: Path, ledger: LedgerStore
    ) -> None:
        script = _child_script(tmp_path)
        host, _ = _build(ledger, script, tmp_path, _HAPPY_GRANTS)
        result = await host.run("say hello")
        assert result.status is PipelineStatus.COMPLETED

        captured = host.child_stderr
        assert captured is not None
        assert "child stderr: happy path" in captured

        events = await ledger.read_correlation(host.journal.run_id)
        for event in events:
            assert "child stderr" not in json.dumps(event.payload, default=str)


class TestDeterminism:
    """Two runs, one call sequence — the population replay compares."""

    async def _drive(
        self, ledger: LedgerStore, script: Path, workspace: Path
    ) -> tuple[RpcPipelineHost, PipelineResult]:
        host, _ = _build(ledger, script, workspace, _HAPPY_GRANTS)
        result = await host.run("same goal")
        assert result.status is PipelineStatus.COMPLETED
        return host, result

    async def test_two_runs_make_the_identical_call_sequence(
        self, tmp_path: Path, ledger: LedgerStore
    ) -> None:
        script = _child_script(tmp_path)
        first_host, first = await self._drive(ledger, script, tmp_path)
        second_host, second = await self._drive(ledger, script, tmp_path)

        assert second.summary == first.summary
        assert second.metrics == first.metrics
        assert second.artifacts == first.artifacts

        first_records = await read_call_journal(ledger, first_host.journal.run_id)
        second_records = await read_call_journal(ledger, second_host.journal.run_id)
        assert [(r.seq, r.method, r.fingerprint) for r in first_records] == [
            (r.seq, r.method, r.fingerprint) for r in second_records
        ]
        assert [r.result for r in first_records] == [r.result for r in second_records]
