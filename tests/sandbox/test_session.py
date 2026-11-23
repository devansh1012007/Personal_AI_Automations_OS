"""Duplex-session tests for the host-process backend.

The session is the contract Wave 5's consumers build on: the sandboxed MCP
client (C5.3) speaks NDJSON over exactly these pipes, and Gate G-H1 requires
that a ``kill -9`` on such a child mid-conversation leaves no process behind.
Every test here runs a real process tree with the real interpreter — no mocks
of the process layer — because the property under test is the *kernel's* view
of the tree, and a mock would test the mock.

The one deliberate duplication with ``test_subprocess_backend.py`` is the
canary: the session path builds the child env through the same
:func:`~paa.sandbox.base.build_child_env`, and the ambient-secret property
has to be re-proven per path — a session that quietly inherited the host
environment would be the same leak wearing a longer-lived pipe.
"""

from __future__ import annotations

import asyncio
import os
import signal
import sys
import time
from pathlib import Path
from typing import cast

import pytest

from paa.core.errors import SandboxError
from paa.sandbox.base import (
    IsolationLevel,
    SandboxResult,
    SandboxSession,
    SandboxSpec,
)
from paa.sandbox.docker_backend import DockerSandbox
from paa.sandbox.dryrun_backend import DryRunSandbox
from paa.sandbox.subprocess_backend import SubprocessSandbox
from paa.sandbox.watchdog import SamplerSource, sample_rss_mb
from paa.sandbox.wsl_backend import WslSandbox

#: Set in the *host* environment by the isolation test. The child must never
#: see this name.
CANARY_VAR = "PAA_TEST_HOST_SECRET"
CANARY_VALUE = "super-secret-api-key-do-not-leak"

#: A duplex child: reads lines, echoes them. The smallest child whose *whole
#: point* is that the pipes stay open — a one-shot ``print`` would pass every
#: duplex test by accident.
_ECHO_CHILD = (
    "import sys\n"
    "for line in sys.stdin:\n"
    "    sys.stdout.write('echo:' + line)\n"
    "    sys.stdout.flush()\n"
)

#: A duplex child whose tree has a grandchild that would outlive it: the root
#: spawns a sleeper, announces the pid (the handshake that proves the tree is
#: really up), then echoes like ``_ECHO_CHILD``.
_TREE_ECHO_CHILD = (
    "import subprocess, sys\n"
    "grandchild = subprocess.Popen(\n"
    "    [sys.executable, '-c', 'import time; time.sleep(120)'])\n"
    "print('grandchild', grandchild.pid, flush=True)\n"
    "for line in sys.stdin:\n"
    "    sys.stdout.write('echo:' + line)\n"
    "    sys.stdout.flush()\n"
)

#: A child that allocates far past the ceiling the test hands it: the
#: "under rlimits" half of the duplex acceptance needs a workload that
#: actually breaches, or the ceiling is decoration the test never exercised.
_MEMORY_CHILD = (
    "import sys, time\n"
    "blob = bytearray(96 * 1024 * 1024)\n"
    "print('allocated', flush=True)\n"
    "time.sleep(60)\n"
)

#: Whether this host can measure RSS at all. The memory-ceiling test needs a
#: working sampler tier (psutil, the Windows native call, or ``/proc``);
#: on a host whose every tier is ``UNAVAILABLE`` the ceiling is honestly not
#: enforced and the test would hang waiting for a kill that cannot happen.
_SAMPLER_AVAILABLE = sample_rss_mb(os.getpid())[2] is not SamplerSource.UNAVAILABLE


#: A sleeping tree: announces itself once so the tests can wait for a *real*
#: handshake rather than a spawn returning, then sleeps far beyond any budget
#: the test hands it — with a grandchild sleeping beside it.
_TREE_SLEEP_CHILD = (
    "import subprocess, sys, time\n"
    "subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(120)'])\n"
    "print('ready', flush=True)\n"
    "time.sleep(120)\n"
)

