"""Cold lake tests — content-addressed storage, signal intake, artifact archival.

Everything here is byte-oriented on purpose. ``Path.write_text``/``read_text``
translate ``\\n`` to ``\\r\\n`` on Windows, so a file's on-disk sha256 would not
match the digest of the string it was written from, and every content-addressing
assertion would fail for a reason that has nothing to do with the code under
test. ``write_bytes``/``read_bytes`` throughout.
"""

from __future__ import annotations

import hashlib
import io
import json
from pathlib import Path

import pytest
import zstandard

from paa.core.errors import StorageError
from paa.storage.coldlake import (
    ArtifactArchive,
    BlobRef,
    ContentAddressedStore,
    SignalRepository,
)
from paa.storage.relational.database import Database

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

#: Deliberately tiny so "larger than one chunk" costs kilobytes, not megabytes.
CHUNK = 1024


@pytest.fixture
def cas(tmp_path: Path) -> ContentAddressedStore:
    return ContentAddressedStore(tmp_path / "cold_lake", zstd_level=3, chunk_size=CHUNK)


@pytest.fixture
def signals(db: Database, cas: ContentAddressedStore) -> SignalRepository:
    # A 64-byte threshold keeps the "oversized payload" fixtures readable while
    # exercising exactly the same offload path a 40 MB attachment would take.
    return SignalRepository(db, cas, inline_threshold_bytes=64)


@pytest.fixture
def artifacts(db: Database, cas: ContentAddressedStore) -> ArtifactArchive:
    return ArtifactArchive(db, cas)


class CountingReader(io.RawIOBase):
    """A stream that records how large each read request was.

    This is how "streams in chunks" gets *proved* rather than asserted about:
    if the implementation ever slurped the whole payload, ``max_read`` would
    equal the payload size instead of the chunk size.
    """

    def __init__(self, payload: bytes) -> None:
        self._buffer = io.BytesIO(payload)
        self.reads: list[int] = []

    def read(self, size: int = -1) -> bytes:  # type: ignore[override]
        chunk = self._buffer.read(size)
        self.reads.append(size)
        return chunk

    @property
    def max_read(self) -> int:
        return max(self.reads) if self.reads else 0


def blob_files(cas: ContentAddressedStore) -> list[Path]:
    """Every file actually sitting in the blob tree."""
    return sorted(p for p in (cas.root / "blobs").rglob("*") if p.is_file())


def corrupt_in_place(path: Path, replacement: bytes = b"tampered content") -> None:
    """Replace a blob's bytes with a *valid* zstd frame of different content.

    Writing garbage would be caught by the decompressor, which proves nothing
    about hash verification. A well-formed frame decompresses cleanly and can
    only be caught by checking the digest — which is the guarantee under test.
    """
    path.write_bytes(zstandard.ZstdCompressor(level=3).compress(replacement))


# ---------------------------------------------------------------------------
# CAS — round-trip and addressing
# ---------------------------------------------------------------------------


def test_put_and_get_roundtrip(cas: ContentAddressedStore) -> None:
    payload = b'{"channel": "email", "body": "hello"}'
    ref = cas.put(payload)

    assert ref.sha256 == hashlib.sha256(payload).hexdigest()
    assert ref.size_bytes == len(payload)
    assert ref.uri == f"cas://{ref.sha256}"
    assert cas.get(ref.sha256) == payload
    assert cas.exists(ref.sha256)


def test_hash_is_taken_over_raw_not_compressed_bytes(tmp_path: Path) -> None:
    """A blob's address must not depend on the zstd level that wrote it.

    Otherwise re-compressing the archive would rename every blob in it, and
    every ``blob_uri`` in the database would dangle.
    """
    payload = b"x" * 4096
    low = ContentAddressedStore(tmp_path / "low", zstd_level=1)
    high = ContentAddressedStore(tmp_path / "high", zstd_level=19)

    assert low.put(payload).sha256 == high.put(payload).sha256


