"""Shared fixtures for the thread control-plane suite (C7.5, W7).

The whole suite runs offline and deterministically: the sandbox is the real
subprocess backend, the harnesses are the two shipped fakes, the ledger is a
real :class:`~paa.ledger.store.LedgerStore` over a throwaway SQLite file, and
git — where a test needs a git story — runs host-side exactly the way
:func:`paa.thread.engine.create_worktree` does. Nothing here touches a
network, a vendor CLI or the developer's real ``PAA_HOME``.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from paa.config import PolicySettings, Settings
from paa.context.gatherer import BoundedContextGatherer, ContextElement
from paa.ledger.store import LedgerStore
from paa.sandbox.subprocess_backend import SubprocessSandbox
from paa.thread.engine import ThreadEngine
from paa.thread.record import ThreadRecord

__all__ = ["git", "make_thread"]


def git(repo: Path, *args: str) -> None:
    """One host-side git invocation against ``repo``; refuses with stderr.

    The control plane's own setup half: thread tests arrange repositories the
    way :func:`paa.thread.engine.create_worktree` arranges worktrees —
    directly, not through the sandbox, because arranging the workspace is not
    a harness touching it.
    """
    result = subprocess.run(
        ["git", "-C", str(repo), *args], capture_output=True, text=True
    )
    assert result.returncode == 0, f"git {' '.join(args)} failed: {result.stderr}"


def make_thread(
    *,
    title: str = "the swap demo",
    workspace: str = "/tmp/ws",
    mandate: dict | None = None,
) -> ThreadRecord:
    """A minimal well-formed thread record, for unit-level halves.

    Deliberately not journalled or saved: the callers that need persistence
    call :func:`paa.thread.record.save_thread` themselves, so a unit test
    cannot accidentally pin a file under the wrong home.
    """
    if mandate is None:
        mandate = {
            "v": 1,
            "session_id": None,
            "requester_asserted": {"kind": "user", "id": "operator"},
            "identity_assurance": "asserted",
            "basis": "explicit",
            "policy_version": "f" * 64,
            "policy_label": "ask-v3",
            "scope": {"workspace": workspace},
        }
    return ThreadRecord(
        id="11111111-2222-3333-4444-555555555555",
        title=title,
        workspace=workspace,
        created_at="2026-11-09T00:00:00.000+00:00",
        updated_at="2026-11-09T00:00:00.000+00:00",
        mandate=dict(mandate),
    )


class FakeMemorySource:
    """Synthetic recalled facts — the engine's memory half, canned.

    Implements the ``ThreadMemorySource`` protocol structurally (an async
    ``elements`` taking the thread); the engine accepts it because the
    protocol is structural, which is the point of having made it one.
    """

    def __init__(self, elements: list[ContextElement]) -> None:
        self._elements = elements

    async def elements(self, thread: ThreadRecord) -> list[ContextElement]:
        return list(self._elements)


@pytest.fixture
def workspace(tmp_path: Path) -> Path:
    """An empty directory a thread can call its workspace."""
    ws = tmp_path / "ws"
    ws.mkdir()
    return ws


@pytest.fixture
def git_repo(tmp_path: Path) -> Path:
    """A seeded git repository: one commit, clean tree.

    Thread tests that need a git summary or a worktree run here; the seed
    commit gives ``recent_commits`` something honest to say and keeps the
    status list empty until a harness actually touches the tree.
    """
    repo = tmp_path / "repo"
    repo.mkdir()
    git(repo, "init", "-q")
    git(repo, "config", "user.name", "Thread Test")
    git(repo, "config", "user.email", "thread@test.invalid")
    (repo / "seed.txt").write_text("seed\n", encoding="utf-8")
    git(repo, "add", "-A")
    git(repo, "commit", "-qm", "seed")
    return repo


@pytest.fixture
def policy(paa_home: Path) -> PolicySettings:
    """The effective policy a thread's mandate is granted under."""
    return Settings(home=paa_home).policy


@pytest.fixture
def sandbox() -> SubprocessSandbox:
    """The real one-shot/session subprocess backend — no Docker needed."""
    return SubprocessSandbox()


@pytest.fixture
def gatherer() -> BoundedContextGatherer:
    return BoundedContextGatherer()


@pytest.fixture
def engine(ledger: LedgerStore, paa_home: Path, sandbox: SubprocessSandbox) -> ThreadEngine:
    """A stateless engine over a real ledger, an isolated home, the real sandbox."""
    return ThreadEngine(ledger=ledger, home=paa_home, sandbox=sandbox)
