"""The ``bash`` work tool via the sandbox (N1.3 / T-N1-3).

Unit tests use the canned dry-run backend to prove validation happens
before any process starts. Execution tests use the real subprocess
backend (``slow``) to prove timeout kills, the combined 64 KB cap, honest
isolation reporting, and that a host canary never reaches the child.
"""

from __future__ import annotations

import asyncio
import fnmatch
import json
import os
import shutil
import sys
import time
from pathlib import Path
from typing import Any

import pytest

from paa.coder import shell_tools as st
from paa.coder.permissions import (
    DEFAULT_MUTATING_TOOLS,
    PermissionMode,
    PermissionOutcome,
    ToolPermissionPolicy,
)
from paa.coder.workspace import Workspace, resolve_workspace
from paa.config import reset_settings_cache
from paa.sandbox._win32 import IS_WINDOWS
from paa.sandbox.dryrun_backend import DryRunSandbox
from paa.sandbox.subprocess_backend import SubprocessSandbox

CANARY_VAR = "PAA_TEST_HOST_SECRET"
CANARY_VALUE = "super-secret-canary-do-not-leak-9f2e"


def _ws(root: Path) -> Workspace:
    return Workspace(root=resolve_workspace(str(root)))


def _payload(raw: str) -> dict[str, Any]:
    data = json.loads(raw)
    assert isinstance(data, dict)
    return data


def _echo_cmd(text: str) -> str:
    return f'Write-Output "{text}"' if IS_WINDOWS else f"echo {text}"


def _exit_cmd(code: int) -> str:
    return f"exit {code}"


def _sleep_cmd(seconds: int) -> str:
    return (
        f"Start-Sleep -Seconds {seconds}" if IS_WINDOWS else f"sleep {seconds}"
    )


def _env_dump_cmd() -> str:
    return "Get-ChildItem Env:" if IS_WINDOWS else "env"


def _print_env_cmd(name: str) -> str:
    return f"Write-Output $env:{name}" if IS_WINDOWS else f'printf "%s" "${name}"'


def _python_cmd(script: str, *args: str) -> str:
    """A shell string invoking ``sys.executable`` on a workspace script."""
    parts = [f'"{sys.executable}"', f'"{script}"', *[f'"{a}"' for a in args]]
    joined = " ".join(parts)
    return f"& {joined}" if IS_WINDOWS else joined


def _require_shell() -> None:
    if shutil.which(st.shell_executable()) is None:
        pytest.skip(f"shell {st.shell_executable()} not on PATH")


class TestShellArgv:
    def test_windows_argv_is_pwsh_no_profile(self, monkeypatch: Any) -> None:
        monkeypatch.setattr(st, "IS_WINDOWS", True)
        assert st.shell_executable() == "pwsh"
        assert st.shell_argv("echo hi") == ("pwsh", "-NoProfile", "-Command", "echo hi")

    def test_posix_argv_is_bash(self, monkeypatch: Any) -> None:
        monkeypatch.setattr(st, "IS_WINDOWS", False)
        assert st.shell_executable() == "bash"
        assert st.shell_argv("echo hi") == ("bash", "-c", "echo hi")

    def test_argv_is_a_tuple_never_a_string(self) -> None:
        argv = st.shell_argv("echo hi; rm -rf /")
        assert isinstance(argv, tuple)
        assert argv[-1] == "echo hi; rm -rf /"
        assert all(isinstance(p, str) and p for p in argv)