#: Prints what it can and cannot see of its environment.
_ENV_CHILD = (
    "import os\n"
    "print(os.environ.get('PAA_TEST_HOST_SECRET', '<ABSENT>'))\n"
    "print(os.environ.get('PAA_SESSION_ALLOWED', '<ABSENT>'))\n"
    "print('|'.join(sorted(os.environ)))\n"
)


def session_spec(workspace: Path, code: str, **overrides: object) -> SandboxSpec:
    """A spec that runs ``code`` with the current interpreter."""
    defaults: dict[str, object] = {
        "command": (sys.executable, "-c", code),
        "workspace_path": workspace,
        "timeout_seconds": 30.0,
        "memory_mb": 256,
    }
    defaults.update(overrides)
    return SandboxSpec(**defaults)  # type: ignore[arg-type]


class LineReader:
    """The caller's half of a line protocol over chunk reads.

    ``read()`` hands back arbitrary pipe-sized chunks; *somebody* has to
    reassemble lines, and the sandbox refuses to be that somebody — the
    framing is the caller's business, not the sandbox's. This is the
    smallest honest caller, and every protocol client (NDJSON, ACP) is
    this loop with more fields.
    """

    def __init__(self, session: SandboxSession) -> None:
        self._session = session
        self._buffer = b""

    async def readline(self, *, timeout: float = 10.0) -> bytes:
        """One framed line, without its newline."""
        while b"\n" not in self._buffer:
            chunk = await self._session.read(timeout=timeout)
            if not chunk:
                raise AssertionError("child closed stdout before the expected line")
            self._buffer += chunk
        line, _, self._buffer = self._buffer.partition(b"\n")
        return line


def _pid_is_gone(pid: int) -> bool:
    """Whether ``pid`` no longer exists — signal 0 is the existence probe."""
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return True
    except PermissionError:  # pragma: no cover - exists but is not ours
        return False
    return False


def _pgid_is_gone(pgid: int) -> bool:
    """Whether the process group has no members left.

    The group id is only reusable once *every* member is gone, so a probe
    that fails proves the whole tree died; a probe that succeeds means a
    member — maybe one that was meant to die — is still there.
    """
    try:
        os.killpg(pgid, 0)
    except ProcessLookupError:
        return True
    except PermissionError:  # pragma: no cover - exists but is not ours
        return False
    return False


async def _wait_until_tree_gone(pgid: int, *pids: int, timeout: float = 10.0) -> None:
    """Poll until the group and every named pid are gone.

    The kernel is asynchronous about death: SIGKILL lands at the
    scheduler's pleasure and a reaped slot can linger a moment. Asserting
    immediately would be asserting a race nobody agreed to run, so the
    probe polls — bounded, because a tree that never dies must *fail* the
    test rather than hang it.
    """
    deadline = time.monotonic() + timeout
    while not (_pgid_is_gone(pgid) and all(_pid_is_gone(pid) for pid in pids)):
        if time.monotonic() > deadline:
            pytest.fail(f"process group {pgid} still has members {timeout}s after the kill")
        await asyncio.sleep(0.05)


async def _wait_until_dead(session: SandboxSession, *, timeout: float = 5.0) -> None:
    """Poll until the session's root child is dead, bounded.

    Reaping is asynchronous to the pipes: ``read()`` can deliver the
    child's last bytes before asyncio has waited its exit. Asserting on
    ``alive`` immediately after EOF would be a race, so this waits it out
    — bounded, because a child that never dies must fail the test rather
    than hang it.
    """
    deadline = time.monotonic() + timeout
    while session.alive:
        if time.monotonic() > deadline:
            pytest.fail(f"session pid {session.pid} still alive after {timeout}s")
        await asyncio.sleep(0.05)


