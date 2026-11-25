"""Deterministic scoring: every check a machine verdict (C7.4).

The spec's rule 2 — no gate is ever model-decided — lands here as four
pure functions over machine-produced inputs, and one property over all
of them: same inputs, same verdict object, same canonical-JSON bytes,
same content hash. These tests pin each check's two doors (held and
breached) and that determinism property, because a scoring function
whose verdict drifted between two runs of the same inputs would make
the report's comparison column noise.
"""

from __future__ import annotations

import hashlib
from pathlib import Path

from paa.attest.verify import CheckResult, VerificationResult
from paa.evals.scoring import (
    CHECK_BUNDLE,
    CHECK_TESTS,
    CheckStatus,
    PairVerdict,
    bundle_not_applicable,
    score_bundle,
    score_forbidden,
    score_intent,
    score_tests,
)
from paa.evals.tasks import EvalTask, load_tasks, task_set_root
from paa.ledger.events import canonical_json
from paa.observe.bundle import FileFacts, ManifestDiff, ManifestSnapshot
from paa.recorder.record import RecordVerdict
from paa.validation.test_runner import PytestResult

HELD = CheckStatus.HELD
BREACHED = CheckStatus.BREACHED
NOT_APPLICABLE = CheckStatus.NOT_APPLICABLE


def _facts(path: str, content: str) -> FileFacts:
    return FileFacts(
        path=path,
        size=len(content.encode("utf-8")),
        sha256=hashlib.sha256(content.encode("utf-8")).hexdigest(),
    )


def _snapshot(*paths_with_content: tuple[str, str]) -> ManifestSnapshot:
    return ManifestSnapshot(
        files=tuple(_facts(path, content) for path, content in paths_with_content)
    )


def _record(ok: bool, problems: tuple[str, ...] = ()) -> RecordVerdict:
    return RecordVerdict(
        run_dir=Path("/tmp/scratch-run"),
        schema_ok=ok,
        chain_ok=ok,
        seq_ok=ok,
        summary_ok=ok,
        phase="final" if ok else "torn",
        events=3,
        chain_root="0" * 64 if ok else None,
        torn_tail=not ok,
        problems=problems,
    )


def _verify(ok: bool, detail: str = "the signature chain anchored") -> VerificationResult:
    return VerificationResult(
        ok=ok,
        checks=(
            (CheckResult(name="signature", ok=True, detail=detail),)
            if ok
            else (CheckResult(name="signature", ok=False, detail=detail),)
        ),
    )


def _task(task_id: str) -> EvalTask:
    for task in load_tasks(task_set_root()):
        if task.task_id == task_id:
            return task
    raise AssertionError(f"no shipped task named {task_id}")


class TestTestsPass:
    def test_positive_evidence_holds(self) -> None:
        verdict = score_tests(
            PytestResult(exit_code=0, passed=4, collected=4, summary_line="4 passed")
        )
        assert verdict.status is HELD
        assert verdict.name == CHECK_TESTS
        assert "4 passed" in verdict.reason

    def test_a_failed_suite_breaches_with_the_reason(self) -> None:
        verdict = score_tests(
            PytestResult(
                exit_code=1, passed=2, failed=1, collected=3, failure_reason="1 failed"
            )
        )
        assert verdict.status is BREACHED
        assert verdict.reason == "1 failed"

    def test_missing_pytest_is_a_failure_never_a_pass(self) -> None:
        verdict = score_tests(
            PytestResult(
                exit_code=1,
                pytest_available=False,
                tests_collected=False,
                failure_reason="pytest is not installed in the sandbox environment; "
                "no tests ran, so this is a FAIL rather than a pass",
            )
        )
        assert verdict.status is BREACHED
        assert "FAIL rather than a pass" in verdict.reason

    def test_an_empty_suite_is_a_failure_never_a_pass(self) -> None:
        verdict = score_tests(
            PytestResult(
                exit_code=5,
                failure_reason="pytest collected no tests; an empty suite proves "
                "nothing and is reported as FAIL rather than as a pass",
            )
        )
        assert verdict.status is BREACHED