def test_identical_content_deduplicates(cas: ContentAddressedStore) -> None:
    """Content addressing means dedup is free — same bytes, one blob, no bookkeeping."""
    payload = b"the same attachment on twenty different emails"

    first = cas.put(payload)
    second = cas.put(payload)

    assert first.sha256 == second.sha256
    assert len(blob_files(cas)) == 1
    assert cas.get(first.sha256) == payload


def test_different_content_does_not_collide(cas: ContentAddressedStore) -> None:
    a = cas.put(b"payload one")
    b = cas.put(b"payload two")

    assert a.sha256 != b.sha256
    assert len(blob_files(cas)) == 2
    assert cas.get(a.sha256) == b"payload one"
    assert cas.get(b.sha256) == b"payload two"


def test_blob_path_is_fanned_out(cas: ContentAddressedStore) -> None:
    """Two levels of fan-out keep any one directory small on NTFS."""
    ref = cas.put(b"anything")
    stored = blob_files(cas)[0]

    assert stored.parent.name == ref.sha256[2:4]
    assert stored.parent.parent.name == ref.sha256[:2]
    assert stored.name == f"{ref.sha256}.zst"


def test_uncompressed_blobs_roundtrip(cas: ContentAddressedStore) -> None:
    payload = b"already-compressed bytes gain nothing from zstd"
    ref = cas.put(payload, compress=False)

    assert blob_files(cas)[0].suffix == ".bin"
    assert cas.get(ref.sha256) == payload
    assert cas.stat(ref.sha256) is not None


# ---------------------------------------------------------------------------
# CAS — corruption detection
# ---------------------------------------------------------------------------


def test_corrupted_blob_fails_hash_verification(cas: ContentAddressedStore) -> None:
    """The worst failure mode this layer has: silent bit-rot in the archive."""
    ref = cas.put(b"the original, authoritative payload")
    corrupt_in_place(blob_files(cas)[0])

    with pytest.raises(StorageError) as excinfo:
        cas.get(ref.sha256)

    assert "hash verification" in str(excinfo.value)
    assert excinfo.value.substrate == "cas"
    assert excinfo.value.details["expected"] == ref.sha256


def test_unreadable_frame_is_reported_as_a_storage_error(cas: ContentAddressedStore) -> None:
    """Truncation and scrambling surface as StorageError, not a codec exception."""
    ref = cas.put(b"a payload that will be mangled")
    blob_files(cas)[0].write_bytes(b"not a zstd frame at all")

    with pytest.raises(StorageError):
        cas.get(ref.sha256)


def test_streamed_read_detects_corruption_at_the_end(cas: ContentAddressedStore) -> None:
    """Streaming cannot verify up front, but it must still refuse to stay silent."""
    payload = b"y" * (CHUNK * 4)
    ref = cas.put(payload)
    corrupt_in_place(blob_files(cas)[0], b"z" * (CHUNK * 4))

    with pytest.raises(StorageError, match="hash verification"):
        b"".join(cas.get_stream(ref.sha256))


def test_missing_blob_raises(cas: ContentAddressedStore) -> None:
    absent = hashlib.sha256(b"never stored").hexdigest()

    assert not cas.exists(absent)
    assert cas.stat(absent) is None
    with pytest.raises(StorageError, match="not found"):
        cas.get(absent)


def test_digest_shaped_like_a_path_is_refused(cas: ContentAddressedStore) -> None:
    """A digest becomes directory components, so traversal must die at the door."""
    for hostile in ("../../etc/passwd", "..", "a" * 63, "g" * 64, ""):
        with pytest.raises(StorageError):
            cas.get(hostile)


# ---------------------------------------------------------------------------
# CAS — streaming
# ---------------------------------------------------------------------------