def _assert_died_by_kill(result: SandboxResult) -> None:
    """Assert the child ended by a kill, on the channels a sandbox can own.

    A negative status is the signal the kill delivered. ``255`` is *not* an
    exit the child made: it is asyncio's ``PidfdChildWatcher`` fabricating a
    returncode when its ``os.waitpid`` loses the corpse to a reaper outside
    this process tree — CPython ``asyncio/unix_events.py`` says it in as many
    words ("The child process is already reaped (may happen if waitpid() is
    called elsewhere) ... will report returncode 255"), and this host
    reproduces it about once per few hundred session kills — always with the
    containment facts (``killed_reason``, ``timed_out``, a reaped tree)
    intact. The kill itself is attested by those fields and by the
    tree-gone probes; the exit-status channel is the one thing a sandbox
    cannot own, and refusing to fail on the documented fabrication is
    honesty about that, not a weakened assertion.
    """
    assert result.exit_code is not None
    assert result.exit_code < 0 or result.exit_code == 255


# ---------------------------------------------------------------------------
# The duplex pipe itself
# ---------------------------------------------------------------------------


class TestDuplexEcho:
    async def test_lines_written_are_echoed_back_under_the_resource_envelope(
        self, tmp_path: Path
    ) -> None:
        """An echo child over the duplex pipe, inside a real envelope.

        The ``memory_mb`` is a real ceiling, not decoration: the watchdog
        samples the tree for the session's whole lifetime, and a child that
        breached it would be killed — this test would hang on the missing
        reply rather than pass vacuously.
        """
        sandbox = SubprocessSandbox()
        session = await sandbox.open(
            session_spec(tmp_path, _ECHO_CHILD, memory_mb=64, cpu_cores=1.0)
        )
        reader = LineReader(session)
        for i in range(5):
            await session.write(f"line-{i}\n".encode())
        replies = [await reader.readline() for _ in range(5)]

        assert replies == [f"echo:line-{i}".encode() for i in range(5)]

        result = await session.close()
        # Closing a live session is a kill, and the result says so.
        assert result.killed_reason == "closed"
        _assert_died_by_kill(result)
        assert result.timed_out is False
        # The transcript the ledger gets is the conversation the caller
        # consumed through read() — not a separate capture the caller never
        # could have seen.
        assert "echo:line-4" in result.stdout
        assert result.truncated_output is False
        assert result.backend == "subprocess"
        assert result.isolation_level is IsolationLevel.PROCESS


class TestReadSemantics:
    async def test_an_empty_read_within_the_budget_raises_timeout_error(
        self, tmp_path: Path
    ) -> None:
        """The empty read is the caller's clock, and it says so loudly."""
        session = await SubprocessSandbox().open(
            session_spec(tmp_path, "import time; time.sleep(60)")
        )
        with pytest.raises(TimeoutError):
            await session.read(timeout=0.2)
        await session.close()

    async def test_eof_is_reported_as_an_empty_chunk(self, tmp_path: Path) -> None:
        """``b""`` is end-of-stream, a value the duplex caller consumes."""
        session = await SubprocessSandbox().open(
            session_spec(tmp_path, "print('bye')")
        )
        got = bytearray()
        while chunk := await session.read(timeout=10.0):
            got += chunk
        assert b"bye" in got

        await _wait_until_dead(session)
        assert session.alive is False

        result = await session.close()
        assert result.exit_code == 0
        assert result.killed_reason is None
        assert result.timed_out is False
        assert "bye" in result.stdout

    async def test_the_pipes_are_gone_after_close(self, tmp_path: Path) -> None:
        """read/write on a closed session raise the typed error, not a hang."""
        session = await SubprocessSandbox().open(
            session_spec(tmp_path, "import time; time.sleep(60)")
        )
        await session.close()
        with pytest.raises(SandboxError):
            await session.read(timeout=1.0)
        with pytest.raises(SandboxError):
            await session.write(b"x")

    async def test_writing_to_a_dead_child_raises_the_typed_error(
        self, tmp_path: Path
    ) -> None:
        """A dead child's stdin is gone: writing into the void must refuse."""
        session = await SubprocessSandbox().open(
            session_spec(tmp_path, "print('dying now')")
        )
        while await session.read(timeout=10.0):
            pass
        await _wait_until_dead(session)
        assert session.alive is False

        with pytest.raises(SandboxError):
            await session.write(b"too late\n")
        await session.close()


