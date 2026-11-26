"""The guarded git commit — host-side git behind the integrity gate (C8.1 stage 5).

Real git subprocesses (marked ``slow``, the ``tests/coder`` convention), and
the machinery under test is never its own fixture: repositories are
initialised with plain ``subprocess`` argv, exactly the way the coder
suite sets its git fixtures up.

What these tests pin:

* **the repo-root jail** — ``is_git_repository`` is ``show-toplevel ==
  root``, not merely ``rev-parse`` succeeding: a subdirectory of someone
  else's repository is not a repository this loop may commit into;
* **the staging path jail** — named files only (no globs, no
  directories), inside the repository, never the git directory or an
  environment file;
* **the identity gate** — a host that cannot author a commit refuses
  before anything is staged, naming the remedy (a commit misattributed
  to a fabricated author is a lie in the record);
* **the fixed argv** — staging and committing are the only things that
  happen, and the new HEAD is the commit's own id.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from paa.core.errors import PaaError
from paa.improve.gitcommit import guarded_git_commit, is_git_repository

pytestmark = pytest.mark.slow


def _git(cwd: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *args], cwd=str(cwd), capture_output=True, text=True, check=True
    )


def _repo(path: Path, *, keep_identity: bool = True) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    (path / "playbooks.md").write_text("# ops playbooks\n", encoding="utf-8")
    _git(path, "init", "-q", "-b", "main")
    _git(path, "config", "user.email", "improve@example.invalid")
    _git(path, "config", "user.name", "Improve Test")
    _git(path, "add", "--", "playbooks.md")
    _git(path, "commit", "-q", "-m", "seed")
    if not keep_identity:
        # The seed commit needed an author; the probe must find nothing
        # *now*, which is the state the gate exists to name.
        _git(path, "config", "--unset", "user.email")
        _git(path, "config", "--unset", "user.name")
    return path.resolve()


def _head(repo: Path) -> str:
    return _git(repo, "rev-parse", "HEAD").stdout.strip()


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    return _repo(tmp_path / "repo")


class TestTheRepoRootJail:
    async def test_a_repository_root_is_itself(self, repo: Path) -> None:
        assert await is_git_repository(repo)

    async def test_a_plain_directory_is_not_a_repository(self, tmp_path: Path) -> None:
        plain = tmp_path / "plain"
        plain.mkdir()
        assert not await is_git_repository(plain)

    async def test_a_subdirectory_of_a_repository_is_not_a_root(
        self, tmp_path: Path
    ) -> None:
        """The jail rule: git would walk up and write into a repo the loop never named."""
        repo = _repo(tmp_path / "repo")
        sub = repo / "docs"
        sub.mkdir()
        assert not await is_git_repository(sub)


class TestTheGuardedCommit:
    async def test_a_named_file_is_staged_and_committed(
        self, repo: Path, paa_home: Path
    ) -> None:
        before = _head(repo)
        (repo / "playbooks.md").write_text("# ops playbooks\n\nnew rule\n", encoding="utf-8")
        result = await guarded_git_commit(
            repo, ["playbooks.md"], "improve: record the lesson"
        )
        assert result.head == _head(repo)
        assert result.head != before
        assert result.staged == ("playbooks.md",)
        subject = _git(repo, "log", "--format=%s", "-1").stdout.strip()
        assert subject == "improve: record the lesson"

    async def test_a_subdirectory_root_is_refused(
        self, tmp_path: Path, paa_home: Path
    ) -> None:
        repo = _repo(tmp_path / "repo")
        sub = repo / "docs"
        sub.mkdir()
        with pytest.raises(PaaError, match="not the top of a git working tree"):
            await guarded_git_commit(sub, ["playbooks.md"], "improve: x")

    async def test_glob_characters_are_refused(self, repo: Path, paa_home: Path) -> None:
        with pytest.raises(PaaError, match="not a named file"):
            await guarded_git_commit(repo, ["*.md"], "improve: x")

    async def test_an_absolute_staging_path_is_refused(
        self, repo: Path, paa_home: Path
    ) -> None:
        with pytest.raises(PaaError, match="must be relative"):
            await guarded_git_commit(repo, [str(repo / "playbooks.md")], "improve: x")

    async def test_a_path_outside_the_repository_is_refused(
        self, repo: Path, paa_home: Path
    ) -> None:
        with pytest.raises(PaaError, match=r"escapes|outside|workspace"):
            await guarded_git_commit(repo, ["../outside.md"], "improve: x")

    async def test_the_git_directory_is_refused(
        self, repo: Path, paa_home: Path
    ) -> None:
        with pytest.raises(PaaError, match="refusing to stage"):
            await guarded_git_commit(repo, [".git/config"], "improve: x")

    async def test_an_environment_file_is_refused(
        self, repo: Path, paa_home: Path
    ) -> None:
        with pytest.raises(PaaError, match="refusing to stage"):
            await guarded_git_commit(repo, [".env"], "improve: x")

    async def test_a_host_without_identity_refuses_before_staging(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The identity probe reads git config; without HOME it cannot.

        The child environment passes ``HOME`` through by design (git needs
        it for global config), so the test narrows the passthrough to
        ``PATH`` alone — the one shape in which no identity can be found
        from anywhere, which is the state the gate exists to name.
        """
        from paa.improve import gitcommit

        # Local identity unset after the seed commit, and the child's
        # HOME passthrough narrowed away: the probe finds nothing from
        # anywhere, which is the state the gate exists to name.
        repo = _repo(tmp_path / "repo", keep_identity=False)
        monkeypatch.setattr(gitcommit, "_ENV_PASSTHROUGH", frozenset({"PATH"}))
        with pytest.raises(PaaError, match=r"user\.name"):
            await guarded_git_commit(repo, ["playbooks.md"], "improve: x")

    async def test_a_failed_stage_refuses_without_committing(
        self, repo: Path, paa_home: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A staging path that names no file on disk fails the git add."""
        before = _head(repo)
        with pytest.raises(PaaError, match="git add failed"):
            await guarded_git_commit(repo, ["no-such-file.md"], "improve: x")
        assert _head(repo) == before
