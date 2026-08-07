"""Shared pytest fixtures.

Every fixture is function-scoped and writes into a `tmp_path`, so tests are
fully isolated and can run in any order.
"""

from __future__ import annotations

import hashlib
import os
from collections.abc import AsyncIterator, Iterator
from pathlib import Path
from typing import ClassVar

import pytest

from paa.config import Settings, reset_settings_cache
from paa.ledger.store import LedgerStore
from paa.storage.relational.database import Database


@pytest.fixture
def paa_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    """Isolated PAA_HOME so tests never touch the developer's real state."""
    home = tmp_path / "paa_home"
    home.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("PAA_HOME", str(home))
    reset_settings_cache()
    yield home
    reset_settings_cache()


@pytest.fixture
def settings(paa_home: Path) -> Settings:
    s = Settings(home=paa_home)
    s.ensure_directories()
    return s


@pytest.fixture
async def db(tmp_path: Path) -> AsyncIterator[Database]:
    """Connected database on a throwaway file."""
    database = Database(tmp_path / "test.db")
    await database.connect()
    try:
        yield database
    finally:
        await database.close()


@pytest.fixture
async def ledger(db: Database) -> LedgerStore:
    return LedgerStore(db)


class FakeSnapshotter:
    """In-memory workspace snapshotter for recovery tests.

    Mirrors the real `paa.validation.workspace.WorkspaceSnapshot` contract
    without depending on it, so ledger tests stay independent of the
    validation package.
    """

    def __init__(self) -> None:
        self.restore_calls: list[tuple[Path, int]] = []

    def manifest(self, root: Path) -> dict[str, str]:
        out: dict[str, str] = {}
        if not root.exists():
            return out
        for path in sorted(root.rglob("*")):
            if path.is_file():
                rel = path.relative_to(root).as_posix()
                out[rel] = hashlib.sha256(path.read_bytes()).hexdigest()
        return out

    def manifest_hash(self, manifest: dict[str, str]) -> str:
        # Order-independent: sort before hashing.
        material = "\n".join(f"{k}:{v}" for k, v in sorted(manifest.items()))
        return hashlib.sha256(material.encode()).hexdigest()

    def restore(self, root: Path, manifest: dict[str, str]) -> list[str]:
        """Restore the tree so that it matches ``manifest``.

        The real implementation restores exact bytes from a backup directory;
        here a digest -> bytes registry populated by :meth:`remember` stands in.

        Everything is byte-oriented deliberately. ``write_text``/``read_text``
        would let Windows translate ``\\n`` to ``\\r\\n`` on write and back on
        read, so a file's on-disk sha256 would never equal the digest of the
        string it was written from — and the manifest would report phantom
        drift on every file in the tree.
        """
        changed: list[str] = []
        current = self.manifest(root)

        for rel in set(current) - set(manifest):
            (root / rel).unlink()
            changed.append(rel)

        for rel, digest in manifest.items():
            target = root / rel
            if current.get(rel) == digest:
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(self._content_for(digest))
            changed.append(rel)
        return sorted(changed)

    #: Digest -> bytes registry, deliberately shared across instances so a
    #: fixture can register content that a differently-constructed snapshotter
    #: later restores. Annotated ClassVar to make that sharing explicit rather
    #: than looking like an accidental mutable default.
    _contents: ClassVar[dict[str, bytes]] = {}

    def remember(self, content: str | bytes) -> str:
        """Register content so :meth:`restore` can put it back."""
        raw = content.encode("utf-8") if isinstance(content, str) else content
        digest = hashlib.sha256(raw).hexdigest()
        FakeSnapshotter._contents[digest] = raw
        return digest

    def remember_tree(self, root: Path) -> None:
        """Register every file currently under ``root``."""
        for path in root.rglob("*"):
            if path.is_file():
                self.remember(path.read_bytes())

    def _content_for(self, digest: str) -> bytes:
        return FakeSnapshotter._contents.get(digest, b"")


@pytest.fixture
def snapshotter() -> FakeSnapshotter:
    return FakeSnapshotter()


class RecordingRequeuer:
    """Captures re-queue calls made by the recovery engine."""

    def __init__(self) -> None:
        self.calls: list[dict] = []

    async def enqueue(self, stream, payload, **kwargs):
        self.calls.append({"stream": stream, "payload": payload, **kwargs})
        return payload


@pytest.fixture
def requeuer() -> RecordingRequeuer:
    return RecordingRequeuer()


@pytest.fixture(autouse=True)
def _quiet_logs(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep structlog output out of test reports unless explicitly debugging."""
    if not os.environ.get("PAA_TEST_VERBOSE"):
        import logging

        logging.disable(logging.WARNING)