# ---------------------------------------------------------------------------
# Tree kills — the containment the session exists to hold
# ---------------------------------------------------------------------------


class TestKillNineMidStream:
    @pytest.mark.slow
    async def test_kill_nine_reaps_the_tree_and_close_is_a_no_op(
        self, tmp_path: Path
    ) -> None:
        """``kill -9`` the child mid-conversation: the session reaps the tree.

        The SIGKILL comes from *outside* the sandbox — the one death the
        session did not cause and cannot have pre-reaped — so this is the
        honest test of the watcher: the root's group dies with it, the
        grandchild included, and the caller's later ``close()`` has nothing
        left to do. Gate G-H1's requirement in the concrete.
        """
        sandbox = SubprocessSandbox()
        session = await sandbox.open(session_spec(tmp_path, _TREE_ECHO_CHILD))
        reader = LineReader(session)

        announcement = await reader.readline()
        grandchild_pid = int(announcement.split()[1])
        await session.write(b"mid-stream\n")
        assert await reader.readline() == b"echo:mid-stream"

        # Non-vacuous guard: the tree is really up right now.
        os.kill(grandchild_pid, 0)
        os.killpg(session.pid, 0)

        os.kill(session.pid, signal.SIGKILL)
        await _wait_until_tree_gone(session.pid, grandchild_pid)
        assert session.alive is False

        result = await session.close()
        # -SIGKILL is the status our test's own kill delivered; the 255
        # alternative is asyncio's fabricated "reaped elsewhere" status —
        # see _assert_died_by_kill for the mechanism and why it is tolerated.
        assert result.exit_code == -signal.SIGKILL or result.exit_code == 255
        assert result.killed_reason is None
        assert result.timed_out is False

        # And close() is a no-op from here on: the same result object, no
        # second teardown, nothing left to kill.
        assert await session.close() is result
        with pytest.raises(SandboxError):
            await session.read(timeout=1.0)
        with pytest.raises(SandboxError):
            await session.write(b"x")


class TestCloseCancels:
    @pytest.mark.slow
    async def test_close_returns_promptly_and_kills_the_tree(
        self, tmp_path: Path
    ) -> None:
        """close() while a grandchild is mid-sleep: prompt, total.

        The 5-second bound is the contract the coder loop's cancel seam
        depends on — a teardown that lingers past it would hold the caller
        hostage to a workload it already gave up on.
        """
        sandbox = SubprocessSandbox()
        session = await sandbox.open(session_spec(tmp_path, _TREE_SLEEP_CHILD))
        assert await LineReader(session).readline() == b"ready"

        started = time.monotonic()
        result = await session.close()
        elapsed = time.monotonic() - started

        assert elapsed < 5.0, f"close took {elapsed:.1f}s — the tree was not killed promptly"
        assert result.killed_reason == "closed"
        _assert_died_by_kill(result)
        assert result.timed_out is False
        assert session.alive is False
        await _wait_until_tree_gone(session.pid)


class TestWallClockDeadline:
    @pytest.mark.slow
    async def test_an_idle_session_tree_times_out(self, tmp_path: Path) -> None:
        """``timeout_seconds`` on a session is a lifetime budget.

        An idle child and its idle grandchild, closed by nobody: the
        deadline is the only actor, so it must kill the *tree*, not just the
        root — a surviving grandchild would be an orphan the budget was
        promised to prevent.
        """
        sandbox = SubprocessSandbox()
        session = await sandbox.open(
            session_spec(tmp_path, _TREE_SLEEP_CHILD, timeout_seconds=1.5)
        )
        assert await LineReader(session).readline() == b"ready"

        await _wait_until_dead(session, timeout=15.0)
        assert session.alive is False

        result = await session.close()
        assert result.timed_out is True
        assert result.killed_reason == "timeout"
        _assert_died_by_kill(result)
        await _wait_until_tree_gone(session.pid)


