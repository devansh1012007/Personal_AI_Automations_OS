"""Deterministic validation — exit codes are the verdict (C8.1 stage 3).

Real subprocesses with real exit codes, the ``tests/coder`` convention
for process-shaped machinery (marked ``slow``). What these tests pin:

* **no gate is model-decided** — the verdict is exactly
  exit-code-zero-and-not-timed-out for every command; a red exit, a
  command that cannot start, and a command past its wall budget are all
  resolved reds with named reasons, never interpretations;
* **zero commands is a refusal, not a pass** — a loop that validated
  nothing has not validated;
* **the real tree is never touched** — the proposal is applied to the
  scratch copy, and the operator's checkout is byte-identical after a
  failing gate;
* **the evidence file is the record** — each command's full outcome
  (argv, exit code, capped outputs, digests) lands as JSON, ready to
  ride the attested bundle as a blob;
* **the operator's argv mistakes are refused by name** — a flat list of
  strings where a list of argv lists belongs is the one shape that
  slips through a parts-are-strings check.
"""

from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path

import pytest

from paa.core.errors import PaaError
from paa.improve.proposal import FileChange, PatchProposal
from paa.improve.validation import run_validation

pytestmark = pytest.mark.slow


def _green() -> list[list[str]]:
    return [[sys.executable, "-c", "raise SystemExit(0)"]]


def _proposal(repo: Path, path: str = "playbooks.md") -> PatchProposal:
    """A proposal built against the repo's actual current content.

    The prior-hash guard is part of the contract under test's
    neighbour (``test_proposal.py``); here the proposal must be
    applicable, so its prior digest is the file as it stands.
    """
    prior = (repo / path).read_bytes() if (repo / path).exists() else b""
    return PatchProposal(
        intent="record the lesson",
        rationale="friction was high",
        changes=(
            FileChange(
                path=path,
                content="new content\n",
                prior_sha256=hashlib.sha256(prior).hexdigest(),
            ),
        ),
        domain="deploy",
    )


def _repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "playbooks.md").write_text("old content\n", encoding="utf-8")
    (repo / ".git").mkdir()
    (repo / ".git" / "HEAD").write_text("ref: refs/heads/main\n", encoding="utf-8")
    (repo / ".pytest_cache").mkdir()
    (repo / ".pytest_cache" / "cached.txt").write_text("cache\n", encoding="utf-8")
    return repo


class TestTheVerdict:
    async def test_a_green_command_passes(self, tmp_path: Path) -> None:
        repo = _repo(tmp_path)
        verdict = await run_validation(
            _proposal(repo),
            repo_root=repo,
            scratch_root=tmp_path / "scratch",
            commands=_green(),
        )
        assert verdict.passed
        assert verdict.reason == ""
        assert len(verdict.commands) == 1
        assert verdict.commands[0].green
        assert verdict.commands[0].exit_code == 0

    async def test_a_red_exit_code_fails_the_proposal(self, tmp_path: Path) -> None:
        repo = _repo(tmp_path)
        verdict = await run_validation(
            _proposal(repo),
            repo_root=repo,
            scratch_root=tmp_path / "scratch",
            commands=[[sys.executable, "-c", "raise SystemExit(3)"]],
        )
        assert not verdict.passed
        assert len(verdict.failed_commands) == 1
        assert verdict.failed_commands[0].exit_code == 3

    async def test_one_red_command_fails_the_whole_verdict(self, tmp_path: Path) -> None:
        repo = _repo(tmp_path)
        verdict = await run_validation(
            _proposal(repo),
            repo_root=repo,
            scratch_root=tmp_path / "scratch",
            commands=[*_green(), [sys.executable, "-c", "raise SystemExit(1)"]],
        )
        assert not verdict.passed
        assert len(verdict.commands) == 2
        assert len(verdict.failed_commands) == 1

    async def test_zero_commands_is_a_refusal_not_a_pass(self, tmp_path: Path) -> None:
        repo = _repo(tmp_path)
        verdict = await run_validation(
            _proposal(repo),
            repo_root=repo,
            scratch_root=tmp_path / "scratch",
            commands=[],
        )
        assert not verdict.passed
        assert "no validation commands" in verdict.reason

    async def test_a_command_past_its_wall_budget_is_a_resolved_red(
        self, tmp_path: Path
    ) -> None:
        repo = _repo(tmp_path)
        verdict = await run_validation(
            _proposal(repo),
            repo_root=repo,
            scratch_root=tmp_path / "scratch",
            commands=[[sys.executable, "-c", "import time; time.sleep(30)"]],
            timeout_s=1,
        )
        assert not verdict.passed
        assert verdict.commands[0].timed_out
        assert not verdict.commands[0].green

    async def test_a_command_that_cannot_start_is_a_resolved_red(
        self, tmp_path: Path
    ) -> None:
        repo = _repo(tmp_path)
        verdict = await run_validation(
            _proposal(repo),
            repo_root=repo,
            scratch_root=tmp_path / "scratch",
            commands=[["no-such-binary-anywhere", "--flag"]],
        )
        assert not verdict.passed
        command = verdict.commands[0]
        assert command.exit_code is None
        assert not command.timed_out
        # The named reason rides the stderr field and is digested, not
        # swallowed — the verdict names what could not run.
        assert "FileNotFoundError" in command.stderr
        assert command.stderr_bytes > 0
        assert command.stderr_sha256 == hashlib.sha256(
            command.stderr.encode("utf-8")
        ).hexdigest()

    async def test_a_missing_repo_is_refused_by_name(self, tmp_path: Path) -> None:
        with pytest.raises(PaaError, match="was not found"):
            await run_validation(
                _proposal(tmp_path),
                repo_root=tmp_path / "not-a-repo",
                scratch_root=tmp_path / "scratch",
                commands=_green(),
            )

    async def test_a_flat_command_list_is_refused_by_name(
        self, tmp_path: Path
    ) -> None:
        """One command typed flat is the mistake the check exists for.

        Iterating a bare string yields one-character strings, so a plain
        parts-are-strings check would accept it and run a command named
        ``r`` — the refusal names the shape instead.
        """
        repo = _repo(tmp_path)
        with pytest.raises(PaaError, match="non-empty argv of strings"):
            await run_validation(
                _proposal(repo),
                repo_root=repo,
                scratch_root=tmp_path / "scratch",
                commands=["ruff", "check", "src"],
            )


