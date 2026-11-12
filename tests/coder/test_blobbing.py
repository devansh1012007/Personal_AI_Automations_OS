"""N2.6: 8 KB blobbing for ledger payloads (events contract + journal size check).

What is pinned here
-------------------
* The threshold is bytes of the canonical form: exactly 8 KB stays inline,
  one byte over is offloaded to the cold lake.
* A blobbed row pins the blob's SHA-256 and size and carries only a short
  preview; the stub itself stays well under the limit it enforces.
* Read-back is byte-identical through the ledger: a resolved payload's
  canonical bytes are exactly the bytes that were stored.
* Tampering is detected at each layer: blob bytes (store Corrupt or the
  resolver's hash/size checks), descriptor fields (size/sha validation),
  and the ledger chain (a swapped descriptor breaks its digest).
* Small payloads are unchanged: no blob is stored and the golden v1
  shapes are preserved.
* The journal applies the check to every payload it appends: an oversized
  PROPOSED/DENIED row is blobbed when a store is configured and raises
  (appending nothing) when none is.
* The production path is wired: ``CoderAgent.build``'s default journal
  carries the configured cold-lake store, so an oversized row through the
  built agent's own journal is blobbed into ``PAA_HOME`` and reads back
  sha-pinned (W2-REV-05 MAJOR-1).
"""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any

import pytest

from paa.coder.agent import CoderAgent
from paa.coder.journal import LedgerToolJournal
from paa.coder.loop import ModelTurn, ToolCtx
from paa.coder.permissions import PermissionMode
from paa.config import Settings
from paa.core.types import EventType, new_correlation_id
from paa.ledger.events import (
    BLOB_PREVIEW_CHARS,
    BLOB_THRESHOLD_BYTES,
    LedgerEvent,
    canonical_json,
    is_blobbed_payload,
    maybe_blob_payload,
    payload_requires_blob,
    payload_size_bytes,
    read_blob_bytes,
    resolve_blobbed_payload,
    store_blob_bytes,
)
from paa.ledger.replay import TaskPhase, replay
from paa.ledger.store import LedgerStore
from paa.sandbox.base import IsolationLevel
from paa.storage.coldlake import get_blob_store
from paa.storage.coldlake.cas import ContentAddressedStore
from paa.storage.coldlake.errors import Corrupt, NotFound


def _store(tmp_path: Path) -> ContentAddressedStore:
    return ContentAddressedStore(tmp_path / "cold")


def _payload_of_exact_size(target: int) -> dict[str, Any]:
    """A payload whose canonical form is exactly ``target`` bytes."""
    empty = payload_size_bytes({"v": 1, "data": ""})
    pad = target - empty
    assert pad >= 0
    return {"v": 1, "data": "x" * pad}


def _large_payload(extra: int = 2048) -> dict[str, Any]:
    return {"v": 1, "data": "y" * (BLOB_THRESHOLD_BYTES + extra)}


def _ctx(**over: Any) -> ToolCtx:
    base: dict[str, Any] = {
        "session_id": "sess-1",
        "call_id": "call-1",
        "tool": "read_file",
        "args_sha256": "a" * 64,
        "args_preview_redacted": "read_file(path=a.py)",
        "isolation_level": IsolationLevel.NONE,
    }
    base.update(over)
    return ToolCtx(**base)


class TestThreshold:
    def test_threshold_is_8kb(self) -> None:
        assert BLOB_THRESHOLD_BYTES == 8192

    def test_size_is_canonical_bytes(self) -> None:
        payload = {"v": 1, "data": "héllo"}
        expected = len(canonical_json(payload).encode("utf-8"))
        assert payload_size_bytes(payload) == expected

    def test_exactly_at_threshold_stays_inline(self, tmp_path: Path) -> None:
        payload = _payload_of_exact_size(BLOB_THRESHOLD_BYTES)
        assert payload_size_bytes(payload) == BLOB_THRESHOLD_BYTES
        assert not payload_requires_blob(payload)
        store = _store(tmp_path)
        handed = maybe_blob_payload(payload, store)
        assert handed == payload
        assert not is_blobbed_payload(handed)
        assert not any(p.is_file() for p in store.root.rglob("*"))

    def test_one_byte_over_is_blobbed(self, tmp_path: Path) -> None:
        payload = _payload_of_exact_size(BLOB_THRESHOLD_BYTES + 1)
        assert payload_size_bytes(payload) == BLOB_THRESHOLD_BYTES + 1
        assert payload_requires_blob(payload)
        store = _store(tmp_path)
        handed = maybe_blob_payload(payload, store)
        assert is_blobbed_payload(handed)
        assert handed["v"] == 1
        assert set(handed) == {"v", "blob"}