def test_put_stream_reads_in_chunks(cas: ContentAddressedStore) -> None:
    """Never load a whole payload: the source is only ever asked for one chunk."""
    payload = b"".join(bytes([i % 256]) * CHUNK for i in range(12))  # 12 KiB, 12x chunk
    reader = CountingReader(payload)

    ref = cas.put_stream(reader)  # type: ignore[arg-type]

    assert ref.sha256 == hashlib.sha256(payload).hexdigest()
    assert ref.size_bytes == len(payload)
    assert reader.max_read == CHUNK, "payload was slurped instead of streamed"
    assert len(reader.reads) > 1
    assert cas.get(ref.sha256) == payload


def test_get_stream_yields_multiple_chunks(cas: ContentAddressedStore) -> None:
    payload = b"".join(bytes([i % 256]) * CHUNK for i in range(8))
    ref = cas.put(payload)

    chunks = list(cas.get_stream(ref.sha256))

    assert len(chunks) > 1, "a payload of 8 chunks came back in one piece"
    assert max(len(c) for c in chunks) <= CHUNK
    assert b"".join(chunks) == payload


def test_put_stream_deduplicates_against_put(cas: ContentAddressedStore) -> None:
    """Both write paths must agree on an address, or dedup silently stops working."""
    payload = b"identical content, two different code paths" * 100

    inline = cas.put(payload)
    streamed = cas.put_stream(io.BytesIO(payload))

    assert inline.sha256 == streamed.sha256
    assert len(blob_files(cas)) == 1


def test_put_stream_size_hint_is_checked(cas: ContentAddressedStore) -> None:
    """A wrong hint is a caller bug; storing the blob anyway would bake it in."""
    with pytest.raises(StorageError, match="declared size"):
        cas.put_stream(io.BytesIO(b"twelve bytes"), size=999)


def test_put_stream_leaves_no_staging_files(cas: ContentAddressedStore) -> None:
    cas.put_stream(io.BytesIO(b"payload"), size=7)
    with pytest.raises(StorageError):
        cas.put_stream(io.BytesIO(b"payload"), size=999)

    assert list((cas.root / "staging").glob("*.part")) == []


# ---------------------------------------------------------------------------
# CAS — metadata
# ---------------------------------------------------------------------------


def test_stat_reports_both_sizes(cas: ContentAddressedStore) -> None:
    payload = b"a" * 8192  # compresses hard, so the two sizes clearly differ
    ref = cas.put(payload)

    stat = cas.stat(ref.sha256)

    assert stat is not None
    assert stat.size_bytes == 8192
    assert stat.compressed_bytes < stat.size_bytes
    assert stat.compression_ratio < 1.0
    assert stat.uri == ref.uri


def test_stat_recovers_size_when_the_frame_declares_none(cas: ContentAddressedStore) -> None:
    """A streamed write without a hint has no declared size; stat must still be right.

    This is the fallback path that counts by decompressing, and it is the one
    that silently returns nonsense if it regresses.
    """
    payload = b"b" * (CHUNK * 3)
    ref = cas.put_stream(io.BytesIO(payload))  # no size hint

    stat = cas.stat(ref.sha256)

    assert stat is not None
    assert stat.size_bytes == len(payload)


def test_iter_blobs_and_total_bytes(cas: ContentAddressedStore) -> None:
    refs = [cas.put(f"payload number {i}".encode()) for i in range(5)]

    listed = list(cas.iter_blobs())

    assert {r.sha256 for r in listed} == {r.sha256 for r in refs}
    assert [r.sha256 for r in listed] == sorted(r.sha256 for r in listed)
    # total_bytes is the on-disk (compressed) footprint.
    assert cas.total_bytes() == sum(r.compressed_bytes for r in listed)


def test_delete_removes_a_blob(cas: ContentAddressedStore) -> None:
    ref = cas.put(b"transient payload")

    assert cas.delete(ref.sha256) is True
    assert not cas.exists(ref.sha256)
    assert cas.delete(ref.sha256) is False  # idempotent, not an error