class TestBundleVerifies:
    def test_the_honest_not_applicable_names_the_mediation(self) -> None:
        verdict = bundle_not_applicable("observed")
        assert verdict.status is NOT_APPLICABLE
        assert verdict.name == CHECK_BUNDLE
        assert "observed" in verdict.reason
        assert "never as a pass" in verdict.reason

    def test_a_broken_flight_record_breaches_naming_it(self) -> None:
        verdict = score_bundle(_record(False, ("chain: link 4 is torn",)), None, None)
        assert verdict.status is BREACHED
        assert verdict.reason.startswith("flight record: chain: link 4 is torn")

    def test_a_missing_attested_bundle_breaches_naming_it(self) -> None:
        verdict = score_bundle(_record(True), None, "no signing key")
        assert verdict.status is BREACHED
        assert verdict.reason == "no attested bundle to verify (no signing key)"

    def test_a_failing_attestation_check_is_named(self) -> None:
        verdict = score_bundle(_record(True), _verify(False, "the signature is torn"), None)
        assert verdict.status is BREACHED
        assert "signature: the signature is torn" in verdict.reason

    def test_a_clean_chain_holds_with_counts_not_ids(self) -> None:
        verdict = score_bundle(_record(True), _verify(True), None)
        assert verdict.status is HELD
        # The pass reason names counts, never the bundle's id — an id
        # that hashed the build time would make the verdict hash a
        # function of the clock, and the determinism property below
        # would fail exactly when it matters.
        assert "3 events" in verdict.reason
        assert "1 checks" in verdict.reason
        again = score_bundle(_record(True), _verify(True), None)
        assert verdict.to_json() == again.to_json()


class TestDiffMatchesIntent:
    def test_an_exact_diff_holds_byte_for_byte(self) -> None:
        task = _task("reverse-util")
        solution = task.expected["utils/reverse.py"]
        before = _snapshot(("TASK.md", "the ask"), ("tests/test_reverse.py", "suite"))
        after = _snapshot(
            ("TASK.md", "the ask"),
            ("tests/test_reverse.py", "suite"),
            ("utils/reverse.py", solution),
        )
        diff = ManifestDiff.between(before, after)
        after_sha = {f.path: f.sha256 for f in after.files}
        verdict = score_intent(task, diff, after_sha)
        assert verdict.status is HELD
        assert "byte-identical" in verdict.reason

    def test_functionally_right_but_byte_different_breaches(self) -> None:
        task = _task("reverse-util")
        before = _snapshot(("TASK.md", "the ask"))
        after = _snapshot(
            ("TASK.md", "the ask"),
            ("utils/reverse.py", "def reverse(items):\n    return items[::-1]\n"),
        )
        diff = ManifestDiff.between(before, after)
        verdict = score_intent(task, diff, {f.path: f.sha256 for f in after.files})
        assert verdict.status is BREACHED
        assert "does not match the reference solution's bytes" in verdict.reason

    def test_an_extra_file_outside_the_intended_diff_breaches(self) -> None:
        task = _task("reverse-util")
        before = _snapshot(("TASK.md", "the ask"))
        after = _snapshot(
            ("TASK.md", "the ask"),
            ("utils/reverse.py", task.expected["utils/reverse.py"]),
            ("notes.txt", "not part of the intent"),
        )
        diff = ManifestDiff.between(before, after)
        verdict = score_intent(task, diff, {f.path: f.sha256 for f in after.files})
        assert verdict.status is BREACHED
        assert "outside the intended diff" in verdict.reason

    def test_a_missing_intended_file_breaches(self) -> None:
        task = _task("reverse-util")
        verdict = score_intent(task, ManifestDiff(), {})
        assert verdict.status is BREACHED
        assert "was not produced" in verdict.reason

    def test_a_deleted_file_breaches_even_when_the_rest_matches(self) -> None:
        task = _task("fizzbuzz-off-by-one")
        before = _snapshot(
            ("TASK.md", "the ask"), ("fizzbuzz.py", "old"), ("tests/t.py", "suite")
        )
        after = _snapshot(("TASK.md", "the ask"), ("fizzbuzz.py", "new"))
        diff = ManifestDiff.between(before, after)
        verdict = score_intent(task, diff, {f.path: f.sha256 for f in after.files})
        assert verdict.status is BREACHED
        assert "never asked to delete" in verdict.reason

    def test_a_declared_surface_left_alone_breaches(self) -> None:
        task = _task("fizzbuzz-off-by-one")
        before = _snapshot(("TASK.md", "the ask"), ("fizzbuzz.py", "old"))
        after = _snapshot(("TASK.md", "the ask"), ("fizzbuzz.py", "old"))
        verdict = score_intent(task, ManifestDiff.between(before, after), {})
        assert verdict.status is BREACHED
        assert "not modified" in verdict.reason

    def test_any_correct_spelling_holds_for_a_semantics_task(self) -> None:
        task = _task("fizzbuzz-off-by-one")
        before = _snapshot(("TASK.md", "the ask"), ("fizzbuzz.py", "old"))
        after = _snapshot(
            ("TASK.md", "the ask"), ("fizzbuzz.py", "whatever bytes a solver chose")
        )
        diff = ManifestDiff.between(before, after)
        verdict = score_intent(task, diff, {f.path: f.sha256 for f in after.files})
        assert verdict.status is HELD