class TestMaybeResolve:
    def test_small_payload_unchanged(self, tmp_path: Path) -> None:
        payload = {"v": 1, "session_id": "s", "tool": "read_file"}
        store = _store(tmp_path)
        assert maybe_blob_payload(payload, store) == payload
        assert resolve_blobbed_payload(payload, store) == payload
        assert not is_blobbed_payload(payload)

    def test_large_payload_stub_is_small_and_pinned(self, tmp_path: Path) -> None:
        payload = _large_payload()
        store = _store(tmp_path)
        stub = maybe_blob_payload(payload, store)
        assert is_blobbed_payload(stub)
        assert payload_size_bytes(stub) <= BLOB_THRESHOLD_BYTES
        descriptor = stub["blob"]
        assert isinstance(descriptor, dict)
        data = canonical_json(payload).encode("utf-8")
        assert descriptor["sha256"] == hashlib.sha256(data).hexdigest()
        assert descriptor["size_bytes"] == len(data)
        assert descriptor["size_bytes"] > BLOB_THRESHOLD_BYTES
        assert isinstance(descriptor["preview"], str)
        assert len(descriptor["preview"]) <= BLOB_PREVIEW_CHARS
        # The preview orients; it is not the content.
        assert payload["data"] not in descriptor["preview"]

    def test_round_trip_is_byte_identical(self, tmp_path: Path) -> None:
        payload = _large_payload()
        store = _store(tmp_path)
        stub = maybe_blob_payload(payload, store)
        back = resolve_blobbed_payload(stub, store)
        assert back == payload
        assert canonical_json(back).encode("utf-8") == canonical_json(payload).encode(
            "utf-8"
        )

    def test_non_ascii_large_payload_round_trips(self, tmp_path: Path) -> None:
        payload = {"v": 1, "data": "é" * 5000}
        assert payload_requires_blob(payload)
        store = _store(tmp_path)
        back = resolve_blobbed_payload(maybe_blob_payload(payload, store), store)
        assert back == payload

    def test_blob_key_without_stub_shape_is_not_blobbed(self, tmp_path: Path) -> None:
        payload = {"v": 1, "blob": "just a string field", "tool": "bash"}
        store = _store(tmp_path)
        assert not is_blobbed_payload(payload)
        assert resolve_blobbed_payload(payload, store) == payload

    def test_malformed_stub_raises_not_silent(self, tmp_path: Path) -> None:
        store = _store(tmp_path)
        bogus = {"v": 1, "blob": {"sha256": "not-a-digest", "size_bytes": 3, "preview": "x"}}
        assert not is_blobbed_payload(bogus)
        with pytest.raises(ValueError, match="malformed descriptor"):
            resolve_blobbed_payload(bogus, store)

    def test_missing_blob_raises_not_found(self, tmp_path: Path) -> None:
        payload = _large_payload()
        store = _store(tmp_path)
        stub = maybe_blob_payload(payload, store)
        other = _store(tmp_path / "elsewhere")
        with pytest.raises(NotFound):
            resolve_blobbed_payload(stub, other)

    def test_reblobbing_is_idempotent(self, tmp_path: Path) -> None:
        payload = _large_payload()
        store = _store(tmp_path)
        stub = maybe_blob_payload(payload, store)
        assert maybe_blob_payload(stub, store) == stub


