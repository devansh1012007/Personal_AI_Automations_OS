"""Cancellation tests for the host-process backend.

The cancel seam of W3-IMPL-03 lives in the coder loop: on
``CancelledError`` the loop journals the FAILED row through the existing
``journal.after`` call and re-raises. That only works if the backend
itself returns control promptly and leaves no child behind — a backend
that unwinds past ``proc.wait()`` without killing the tree orphans the
workload (and on Windows, ``KILL_ON_JOB_CLOSE`` only reaps it when the
*host* process exits). These tests pin the backend's half of that
contract: cancel during the wait must kill the process tree, reap the
process, and re-raise — never return a ``SandboxResult``, which would
read as normal completion and journal COMPLETED.
"""

from __future__ import annotations

import asyncio
import gc
import sys
import time
import warnings
from pathlib import Path

import pytest

from paa.sandbox.base import SandboxSpec
from paa.sandbox.subprocess_backend import SubprocessSandbox


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


def _write_heartbeat_pair(tmp_path: Path) -> tuple[Path, Path, Path]:
    """Return ``(heartbeat, grandchild, parent)`` script paths.

    The grandchild appends one byte to the heartbeat file every 0.05 s.
    The parent spawns it, waits for the first byte (a handshake — nested
    interpreter startups on Windows can exceed a tight budget under
    parallel load, and without the handshake the run could be killed
    before the grandchild ever wrote, making the whole test vacuous),
    then sleeps far beyond any budget the test hands it.
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
    return heartbeat, grandchild, parent


async def _cancel_and_reraise(task: asyncio.Task[object]) -> float:
    """Cancel ``task``, expect ``CancelledError``, return seconds taken."""
    started = time.monotonic()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    return time.monotonic() - started


async def _wait_for_heartbeat(task: asyncio.Task[object], heartbeat: Path) -> None:
    """Poll until the heartbeat is non-empty (the grandchild is running)."""
    deadline = time.monotonic() + 30.0
    while time.monotonic() < deadline:
        if heartbeat.exists() and heartbeat.stat().st_size > 0:
            return
        if task.done():
            pytest.fail("run completed before cancel — the test proves nothing")
        await asyncio.sleep(0.05)
    pytest.fail("grandchild never started — the test proves nothing")


class TestCancelKillsTheTree:
    @pytest.mark.slow
    async def test_cancel_kills_the_tree_leaving_no_orphan(self, tmp_path: Path) -> None:
        """A cancelled run must stop the whole tree — parent *and* grandchild.

        Bounded wait (``timeout_seconds=60``), so this exercises the
        ``asyncio.wait_for(proc.wait(), ...)`` arm: an external cancel
        surfaces as ``CancelledError``, not ``TimeoutError``, from that
        await. The heartbeat probe is the only honest oracle — the
        grandchild is nobody's child handle once the parent is reaped, so
        if it is still appending two seconds later, the tree lived.
        """
        heartbeat, grandchild, parent = _write_heartbeat_pair(tmp_path)
        sandbox = SubprocessSandbox()
        task = asyncio.create_task(
            sandbox.run(
                SandboxSpec(
                    command=(sys.executable, str(parent), str(grandchild), str(heartbeat)),
                    workspace_path=tmp_path,
                    timeout_seconds=60.0,
                )
            )
        )
        await _wait_for_heartbeat(task, heartbeat)

        elapsed = await _cancel_and_reraise(task)
        assert elapsed < 5.0, f"cancel took {elapsed:.1f}s — the tree was not killed promptly"

        # Let any in-flight write land, then sample twice. asyncio.sleep, not
        # time.sleep: blocking the event loop here would stall the sandbox's
        # own machinery, so a surviving orphan could go unnoticed precisely
        # because the test froze the machinery meant to catch it.
        await asyncio.sleep(1.5)
        first = heartbeat.stat().st_size if heartbeat.exists() else 0
        await asyncio.sleep(1.5)
        second = heartbeat.stat().st_size if heartbeat.exists() else 0

        assert first > 0, "grandchild never wrote — the test proves nothing"
        assert second == first, (
            f"heartbeat grew from {first} to {second} bytes after the cancel — "
            "an orphaned grandchild survived the cancel path"
        )

    @pytest.mark.slow
    async def test_cancel_kills_the_tree_without_a_timeout(self, tmp_path: Path) -> None:
        """Unbounded wait (``timeout_seconds=None``) takes the bare
        ``await proc.wait()`` arm — a different code path in ``run`` that
        must kill the tree on cancel just the same."""
        heartbeat, grandchild, parent = _write_heartbeat_pair(tmp_path)
        sandbox = SubprocessSandbox()
        task = asyncio.create_task(
            sandbox.run(
                SandboxSpec(
                    command=(sys.executable, str(parent), str(grandchild), str(heartbeat)),
                    workspace_path=tmp_path,
                    timeout_seconds=None,
                )
            )
        )
        await _wait_for_heartbeat(task, heartbeat)

        elapsed = await _cancel_and_reraise(task)
        assert elapsed < 5.0, f"cancel took {elapsed:.1f}s — the tree was not killed promptly"

        await asyncio.sleep(1.5)
        first = heartbeat.stat().st_size if heartbeat.exists() else 0
        await asyncio.sleep(1.5)
        second = heartbeat.stat().st_size if heartbeat.exists() else 0

        assert first > 0, "grandchild never wrote — the test proves nothing"
        assert second == first, (
            f"heartbeat grew from {first} to {second} bytes after the cancel — "
            "an orphaned grandchild survived the cancel path"
        )


class TestCancelReraisesPromptly:
    async def test_cancel_reraises_promptly_without_a_result(self, tmp_path: Path) -> None:
        """The cancel must come back as ``CancelledError``, not a result.

        Returning a ``SandboxResult`` here would make the coder loop
        journal COMPLETED — a cancelled run would read as a clean one.
        """
        sandbox = SubprocessSandbox()
        task = asyncio.create_task(
            sandbox.run(
                python_spec(tmp_path, "import time; time.sleep(60)", timeout_seconds=60.0)
            )
        )
        await asyncio.sleep(0.3)
        assert not task.done()

        elapsed = await _cancel_and_reraise(task)
        assert elapsed < 5.0, f"cancel took {elapsed:.1f}s — it did not return promptly"

    async def test_cancel_reaps_the_process_without_resource_warnings(
        self, tmp_path: Path
    ) -> None:
        """An unreaped child emits an asyncio ``ResourceWarning`` at
        transport GC — the same noise the W3-IMPL-03 orphan probe saw on
        stderr. A reaped one stays silent."""
        sandbox = SubprocessSandbox()
        task = asyncio.create_task(
            sandbox.run(
                python_spec(tmp_path, "import time; time.sleep(60)", timeout_seconds=60.0)
            )
        )
        await asyncio.sleep(0.3)

        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            await _cancel_and_reraise(task)
            gc.collect()

        resource_warnings = [
            warning for warning in caught if issubclass(warning.category, ResourceWarning)
        ]
        assert not resource_warnings, (
            f"ResourceWarnings after cancel (unreaped process?): "
            f"{[str(w.message) for w in resource_warnings]}"
        )
