"""The flight record's own properties (C7.3): chain, crash markers, tamper.

The record is the artifact every other recorder surface stands on, so
its guarantees are pinned here directly, not only through the proxy and
core suites: the hash chain links every event to its predecessor, a
tampered line breaks verification at that line, a torn write is
reported as a torn tail, a record whose writer died is an honest
``started`` crash marker, and a second writer cannot splice into
another's chain.
"""

from __future__ import annotations

import json
from itertools import pairwise
from pathlib import Path

import pytest

from paa.recorder.record import (
    EVENT_KINDS,
    GENESIS,
    RECORD_SCHEMA,
    RecordError,
    RunRecord,
    verify_record,
)


def _create(tmp_path: Path, run_id: str = "r1") -> RunRecord:
    return RunRecord.create(
        tmp_path / "record" / run_id,
        run_id=run_id,
        harness=("fake-harness", "--flag"),
        workspace=tmp_path,
        policy={"mode": "ask", "allow": [], "deny": [], "ask": []},
        honesty={"mediated": "proxy calls", "observed": "everything else"},
        recorder_version="paa-recorder/test",
    )


class TestTheChain:
    def test_event_zero_carries_the_genesis_word(self, tmp_path: Path) -> None:
        record = _create(tmp_path)
        events = record.events()
        assert events[0]["prev"] == GENESIS
        assert events[0]["kind"] == "run_started"
        assert events[0]["schema"] == RECORD_SCHEMA

    def test_every_event_links_to_its_predecessor(self, tmp_path: Path) -> None:
        record = _create(tmp_path)
        record.append("policy_config", {"mode": "ask"})
        record.append("snapshot_before", {"files": 3})
        events = record.events()
        assert [e["seq"] for e in events] == [0, 1, 2]
        for previous, current in pairwise(events):
            assert current["prev"] == previous["sha256"]

    def test_the_timestamp_is_inside_the_hash(self, tmp_path: Path) -> None:
        """The chain binds *when*, not just *what*.

        The attest bundle is a pure function of its inputs and pins
        same-inputs-same-root determinism; a flight record is a journal
        of real events at real times, so its honest counterpart property
        is that the timestamp is covered by the digest — backdating an
        event is tampering, and verification says so.
        """
        record = _create(tmp_path)
        record.append("policy_config", {"mode": "ask"})
        lines = (record.run_dir / "record.jsonl").read_text().splitlines()
        event = json.loads(lines[1])
        event["at"] = "2020-01-01T00:00:00+00:00"
        lines[1] = json.dumps(event, separators=(",", ":"), sort_keys=True)
        (record.run_dir / "record.jsonl").write_text("\n".join(lines) + "\n")
        verdict = verify_record(record.run_dir)
        assert not verdict.chain_ok
        assert any("digest does not match" in p for p in verdict.problems)

    def test_finalise_writes_the_last_word_and_the_final_summary(
        self, tmp_path: Path
    ) -> None:
        record = _create(tmp_path)
        record.finalise(extra={"classification": "clean_exit"})
        summary = record.summary()
        assert summary["phase"] == "final"
        assert summary["chain_root"] == record.chain_root
        assert summary["events"] == len(record.events())
        assert record.events()[-1]["kind"] == "record_finalised"
        assert record.events()[-1]["data"]["classification"] == "clean_exit"

    def test_the_summary_cannot_lie_about_the_chain_root(self, tmp_path: Path) -> None:
        record = _create(tmp_path)
        record.finalise()
        summary = json.loads((tmp_path / "record" / "r1" / "record.json").read_text())
        summary["chain_root"] = "0" * 64
        (tmp_path / "record" / "r1" / "record.json").write_text(json.dumps(summary))
        verdict = verify_record(tmp_path / "record" / "r1")
        assert not verdict.ok
        assert not verdict.summary_ok
        assert any("chain_root" in p for p in verdict.problems)


class TestTamperDetection:
    def _final(self, tmp_path: Path) -> Path:
        record = _create(tmp_path)
        record.append("policy_config", {"mode": "ask"})
        record.append("snapshot_before", {"files": 1})
        record.finalise()
        return tmp_path / "record" / "r1"

    def test_a_substituted_payload_breaks_the_chain_at_that_line(
        self, tmp_path: Path
    ) -> None:
        run_dir = self._final(tmp_path)
        lines = (run_dir / "record.jsonl").read_text().splitlines()
        event = json.loads(lines[1])
        event["data"]["mode"] = "bypass"
        lines[1] = json.dumps(event, separators=(",", ":"), sort_keys=True)
        (run_dir / "record.jsonl").write_text("\n".join(lines) + "\n")
        verdict = verify_record(run_dir)
        assert not verdict.chain_ok
        assert any("digest does not match" in p for p in verdict.problems)

    def test_a_truncated_tail_breaks_the_chain(self, tmp_path: Path) -> None:
        run_dir = self._final(tmp_path)
        raw = (run_dir / "record.jsonl").read_text().splitlines()
        (run_dir / "record.jsonl").write_text("\n".join(raw[:-1]) + "\n")
        verdict = verify_record(run_dir)
        assert not verdict.ok
        # The summary still says the record had the final event: the
        # count mismatch is named, not guessed.
        assert any("event count" in p for p in verdict.problems)

    def test_a_reordered_sequence_is_named_as_a_sequence_defect(
        self, tmp_path: Path
    ) -> None:
        run_dir = self._final(tmp_path)
        lines = (run_dir / "record.jsonl").read_text().splitlines()
        lines[1], lines[2] = lines[2], lines[1]
        (run_dir / "record.jsonl").write_text("\n".join(lines) + "\n")
        verdict = verify_record(run_dir)
        assert not verdict.seq_ok or not verdict.chain_ok


