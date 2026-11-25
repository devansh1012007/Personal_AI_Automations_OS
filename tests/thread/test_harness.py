"""The harness binding registry — the plug-in point for real harnesses (C7.5).

What is pinned here
-------------------
* The registry resolves both shipped fakes and lists exactly them.
* A binding is a value: given one segment context it yields a concrete argv
  (``sys.executable`` + the fixture script, never a bare ``python``) and the
  env markers a harness may attribute its stint by.
* An unknown name is a refusal that names the options — an operator typo
  should be one edit from corrected, not a dig through the registry.
* Both shipped fixture scripts actually run, standalone and offline: the
  swap demo's "harnesses" are real child processes, not mocks.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

from paa.thread.errors import ThreadError
from paa.thread.harness import (
    BindingContext,
    get_binding,
    known_harnesses,
)

_CONTEXT = BindingContext(
    workspace=Path("/tmp/ws"),
    thread_id="11111111-2222-3333-4444-555555555555",
    segment_id="seg-1",
)


class TestTheRegistry:
    def test_ships_exactly_the_two_fakes(self) -> None:
        assert known_harnesses() == ("fake-alpha", "fake-beta")

    def test_both_fakes_resolve(self) -> None:
        for name in known_harnesses():
            binding = get_binding(name)
            assert binding.name == name
            assert binding.description  # a binding that cannot say what it is

    def test_unknown_name_is_a_refusal_naming_the_options(self) -> None:
        with pytest.raises(ThreadError, match="unknown harness") as excinfo:
            get_binding("claude-code")
        assert "fake-alpha" in str(excinfo.value)
        assert "fake-beta" in str(excinfo.value)


class TestCommandConstruction:
    def test_the_argv_is_the_absolute_interpreter_plus_the_script(self) -> None:
        argv = get_binding("fake-alpha").command_for(_CONTEXT)
        assert argv[0] == sys.executable
        script = Path(argv[1])
        assert script.name == "fake_alpha.py"
        assert script.is_file()

    def test_each_fake_binds_its_own_script(self) -> None:
        alpha = get_binding("fake-alpha").command_for(_CONTEXT)
        beta = get_binding("fake-beta").command_for(_CONTEXT)
        assert alpha != beta

    def test_env_carries_the_thread_and_segment_markers(self) -> None:
        env = get_binding("fake-beta").env_for(_CONTEXT)
        assert env["PAA_THREAD_ID"] == _CONTEXT.thread_id
        assert env["PAA_THREAD_SEGMENT"] == _CONTEXT.segment_id


class TestTheFixtureScriptsRun:
    """The fakes are the demo's hands; here they run as plain children."""

    def _run(self, name: str, cwd: Path) -> subprocess.CompletedProcess[str]:
        argv = get_binding(name).command_for(_CONTEXT)
        return subprocess.run(argv, cwd=cwd, capture_output=True, text=True, timeout=60)

    def test_fake_alpha_opens_the_work(self, tmp_path: Path) -> None:
        result = self._run("fake-alpha", tmp_path)
        assert result.returncode == 0, result.stderr
        notes = (tmp_path / "thread-notes.md").read_text(encoding="utf-8")
        assert "fake-alpha" in notes
        assert "standalone" in notes  # no segment marker in a bare run
        assert (tmp_path / "alpha-work.txt").read_text(
            encoding="utf-8"
        ) == "fake-alpha did a unit of work\n"

    def test_fake_beta_continues_the_work(self, tmp_path: Path) -> None:
        """Beta appends to alpha's files — the visible half of context intact."""
        self._run("fake-alpha", tmp_path)
        result = self._run("fake-beta", tmp_path)
        assert result.returncode == 0, result.stderr
        notes = (tmp_path / "thread-notes.md").read_text(encoding="utf-8")
        assert "1 note line(s) went before" in notes
        work = (tmp_path / "alpha-work.txt").read_text(encoding="utf-8")
        assert "fake-alpha did a unit of work" in work
        assert "fake-beta continued the same unit of work" in work
