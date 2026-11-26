"""Shared fixtures for the guarded self-improvement loop's suite (C8.1).

The coder suite's rule applies here too: the machinery under test is never
its own fixture. Git repositories are initialised with plain subprocess
argv, the ledger is the real ``LedgerStore`` over a throwaway SQLite file,
and the reflection input is real events — ``TASK_REQUESTED`` with a
``deploy`` goal plus a correction and a rollback, the same seed the
daemon's own tests use, which yields exactly one high-friction domain
(friction 4.5: (1x1.5 + 1x3.0) / max(0, 1)).

Suites that drive git or gate-command subprocesses are marked ``slow``,
the ``tests/coder`` convention for real-process tests.
"""

from __future__ import annotations

import shutil
import subprocess
import uuid
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import pytest

from paa.core.types import EventType, new_correlation_id
from paa.improve.gate import Approver, HumanDecision
from paa.improve.pipeline import ImproveFacade, ImproveReflectionLoop
from paa.ledger.events import LedgerEvent
from paa.ledger.store import LedgerStore
from paa.pipelines.base import PipelineTask
from paa.pipelines.journal import CallJournal
from paa.storage.relational.database import Database


def _git(cwd: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *args],
        cwd=str(cwd),
        capture_output=True,
        text=True,
        check=True,
    )


def make_repo(tmp_path: Path, name: str = "repo") -> Path:
    """A real git repository with local identity and a seed playbook."""
    repo = tmp_path / name
    repo.mkdir(parents=True, exist_ok=True)
    (repo / "playbooks.md").write_text("# ops playbooks\n", encoding="utf-8")
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "config", "user.email", "improve@example.invalid")
    _git(repo, "config", "user.name", "Improve Test")
    _git(repo, "add", "--", "playbooks.md")
    _git(repo, "commit", "-q", "-m", "seed")
    return repo.resolve()


def head_of(repo: Path) -> str:
    return _git(repo, "rev-parse", "HEAD").stdout.strip()


def subject_of(repo: Path) -> str:
    """The HEAD commit's subject line, read the way ``head_of`` reads HEAD.

    A sync helper beside its kin so no async test runs a blocking
    subprocess inline (the suite's ASYNC discipline): the git helpers are
    the machinery's own convention, called from the tests that assert on
    their output.
    """
    return _git(repo, "log", "--format=%s", "-1").stdout.strip()


async def seed_high_friction(ledger: LedgerStore) -> None:
    """One deploy task, one correction, one rollback, zero clean commits."""
    cid = new_correlation_id()
    await ledger.append(
        LedgerEvent.create(
            cid,
            EventType.TASK_REQUESTED,
            payload={"request": {"goal": "deploy the service"}},
        )
    )
    await ledger.append(LedgerEvent.create(cid, EventType.USER_CORRECTION))
    await ledger.append(
        LedgerEvent.create(cid, EventType.STATE_ROLLBACK_TRIGGERED, discriminator="r1")
    )


async def seed_clean_history(ledger: LedgerStore) -> None:
    """One deploy task that committed cleanly: friction 0, nothing to propose."""
    cid = new_correlation_id()
    await ledger.append(
        LedgerEvent.create(
            cid,
            EventType.TASK_REQUESTED,
            payload={"request": {"goal": "deploy the service"}},
        )
    )
    await ledger.append(LedgerEvent.create(cid, EventType.MUTATION_COMMITTED))


async def approve(
    proposal: object, verdict: object
) -> HumanDecision:
    """A scripted human who says yes."""
    return HumanDecision(
        decision="approved",
        approver_kind="human",
        approver_id="test-approver",
        note="scripted approval",
    )


async def refuse(
    proposal: object, verdict: object
) -> HumanDecision:
    """A scripted human who says no."""
    return HumanDecision(
        decision="rejected",
        approver_kind="human",
        approver_id="test-approver",
        note="scripted refusal",
    )


async def run_cycle(
    ledger: LedgerStore,
    repo: Path,
    tmp_path: Path,
    *,
    validators: Sequence[Sequence[str]],
    approver: Approver | None = None,
    replay: Any = None,
    params: dict[str, Any] | None = None,
    allow_insecure_signing: bool = True,
    target_file: str = "playbooks.md",
    run_id: uuid.UUID | None = None,
) -> tuple[uuid.UUID, Any]:
    """Drive one guarded cycle on its own lineage; return (run id, result).

    The shared driver both doors use (the test suite here, the conformance
    runner, and the CLI's ``run_improve_pipeline``): journal the lifecycle
    rows around the pipeline exactly the way the registry's front door
    does, so a cycle driven from a test and a cycle driven from the CLI
    leave the same ledger shape. ``run_id`` re-drives an existing lineage
    (the replay tests): the lifecycle rows are idempotent per lineage, so
    the re-drive adds no duplicates to the fold.
    """
    run_id = run_id or new_correlation_id()
    journal = CallJournal(ledger, run_id)
    await journal.run_started(ImproveReflectionLoop.name, ImproveReflectionLoop.version)
    facade = ImproveFacade(
        ledger=ledger,
        repo_root=repo,
        artifact_home=tmp_path / "improve-artifacts",
        journal=journal,
        validators=validators,
        approver=approver,
        replay=replay,
        allow_insecure_signing=allow_insecure_signing,
        target_file=target_file,
    )
    task = PipelineTask(
        correlation_id=run_id,
        session_id=None,
        goal="self-improve",
        params=dict(params or {}),
    )
    result = await ImproveReflectionLoop().run(task, facade)
    await journal.run_completed(result.status.value, metrics=dict(result.metrics))
    return run_id, result


@pytest.fixture
def git_repo(tmp_path: Path) -> Path:
    """A fixture git repository at ``tmp_path/repo`` (identity configured)."""
    if shutil.which("git") is None:
        pytest.skip("git is not available on this host")
    return make_repo(tmp_path)


@pytest.fixture
async def improve_ledger(db: Database) -> LedgerStore:
    """A ledger pre-seeded with one high-friction deploy domain."""
    ledger = LedgerStore(db)
    await seed_high_friction(ledger)
    return ledger