# ---------------------------------------------------------------------------
# The resource envelope — the "under rlimits" half of the acceptance
# ---------------------------------------------------------------------------


class TestMemoryCeiling:
    @pytest.mark.slow
    @pytest.mark.skipif(
        not _SAMPLER_AVAILABLE,
        reason="no RSS sampler tier works on this host, so the session's "
        "memory ceiling is honestly not enforced (see SamplerSource "
        "tiers) and a breach-kill test would hang waiting for it",
    )
    async def test_a_breaching_child_is_killed_by_the_envelope_not_the_clock(
        self, tmp_path: Path
    ) -> None:
        """The session's ``memory_mb`` is enforced while the pipes are open.

        The duplex acceptance is "echo child over duplex pipe under
        rlimits"; this is the second half making the "under" real: a child
        that runs away past the ceiling is killed by the session's
        watchdog with ``killed_reason="memory"`` — not by the wall clock
        (which is set far beyond what the breach needs), so the kill is
        attributable to the envelope and not to a budget racing it. The
        peak that justified the kill is reported, and the tree dies with
        the child. Without the watchdog actually running — an earlier
        revision of this machinery claimed
        ``memory_enforcement="watchdog"`` while the sampler never started —
        this test is the one that hangs: a session that merely recorded the
        ceiling would never notice the breach, and the child would sit in
        its 60-second sleep until the wall clock killed it instead.
        """
        sandbox = SubprocessSandbox()
        session = await sandbox.open(
            session_spec(tmp_path, _MEMORY_CHILD, memory_mb=32, timeout_seconds=30.0)
        )
        assert await LineReader(session).readline() == b"allocated"

        # The watchdog kills the tree on its own cadence; waiting for the
        # death before close() means the result attributes the kill to the
        # breach rather than to "closed".
        await _wait_until_dead(session, timeout=15.0)
        result = await session.close()

        assert result.killed_reason == "memory"
        assert result.timed_out is False
        _assert_died_by_kill(result)
        assert result.peak_rss_mb is not None and result.peak_rss_mb > 32
        await _wait_until_tree_gone(session.pid)


# ---------------------------------------------------------------------------
# The env allowlist, on the session path
# ---------------------------------------------------------------------------