class TestTamper:
    def test_blob_bytes_tampered_raise_corrupt(self, tmp_path: Path) -> None:
        payload = _large_payload()
        store = _store(tmp_path)
        stub = maybe_blob_payload(payload, store)
        sha = stub["blob"]["sha256"]
        leaf = next(store.root.rglob(f"{sha}*"))
        leaf.write_bytes(b"corrupted bytes, not a frame")
        with pytest.raises(Corrupt):
            resolve_blobbed_payload(stub, store)

    def test_blob_bytes_replaced_raise(self, tmp_path: Path) -> None:
        """A different (valid) frame at the same address is still a mismatch."""
        payload = _large_payload()
        store = _store(tmp_path)
        stub = maybe_blob_payload(payload, store)
        sha = stub["blob"]["sha256"]
        leaf = next(store.root.rglob(f"{sha}*"))
        # Overwrite with a valid frame of different content: the store's
        # own check must still catch it, and so must the resolver's.
        fresh = _store(tmp_path / "fresh")
        fresh.put(b"completely different bytes of similar length!!")
        other_file = next(p for p in fresh.root.rglob("*") if p.is_file())
        leaf.write_bytes(other_file.read_bytes())
        with pytest.raises((Corrupt, ValueError)):
            resolve_blobbed_payload(stub, store)

    def test_descriptor_size_tamper_raises(self, tmp_path: Path) -> None:
        payload = _large_payload()
        store = _store(tmp_path)
        stub = maybe_blob_payload(payload, store)
        forged = {
            "v": 1,
            "blob": {**stub["blob"], "size_bytes": stub["blob"]["size_bytes"] + 1},
        }
        with pytest.raises(ValueError, match="size mismatch"):
            resolve_blobbed_payload(forged, store)

    async def test_descriptor_sha_substitution_breaks_chain(
        self, tmp_path: Path, ledger: LedgerStore
    ) -> None:
        payload_a = {"v": 1, "data": "a" * 9000}
        payload_b = {"v": 1, "data": "b" * 9000}
        store = _store(tmp_path)
        stub_a = maybe_blob_payload(payload_a, store)
        stub_b = maybe_blob_payload(payload_b, store)
        cid = new_correlation_id()
        sealed = await ledger.append(
            LedgerEvent.create(cid, EventType.TOOL_CALL_PROPOSED, payload=stub_a)
        )
        ok, problems = await ledger.verify_chain(cid)
        assert ok, problems
        # Swap in B's descriptor: the resolver would hash-verify B's bytes
        # (they match B's address) and return B's payload — which is why
        # the chain, not the resolver, is the guard for a coherent swap.
        swapped = sealed.model_copy(
            update={"payload": {"v": 1, "blob": stub_b["blob"]}}
        )
        assert not swapped.verify_digest()
        assert resolve_blobbed_payload(swapped.payload, store) == payload_b

    def test_non_canonical_blob_bytes_rejected(self, tmp_path: Path) -> None:
        store = _store(tmp_path)
        non_canonical = b'{"b": 1, "a": 2}'
        descriptor = store_blob_bytes(non_canonical, store)
        stub = {"v": 1, "blob": descriptor}
        assert is_blobbed_payload(stub)
        # The bytes are a valid blob of *something*, but not the canonical
        # form of the dict they parse to — only canonical bytes are
        # accepted as an original payload.
        with pytest.raises(ValueError, match="canonical"):
            resolve_blobbed_payload(stub, store)


class TestRawBytes:
    def test_large_bytes_round_trip(self, tmp_path: Path) -> None:
        data = b"result-bytes:" + b"z" * 9000
        store = _store(tmp_path)
        descriptor = store_blob_bytes(data, store)
        assert descriptor["sha256"] == hashlib.sha256(data).hexdigest()
        assert descriptor["size_bytes"] == len(data)
        assert read_blob_bytes(descriptor, store) == data

    def test_descriptor_validation_before_fetch(self, tmp_path: Path) -> None:
        store = _store(tmp_path)
        with pytest.raises(ValueError, match="malformed"):
            read_blob_bytes({"sha256": "x", "size_bytes": 1, "preview": "p"}, store)
        with pytest.raises(ValueError, match="malformed"):
            read_blob_bytes({"sha256": "a" * 64, "size_bytes": True, "preview": "p"}, store)


class TestLedgerRoundTrip:
    async def test_blobbed_event_round_trips_through_ledger(
        self, tmp_path: Path, ledger: LedgerStore
    ) -> None:
        payload = _large_payload()
        store = _store(tmp_path)
        stub = maybe_blob_payload(payload, store)
        cid = new_correlation_id()
        await ledger.append(
            LedgerEvent.create(cid, EventType.TOOL_CALL_PROPOSED, payload=stub)
        )
        rows = await ledger.read_correlation(cid)
        assert len(rows) == 1
        assert is_blobbed_payload(rows[0].payload)
        assert payload_size_bytes(rows[0].payload) <= BLOB_THRESHOLD_BYTES
        assert resolve_blobbed_payload(rows[0].payload, store) == payload
        ok, problems = await ledger.verify_chain(cid)
        assert ok, problems

    async def test_replay_never_reads_blob_unverified(
        self, tmp_path: Path, ledger: LedgerStore
    ) -> None:
        payload = _large_payload()
        store = _store(tmp_path)
        stub = maybe_blob_payload(payload, store)
        cid = new_correlation_id()
        await ledger.append(
            LedgerEvent.create(cid, EventType.TOOL_CALL_PROPOSED, payload=stub)
        )
        rows = await ledger.read_correlation(cid)
        # Replay is pure: it folds the stub (a no-op arm) and never fetches.
        projection = replay(rows)
        assert projection.phase is TaskPhase.CREATED
        assert projection.unknown_events == []
        # Only the verified resolver fetches.
        assert resolve_blobbed_payload(rows[0].payload, store) == payload

    async def test_replay_over_blobbed_and_small_rows(
        self, tmp_path: Path, ledger: LedgerStore
    ) -> None:
        store = _store(tmp_path)
        cid = new_correlation_id()
        await ledger.append(
            LedgerEvent.create(
                cid,
                EventType.TOOL_CALL_PROPOSED,
                payload=maybe_blob_payload(_large_payload(), store),
            )
        )
        await ledger.append(
            LedgerEvent.create(
                cid,
                EventType.TOOL_CALL_COMPLETED,
                payload={"v": 1, "tool": "read_file", "result_sha256": "e" * 64},
            )
        )
        rows = await ledger.read_correlation(cid)
        assert [is_blobbed_payload(r.payload) for r in rows] == [True, False]
        projection = replay(rows)
        assert projection.phase is TaskPhase.CREATED
        ok, problems = await ledger.verify_chain(cid)
        assert ok, problems