class TestCrashDiscipline:
    def test_a_writer_that_died_leaves_the_started_marker(self, tmp_path: Path) -> None:
        """The honest crash: every promised event on disk, never finalised."""
        record = _create(tmp_path)
        record.append("policy_config", {"mode": "ask"})
        # ... and then the recorder dies. No finalise.
        verdict = verify_record(tmp_path / "record" / "r1")
        assert verdict.chain_ok
        assert verdict.phase == "started"
        assert not verdict.ok
        assert any("never finalised" in p for p in verdict.problems)

    def test_a_torn_write_is_a_torn_tail_not_silence(self, tmp_path: Path) -> None:
        _record = _create(tmp_path)
        _record.finalise()
        run_dir = tmp_path / "record" / "r1"
        with (run_dir / "record.jsonl").open("a") as handle:
            handle.write('{"schema": "paa-record/1", "seq": 99, "kind": "tool_')
        verdict = verify_record(run_dir)
        assert verdict.torn_tail
        assert not verdict.ok
        assert any("torn write" in p for p in verdict.problems)

    def test_appending_onto_a_torn_tail_is_refused(self, tmp_path: Path) -> None:
        _record = _create(tmp_path)
        run_dir = tmp_path / "record" / "r1"
        with (run_dir / "record.jsonl").open("a") as handle:
            handle.write('{"schema": "paa-record/1", "seq": 9')
        resumed = RunRecord.open_existing(run_dir)  # loads the intact prefix
        with pytest.raises(RecordError, match="torn write"):
            resumed.append("policy_config", {"mode": "ask"})


class TestTheTwoWriterDiscipline:
    def test_a_second_writer_extends_the_same_chain_no_splice_no_loss(
        self, tmp_path: Path
    ) -> None:
        """The handoff: core writes, proxy writes, core writes again.

        The recorder core holds a record while the proxy (another
        process) appends events; the core's next append must continue
        from the proxy's tail — one chain, both writers — never splice a
        parallel sequence and never drop the other's events.
        """
        first = _create(tmp_path)
        second = RunRecord.open_existing(first.run_dir)
        second.append("proxy_started", {"proxy": "paa-record-proxy"})
        second.append("tool_call", {"tool": "bash", "arguments": {}})
        # The first writer's in-memory tail is now stale: its next
        # append adopts the verified disk tail and extends it.
        first.append("child_exit", {"exit_code": 0})
        events = first.events()
        assert [e["kind"] for e in events] == [
            "run_started",
            "proxy_started",
            "tool_call",
            "child_exit",
        ]
        for previous, current in pairwise(events):
            assert current["prev"] == previous["sha256"]
        first.finalise()
        assert verify_record(first.run_dir).ok

    def test_appending_onto_a_tampered_tail_is_refused(self, tmp_path: Path) -> None:
        record = _create(tmp_path)
        lines = (record.run_dir / "record.jsonl").read_text().splitlines()
        event = json.loads(lines[0])
        event["data"]["run_id"] = "forged"
        lines[0] = json.dumps(event, separators=(",", ":"), sort_keys=True)
        (record.run_dir / "record.jsonl").write_text("\n".join(lines) + "\n")
        with pytest.raises(RecordError, match="does not verify"):
            record.append("policy_config", {"mode": "ask"})

    def test_a_resumed_record_extends_the_same_chain(self, tmp_path: Path) -> None:
        record = _create(tmp_path)
        resumed = RunRecord.open_existing(record.run_dir)
        resumed.append("policy_config", {"mode": "ask"})
        events = resumed.events()
        assert events[-1]["prev"] == events[-2]["sha256"]
        assert verify_record(record.run_dir).chain_ok

    def test_opening_a_corrupt_record_refuses_to_extend_it(
        self, tmp_path: Path
    ) -> None:
        record = _create(tmp_path)
        lines = (record.run_dir / "record.jsonl").read_text().splitlines()
        event = json.loads(lines[0])
        event["data"]["run_id"] = "forged"
        lines[0] = json.dumps(event, separators=(",", ":"), sort_keys=True)
        (record.run_dir / "record.jsonl").write_text("\n".join(lines) + "\n")
        with pytest.raises(RecordError, match="digest does not match"):
            RunRecord.open_existing(record.run_dir)


class TestTheVocabulary:
    def test_the_event_kinds_are_closed(self, tmp_path: Path) -> None:
        record = _create(tmp_path)
        with pytest.raises(RecordError, match="closed"):
            record.append("something_new", {})

    def test_every_v1_kind_round_trips_through_the_chain(
        self, tmp_path: Path
    ) -> None:
        """The histogram a reviewer reads names every kind a writer writes."""
        record = _create(tmp_path)
        for kind in EVENT_KINDS:
            if kind in ("run_started", "record_finalised"):
                continue
            record.append(kind, {"probe": kind})
        record.finalise()
        verdict = verify_record(record.run_dir)
        assert verdict.ok
        for kind in EVENT_KINDS:
            if kind != "record_finalised":
                assert verdict.histogram.get(kind) == 1

    def test_a_foreign_summary_is_refused_not_reinterpreted(self, tmp_path: Path) -> None:
        run_dir = tmp_path / "record" / "foreign"
        run_dir.mkdir(parents=True)
        (run_dir / "record.json").write_text(json.dumps({"schema": "something/else"}))
        verdict = verify_record(run_dir)
        assert not verdict.schema_ok
        assert not verdict.ok