def test_blobref_uri_parsing() -> None:
    digest = hashlib.sha256(b"x").hexdigest()

    assert BlobRef.parse_uri(f"cas://{digest}") == digest
    for hostile in ("http://example.com/x", f"cas://{digest[:10]}", digest):
        with pytest.raises(StorageError):
            BlobRef.parse_uri(hostile)


# ---------------------------------------------------------------------------
# Signals — intake and idempotency
# ---------------------------------------------------------------------------


async def test_record_and_get_roundtrip(signals: SignalRepository) -> None:
    signal = await signals.record("email", {"subject": "hello", "from": "a@b.c"}, "msg-1")

    fetched = await signals.get(signal.id)

    assert fetched is not None
    assert fetched.channel == "email"
    assert fetched.external_id == "msg-1"
    assert fetched.sync_status == "unprocessed"
    assert signals.payload_json(fetched) == {"subject": "hello", "from": "a@b.c"}
    assert signals.verify_payload(fetched)


async def test_record_is_idempotent_on_channel_and_external_id(
    signals: SignalRepository, db: Database
) -> None:
    """A webhook retry and a poller replay must not file the same event twice."""
    first = await signals.record("email", {"n": 1}, "msg-1")
    second = await signals.record("email", {"n": 2}, "msg-1")

    assert second.id == first.id
    # The incumbent wins: the payload is *not* overwritten by the replay.
    assert signals.payload_json(second) == {"n": 1}
    assert await db.fetch_value("SELECT COUNT(*) FROM cold_lake_signals") == 1


async def test_same_external_id_on_another_channel_is_distinct(
    signals: SignalRepository,
) -> None:
    """The key is (channel, external_id) — ids are only unique within a channel."""
    email = await signals.record("email", {"n": 1}, "shared-id")
    slack = await signals.record("slack", {"n": 2}, "shared-id")

    assert email.id != slack.id


async def test_signals_without_an_external_id_are_never_deduplicated(
    signals: SignalRepository, db: Database
) -> None:
    """No key means no claim of sameness; two identical payloads are two events."""
    a = await signals.record("cron", {"tick": True})
    b = await signals.record("cron", {"tick": True})

    assert a.id != b.id
    assert await db.fetch_value("SELECT COUNT(*) FROM cold_lake_signals") == 2


async def test_record_rejects_non_json_payloads(signals: SignalRepository) -> None:
    with pytest.raises(StorageError, match="not valid JSON"):
        await signals.record("email", "this is not json")
    with pytest.raises(StorageError, match="UTF-8"):
        await signals.record("email", b"\xff\xfe binary")


async def test_string_payloads_are_stored_verbatim(signals: SignalRepository) -> None:
    """Raw history means byte-identical: no re-encoding, no key reordering."""
    raw = '{"z": 1, "a": 2}'
    signal = await signals.record("webhook", raw)

    assert signal.raw_payload == raw


# ---------------------------------------------------------------------------
# Signals — payload offload
# ---------------------------------------------------------------------------


async def test_oversized_payload_goes_to_the_cas(
    signals: SignalRepository, cas: ContentAddressedStore, db: Database
) -> None:
    """A big payload must leave the row, or every unprocessed-poll drags it along."""
    body = "x" * 5000
    signal = await signals.record("email", {"body": body}, "big-1")

    assert signal.is_offloaded
    assert signal.blob_uri is not None
    # The row keeps a pointer, not the payload.
    assert len(signal.raw_payload) < 200
    assert body not in signal.raw_payload
    assert json.loads(signal.raw_payload)["_cas_uri"] == signal.blob_uri

    stored_row = await db.fetch_one(
        "SELECT raw_payload, blob_uri FROM cold_lake_signals WHERE id = ?", (signal.id,)
    )
    assert stored_row is not None
    assert body not in stored_row["raw_payload"]
    assert stored_row["blob_uri"] == signal.blob_uri

    # And the real payload is intact behind the pointer.
    assert signals.payload_json(signal) == {"body": body}
    assert cas.exists(BlobRef.parse_uri(signal.blob_uri))


