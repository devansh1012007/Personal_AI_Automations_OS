"""Containment tests for the host-process backend.

These assert *security properties*, not merely that the happy path works. Each
test here corresponds to a specific claim made in
:mod:`paa.sandbox.subprocess_backend`'s docstring — the point is that the
docstring cannot drift away from the behaviour without a test going red.
"""

from __future__ import annotations

import asyncio
import os
import sys
import time
from pathlib import Path

import pytest

from paa.config import SandboxSettings
from paa.core.errors import SandboxError
from paa.sandbox.base import IsolationLevel, SandboxSpec, assert_inside_workspace
from paa.sandbox.subprocess_backend import SubprocessSandbox

#: Set in the *host* environment by the isolation test. The child must never
#: see this name.
CANARY_VAR = "PAA_TEST_HOST_SECRET"
CANARY_VALUE = "super-secret-api-key-do-not-leak"


def python_spec(workspace: Path, code: str, **overrides: object) -> SandboxSpec:
    """A spec that runs ``code`` with the current interpreter."""
    defaults: dict[str, object] = {
        "command": (sys.executable, "-c", code),
        "workspace_path": workspace,
        "timeout_seconds": 30.0,
        "memory_mb": 256,
    }
    defaults.update(overrides)
    return SandboxSpec(**defaults)  # type: ignore[arg-type]


class TestBasicExecution:
    async def test_runs_a_real_command_and_captures_stdout(self, tmp_path: Path) -> None:
        sandbox = SubprocessSandbox()
        result = await sandbox.run(python_spec(tmp_path, "print('hello from the sandbox')"))

        assert result.exit_code == 0
        assert "hello from the sandbox" in result.stdout
        assert result.ok
        assert not result.timed_out
        assert result.backend == "subprocess"
        assert result.isolation_level is IsolationLevel.PROCESS

    async def test_captures_stderr_and_nonzero_exit(self, tmp_path: Path) -> None:
        sandbox = SubprocessSandbox()
        result = await sandbox.run(
            python_spec(tmp_path, "import sys; sys.stderr.write('boom'); sys.exit(3)")
        )

        assert result.exit_code == 3
        assert "boom" in result.stderr
        assert not result.ok

    async def test_cwd_is_the_workspace(self, tmp_path: Path) -> None:
        sandbox = SubprocessSandbox()
        result = await sandbox.run(python_spec(tmp_path, "import os; print(os.getcwd())"))

        assert result.exit_code == 0
        # resolve() both sides: tmp_path on macOS/Windows may be a symlink or
        # an 8.3 short name, and comparing unresolved would fail spuriously.
        assert Path(result.stdout.strip()).resolve() == tmp_path.resolve()

    async def test_healthcheck_is_always_true(self) -> None:
        assert await SubprocessSandbox().healthcheck() is True


