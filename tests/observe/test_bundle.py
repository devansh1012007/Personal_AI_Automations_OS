"""The T1 observation bundle's own contract (C7.1).

The bundle is the artifact ``paa observe`` leaves behind, and its two
invariants are what make it evidence rather than a log:

* **complete or says it is not** — the two-phase write (``started``
  skeleton, then ``final``) means a file on disk is always a valid,
  self-describing record of *something*, and a record still saying
  ``started`` is itself the crash marker;
* **never overstates** — ``resume`` is ``"none"`` by construction, the
  tier is ``T1`` by construction, and capped payloads carry an explicit
  ``truncated`` marker instead of silently losing their tail.

These tests pin the format's mechanics: the manifest walk and diff, the
capped streams, the atomic write, and the read-back contract (including
the refusal to reinterpret a foreign schema).
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from paa.observe.bundle import (
    BUNDLE_SCHEMA,
    EVIDENCE_TIER,
    GitChanges,
    ManifestDiff,
    ObserveBundle,
    capped_stream,
    read_bundle,
    snapshot_manifest,
)


def _write(root: Path, rel: str, text: str) -> None:
    target = root / rel
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(text, encoding="utf-8")


class TestManifestSnapshot:
    def test_every_regular_file_is_recorded_by_content_identity(
        self, tmp_path: Path
    ) -> None:
        _write(tmp_path, "a.txt", "alpha")
        _write(tmp_path, "nested/b.txt", "beta" * 1000)
        snap = snapshot_manifest(tmp_path)
        by_path = snap.by_path()
        assert snap.count == 2
        assert by_path["a.txt"].size == 5
        assert len(by_path["nested/b.txt"].sha256) == 64
        import hashlib

        assert (
            by_path["a.txt"].sha256
            == hashlib.sha256(b"alpha").hexdigest()
        )

    def test_identical_trees_snapshot_identically(self, tmp_path: Path) -> None:
        _write(tmp_path, "one", "same")
        first = snapshot_manifest(tmp_path)
        (tmp_path / "one").unlink()
        _write(tmp_path, "one", "same")
        assert snapshot_manifest(tmp_path) == first

    def test_git_object_store_is_skipped(self, tmp_path: Path) -> None:
        _write(tmp_path, "work.txt", "w")
        _write(tmp_path, ".git/objects/pack-abc.pack", "opaque bytes")
        snap = snapshot_manifest(tmp_path)
        assert [f.path for f in snap.files] == ["work.txt"]

    def test_symlink_is_its_own_fact_not_followed(self, tmp_path: Path) -> None:
        _write(tmp_path, "real.txt", "content")
        (tmp_path / "link.txt").symlink_to("real.txt")
        snap = snapshot_manifest(tmp_path)
        link = snap.by_path()["link.txt"]
        real = snap.by_path()["real.txt"]
        assert link.sha256 != real.sha256  # the link text is hashed, not the target


class TestManifestDiff:
    def test_created_modified_deleted_by_content(self, tmp_path: Path) -> None:
        _write(tmp_path, "kept", "same")
        _write(tmp_path, "gone", "old")
        _write(tmp_path, "changed", "before")
        before = snapshot_manifest(tmp_path)

        (tmp_path / "gone").unlink()
        _write(tmp_path, "changed", "after")
        _write(tmp_path, "fresh", "new")
        after = snapshot_manifest(tmp_path)

        diff = ManifestDiff.between(before, after)
        assert [f.path for f in diff.created] == ["fresh"]
        assert [f.path for f in diff.deleted] == ["gone"]
        assert [(a.path, b.path) for a, b in diff.modified] == [("changed", "changed")]
        assert diff.changed is True

    def test_a_mode_only_change_is_invisible_and_says_nothing(self, tmp_path: Path):
        """The manifest answers content; a chmod is the git section's business."""
        _write(tmp_path, "f", "bytes")
        before = snapshot_manifest(tmp_path)
        (tmp_path / "f").chmod(0o444)
        after = snapshot_manifest(tmp_path)
        assert ManifestDiff.between(before, after).changed is False

    def test_unchanged_workspace_diffs_to_nothing(self, tmp_path: Path) -> None:
        _write(tmp_path, "x", "y")
        snap = snapshot_manifest(tmp_path)
        assert ManifestDiff.between(snap, snap) == ManifestDiff()


