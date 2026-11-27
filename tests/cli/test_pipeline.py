"""``paa pipeline list/inspect/run/trace`` through Typer's CliRunner (C6.5).

The same throwaway-home discipline ``tests/cli/test_cli.py`` applies to
``submit``: an isolated ``PAA_HOME``, the offline echo provider, and the
AUTO policy — a real runtime over a real ledger, driven offline, so the
commands are proven as commands (exit codes, tables, refusals), not as
functions.
"""

from __future__ import annotations

import asyncio
import importlib.util
import uuid
from pathlib import Path
from typing import Any

import pytest
import typer
from typer.testing import CliRunner

from paa.cli.main import app

runner = CliRunner()

HAS_UVICORN = importlib.util.find_spec("uvicorn") is not None


@pytest.fixture(autouse=True)
def _isolated_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Every pipeline-CLI test gets a throwaway home + offline echo."""
    home = tmp_path / "home"
    home.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("PAA_HOME", str(home))
    # Rich sizes its tables to the terminal; the runner's narrow default
    # truncates the builtin names mid-word. A wide explicit width keeps
    # the table assertions about content, not wrapping.
    monkeypatch.setenv("COLUMNS", "220")
    monkeypatch.setenv("PAA_MODELS__LOCAL_PROVIDER", "echo")
    monkeypatch.setenv("PAA_MODELS__ESCALATION_PROVIDER", "none")
    monkeypatch.setenv("PAA_POLICY__MODE", "AUTO")
    monkeypatch.setenv("PAA_SANDBOX__BACKEND", "subprocess")
    from paa.config import reset_settings_cache

    reset_settings_cache()


_LOCAL_MANIFEST = """
[pipeline]
name = "tdd-loop"
version = "0.1.0"
protocol = "paa-pipeline/1"
description = "Write a failing test, make it pass."
requires_runtime = ">=4.1,<5"

[run]
command = ["python", "-m", "tdd_loop"]
resume = "journal"