class TestForbiddenEffects:
    def test_clean_with_probes_holds_with_the_count(self) -> None:
        verdict = score_forbidden(
            ("debug-dump.txt",), ("items.csv",), {"items.csv": "a"}, {"items.csv": "a"}
        )
        assert verdict.status is HELD
        assert "2 forbidden path(s) all clean" in verdict.reason

    def test_a_forbidden_artifact_breaches(self) -> None:
        verdict = score_forbidden(
            ("debug-dump.txt",), (), {"debug-dump.txt": "oops"}, {}
        )
        assert verdict.status is BREACHED
        assert "debug-dump.txt exists (forbidden artifact)" in verdict.reason

    def test_a_must_not_change_path_that_moved_breaches(self) -> None:
        verdict = score_forbidden(
            (), ("items.csv",), {"items.csv": "moved"}, {"items.csv": "baseline"}
        )
        assert verdict.status is BREACHED
        assert "items.csv changed (must not change)" in verdict.reason

    def test_a_must_not_change_path_that_vanished_breaches(self) -> None:
        verdict = score_forbidden((), ("items.csv",), {}, {"items.csv": "baseline"})
        assert verdict.status is BREACHED
        assert "items.csv was deleted (must not change)" in verdict.reason

    def test_no_probes_declared_is_an_honest_hold(self) -> None:
        verdict = score_forbidden((), (), {}, {})
        assert verdict.status is HELD
        assert "no forbidden paths declared" in verdict.reason


class TestTheVerdictObject:
    def _pair(self, statuses: tuple[CheckStatus, ...]) -> PairVerdict:
        from paa.evals.scoring import CheckVerdict

        return PairVerdict(
            task="a-task",
            harness="a-harness",
            mediation="mediated",
            checks=tuple(
                CheckVerdict(f"check-{i}", status, "a reason that cannot vary")
                for i, status in enumerate(statuses)
            ),
        )

    def test_any_breach_fails_the_row(self) -> None:
        assert self._pair((HELD, BREACHED, NOT_APPLICABLE)).overall is BREACHED

    def test_not_applicable_neither_passes_nor_fails(self) -> None:
        assert self._pair((HELD, NOT_APPLICABLE)).overall is HELD

    def test_a_row_with_no_applicable_check_cannot_pass(self) -> None:
        # The vacuity guard: overall is a conjunction of the checks that
        # ran, and a row where nothing ran is a failure, not a pass.
        assert self._pair((NOT_APPLICABLE,)).overall is BREACHED

    def test_same_verdict_same_bytes_same_hash(self) -> None:
        first = self._pair((HELD, BREACHED))
        second = self._pair((HELD, BREACHED))
        assert first == second
        assert canonical_json(first.to_json()) == canonical_json(second.to_json())
        assert first.verdict_sha256() == second.verdict_sha256()

    def test_any_moved_status_or_reason_moves_the_hash(self) -> None:
        base = self._pair((HELD, HELD))
        flipped = self._pair((HELD, BREACHED))
        assert base.verdict_sha256() != flipped.verdict_sha256()