async def test_content_hash_is_the_cas_address(signals: SignalRepository) -> None:
    """One value identifies the payload in the row and in the blob store."""
    signal = await signals.record("email", {"body": "y" * 5000}, "big-2")

    assert signal.blob_uri is not None
    assert BlobRef.parse_uri(signal.blob_uri) == signal.content_hash


async def test_small_payloads_stay_inline(signals: SignalRepository) -> None:
    signal = await signals.record("email", {"n": 1}, "small-1")

    assert not signal.is_offloaded
    assert signal.blob_uri is None
    assert signals.payload_json(signal) == {"n": 1}


async def test_identical_oversized_payloads_share_one_blob(
    signals: SignalRepository, cas: ContentAddressedStore
) -> None:
    body = {"body": "z" * 5000}
    first = await signals.record("email", body, "dup-1")
    second = await signals.record("email", body, "dup-2")

    assert first.id != second.id
    assert first.blob_uri == second.blob_uri
    assert len(blob_files(cas)) == 1


# ---------------------------------------------------------------------------
# Signals — lifecycle
# ---------------------------------------------------------------------------


async def test_claim_unprocessed_is_exclusive(signals: SignalRepository) -> None:
    """Two pollers must never both believe they own a signal."""
    for i in range(3):
        await signals.record("email", {"n": i}, f"msg-{i}")

    claimed = await signals.claim_unprocessed(limit=2)
    remaining = await signals.claim_unprocessed(limit=10)

    assert len(claimed) == 2
    assert all(s.sync_status == "processing" for s in claimed)
    assert len(remaining) == 1
    assert {s.id for s in claimed}.isdisjoint({s.id for s in remaining})
    assert await signals.claim_unprocessed(limit=10) == []


async def test_claim_takes_the_oldest_first(signals: SignalRepository) -> None:
    ordered = [await signals.record("email", {"n": i}, f"msg-{i}") for i in range(3)]

    claimed = await signals.claim_unprocessed(limit=3)

    assert [s.id for s in claimed] == [s.id for s in ordered]


async def test_mark_processed(signals: SignalRepository) -> None:
    signal = await signals.record("email", {"n": 1}, "msg-1")
    await signals.claim_unprocessed(limit=1)

    assert await signals.mark_processed(signal.id) is True

    fetched = await signals.get(signal.id)
    assert fetched is not None
    assert fetched.sync_status == "processed"
    assert fetched.processed_at is not None


async def test_mark_malformed_keeps_the_payload_and_the_reason(
    signals: SignalRepository,
) -> None:
    """A malformed signal is exactly the input a future parser fix needs."""
    signal = await signals.record("email", {"n": 1}, "msg-1")

    assert await signals.mark_malformed(signal.id, "no subject field") is True

    fetched = await signals.get(signal.id)
    assert fetched is not None
    assert fetched.sync_status == "malformed"
    assert fetched.error_detail == "no subject field"
    assert signals.payload_json(fetched) == {"n": 1}


async def test_release_returns_a_claim_to_the_pool(signals: SignalRepository) -> None:
    signal = await signals.record("email", {"n": 1}, "msg-1")
    await signals.claim_unprocessed(limit=1)

    assert await signals.release(signal.id) is True
    assert [s.id for s in await signals.claim_unprocessed(limit=1)] == [signal.id]


async def test_status_transitions_on_a_missing_signal_report_no_change(
    signals: SignalRepository,
) -> None:
    assert await signals.mark_processed("nonexistent") is False
    assert await signals.mark_malformed("nonexistent", "boom") is False


