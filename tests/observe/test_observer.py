"""The observer core and the crash classification (C7.1).

Two suites, one honesty rule:

* **the classification matrix** — every way a child can end (clean,
  error, signalled, timed out, resource-killed) maps to exactly one
  named state, with the sandbox's own ``killed_reason`` outranking the
  exit code and a negative POSIX exit decoded to the signal number;
* **real scripted runs** — a fake harness (a Python ``-c`` one-liner)
  driven through the real subprocess backend, including the ``kill -9``
  chaos the campaign's other loops already carry: a child that kills
  itself mid-work must leave a *complete* bundle with a named state,
  never a retry, never silence.

The crash-marker property gets its own test: an observer that dies
after the skeleton write leaves a bundle stuck at ``phase="started"``,
and that file is the evidence.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from paa.core.errors import SandboxError
from paa.observe.bundle import GitChanges, ManifestDiff, read_bundle
from paa.observe.observer import (
    CrashClass,
    ObserverCore,
    classify_outcome,
)
from paa.sandbox.base import SandboxResult
from paa.sandbox.subprocess_backend import SubprocessSandbox


def _result(**kwargs: object) -> SandboxResult:
    """A fabricated sandbox result with the defaults a live run fills in."""
    return SandboxResult(**kwargs)  # type: ignore[arg-type]


def _git(ws: Path, *args: str) -> None:
    """A sync git call for fixture-building (the coder suite's own pattern)."""
    import subprocess

    subprocess.run(["git", *args], cwd=ws, check=True)


def _no_changes() -> tuple[ManifestDiff, GitChanges]:
    return ManifestDiff(), GitChanges(available=False, reason="not a repo")


class TestTheClassificationMatrix:
    def test_clean_exit(self) -> None:
        verdict = classify_outcome(
            _result(exit_code=0, duration_ms=10.0), *_no_changes()
        )
        assert verdict.state is CrashClass.CLEAN_EXIT
        assert verdict.signal is None
        assert verdict.recoverable is False

    def test_error_exit_keeps_the_code_the_child_chose(self) -> None:
        verdict = classify_outcome(
            _result(exit_code=3, duration_ms=10.0), *_no_changes()
        )
        assert verdict.state is CrashClass.ERROR_EXIT
        assert "exited non-zero (3)" in verdict.notes

    def test_negative_exit_is_the_kernels_account_of_a_signal(self) -> None:
        verdict = classify_outcome(
            _result(exit_code=-9, duration_ms=10.0), *_no_changes()
        )
        assert verdict.state is CrashClass.SIGNALLED
        assert verdict.signal == 9
        assert verdict.to_json()["signal_name"] == "SIGKILL"
        assert "SIGKILL" in verdict.notes

    def test_no_exit_code_is_signalled_without_inventing_a_number(self) -> None:
        verdict = classify_outcome(
            _result(exit_code=None, duration_ms=10.0), *_no_changes()
        )
        assert verdict.state is CrashClass.SIGNALLED
        assert verdict.signal is None
        assert verdict.to_json()["signal_name"] is None

    def test_the_backends_own_timeout_account_outranks_the_exit_code(self) -> None:
        verdict = classify_outcome(
            _result(exit_code=None, killed_reason="timeout", duration_ms=1000.0),
            *_no_changes(),
        )
        assert verdict.state is CrashClass.TIMED_OUT

    def test_the_memory_ceiling_is_its_own_named_death(self) -> None:
        verdict = classify_outcome(
            _result(exit_code=None, killed_reason="memory", duration_ms=100.0),
            *_no_changes(),
        )
        assert verdict.state is CrashClass.RESOURCE_KILLED

    def test_the_workspace_facts_ride_the_verdict(self) -> None:
        from paa.observe.bundle import FileFacts

        full = ManifestDiff(
            created=(FileFacts("new", 1, "a"),),
            modified=((FileFacts("m", 1, "a"), FileFacts("m", 2, "b")),),
            deleted=(FileFacts("old", 1, "a"),),
        )
        git = GitChanges(available=True, status=" M f")
        verdict = classify_outcome(_result(exit_code=0), full, git)
        assert verdict.workspace_changed is True
        assert (verdict.files_created, verdict.files_modified, verdict.files_deleted) == (
            1,
            1,
            1,
        )
        assert verdict.git_dirty is True
        assert "1 created" in verdict.notes and "1 deleted" in verdict.notes
        assert "dirty" in verdict.notes

    def test_the_json_says_resume_none_even_for_a_clean_run(self) -> None:
        verdict = classify_outcome(_result(exit_code=0), *_no_changes())
        assert verdict.to_json()["resume"] == "none"
        assert verdict.to_json()["recoverable"] is False


class TestRealScriptedRuns:
    """A fake harness on the real subprocess backend, every which way."""

    @pytest.fixture
    def core(self, tmp_path: Path) -> ObserverCore:
        return ObserverCore(
            SubprocessSandbox(), bundle_home=tmp_path / "bundles"
        )

    @pytest.fixture
    def workspace(self, tmp_path: Path) -> Path:
        ws = tmp_path / "ws"
        ws.mkdir()
        (ws / "seed.txt").write_text("seed\n", encoding="utf-8")
        return ws

    async def test_a_host_without_git_still_finalises_the_bundle(
        self, core: ObserverCore, workspace: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Git is a witness, not the subject: a missing binary is recorded."""

        async def no_git(
            self: ObserverCore, workspace: Path, args: tuple[str, ...]
        ) -> SandboxResult:
            raise SandboxError("git: executable not found")

        monkeypatch.setattr(ObserverCore, "_git", no_git)
        out = await core.observe(
            [sys.executable, "-c", "pass"], workspace=workspace, timeout_s=60
        )
        assert out.classification.state is CrashClass.CLEAN_EXIT
        bundle = read_bundle(out.bundle_path)
        assert bundle.phase == "final"
        assert bundle.git is not None
        assert bundle.git.available is False
        assert "git unavailable" in (bundle.git.reason or "")

    async def test_the_done_when_a_fake_harness_run_leaves_a_complete_t1_bundle(
        self, core: ObserverCore, workspace: Path
    ) -> None:
        """The spec's C7.1 done-when, verbatim, as a test.

        A scripted fake harness creates a file, modifies another, prints
        to both streams, exits 0 — and the bundle on disk is the complete
        T1 record of all of it.
        """
        out = await core.observe(
            [
                sys.executable,
                "-c",
                "import pathlib, sys; "
                "pathlib.Path('made.txt').write_text('by harness'); "
                "pathlib.Path('seed.txt').write_text('changed'); "
                "print('stdout line'); print('stderr line', file=sys.stderr)",
            ],
            workspace=workspace,
            timeout_s=60,
            run_id="fixed-run-id",
        )
        assert out.classification.state is CrashClass.CLEAN_EXIT
        assert out.result.exit_code == 0

        bundle = read_bundle(out.bundle_path)
        assert bundle.phase == "final"
        assert bundle.resume == "none"
        assert bundle.evidence_tier == "T1"
        assert bundle.run_id == "fixed-run-id"
        assert bundle.command[-1].startswith("import pathlib")
        # manifest: one created, one modified
        assert [f["path"] for f in bundle.manifest["created"]] == ["made.txt"]
        assert [m["after"]["path"] for m in bundle.manifest["modified"]] == ["seed.txt"]
        assert bundle.manifest["deleted"] == []
        # transcript: both streams, whole, correctly hashed
        assert bundle.transcript_stdout is not None
        assert "stdout line" in bundle.transcript_stdout.text
        assert bundle.transcript_stdout.truncated is False
        assert bundle.transcript_stderr is not None
        assert "stderr line" in bundle.transcript_stderr.text
        # child facts and verdict
        assert bundle.child["exit_code"] == 0
        assert bundle.classification["state"] == "clean_exit"
        assert bundle.classification["resume"] == "none"
        # a non-git workspace says so honestly
        assert bundle.git is not None
        assert bundle.git.available is False
        assert bundle.git.reason

    async def test_error_exit_is_classified_not_hidden(
        self, core: ObserverCore, workspace: Path
    ) -> None:
        out = await core.observe(
            [sys.executable, "-c", "import sys; sys.exit(3)"],
            workspace=workspace,
            timeout_s=60,
        )
        assert out.classification.state is CrashClass.ERROR_EXIT
        assert out.classification.exit_code == 3

    async def test_kill_9_chaos_a_self_killed_child_leaves_a_complete_bundle(
        self, core: ObserverCore, workspace: Path
    ) -> None:
        """The observer's kill -9 drill (the campaign's chaos standard).

        The harness writes half its work, then SIGKILLs itself. The
        observer must still come back with a complete final bundle, the
        death named as signalled with signal 9, the half-done work
        visible in the manifest diff — and the child must have run
        exactly once (the bundle is written from the one and only run).
        """
        out = await core.observe(
            [
                sys.executable,
                "-c",
                "import os, pathlib, signal; "
                "pathlib.Path('half.txt').write_text('half'); "
                "os.kill(os.getpid(), signal.SIGKILL)",
            ],
            workspace=workspace,
            timeout_s=60,
        )
        assert out.classification.state is CrashClass.SIGNALLED
        assert out.classification.signal == 9
        # The verdict records the sandbox's own account of the code — on
        # POSIX that is the kernel's negative convention (-9), never a
        # code the child chose.
        assert out.classification.exit_code in (None, -9)
        assert out.classification.files_created == 1  # half.txt
        assert out.classification.workspace_changed is True

        bundle = read_bundle(out.bundle_path)
        assert bundle.phase == "final"
        assert [f["path"] for f in bundle.manifest["created"]] == ["half.txt"]
        assert bundle.classification["signal_name"] == "SIGKILL"

    async def test_the_wall_clock_kills_and_classifies(
        self, core: ObserverCore, workspace: Path
    ) -> None:
        out = await core.observe(
            [sys.executable, "-c", "import time; time.sleep(30)"],
            workspace=workspace,
            timeout_s=1,
        )
        assert out.classification.state is CrashClass.TIMED_OUT
        assert out.result.timed_out is True
        assert out.result.killed_reason == "timeout"

    async def test_a_deletion_is_evidence_too(
        self, core: ObserverCore, workspace: Path
    ) -> None:
        out = await core.observe(
            [sys.executable, "-c", "import os; os.remove('seed.txt')"],
            workspace=workspace,
            timeout_s=60,
        )
        assert out.classification.files_deleted == 1
        bundle = read_bundle(out.bundle_path)
        assert [f["path"] for f in bundle.manifest["deleted"]] == ["seed.txt"]

    async def test_the_transcript_cap_truncates_with_a_marker(
        self, core: ObserverCore, workspace: Path
    ) -> None:
        out = await core.observe(
            [sys.executable, "-c", "print('x' * 4096)"],
            workspace=workspace,
            timeout_s=60,
            transcript_cap_kb=1,  # 1024 bytes, well under the 4096+ emitted
        )
        bundle = read_bundle(out.bundle_path)
        assert bundle.transcript_stdout is not None
        assert bundle.transcript_stdout.truncated is True
        assert bundle.transcript_stdout.byte_length >= 4096
        assert len(bundle.transcript_stdout.text.encode("utf-8")) <= 1024

    async def test_an_observer_crash_leaves_the_started_skeleton_as_the_marker(
        self, tmp_path: Path, workspace: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The crash marker: nobody came back, and the file says so."""

        class ExplodingSandbox(SubprocessSandbox):
            async def run(self, spec: object) -> SandboxResult:  # type: ignore[override]
                raise SandboxError("the observer's world ended mid-run")

        core = ObserverCore(ExplodingSandbox(), bundle_home=tmp_path / "bundles")
        with pytest.raises(SandboxError):
            await core.observe(
                ["echo", "never-observed"], workspace=workspace, timeout_s=5
            )
        # Exactly one bundle exists, it never got past "started", and it
        # is valid JSON a consumer can read and classify.
        files = list((tmp_path / "bundles" / "observe").rglob("bundle.json"))
        assert len(files) == 1
        stuck = read_bundle(files[0])
        assert stuck.phase == "started"
        assert stuck.ended_at is None

    async def test_an_empty_command_is_refused(
        self, core: ObserverCore, workspace: Path
    ) -> None:
        with pytest.raises(SandboxError, match="non-empty command"):
            await core.observe([], workspace=workspace)

    async def test_run_id_defaults_to_a_fresh_identity(
        self, core: ObserverCore, workspace: Path
    ) -> None:
        out = await core.observe(
            [sys.executable, "-c", "pass"],
            workspace=workspace,
            timeout_s=30,
        )
        assert len(out.run_id) == 32  # a uuid4 hex, minted by the core


class TestTheGitSection:
    """Git's own view of the workspace, asked through the same sandbox."""

    @pytest.fixture
    def core(self, tmp_path: Path) -> ObserverCore:
        return ObserverCore(SubprocessSandbox(), bundle_home=tmp_path / "bundles")

    @pytest.fixture
    def repo(self, tmp_path: Path) -> Path:
        ws = tmp_path / "repo"
        ws.mkdir()
        _git(ws, "init")
        _git(ws, "config", "user.email", "t@t")
        _git(ws, "config", "user.name", "t")
        (ws / "tracked.txt").write_text("committed\n", encoding="utf-8")
        _git(ws, "add", ".")
        _git(ws, "commit", "-qm", "baseline")
        return ws

    async def test_a_committing_harness_is_recorded_head_to_head(
        self, core: ObserverCore, repo: Path
    ) -> None:
        out = await core.observe(
            [
                "sh",
                "-c",
                "echo changed > tracked.txt && "
                "git -c user.email=t@t -c user.name=t commit -qam 'by harness'",
            ],
            workspace=repo,
            timeout_s=60,
        )
        assert out.classification.state is CrashClass.CLEAN_EXIT
        git = out.bundle.git
        assert git is not None
        assert git.available is True
        assert git.head_before is not None and git.head_after is not None
        assert git.head_before != git.head_after
        assert len(git.commits) == 1
        assert "by harness" in git.commits[0]["line"]
        assert out.classification.commits == 1
        assert out.classification.git_dirty is False

    async def test_a_dirty_worktree_without_commits_is_dirty(
        self, core: ObserverCore, repo: Path
    ) -> None:
        out = await core.observe(
            ["sh", "-c", "echo dirt > untracked.txt"],
            workspace=repo,
            timeout_s=60,
        )
        git = out.bundle.git
        assert git is not None
        assert git.dirty is True
        assert "untracked.txt" in git.status
        assert git.commits == ()
        assert out.classification.git_dirty is True

    async def test_a_clean_worktree_reads_clean(
        self, core: ObserverCore, repo: Path
    ) -> None:
        out = await core.observe(
            [sys.executable, "-c", "pass"], workspace=repo, timeout_s=60
        )
        git = out.bundle.git
        assert git is not None
        assert git.available is True
        assert git.dirty is False

    async def test_a_repo_with_no_commits_still_reports_status(
        self, core: ObserverCore, tmp_path: Path
    ) -> None:
        ws = tmp_path / "fresh-repo"
        ws.mkdir()
        _git(ws, "init")
        out = await core.observe(
            ["sh", "-c", "echo x > loose.txt"], workspace=ws, timeout_s=60
        )
        git = out.bundle.git
        assert git is not None
        assert git.available is True
        assert git.head_before is None  # nothing was committed before the run
        assert git.dirty is True
        assert "loose.txt" in git.status