class TestArgumentValidation:
    async def test_empty_command_refused_before_exec(self, tmp_path: Path) -> None:
        dry = DryRunSandbox(stdout="should-not-appear")
        tool = st.BashTool(_ws(tmp_path), sandbox=dry)
        payload = _payload(await tool.run({"command": "   "}))
        assert payload["ok"] is False
        assert "non-empty" in str(payload["error"])
        assert dry.invocations == []

    async def test_non_string_command_refused(self, tmp_path: Path) -> None:
        dry = DryRunSandbox()
        tool = st.BashTool(_ws(tmp_path), sandbox=dry)
        for bad in (None, 42, ["echo", "hi"], {"cmd": "x"}):
            payload = _payload(await tool.run({"command": bad}))
            assert payload["ok"] is False
            assert payload["exit_code"] is None
        assert dry.invocations == []

    async def test_command_with_nul_refused(self, tmp_path: Path) -> None:
        dry = DryRunSandbox()
        tool = st.BashTool(_ws(tmp_path), sandbox=dry)
        payload = _payload(await tool.run({"command": "echo a\x00b"}))
        assert payload["ok"] is False
        assert "NUL" in str(payload["error"])
        assert dry.invocations == []

    async def test_unknown_argument_refused(self, tmp_path: Path) -> None:
        dry = DryRunSandbox()
        tool = st.BashTool(_ws(tmp_path), sandbox=dry)
        payload = _payload(
            await tool.run({"command": "echo hi", "timeout": 5})
        )
        assert payload["ok"] is False
        assert "unknown argument" in str(payload["error"])
        assert dry.invocations == []

    async def test_timeout_defaults_to_120(self, tmp_path: Path) -> None:
        dry = DryRunSandbox(stdout="hi")
        tool = st.BashTool(_ws(tmp_path), sandbox=dry)
        payload = _payload(await tool.run({"command": "echo hi"}))
        assert payload["ok"] is True
        assert payload["timeout_seconds"] == 120.0
        assert dry.invocations[-1]["timeout_seconds"] == 120.0

    async def test_timeout_above_cap_refused(self, tmp_path: Path) -> None:
        dry = DryRunSandbox()
        tool = st.BashTool(_ws(tmp_path), sandbox=dry)
        for bad in (601, 600.1, 10_000):
            payload = _payload(
                await tool.run({"command": "echo hi", "timeout_seconds": bad})
            )
            assert payload["ok"] is False
            assert "exceed" in str(payload["error"])
        assert dry.invocations == []

    async def test_timeout_at_cap_runs(self, tmp_path: Path) -> None:
        dry = DryRunSandbox(stdout="hi")
        tool = st.BashTool(_ws(tmp_path), sandbox=dry)
        payload = _payload(
            await tool.run({"command": "echo hi", "timeout_seconds": 600})
        )
        assert payload["ok"] is True
        assert payload["timeout_seconds"] == 600.0

    async def test_timeout_nonpositive_and_types_refused(
        self, tmp_path: Path
    ) -> None:
        dry = DryRunSandbox()
        tool = st.BashTool(_ws(tmp_path), sandbox=dry)
        for bad in (0, -1, True, "nope", None):
            payload = _payload(
                await tool.run({"command": "echo hi", "timeout_seconds": bad})
            )
            assert payload["ok"] is False, bad
        assert dry.invocations == []

    async def test_timeout_numeric_string_accepted(self, tmp_path: Path) -> None:
        dry = DryRunSandbox(stdout="hi")
        tool = st.BashTool(_ws(tmp_path), sandbox=dry)
        payload = _payload(
            await tool.run({"command": "echo hi", "timeout_seconds": "30"})
        )
        assert payload["ok"] is True
        assert payload["timeout_seconds"] == 30.0

    async def test_env_bad_key_and_value_refused(self, tmp_path: Path) -> None:
        dry = DryRunSandbox()
        tool = st.BashTool(_ws(tmp_path), sandbox=dry)
        payload = _payload(
            await tool.run({"command": "echo hi", "env": {"BAD KEY": "v"}})
        )
        assert payload["ok"] is False
        payload = _payload(
            await tool.run({"command": "echo hi", "env": {"GOOD": 7}})
        )
        assert payload["ok"] is False
        assert dry.invocations == []

    async def test_allow_network_must_be_bool(self, tmp_path: Path) -> None:
        dry = DryRunSandbox()
        tool = st.BashTool(_ws(tmp_path), sandbox=dry)
        payload = _payload(
            await tool.run({"command": "echo hi", "allow_network": "yes"})
        )
        assert payload["ok"] is False
        assert dry.invocations == []

    async def test_schema_shape(self, tmp_path: Path) -> None:
        schema = st.BashTool(_ws(tmp_path), sandbox=DryRunSandbox()).schema()
        assert schema["required"] == ["command"]
        assert schema["additionalProperties"] is False
        assert set(schema["properties"]) >= {
            "command",
            "timeout_seconds",
            "env",
            "allow_network",
        }