class TestEnvironmentIsolation:
    async def test_the_child_sees_only_the_allowlisted_env(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The session path builds the env the same way ``run()`` does.

        A duplex child inherits nothing either: a canary in the host's
        ``os.environ`` must not reach a child that now lives *longer* than
        the call that spawned it — the leak would be worse here, not
        better, because the child holds the pipe open while it reads the
        secret.
        """
        monkeypatch.setenv(CANARY_VAR, CANARY_VALUE)
        assert os.environ[CANARY_VAR] == CANARY_VALUE

        sandbox = SubprocessSandbox()
        session = await sandbox.open(
            session_spec(
                tmp_path,
                _ENV_CHILD,
                env={"PAA_SESSION_ALLOWED": "passed-deliberately"},
            )
        )
        reader = LineReader(session)

        assert await reader.readline() == b"<ABSENT>"
        assert await reader.readline() == b"passed-deliberately"
        keys = (await reader.readline()).decode().split("|")

        assert CANARY_VAR not in keys
        assert CANARY_VALUE not in (await session.close()).stdout
        # A whole-environment splat would be dozens of variables; the
        # bootstrap set is a handful.
        assert len(keys) < 20, f"child environment is suspiciously large: {sorted(keys)}"


# ---------------------------------------------------------------------------
# One-shots inside the envelope
# ---------------------------------------------------------------------------


class TestRunWithin:
    async def test_a_one_shot_runs_inside_the_envelope_and_returns_a_result(
        self, tmp_path: Path
    ) -> None:
        """``run_within`` is a normal one-shot under the session's jail.

        The command's cwd is the session's workspace — the envelope's jail
        is the session's, and the child of the envelope cannot be somewhere
        the session never agreed to be — and the session itself stays
        duplex-usable afterwards, because the envelope is a fact of the
        session, not a single exchange.
        """
        sandbox = SubprocessSandbox()
        session = await sandbox.open(session_spec(tmp_path, _ECHO_CHILD))

        one_shot = SandboxSpec(
            command=(
                sys.executable,
                "-c",
                "import os\nprint(os.getcwd())\nprint('one-shot-ok')",
            ),
            workspace_path=tmp_path,
            timeout_seconds=10.0,
            memory_mb=128,
        )
        result = await sandbox.run_within(session, one_shot)

        assert isinstance(result, SandboxResult)
        assert result.ok
        assert result.exit_code == 0
        assert "one-shot-ok" in result.stdout
        assert result.backend == "subprocess"
        assert result.isolation_level is IsolationLevel.PROCESS
        assert Path(result.stdout.splitlines()[0]).resolve() == tmp_path.resolve()

        await session.write(b"still-alive\n")
        assert await LineReader(session).readline() == b"echo:still-alive"
        await session.close()

    async def test_a_workspace_outside_the_envelope_is_refused(
        self, tmp_path: Path
    ) -> None:
        """The envelope's jail is the session's workspace, full stop."""
        sandbox = SubprocessSandbox()
        session = await sandbox.open(session_spec(tmp_path, _ECHO_CHILD))
        outside = tmp_path.parent / "elsewhere-sessions"
        outside.mkdir()

        with pytest.raises(SandboxError) as exc_info:
            await sandbox.run_within(
                session,
                SandboxSpec(
                    command=(sys.executable, "-c", "print('x')"),
                    workspace_path=outside,
                    timeout_seconds=10.0,
                ),
            )
        assert "escapes the workspace jail" in str(exc_info.value)
        await session.close()

    async def test_a_closed_session_has_no_envelope(self, tmp_path: Path) -> None:
        """``run_within`` after ``close()`` refuses rather than resurrects."""
        sandbox = SubprocessSandbox()
        session = await sandbox.open(session_spec(tmp_path, _ECHO_CHILD))
        await session.close()

        with pytest.raises(SandboxError, match="envelope is gone"):
            await sandbox.run_within(
                session,
                SandboxSpec(
                    command=(sys.executable, "-c", "print('x')"),
                    workspace_path=tmp_path,
                    timeout_seconds=10.0,
                ),
            )

    async def test_a_foreign_session_is_refused(self, tmp_path: Path) -> None:
        """The envelope must be one this backend opened."""

        class _ForeignSession(SandboxSession):
            """A session from no backend at all.

            Only the identity matters: ``run_within`` must refuse an
            envelope it never established, and the cheapest honest way to
            prove that is an object that is definitely not this backend's.
            """

            @property
            def pid(self) -> int | None:
                return None

            @property
            def alive(self) -> bool:
                return False

            async def read(self, *, timeout: float | None = None) -> bytes:
                raise AssertionError("never reached")

            async def write(self, data: bytes) -> None:
                raise AssertionError("never reached")

            async def close(self) -> SandboxResult:
                raise AssertionError("never reached")

        with pytest.raises(SandboxError, match="session opened by the subprocess backend"):
            await SubprocessSandbox().run_within(
                _ForeignSession(),
                SandboxSpec(
                    command=(sys.executable, "-c", "print('x')"),
                    workspace_path=tmp_path,
                    timeout_seconds=10.0,
                ),
            )


# ---------------------------------------------------------------------------
# Honest refusals
# ---------------------------------------------------------------------------


class TestHonestRefusals:
    """Backends that cannot deliver a duplex child must say so loudly.

    A session is a longer-lived containment commitment than a one-shot; a
    backend that silently degraded it would be worse than the quiet
    downgrade ``IsolationLevel`` exists to prevent, so the refusal — not a
    fake session, not a silent fall-back to one-shot — is the contract.
    """

    async def test_dryrun_refuses_to_open(self, tmp_path: Path) -> None:
        with pytest.raises(SandboxError, match="executes nothing"):
            await DryRunSandbox().open(session_spec(tmp_path, "print(1)"))

    async def test_wsl_refuses_to_open(self, tmp_path: Path) -> None:
        with pytest.raises(SandboxError, match="not implemented for the WSL backend"):
            await WslSandbox().open(session_spec(tmp_path, "print(1)"))

    async def test_docker_refuses_to_open(self, tmp_path: Path) -> None:
        with pytest.raises(SandboxError, match="not implemented for the Docker backend"):
            await DockerSandbox().open(session_spec(tmp_path, "print(1)"))

    async def test_refusing_backends_refuse_run_within(self, tmp_path: Path) -> None:
        """The base ``run_within`` refusal: no session, no envelope."""
        spec = session_spec(tmp_path, "print('x')")
        # The session argument is never touched — the refusal fires on the
        # backend's own inability, which is exactly what is being pinned.
        with pytest.raises(SandboxError, match="run_within is not implemented"):
            await DryRunSandbox().run_within(cast(SandboxSession, None), spec)

    def test_the_session_contract_is_abstract(self) -> None:
        """The contract cannot be satisfied by instantiating the ABC itself."""
        with pytest.raises(TypeError):
            SandboxSession()  # type: ignore[abstract]


# ---------------------------------------------------------------------------
# The one-shot wrapper kept its old contract
# ---------------------------------------------------------------------------


class TestRunIsStillASessionWrapper:
    async def test_run_behaves_as_the_one_shot_it_always_was(
        self, tmp_path: Path
    ) -> None:
        """``run()`` is a thin wrapper over ``open()`` — and stays one-shot.

        The full byte-compatibility witness is the existing
        ``test_subprocess_backend.py`` suite (it runs against this same
        wrapper); what is pinned here is the property that motivated the
        wrapper: a stdin-*reading* child still sees EOF, because the wrapper
        seals the pipe the session opened — a session-fed child would
        otherwise block on a pipe nobody writes to, and a one-shot that
        changed its stdin contract would be anything but thin.
        """
        sandbox = SubprocessSandbox()
        result = await sandbox.run(
            session_spec(
                tmp_path,
                "import sys\n"
                "print('read', sys.stdin.read(16))\n"
                "print('done')\n",
            )
        )
        assert result.exit_code == 0
        assert result.stdout == "read \ndone\n"
        assert result.ok
        assert result.timed_out is False
        assert result.killed_reason is None

    async def test_run_with_no_wall_clock_waits_for_the_work(
        self, tmp_path: Path
    ) -> None:
        """``timeout_seconds=None`` means wait forever, not wait zero.

        The session's deadline is armed by the same field and must inherit
        the same semantics — an un-armed deadline is the whole difference
        between "no budget" and "zero budget".
        """
        sandbox = SubprocessSandbox()
        session = await sandbox.open(
            session_spec(tmp_path, _ECHO_CHILD, timeout_seconds=None)
        )
        await session.write(b"unbounded\n")
        assert await LineReader(session).readline() == b"echo:unbounded"
        result = await session.close()
        assert result.killed_reason == "closed"
        assert result.timed_out is False

        # And the one-shot path with no budget finishes its work rather than
        # dying at a default: a 0.5-second worker under no deadline must
        # complete.
        done = await sandbox.run(
            session_spec(tmp_path, "import time; time.sleep(0.5)", timeout_seconds=None)
        )
        assert done.exit_code == 0
        assert done.ok


# ---------------------------------------------------------------------------
# Windows-only behaviour follows the existing skip pattern (tests/sandbox/
# test_win32.py): nothing here is Windows-gated because every assertion runs
# on the POSIX session path this host exercises; the Windows branches in the
# backend (Job Object, taskkill) are pinned by test_win32.py instead.
# ---------------------------------------------------------------------------