class TestEnvironmentIsolation:
    """The child must NOT inherit the host environment. RFC §13 / ADR-0006.

    This is a real security property: a developer machine's environment holds
    API keys and database credentials, and ``subprocess`` inherits ``os.environ``
    by default. Regressing this would silently hand every secret on the host to
    agent-authored code.
    """

    async def test_child_cannot_see_a_host_environment_variable(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv(CANARY_VAR, CANARY_VALUE)
        # Sanity: the canary really is in the parent's environment, so a pass
        # below means isolation worked rather than that the setup no-oped.
        assert os.environ[CANARY_VAR] == CANARY_VALUE

        sandbox = SubprocessSandbox()
        result = await sandbox.run(
            python_spec(
                tmp_path,
                f"import os; print(os.environ.get({CANARY_VAR!r}, '<ABSENT>'))",
            )
        )

        assert result.exit_code == 0
        assert "<ABSENT>" in result.stdout
        assert CANARY_VALUE not in result.stdout
        assert CANARY_VALUE not in result.stderr

    async def test_child_environment_is_small_and_explicit(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv(CANARY_VAR, CANARY_VALUE)
        sandbox = SubprocessSandbox()
        result = await sandbox.run(
            python_spec(
                tmp_path,
                "import os; print('|'.join(sorted(os.environ)))",
            )
        )

        keys = set(result.stdout.strip().split("|"))
        assert CANARY_VAR not in keys
        # A whole-environment splat would be dozens of variables; the
        # bootstrap set is a handful.
        assert len(keys) < 20, f"child environment is suspiciously large: {sorted(keys)}"

    async def test_allowlisted_variables_do_reach_the_child(self, tmp_path: Path) -> None:
        """The allowlist is a gate, not a wall — deliberate passing must work."""
        sandbox = SubprocessSandbox()
        result = await sandbox.run(
            python_spec(
                tmp_path,
                "import os; print(os.environ.get('PAA_ALLOWED', '<ABSENT>'))",
                env={"PAA_ALLOWED": "explicitly-passed"},
            )
        )

        assert "explicitly-passed" in result.stdout


class TestTimeout:
    @pytest.mark.slow
    async def test_timeout_actually_kills_the_process(self, tmp_path: Path) -> None:
        """A sleep far longer than the timeout must return promptly, killed."""
        sandbox = SubprocessSandbox()
        started = time.monotonic()
        result = await sandbox.run(
            python_spec(tmp_path, "import time; time.sleep(60)", timeout_seconds=2.0)
        )
        elapsed = time.monotonic() - started

        assert result.timed_out is True
        assert result.killed_reason == "timeout"
        assert not result.ok
        # The value of the timeout is that it returns *promptly*. A generous
        # bound still fails loudly if the kill path is broken and we actually
        # waited out the full 60s sleep.
        assert elapsed < 30.0, f"timeout took {elapsed:.1f}s — the kill path is not working"

    @pytest.mark.slow
    async def test_process_tree_is_killed_leaving_no_orphan(self, tmp_path: Path) -> None:
        """An orphaned grandchild keeps running and holds the workspace open.

        The parent spawns a grandchild that appends to a heartbeat file, then
        sleeps past the timeout. After the sandbox kills the tree, the file
        must stop growing — if it keeps growing, the grandchild survived.
        """
        heartbeat = tmp_path / "heartbeat.bin"
        grandchild = tmp_path / "grandchild.py"
        parent = tmp_path / "parent.py"

        grandchild.write_text(
            "import sys, time\n"
            "path = sys.argv[1]\n"
            "while True:\n"
            "    with open(path, 'ab') as fh:\n"
            "        fh.write(b'x')\n"
            "    time.sleep(0.05)\n",
            encoding="utf-8",
        )
        # The parent waits for the grandchild to prove it is alive before going
        # to sleep. Without this handshake the test is racy: two nested Python
        # interpreter startups on Windows can easily exceed the sandbox timeout
        # under parallel test load, and the run would then be killed before the
        # grandchild ever wrote a byte — making the assertion below vacuous
        # rather than failing honestly.
        parent.write_text(
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

        sandbox = SubprocessSandbox()
        result = await sandbox.run(
            SandboxSpec(
                command=(sys.executable, str(parent), str(grandchild), str(heartbeat)),
                workspace_path=tmp_path,
                # Generous enough to cover two interpreter startups plus the
                # handshake, while still far below the parent's 60s sleep so the
                # timeout path is unambiguously what ends the run.
                timeout_seconds=25.0,
            )
        )
        assert result.timed_out is True

        # Let any in-flight write land, then sample twice. asyncio.sleep, not
        # time.sleep: blocking the event loop here would also stall the
        # sandbox's own watchdog task, so a surviving orphan could go unnoticed
        # precisely because the test froze the machinery meant to catch it.
        await asyncio.sleep(1.5)
        first = heartbeat.stat().st_size if heartbeat.exists() else 0
        await asyncio.sleep(1.5)
        second = heartbeat.stat().st_size if heartbeat.exists() else 0

        # Guard against a vacuous pass: the grandchild must actually have run.
        assert first > 0, "grandchild never started — the test proves nothing"
        assert second == first, (
            f"heartbeat grew from {first} to {second} bytes after the kill — "
            "an orphaned grandchild survived the process-tree kill"
        )


class TestOutputCapture:
    async def test_output_is_capped_and_flagged_as_truncated(self, tmp_path: Path) -> None:
        """A runaway printer must not exhaust host RAM through the pipe."""
        settings = SandboxSettings(max_capture_bytes=4096)
        sandbox = SubprocessSandbox(settings)

        result = await sandbox.run(
            python_spec(
                tmp_path,
                "import sys\n"
                "for _ in range(4000):\n"
                "    sys.stdout.write('A' * 100)\n"
                "sys.stdout.flush()\n",
                timeout_seconds=30.0,
            )
        )

        assert result.truncated_output is True
        assert len(result.stdout) <= 4096, (
            f"captured {len(result.stdout)} bytes despite a 4096-byte cap"
        )
        # Draining continues past the cap, so the child still finishes cleanly
        # rather than deadlocking on a full pipe.
        assert result.exit_code == 0
        assert not result.timed_out

    async def test_normal_output_is_not_flagged(self, tmp_path: Path) -> None:
        sandbox = SubprocessSandbox(SandboxSettings(max_capture_bytes=4096))
        result = await sandbox.run(python_spec(tmp_path, "print('short')"))

        assert result.truncated_output is False
        assert "short" in result.stdout


class TestWorkspaceJail:
    async def test_missing_workspace_is_refused(self, tmp_path: Path) -> None:
        sandbox = SubprocessSandbox()
        missing = tmp_path / "does-not-exist"

        with pytest.raises(SandboxError) as exc_info:
            await sandbox.run(python_spec(missing, "print(1)"))
        assert "workspace does not exist" in str(exc_info.value)

    async def test_workspace_that_is_a_file_is_refused(self, tmp_path: Path) -> None:
        not_a_dir = tmp_path / "file.txt"
        not_a_dir.write_bytes(b"data")

        with pytest.raises(SandboxError):
            await SubprocessSandbox().run(python_spec(not_a_dir, "print(1)"))

    def test_assert_inside_workspace_rejects_an_outside_path(self, tmp_path: Path) -> None:
        workspace = tmp_path / "ws"
        workspace.mkdir()
        outside = tmp_path / "elsewhere"
        outside.mkdir()

        with pytest.raises(SandboxError) as exc_info:
            assert_inside_workspace(outside, workspace)
        assert "escapes the workspace jail" in str(exc_info.value)

    def test_assert_inside_workspace_rejects_dotdot_traversal(self, tmp_path: Path) -> None:
        workspace = tmp_path / "ws"
        workspace.mkdir()

        with pytest.raises(SandboxError):
            assert_inside_workspace(workspace / ".." / ".." / "etc", workspace)

    def test_assert_inside_workspace_accepts_a_nested_path(self, tmp_path: Path) -> None:
        workspace = tmp_path / "ws"
        (workspace / "sub").mkdir(parents=True)

        resolved = assert_inside_workspace(workspace / "sub" / "file.txt", workspace)
        assert resolved.is_relative_to(workspace.resolve())


class TestSpecValidation:
    def test_empty_command_is_refused(self, tmp_path: Path) -> None:
        with pytest.raises(ValueError, match="at least one element"):
            SandboxSpec(command=(), workspace_path=tmp_path)

    def test_whole_environment_splat_is_refused(self, tmp_path: Path) -> None:
        """The tripwire for ``env=dict(os.environ)``."""
        fake_host_env = {f"VAR_{i}": "x" for i in range(45)}
        fake_host_env["SystemRoot"] = r"C:\Windows"
        fake_host_env["USERPROFILE"] = r"C:\Users\dev"

        with pytest.raises(ValueError, match="copy of the host environment"):
            SandboxSpec(
                command=("echo", "hi"), workspace_path=tmp_path, env=fake_host_env
            )

    def test_negative_timeout_is_refused(self, tmp_path: Path) -> None:
        with pytest.raises(ValueError, match="positive or None"):
            SandboxSpec(command=("echo",), workspace_path=tmp_path, timeout_seconds=-1.0)