async def test_iter_by_channel_filters_and_orders(signals: SignalRepository) -> None:
    for i in range(3):
        await signals.record("email", {"n": i}, f"email-{i}")
    await signals.record("slack", {"n": 99}, "slack-0")
    await signals.mark_processed((await signals.get_by_external_id("email", "email-0")).id)  # type: ignore[union-attr]

    everything = [s async for s in signals.iter_by_channel("email")]
    processed = [s async for s in signals.iter_by_channel("email", status="processed")]
    oldest_first = [s async for s in signals.iter_by_channel("email", newest_first=False)]

    assert len(everything) == 3
    assert all(s.channel == "email" for s in everything)
    assert [s.external_id for s in processed] == ["email-0"]
    assert [s.id for s in oldest_first] == [s.id for s in reversed(everything)]


async def test_count_by_status(signals: SignalRepository) -> None:
    for i in range(3):
        await signals.record("email", {"n": i}, f"msg-{i}")
    await signals.claim_unprocessed(limit=1)

    counts = await signals.count_by_status()

    assert counts == {"processing": 1, "unprocessed": 2}


# ---------------------------------------------------------------------------
# Artifacts
# ---------------------------------------------------------------------------


@pytest.fixture
def host_file(tmp_path: Path) -> Path:
    """A file on disk. write_bytes, never write_text — see the module docstring."""
    path = tmp_path / "workspace" / "config.yaml"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"threshold: 0.85\nmode: SUPERVISED\n")
    return path


async def test_archive_records_content_and_location(
    artifacts: ArtifactArchive, host_file: Path
) -> None:
    artifact = await artifacts.archive(host_file, correlation_id="corr-1")

    expected = hashlib.sha256(host_file.read_bytes()).hexdigest()
    assert artifact.sha256_checksum == expected
    assert artifact.size_bytes == len(host_file.read_bytes())
    assert artifact.absolute_host_path == str(host_file.resolve())
    assert artifact.correlation_id == "corr-1"
    assert artifact.blob_uri == f"cas://{expected}"
    assert artifact.virtual_uri.startswith("paa://artifacts/")


async def test_retrieve_returns_the_original_bytes(
    artifacts: ArtifactArchive, host_file: Path
) -> None:
    artifact = await artifacts.archive(host_file)

    assert await artifacts.retrieve(artifact.id) == host_file.read_bytes()


async def test_lookup_by_virtual_uri_and_checksum(
    artifacts: ArtifactArchive, host_file: Path
) -> None:
    artifact = await artifacts.archive(host_file)

    by_uri = await artifacts.get_by_uri(artifact.virtual_uri)
    by_checksum = await artifacts.get_by_checksum(artifact.sha256_checksum)

    assert by_uri is not None
    assert by_uri.id == artifact.id
    assert [a.id for a in by_checksum] == [artifact.id]
    assert await artifacts.get_by_uri("paa://artifacts/nope/x") is None
    assert await artifacts.get_by_checksum("0" * 64) == []


async def test_archiving_the_same_file_twice_is_idempotent(
    artifacts: ArtifactArchive, host_file: Path, db: Database
) -> None:
    """Same bytes, same name, same virtual URI — one archival act, not two rows."""
    first = await artifacts.archive(host_file)
    second = await artifacts.archive(host_file)

    assert second.id == first.id
    assert await db.fetch_value("SELECT COUNT(*) FROM cold_lake_artifacts_archive") == 1


async def test_one_blob_can_back_many_archival_acts(
    artifacts: ArtifactArchive, cas: ContentAddressedStore, tmp_path: Path
) -> None:
    """Identical content archived from two paths dedupes in the CAS, not in the table."""
    content = b"the same attachment, twice"
    first_path = tmp_path / "a.bin"
    second_path = tmp_path / "b.bin"
    first_path.write_bytes(content)
    second_path.write_bytes(content)

    first = await artifacts.archive(first_path)
    second = await artifacts.archive(second_path)

    assert first.id != second.id
    assert first.sha256_checksum == second.sha256_checksum
    assert len(blob_files(cas)) == 1
    assert len(await artifacts.get_by_checksum(first.sha256_checksum)) == 2