class TestCappedStream:
    def test_under_the_cap_is_whole(self) -> None:
        stream = capped_stream("stdout", "hello", cap_bytes=1024)
        assert stream.text == "hello"
        assert stream.byte_length == 5
        assert stream.truncated is False

    def test_over_the_cap_is_head_plus_honest_marker(self) -> None:
        text = "abcdefghij"
        stream = capped_stream("stdout", text, cap_bytes=4)
        assert stream.text == "abcd"
        assert stream.byte_length == 10  # what the child actually produced
        assert stream.truncated is True

    def test_a_cap_mid_codepoint_keeps_the_whole_codepoint(self) -> None:
        text = "héllo"  # é is 2 bytes in UTF-8
        stream = capped_stream("stdout", text, cap_bytes=2)
        assert stream.text == "h"
        assert stream.truncated is True


class TestTheBundle:
    def _bundle(self, tmp_path: Path) -> ObserveBundle:
        return ObserveBundle(
            run_id="r1",
            command=("echo", "hi"),
            workspace=str(tmp_path),
        )

    def test_skeleton_defaults_say_what_the_spec_demands(self, tmp_path: Path) -> None:
        bundle = self._bundle(tmp_path)
        raw = bundle.to_json()
        assert raw["schema"] == BUNDLE_SCHEMA
        assert bundle.phase == "started"
        assert bundle.resume == "none"
        assert bundle.evidence_tier == EVIDENCE_TIER == "T1"
        assert raw["run"]["resume"] == "none"
        assert raw["run"]["evidence_tier"] == "T1"

    def test_write_is_atomic_and_makes_parents(self, tmp_path: Path) -> None:
        bundle = self._bundle(tmp_path)
        path = tmp_path / "observe" / "r1" / "bundle.json"
        bundle.write(path)
        assert path.exists()
        assert not path.with_name("bundle.json.tmp").exists()
        assert json.loads(path.read_text(encoding="utf-8"))["schema"] == BUNDLE_SCHEMA

    def test_finalise_is_the_one_way_gate(self, tmp_path: Path) -> None:
        bundle = self._bundle(tmp_path)
        bundle.finalise()
        assert bundle.phase == "final"
        assert bundle.ended_at is not None

    def test_round_trip_preserves_every_section(self, tmp_path: Path) -> None:
        bundle = self._bundle(tmp_path)
        bundle.child = {"exit_code": 0, "timed_out": False}
        bundle.transcript_stdout = capped_stream("stdout", "out", cap_bytes=10)
        bundle.transcript_stderr = capped_stream("stderr", "err", cap_bytes=1)
        bundle.manifest = {
            "before_count": 1,
            "after_count": 2,
            "created": [{"path": "n", "size": 1, "sha256": "x"}],
            "modified": [],
            "deleted": [],
        }
        bundle.git = GitChanges(
            available=True,
            status=" M f",
            diff="diff text",
            head_before="a" * 40,
            head_after="b" * 40,
            commits=({"line": "b0000 harness"},),
        )
        bundle.classification = {"state": "clean_exit"}
        bundle.finalise()

        path = tmp_path / "b.json"
        bundle.write(path)
        back = read_bundle(path)

        assert back.run_id == "r1"
        assert back.command == ("echo", "hi")
        assert back.phase == "final"
        assert back.ended_at is not None
        assert back.child["exit_code"] == 0
        assert back.transcript_stdout is not None
        assert back.transcript_stdout.text == "out"
        assert back.transcript_stderr is not None and back.transcript_stderr.truncated
        assert back.manifest["created"][0]["path"] == "n"
        assert back.git is not None
        assert back.git.commits == ({"line": "b0000 harness"},)
        assert back.git.dirty is True
        assert back.classification["state"] == "clean_exit"
        assert back.resume == "none"

    def test_a_foreign_schema_is_refused_not_reinterpreted(self, tmp_path: Path) -> None:
        path = tmp_path / "foreign.json"
        path.write_text(json.dumps({"schema": "someone-elses/1"}), encoding="utf-8")
        with pytest.raises(ValueError, match="paa-observe-bundle/1"):
            read_bundle(path)

    def test_sha256_is_stable_for_identical_content(self, tmp_path: Path) -> None:
        first = self._bundle(tmp_path)
        first.child = {"exit_code": 0}
        first.finalise()
        # Same content re-serialised must hash identically — the attestation
        # a ledger-side consumer relies on.
        assert first.sha256() == first.sha256()
        second = ObserveBundle(
            run_id="r1",
            command=("echo", "hi"),
            workspace=str(tmp_path),
            started_at=first.started_at,
            child=dict(first.child),
        )
        second.phase = "final"
        second.ended_at = first.ended_at
        assert second.sha256() == first.sha256()