class TestTheScratchCopy:
    async def test_the_real_tree_is_never_touched(self, tmp_path: Path) -> None:
        repo = _repo(tmp_path)
        verdict = await run_validation(
            _proposal(repo),
            repo_root=repo,
            scratch_root=tmp_path / "scratch",
            commands=[[sys.executable, "-c", "raise SystemExit(1)"]],
        )
        assert not verdict.passed
        # The operator's checkout is byte-identical after a failing gate.
        assert (repo / "playbooks.md").read_text(encoding="utf-8") == "old content\n"
        # The scratch copy carries the applied proposal.
        scratch = Path(verdict.scratch_root)
        assert (scratch / "playbooks.md").read_text(encoding="utf-8") == "new content\n"

    async def test_version_control_state_and_caches_are_not_copied(
        self, tmp_path: Path
    ) -> None:
        repo = _repo(tmp_path)
        verdict = await run_validation(
            _proposal(repo),
            repo_root=repo,
            scratch_root=tmp_path / "scratch",
            commands=_green(),
        )
        scratch = Path(verdict.scratch_root)
        assert not (scratch / ".git").exists()
        assert not (scratch / ".pytest_cache").exists()

    async def test_the_scratch_root_is_rebuilt_on_reuse(self, tmp_path: Path) -> None:
        repo = _repo(tmp_path)
        scratch = tmp_path / "scratch"
        scratch.mkdir()
        (scratch / "repo").mkdir()
        (scratch / "repo" / "stale.txt").write_text("stale\n", encoding="utf-8")
        verdict = await run_validation(
            _proposal(repo),
            repo_root=repo,
            scratch_root=scratch,
            commands=_green(),
        )
        assert not (Path(verdict.scratch_root) / "stale.txt").exists()


class TestTheEvidenceFile:
    async def test_each_command_leaves_a_json_record_with_its_outputs(
        self, tmp_path: Path
    ) -> None:
        repo = _repo(tmp_path)
        marker = "validation-spoke-on-stdout"
        evidence = tmp_path / "evidence"
        verdict = await run_validation(
            _proposal(repo),
            repo_root=repo,
            scratch_root=tmp_path / "scratch",
            commands=[[sys.executable, "-c", f"print('{marker}')"]],
            evidence_dir=evidence,
        )
        assert verdict.passed
        path = evidence / "validation-01.json"
        assert path.is_file()
        payload = json.loads(path.read_text(encoding="utf-8"))
        assert payload["v"] == 1
        assert payload["kind"] == "improve-validation/1"
        command = payload["command"]
        assert command["argv"] == [sys.executable, "-c", f"print('{marker}')"]
        assert command["exit_code"] == 0
        assert marker in command["stdout"]
        # The digests commit to the full streams the command produced.
        assert command["stdout_sha256"] == hashlib.sha256(
            (marker + "\n").encode("utf-8")
        ).hexdigest()