class TestJournalBlobbing:
    async def test_small_proposed_payload_unchanged(
        self, tmp_path: Path, ledger: LedgerStore
    ) -> None:
        store = _store(tmp_path)
        cid = new_correlation_id()
        journal = LedgerToolJournal(ledger, cid, blob_store=store)
        await journal.before(_ctx())
        rows = await ledger.read_correlation(cid)
        assert len(rows) == 1
        assert rows[0].payload["v"] == 1
        assert rows[0].payload["args_preview_redacted"] == "read_file(path=a.py)"
        assert "blob" not in rows[0].payload

    async def test_oversized_proposed_is_blobbed_and_round_trips(
        self, tmp_path: Path, ledger: LedgerStore
    ) -> None:
        store = _store(tmp_path)
        cid = new_correlation_id()
        journal = LedgerToolJournal(ledger, cid, blob_store=store)
        huge_preview = "x" * (BLOB_THRESHOLD_BYTES + 5000)
        await journal.before(_ctx(args_preview_redacted=huge_preview))
        rows = await ledger.read_correlation(cid)
        assert len(rows) == 1
        assert is_blobbed_payload(rows[0].payload)
        assert payload_size_bytes(rows[0].payload) <= BLOB_THRESHOLD_BYTES
        back = resolve_blobbed_payload(rows[0].payload, store)
        assert back["args_preview_redacted"] == huge_preview
        assert back["tool"] == "read_file"
        ok, problems = await ledger.verify_chain(cid)
        assert ok, problems

    async def test_oversized_denied_reason_is_blobbed(
        self, tmp_path: Path, ledger: LedgerStore
    ) -> None:
        store = _store(tmp_path)
        cid = new_correlation_id()
        journal = LedgerToolJournal(ledger, cid, blob_store=store)
        huge_reason = "denied: " + "r" * (BLOB_THRESHOLD_BYTES + 5000)
        await journal.denied(_ctx(), huge_reason)
        rows = await ledger.read_correlation(cid)
        assert is_blobbed_payload(rows[0].payload)
        back = resolve_blobbed_payload(rows[0].payload, store)
        assert back["reason"] == huge_reason

    async def test_oversized_without_store_raises_and_appends_nothing(
        self, ledger: LedgerStore
    ) -> None:
        cid = new_correlation_id()
        journal = LedgerToolJournal(ledger, cid)
        huge_preview = "x" * (BLOB_THRESHOLD_BYTES + 100)
        with pytest.raises(ValueError, match="blob store"):
            await journal.before(_ctx(args_preview_redacted=huge_preview))
        assert await ledger.read_correlation(cid) == []

    async def test_denied_without_store_small_still_works(
        self, ledger: LedgerStore
    ) -> None:
        cid = new_correlation_id()
        journal = LedgerToolJournal(ledger, cid)
        await journal.denied(_ctx(), "small reason")
        rows = await ledger.read_correlation(cid)
        assert rows[0].payload["reason"] == "small reason"
        assert "blob" not in rows[0].payload

    async def test_duplicate_blobbed_proposed_dedupes(
        self, tmp_path: Path, ledger: LedgerStore
    ) -> None:
        store = _store(tmp_path)
        cid = new_correlation_id()
        journal = LedgerToolJournal(ledger, cid, blob_store=store)
        ctx = _ctx(args_preview_redacted="x" * (BLOB_THRESHOLD_BYTES + 5000))
        await journal.before(ctx)
        await journal.before(ctx)
        rows = await ledger.read_correlation(cid)
        assert len(rows) == 1
        assert is_blobbed_payload(rows[0].payload)
        ok, problems = await ledger.verify_chain(cid)
        assert ok, problems

    async def test_completed_small_stays_inline_with_store(
        self, tmp_path: Path, ledger: LedgerStore
    ) -> None:
        store = _store(tmp_path)
        cid = new_correlation_id()
        journal = LedgerToolJournal(ledger, cid, blob_store=store)
        await journal.after(_ctx(), True, "e" * 64)
        rows = await ledger.read_correlation(cid)
        assert rows[0].payload["result_sha256"] == "e" * 64
        assert "blob" not in rows[0].payload