class TestExecutionRealSandbox:
    @pytest.mark.slow
    async def test_echo_reports_exit_and_isolation(self, tmp_path: Path) -> None:
        _require_shell()
        tool = st.BashTool(_ws(tmp_path), sandbox=SubprocessSandbox())
        payload = _payload(await tool.run({"command": _echo_cmd("hello-shell")}))
        assert payload["ok"] is True
        assert payload["exit_code"] == 0
        assert payload["timed_out"] is False
        assert payload["isolation_level"] == "PROCESS"
        assert payload["backend"] == "subprocess"
        assert payload["network_severed"] is False
        assert "hello-shell" in str(payload["stdout"])

    @pytest.mark.slow
    async def test_nonzero_exit_is_surfaced_not_a_tool_fault(
        self, tmp_path: Path
    ) -> None:
        _require_shell()
        tool = st.BashTool(_ws(tmp_path), sandbox=SubprocessSandbox())
        payload = _payload(await tool.run({"command": _exit_cmd(3)}))
        assert payload["ok"] is True
        assert payload["exit_code"] == 3
        assert payload["timed_out"] is False

    @pytest.mark.slow
    async def test_cwd_is_the_workspace(self, tmp_path: Path) -> None:
        _require_shell()
        (tmp_path / "cwd.py").write_text(
            "import os\nprint(os.getcwd())\n", encoding="utf-8"
        )
        tool = st.BashTool(_ws(tmp_path), sandbox=SubprocessSandbox())
        payload = _payload(await tool.run({"command": _python_cmd("cwd.py")}))
        assert payload["ok"] is True
        assert payload["exit_code"] == 0
        assert Path(str(payload["stdout"]).strip()).resolve() == tmp_path.resolve()

    @pytest.mark.slow
    async def test_timeout_kills_and_reports(self, tmp_path: Path) -> None:
        _require_shell()
        tool = st.BashTool(_ws(tmp_path), sandbox=SubprocessSandbox())
        started = time.monotonic()
        payload = _payload(
            await tool.run(
                {"command": _sleep_cmd(60), "timeout_seconds": 2}
            )
        )
        elapsed = time.monotonic() - started
        assert payload["ok"] is True
        assert payload["timed_out"] is True
        assert elapsed < 30.0, f"timeout took {elapsed:.1f}s"

    @pytest.mark.slow
    async def test_timeout_kills_process_tree(self, tmp_path: Path) -> None:
        _require_shell()
        heartbeat = tmp_path / "heartbeat.bin"
        (tmp_path / "grandchild.py").write_text(
            "import sys, time\n"
            "path = sys.argv[1]\n"
            "while True:\n"
            "    with open(path, 'ab') as fh:\n"
            "        fh.write(b'x')\n"
            "    time.sleep(0.05)\n",
            encoding="utf-8",
        )
        (tmp_path / "parent.py").write_text(
            "import os, subprocess, sys, time\n"
            "script, beat = sys.argv[1], sys.argv[2]\n"
            "subprocess.Popen([sys.executable, script, beat])\n"
            "deadline = time.time() + 20\n"
            "while time.time() < deadline:\n"
            "    if os.path.exists(beat) and os.path.getsize(beat) > 0:\n"
            "        break\n"
            "    time.sleep(0.02)\n"
            "time.sleep(60)\n",
            encoding="utf-8",
        )
        tool = st.BashTool(_ws(tmp_path), sandbox=SubprocessSandbox())
        payload = _payload(
            await tool.run(
                {
                    "command": _python_cmd(
                        "parent.py", "grandchild.py", str(heartbeat)
                    ),
                    "timeout_seconds": 10,
                }
            )
        )
        assert payload["timed_out"] is True
        await asyncio.sleep(1.5)
        first = heartbeat.stat().st_size if heartbeat.exists() else 0
        await asyncio.sleep(1.5)
        second = heartbeat.stat().st_size if heartbeat.exists() else 0
        assert first > 0, "grandchild never started"
        assert second == first, (
            f"heartbeat grew {first} -> {second}: orphan survived the tree kill"
        )

    @pytest.mark.slow
    async def test_combined_cap_truncates_and_marks(self, tmp_path: Path) -> None:
        _require_shell()
        (tmp_path / "big.py").write_text(
            "import sys\nsys.stdout.write('A' * 200000)\n", encoding="utf-8"
        )
        (tmp_path / "small.py").write_text(
            "print('short-out')\n", encoding="utf-8"
        )
        tool = st.BashTool(_ws(tmp_path), sandbox=SubprocessSandbox())
        big = _payload(await tool.run({"command": _python_cmd("big.py")}))
        assert big["ok"] is True
        assert big["truncated"] is True
        assert big["truncated_output"] is True
        combined = len(str(big["stdout"])) + len(str(big["stderr"]))
        assert combined <= 64 * 1024, f"combined output {combined} exceeds cap"

        small = _payload(await tool.run({"command": _python_cmd("small.py")}))
        assert small["truncated"] is False
        assert "short-out" in str(small["stdout"])

    @pytest.mark.slow
    async def test_canary_absent_but_allowlisted_present(
        self, tmp_path: Path, monkeypatch: Any
    ) -> None:
        _require_shell()
        monkeypatch.setenv(CANARY_VAR, CANARY_VALUE)
        assert os.environ[CANARY_VAR] == CANARY_VALUE
        tool = st.BashTool(_ws(tmp_path), sandbox=SubprocessSandbox())

        dump_raw = await tool.run({"command": _env_dump_cmd()})
        assert CANARY_VALUE not in dump_raw
        assert CANARY_VAR not in str(_payload(dump_raw)["stdout"])

        allow_raw = await tool.run(
            {
                "command": _print_env_cmd("PAA_SHELL_ALLOW"),
                "env": {"PAA_SHELL_ALLOW": "allowed-value"},
            }
        )
        payload = _payload(allow_raw)
        assert payload["exit_code"] == 0
        assert "allowed-value" in str(payload["stdout"])
        assert CANARY_VALUE not in allow_raw

    async def test_preflight_is_honest_on_subprocess(self, tmp_path: Path) -> None:
        tool = st.BashTool(_ws(tmp_path), sandbox=SubprocessSandbox())
        facts = tool.preflight(allow_network=False)
        assert facts["backend"] == "subprocess"
        assert facts["isolation_level"] == "PROCESS"
        assert facts["network_severed"] is False
        assert facts["filesystem_confined"] is False
        assert facts["output_cap_bytes"] == 64 * 1024
        assert facts["shell"] == st.shell_executable()

    @pytest.mark.slow
    async def test_missing_shell_reports_start_failure(
        self, tmp_path: Path
    ) -> None:
        # The failure direction when the frozen shell binary is absent is
        # a refused result with isolation, not a raised exception.
        if shutil.which(st.shell_executable()) is not None:
            pytest.skip("shell present; start-failure path not applicable")
        tool = st.BashTool(_ws(tmp_path), sandbox=SubprocessSandbox())
        payload = _payload(await tool.run({"command": "echo hi"}))
        assert payload["ok"] is False
        assert payload["isolation_level"] == "PROCESS"
        assert payload["network_severed"] is False
        assert "failed to start" in str(payload["error"])