[limits]
max_tokens = 200000
max_wall_s = 1800
max_calls = 400
max_sub_runs = 2
"""


class TestListAndInspect:
    def test_list_names_the_four_builtins(self) -> None:
        result = runner.invoke(app, ["pipeline", "list"])
        assert result.exit_code == 0
        for name in (
            "builtin.plan-execute-critique",
            "builtin.chat",
            "coder.tool-loop",
            "builtin.observe",
        ):
            assert name in result.stdout
        assert "builtin" in result.stdout

    def test_inspect_shows_the_observes_two_special_facts(self) -> None:
        """The T1 rows are on the inspect surface, not buried in code."""
        result = runner.invoke(app, ["pipeline", "inspect", "builtin.observe"])
        assert result.exit_code == 0
        assert "builtin.observe" in result.stdout
        assert "T1" in result.stdout
        assert "none" in result.stdout

    def test_list_names_off_contract_local_manifests(self) -> None:
        from paa.config import get_settings

        broken = get_settings().home / "pipelines" / "broken"
        broken.mkdir(parents=True)
        (broken / "pipeline.toml").write_text(
            "[banana]\nx = 1\n", encoding="utf-8"
        )
        result = runner.invoke(app, ["pipeline", "list"])
        assert result.exit_code == 0
        assert "broken" in result.stdout
        assert "off-contract" in result.stdout

    def test_inspect_shows_the_declaration(self) -> None:
        result = runner.invoke(app, ["pipeline", "inspect", "builtin.chat"])
        assert result.exit_code == 0
        assert "builtin.chat" in result.stdout
        assert "context_provider" in result.stdout
        assert "recall_k" in result.stdout

    def test_inspect_unknown_refuses(self) -> None:
        result = runner.invoke(app, ["pipeline", "inspect", "nope"])
        assert result.exit_code == 1
        assert "no registered pipeline" in result.stdout


class TestRun:
    def test_a_local_manifest_refuses_with_the_p3_reason(self) -> None:
        from paa.config import get_settings

        local = get_settings().home / "pipelines" / "tdd-loop"
        local.mkdir(parents=True)
        (local / "pipeline.toml").write_text(_LOCAL_MANIFEST, encoding="utf-8")
        result = runner.invoke(
            app, ["pipeline", "run", "tdd-loop", "--goal", "x"]
        )
        assert result.exit_code == 1
        assert "P3" in result.stdout

    def test_out_of_scope_refuses(self) -> None:
        result = runner.invoke(
            app,
            [
                "pipeline", "run", "coder.tool-loop", "--goal", "x",
                "--allow", "builtin.chat",
            ],
        )
        assert result.exit_code == 1
        assert "outside this mandate" in result.stdout

    def test_unknown_pipeline_refuses(self) -> None:
        result = runner.invoke(app, ["pipeline", "run", "nope", "--goal", "x"])
        assert result.exit_code == 1
        assert "no registered pipeline" in result.stdout

    def test_a_widening_set_refuses(self) -> None:
        result = runner.invoke(
            app,
            [
                "pipeline", "run", "builtin.chat", "--goal", "hi",
                "--set", "recall_k=99999",
            ],
        )
        assert result.exit_code == 1
        assert "above the declared maximum" in result.stdout

    def test_chat_runs_offline_and_is_traced(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        run = runner.invoke(
            app, ["pipeline", "run", "builtin.chat", "--goal", "hello there"]
        )
        assert run.exit_code == 0
        assert "hello there" in run.stdout  # the echo reply carries the message
        # The session id is printed for the trace.
        session = None
        for line in run.stdout.splitlines():
            if line.startswith("session:"):
                session = line.split("session:")[1].strip()
        assert session

        trace = runner.invoke(app, ["pipeline", "trace", session])
        assert trace.exit_code == 0
        assert "PIPELINE_RUN_STARTED" in trace.stdout
        assert "PIPELINE_RUN_COMPLETED" in trace.stdout
        assert "CHAT_TURN" in trace.stdout

    def test_trace_of_an_unknown_lineage_refuses(self) -> None:
        result = runner.invoke(app, ["pipeline", "trace", str(uuid.uuid4())])
        assert result.exit_code == 1
        assert "no lineage" in result.stdout

    def test_coder_tool_loop_runs_offline(self) -> None:
        result = runner.invoke(
            app,
            [
                "pipeline", "run", "coder.tool-loop",
                "--goal", "say done and stop",
                "--mode", "bypass",
                "--set", "max_steps=2",
            ],
        )
        assert result.exit_code == 0
        assert "correlation:" in result.stdout
        assert "session:" in result.stdout

    def test_task_pipeline_runs_through_the_runtime(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # The same offline-plan patch submit's test uses: the echo model
        # proposes a plan when asked for one and echoes otherwise.
        from tests.cli.test_cli import _patch_offline_plan

        _patch_offline_plan(monkeypatch)
        result = runner.invoke(
            app,
            [
                "pipeline", "run", "builtin.plan-execute-critique",
                "--goal", "write a greeting",
            ],
        )
        assert result.exit_code == 0
        assert "correlation:" in result.stdout
        assert "COMMITTED" in result.stdout

    def test_failed_chat_turn_writes_run_failed_not_completed(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A failed turn is recorded as a failure, never a completion."""

        class Exploding:
            async def reply(self, prompt: str, *, on_delta: Any = None) -> Any:
                raise RuntimeError("provider socket died")

        monkeypatch.setattr(
            "paa.chat.service.build_chat_model", lambda settings: Exploding()
        )
        run = runner.invoke(app, ["pipeline", "run", "builtin.chat", "--goal", "hello"])
        assert run.exit_code == 1
        assert "provider socket died" in run.stdout
        session = next(
            line.split("session:")[1].strip()
            for line in run.stdout.splitlines()
            if line.startswith("session:")
        )

        async def kinds() -> list[str]:
            from paa.config import get_settings
            from paa.ledger.store import LedgerStore
            from paa.storage.relational.database import Database

            db = Database(get_settings().storage.sqlite_path)
            await db.connect()
            try:
                events = await LedgerStore(db).read_correlation(uuid.UUID(session))
            finally:
                await db.close()
            return [event.event_type.value for event in events]

        got = asyncio.run(kinds())
        assert "PIPELINE_RUN_STARTED" in got
        assert "PIPELINE_RUN_FAILED" in got
        assert "PIPELINE_RUN_COMPLETED" not in got
        assert "CHAT_TURN_FAILED" in got

    def test_overlay_and_set_reach_the_chat_service(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """Validated knobs reach the service; they are not announced then dropped."""
        from paa.chat.service import ChatService

        seen: dict[str, Any] = {}
        real_send = ChatService.send

        async def spy(self: ChatService, sid: Any, text: str, **kw: Any) -> Any:
            seen["recall_k"] = self.settings.recall_k
            seen["token_budget"] = self.settings.token_budget
            return await real_send(self, sid, text, **kw)

        monkeypatch.setattr(ChatService, "send", spy)
        overlay = tmp_path / "overlay.toml"
        overlay.write_text("[limits]\nmax_tokens = 64\n", encoding="utf-8")
        run = runner.invoke(
            app,
            [
                "pipeline", "run", "builtin.chat", "--goal", "hi",
                "--set", "recall_k=2", "--set", "tokens_budget=500",
                "--overlay", str(overlay),
            ],
        )
        assert run.exit_code == 0
        # min(500, 64): an overlay may only tighten what --set asked for.
        assert seen == {"recall_k": 2, "token_budget": 64}

    def test_context_provider_slot_is_used(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """A resolved slot ref is the provider that compiles the packet."""
        from paa.pipelines import slots
        from paa.pipelines.slots import builtin_context_provider

        ceilings: list[int] = []

        def tracking(**kwargs: Any) -> Any:
            ceilings.append(int(kwargs["token_ceiling"]))
            return builtin_context_provider(**kwargs)

        ref = f"test:track-{uuid.uuid4().hex[:8]}"
        monkeypatch.setitem(slots._PROVIDERS, ref, tracking)
        monkeypatch.setitem(
            slots._REF_INTERFACES, ref, slots.CONTEXT_PROVIDER_INTERFACE
        )
        overlay = tmp_path / "slot.toml"
        overlay.write_text(
            f'[slots]\ncontext_provider = "{ref}"\n[limits]\nmax_tokens = 77\n',
            encoding="utf-8",
        )
        run = runner.invoke(
            app,
            [
                "pipeline", "run", "builtin.chat", "--goal", "hi",
                "--overlay", str(overlay),
            ],
        )
        assert run.exit_code == 0
        assert ceilings == [77]

    def test_unhonourable_chat_overlay_refuses_loudly(self, tmp_path: Path) -> None:
        """An overlay the runner cannot apply refuses before any runtime work."""
        overlay = tmp_path / "bad.toml"
        overlay.write_text("[capabilities]\nrecall = false\n", encoding="utf-8")
        run = runner.invoke(
            app,
            [
                "pipeline", "run", "builtin.chat", "--goal", "hi",
                "--overlay", str(overlay),
            ],
        )
        assert run.exit_code == 1
        assert "cannot honour" in run.stdout
        assert "capabilities" in run.stdout
        assert "session:" not in run.stdout

    def test_trace_of_a_malformed_id_refuses(self) -> None:
        result = runner.invoke(app, ["pipeline", "trace", "not-a-uuid"])
        assert result.exit_code == 1
        assert "not a UUID" in result.stdout


class TestMcpHostRun:
    """The externally-driven MCP session through the registry's door (W9-C9.4).

    The door serves on the process's stdin/stdout — the same pipes
    ``paa mcp serve`` serves — so the reader and sink are patched to the
    in-process loopback shape ``tests/mcp_server`` proved, and the run is
    proven as a command: exit codes, the trace, the overlay refusal.
    """

    def test_list_and_inspect_name_the_adapter(self) -> None:
        result = runner.invoke(app, ["pipeline", "list"])
        assert result.exit_code == 0
        assert "builtin.mcp-host" in result.stdout
        inspect = runner.invoke(app, ["pipeline", "inspect", "builtin.mcp-host"])
        assert inspect.exit_code == 0
        assert "builtin.mcp-host" in inspect.stdout
        assert "T2" in inspect.stdout
        assert "none" in inspect.stdout

    def test_the_session_runs_over_the_pipeline_door_and_is_traced(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import asyncio
        import json

        from paa.cli import mcp as mcp_cli
        from paa.mcp.protocol import PROTOCOL_VERSION, encode_frame

        frames: list[bytes] = []

        class _Sink:
            def write(self, data: bytes) -> None:
                frames.append(data)

            async def drain(self) -> None:
                return None

        async def _fed_reader() -> Any:
            reader = asyncio.StreamReader()
            reader.feed_data(
                encode_frame(
                    {
                        "jsonrpc": "2.0",
                        "id": 0,
                        "method": "initialize",
                        "params": {
                            "protocolVersion": PROTOCOL_VERSION,
                            "capabilities": {},
                            "clientInfo": {"name": "cli-door-test", "version": "0"},
                        },
                    }
                )
            )
            reader.feed_eof()
            return reader

        monkeypatch.setattr(mcp_cli, "_stdin_reader", _fed_reader)
        monkeypatch.setattr(mcp_cli, "_StdoutSink", _Sink)
        run = runner.invoke(
            app, ["pipeline", "run", "builtin.mcp-host", "--goal", "cli-door-test"]
        )
        assert run.exit_code == 0
        assert "completed" in run.stdout
        assert "correlation:" in run.stdout

        # The wire went to the sink: one handshake reply, nothing on stdout.
        assert len(frames) == 1
        reply = json.loads(frames[0].decode("utf-8"))
        assert reply["result"]["protocolVersion"] == PROTOCOL_VERSION

        correlation = next(
            line.split("correlation:")[1].strip()
            for line in run.stdout.splitlines()
            if line.startswith("correlation:")
        )
        trace = runner.invoke(app, ["pipeline", "trace", correlation])
        assert trace.exit_code == 0
        assert "PIPELINE_RUN_STARTED" in trace.stdout
        assert "mcp.session" in trace.stdout
        assert "PIPELINE_CALL_COMPLETED" in trace.stdout
        assert "PIPELINE_RUN_COMPLETED" in trace.stdout

    def test_any_overlay_refuses_as_unhonourable(self, tmp_path: Path) -> None:
        """The session is externally paced: nothing an overlay tightens is enforced.

        The declaration's limits are unbounded because the facade cannot
        enforce a wall clock or a call count over a foreign client, so a
        validated-but-unhonourable overlay is refused loudly rather than
        announced and dropped.
        """
        overlay = tmp_path / "tighten.toml"
        overlay.write_text("[limits]\nmax_calls = 10\n", encoding="utf-8")
        run = runner.invoke(
            app,
            [
                "pipeline", "run", "builtin.mcp-host", "--goal", "x",
                "--overlay", str(overlay),
            ],
        )
        assert run.exit_code == 1
        assert "cannot honour" in run.stdout
        assert "correlation:" not in run.stdout


class TestSetCoercion:
    def test_declared_bool_and_str_params_are_coerced(self) -> None:
        import dataclasses

        from paa.cli.pipeline import _parse_sets, _validate_sets
        from paa.pipelines.slots import BUILTIN_DECLARATIONS, ParamSpec

        declared = dataclasses.replace(
            BUILTIN_DECLARATIONS["builtin.chat"],
            params={
                "flag": ParamSpec("bool", False),
                "label": ParamSpec("str", ""),
            },
        )
        assert _validate_sets(_parse_sets(["flag=true", "label=x"]), declared) == {
            "flag": True,
            "label": "x",
        }
        with pytest.raises(typer.Exit):
            _validate_sets({"flag": "yes"}, declared)
        wrong_type: dict[str, Any] = {"label": 1}
        with pytest.raises(typer.Exit):
            _validate_sets(wrong_type, declared)


class TestManagerRun:
    def test_list_names_the_manager(self) -> None:
        result = runner.invoke(app, ["pipeline", "list"])
        assert result.exit_code == 0
        assert "builtin.manager" in result.stdout

    def test_manager_runs_two_delegations_end_to_end(
        self, tmp_path: Path
    ) -> None:
        """Two different workers, aggregated, journalled, traceable."""
        import json

        goal = json.dumps(
            {
                "delegations": [
                    {"pipeline": "builtin.chat", "goal": "greet"},
                    {"pipeline": "coder.tool-loop", "goal": "fix"},
                ]
            }
        )
        run = runner.invoke(
            app, ["pipeline", "run", "builtin.manager", "--goal", goal]
        )
        assert run.exit_code == 0
        assert "completed" in run.stdout
        # The aggregate line and both children are on the surface.
        assert "2 delegation(s)" in run.stdout
        assert "builtin.chat" in run.stdout
        assert "coder.tool-loop" in run.stdout
        # The lineage id is printed for the trace.
        correlation = next(
            line.split("correlation:")[1].strip()
            for line in run.stdout.splitlines()
            if line.startswith("correlation:")
        )
        trace = runner.invoke(app, ["pipeline", "trace", correlation])
        assert trace.exit_code == 0
        assert "PIPELINE_RUN_STARTED" in trace.stdout
        assert "sub_run" in trace.stdout
        assert "PIPELINE_RUN_COMPLETED" in trace.stdout

    def test_manager_refuses_malformed_delegations_cleanly(self) -> None:
        """A non-object goal or a malformed decomposition refuses loudly."""
        result = runner.invoke(
            app, ["pipeline", "run", "builtin.manager", "--goal", "not json"]
        )
        assert result.exit_code == 1
        assert "JSON" in result.stdout

        import json as _json

        bad = _json.dumps({"delegations": "not-a-list"})
        result = runner.invoke(
            app, ["pipeline", "run", "builtin.manager", "--goal", bad]
        )
        assert result.exit_code == 1
        # The malformed decomposition is a failed composition result —
        # a named outcome, never a crash.
        assert "failed" in result.stdout
        assert "delegations" in result.stdout

    def test_manager_honours_limit_overlays_and_refuses_the_rest(
        self, tmp_path: Path
    ) -> None:
        import json

        goal = json.dumps(
            {"delegations": [{"pipeline": "builtin.chat", "goal": "greet"}]}
        )
        good = tmp_path / "tighten.toml"
        good.write_text("[limits]\nmax_sub_runs = 1\n", encoding="utf-8")
        run = runner.invoke(
            app,
            [
                "pipeline", "run", "builtin.manager", "--goal", goal,
                "--overlay", str(good),
            ],
        )
        assert run.exit_code == 0
        assert "overlay applied" in run.stdout

        bad = tmp_path / "widen.toml"
        bad.write_text("[limits]\nmax_tokens = 999999999\n", encoding="utf-8")
        refused = runner.invoke(
            app,
            [
                "pipeline", "run", "builtin.manager", "--goal", goal,
                "--overlay", str(bad),
            ],
        )
        assert refused.exit_code == 1
        assert "above the declared" in refused.stdout