class _ScriptedModel:
    """Model seam double: never proposes a tool call.

    The composition tests below drive the journal the built loop holds
    directly; the model only has to exist for ``CoderAgent.build`` to
    complete, exactly as the scripted models in ``test_agent.py`` do.
    """

    async def propose(
        self, window: list[dict[str, Any]], tools: list[dict[str, Any]]
    ) -> ModelTurn:
        return ModelTurn(text="nothing to do")


class TestProdComposition:
    """The production path journals with a real, configured blob store.

    W2-REV-05 MAJOR-1: ``CoderAgent.build``'s default journal carried no
    store, so an oversized row failed the tool call closed. These tests
    build the real composition (Runtime, loop, default journal) and drive
    the very journal object the loop holds — no test double in the wire.
    """

    @pytest.fixture
    def prod_settings(self, tmp_path: Path) -> Settings:
        settings = Settings(home=tmp_path / "paa_home")
        settings.ensure_directories()
        return settings

    async def test_built_agent_journal_carries_configured_store(
        self, prod_settings: Settings
    ) -> None:
        agent = await CoderAgent.build(
            prod_settings, model=_ScriptedModel(), mode=PermissionMode.BYPASS
        )
        try:
            # DECISION: white-box reach to the journal the loop holds. The
            # composition assertion needs the object itself, not a rebuild;
            # a rebuild would only prove a second journal could blob.
            journal = agent.loop._journal
            assert isinstance(journal, LedgerToolJournal)
            assert journal._blob_store is not None
            store = journal._blob_store
            assert isinstance(store, ContentAddressedStore)
            assert store.root == prod_settings.storage.cold_lake_path
        finally:
            await agent.close()

    async def test_oversized_row_blobs_and_round_trips_through_prod_journal(
        self, prod_settings: Settings
    ) -> None:
        cid = new_correlation_id()
        agent = await CoderAgent.build(
            prod_settings,
            model=_ScriptedModel(),
            mode=PermissionMode.BYPASS,
            correlation_id=cid,
        )
        try:
            journal = agent.loop._journal
            assert isinstance(journal, LedgerToolJournal)
            huge_preview = "x" * (BLOB_THRESHOLD_BYTES + 5000)
            await journal.before(_ctx(args_preview_redacted=huge_preview))
            rows = await agent.runtime.ledger.read_correlation(cid)
            assert len(rows) == 1
            assert is_blobbed_payload(rows[0].payload)
            assert payload_size_bytes(rows[0].payload) <= BLOB_THRESHOLD_BYTES
            # The blob lives in the PAA_HOME cold lake and reads back
            # byte-identical through the configured store.
            store = get_blob_store(prod_settings.storage)
            back = resolve_blobbed_payload(rows[0].payload, store)
            assert back["args_preview_redacted"] == huge_preview
            sha = rows[0].payload["blob"]["sha256"]
            assert isinstance(store, ContentAddressedStore)
            assert list(store.root.rglob(f"{sha}*"))
            assert (
                hashlib.sha256(canonical_json(back).encode("utf-8")).hexdigest()
                == sha
            )
            ok, problems = await agent.runtime.ledger.verify_chain(cid)
            assert ok, problems
        finally:
            await agent.close()

    async def test_caller_supplied_journal_still_wins(
        self, prod_settings: Settings
    ) -> None:
        """The ``journal=`` seam is untouched: an explicit journal is used
        verbatim, not rebuilt with the default store."""
        first = await CoderAgent.build(
            prod_settings, model=_ScriptedModel(), mode=PermissionMode.BYPASS
        )
        try:
            store = _store(prod_settings.home / "custom")
            cid = new_correlation_id()
            custom = LedgerToolJournal(first.runtime.ledger, cid, blob_store=store)
            second = await CoderAgent.build(
                prod_settings,
                model=_ScriptedModel(),
                mode=PermissionMode.BYPASS,
                correlation_id=cid,
                journal=custom,
            )
            try:
                assert second.loop._journal is custom
            finally:
                await second.close()
        finally:
            await first.close()