class TestJailProtect:
    async def test_jail_violation_refused_without_exec(
        self, tmp_path: Path
    ) -> None:
        dry = DryRunSandbox(stdout="should-not-appear")
        ws = Workspace(root=Path("//server/share"))
        tool = st.BashTool(ws, sandbox=dry)
        payload = _payload(await tool.run({"command": "echo hi"}))
        assert payload["ok"] is False
        assert "jail" in str(payload["error"])
        assert dry.invocations == []

    async def test_protected_paa_home_refused(self, tmp_path: Path) -> None:
        dry = DryRunSandbox(stdout="should-not-appear")
        ws = _ws(tmp_path)
        old = os.environ.get("PAA_HOME")
        os.environ["PAA_HOME"] = str(tmp_path)
        reset_settings_cache()
        try:
            tool = st.BashTool(ws, sandbox=dry)
            payload = _payload(await tool.run({"command": "echo hi"}))
            assert payload["ok"] is False
            assert payload["reason"] == "protected path: paa-home"
            assert dry.invocations == []
        finally:
            if old is None:
                os.environ.pop("PAA_HOME", None)
            else:
                os.environ["PAA_HOME"] = old
            reset_settings_cache()

    @pytest.mark.slow
    async def test_missing_workspace_refused(self, tmp_path: Path) -> None:
        missing = tmp_path / "does-not-exist"
        ws = Workspace(root=missing)
        tool = st.BashTool(ws, sandbox=SubprocessSandbox())
        payload = _payload(await tool.run({"command": _echo_cmd("hi")}))
        assert payload["ok"] is False
        assert "workspace does not exist" in str(payload["error"])

    async def test_require_severance_refused_on_subprocess(
        self, tmp_path: Path
    ) -> None:
        tool = st.BashTool(_ws(tmp_path), sandbox=SubprocessSandbox())
        # Refusal happens before any process starts, so the real backend
        # is safe here: no network attempt is made.
        payload = _payload(
            await tool.run(
                {
                    "command": "echo hi",
                    "allow_network": False,
                    "require_network_severance": True,
                }
            )
        )
        assert payload["ok"] is False
        assert "severance" in str(payload["error"]).lower()
        assert payload["network_severed"] is False