async def test_conflicting_content_under_one_virtual_uri_is_refused(
    artifacts: ArtifactArchive, tmp_path: Path
) -> None:
    """Two different files cannot claim one name without one of them being lost."""
    first = tmp_path / "one.bin"
    second = tmp_path / "two.bin"
    first.write_bytes(b"original content")
    second.write_bytes(b"entirely different content")

    await artifacts.archive(first, virtual_uri="paa://artifacts/pinned/report.pdf")

    with pytest.raises(StorageError, match="different content"):
        await artifacts.archive(second, virtual_uri="paa://artifacts/pinned/report.pdf")


async def test_verify_accepts_an_intact_artifact(
    artifacts: ArtifactArchive, host_file: Path
) -> None:
    artifact = await artifacts.archive(host_file)

    assert await artifacts.verify(artifact.id) is True


async def test_verify_detects_tampering(
    artifacts: ArtifactArchive, cas: ContentAddressedStore, host_file: Path
) -> None:
    """The health check that makes the archive trustworthy rather than merely durable."""
    artifact = await artifacts.archive(host_file)
    corrupt_in_place(blob_files(cas)[0])

    assert await artifacts.verify(artifact.id) is False


async def test_verify_reports_a_missing_blob_without_raising(
    artifacts: ArtifactArchive, cas: ContentAddressedStore, host_file: Path
) -> None:
    """A sweep over many artifacts needs a report, not a stack trace on the first bad one."""
    artifact = await artifacts.archive(host_file)
    cas.delete(artifact.sha256_checksum)

    assert await artifacts.verify(artifact.id) is False


async def test_restore_writes_the_file_back(
    artifacts: ArtifactArchive, host_file: Path, tmp_path: Path
) -> None:
    artifact = await artifacts.archive(host_file)
    destination = tmp_path / "restored" / "config.yaml"

    written = await artifacts.restore(artifact.id, destination)

    assert written == destination
    assert destination.read_bytes() == host_file.read_bytes()


async def test_retrieve_stream_chunks_a_large_artifact(
    artifacts: ArtifactArchive, tmp_path: Path
) -> None:
    payload = b"".join(bytes([i % 256]) * CHUNK for i in range(6))
    big = tmp_path / "big.bin"
    big.write_bytes(payload)
    artifact = await artifacts.archive(big)

    chunks = list(await artifacts.retrieve_stream(artifact.id))

    assert len(chunks) > 1
    assert b"".join(chunks) == payload


async def test_archiving_a_missing_file_is_refused(artifacts: ArtifactArchive, tmp_path: Path) -> None:
    with pytest.raises(StorageError, match="not a readable file"):
        await artifacts.archive(tmp_path / "does-not-exist.txt")
    with pytest.raises(StorageError, match="not a readable file"):
        await artifacts.archive(tmp_path)  # a directory is not a file


async def test_operations_on_an_unknown_artifact_raise(artifacts: ArtifactArchive) -> None:
    assert await artifacts.get("nonexistent") is None
    with pytest.raises(StorageError, match="not found"):
        await artifacts.retrieve("nonexistent")
    with pytest.raises(StorageError, match="not found"):
        await artifacts.verify("nonexistent")


async def test_artifacts_link_to_signals_and_correlations(
    artifacts: ArtifactArchive, signals: SignalRepository, host_file: Path
) -> None:
    """The forensic join: this file came in on that signal, during that task."""
    signal = await signals.record("email", {"subject": "invoice"}, "msg-1")
    artifact = await artifacts.archive(
        host_file, signal_id=signal.id, correlation_id="corr-7"
    )

    for_correlation = await artifacts.list_for_correlation("corr-7")

    assert [a.id for a in for_correlation] == [artifact.id]
    assert for_correlation[0].signal_id == signal.id