class TestPermissions:
    def test_bash_matches_mutating_globs(self) -> None:
        assert any(fnmatch.fnmatch("bash", pat) for pat in DEFAULT_MUTATING_TOOLS)

    def test_plan_denies_ask_prompts_bypass_allows(self) -> None:
        plan = ToolPermissionPolicy(mode=PermissionMode.PLAN)
        assert plan.evaluate("bash", {"command": "ls"}).outcome is (
            PermissionOutcome.DENY
        )
        ask = ToolPermissionPolicy(mode=PermissionMode.ASK)
        assert ask.evaluate("bash", {"command": "ls"}).outcome is (
            PermissionOutcome.ASK
        )
        accept = ToolPermissionPolicy(mode=PermissionMode.ACCEPT_EDITS)
        assert accept.evaluate("bash", {"command": "ls"}).outcome is (
            PermissionOutcome.ASK
        )
        bypass = ToolPermissionPolicy(mode=PermissionMode.BYPASS)
        assert bypass.evaluate("bash", {"command": "ls"}).allowed

    def test_ask_without_confirm_denies_at_the_loop(self, tmp_path: Path) -> None:
        # The tool itself needs no permission code: the loop prompts.
        # Assert the composition (policy + tool name) that N1.5 registers.
        policy = ToolPermissionPolicy(mode=PermissionMode.ASK)
        decision = policy.evaluate(st.BASH_TOOL_NAME, {"command": "echo hi"})
        assert decision.needs_prompt is True
