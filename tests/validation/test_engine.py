"""The deterministic engine — the binary verdict that gates a commit.

``src/paa/validation/engine.py`` composes five checks into one verdict, and
two of its properties are the reason the package exists at all:

* the checks run **cheap to expensive**, and the expensive one — the test
  suite — is the only one that *executes the artifact*. Reordering them
  would mean executing code the security scan had not yet cleared, which
  inverts the entire point of having a scanner;
* the verdict is **binary**. "Mostly valid" is not a category the runtime
  can act on: either the mutation is committed or it is not.

Neither property had a test. This file pins the order, the short-circuit,
the accounting of what was and was not checked, and the rule that a suite
which did not run is a failure rather than a pass.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from paa.core.errors import ValidationError
from paa.validation.engine import (
    DeterministicValidationEngine,
    ValidationArtifact,
    ValidationVerdict,
)
from paa.validation.test_runner import PytestResult, PytestRunner
from paa.validation.workspace import WorkspaceSnapshot

# ---------------------------------------------------------------------------
# A stand-in for the sandbox-backed test runner
# ---------------------------------------------------------------------------


class _StubRunner(PytestRunner):
    """Answers from a script instead of a sandbox, and records every call.

    It subclasses the real runner so the engine's injection point keeps its
    type: the engine takes a ``PytestRunner``, and a duck-typed object
    would be a different contract. Only :meth:`run` is replaced — the
    parsing rules live in :meth:`PytestRunner.parse` and are exercised on
    their own in ``test_test_runner.py``.
    """

    def __init__(self, result: PytestResult) -> None:
        self._result = result
        self.calls: list[Path] = []

    async def run(
        self,
        workspace: Path | str,
        *,
        test_paths: tuple[str, ...] = (),
        extra_args: tuple[str, ...] = (),
        timeout_seconds: float | None = None,
        env: dict[str, str] | None = None,
    ) -> PytestResult:
        self.calls.append(Path(workspace))
        return self._result


PASSING_RESULT = PytestResult(exit_code=0, passed=3, collected=3)
FAILING_RESULT = PytestResult(exit_code=1, passed=2, failed=1, collected=3)


@pytest.fixture
def workspace(tmp_path: Path) -> Path:
    root = tmp_path / "workspace"
    root.mkdir()
    (root / "file.txt").write_text("hello\n", encoding="utf-8")
    return root


# ---------------------------------------------------------------------------
# The order of the checks
# ---------------------------------------------------------------------------


async def test_the_checks_run_in_the_documented_order(workspace: Path) -> None:
    """Cheap to expensive, and the expensive one last.

    The order is not an optimisation: the test suite *runs* the artifact,
    and the checks before it are what establish that it is safe enough to
    run.
    """
    engine = DeterministicValidationEngine(test_runner=_StubRunner(PASSING_RESULT))
    artifact = ValidationArtifact(
        source_files={"a.py": "x = 1\n"},
        patch="--- a/file.txt\n+++ b/file.txt\n@@ -1,1 +1,2 @@\n hello\n+world\n",
        workspace_path=workspace,
        expected_manifest_hash=WorkspaceSnapshot.capture(workspace).manifest_hash,
        payload={"a": 1},
        payload_schema={"type": "object"},
        run_tests=True,
    )

    report = await engine.validate(artifact)

    assert [check.name for check in report.checks] == [
        "schema",
        "ast_security",
        "patch",
        "workspace_drift",
        "tests",
    ]
    assert report.checks_run == ["schema", "ast_security", "patch", "workspace_drift", "tests"]


async def test_the_test_suite_does_not_run_after_a_security_failure(workspace: Path) -> None:
    """The scanner decides whether the artifact is safe to *execute*.

    If the suite ran before the scan cleared the source, the scanner would
    be decoration — the artifact would already have run by the time anyone
    noticed what it was.
    """
    runner = _StubRunner(PASSING_RESULT)
    engine = DeterministicValidationEngine(test_runner=runner)
    artifact = ValidationArtifact(
        source_files={"a.py": "import subprocess\n"},
        workspace_path=workspace,
        run_tests=True,
    )

    report = await engine.validate(artifact)

    assert report.verdict is ValidationVerdict.FAIL
    assert report.short_circuited_at == "ast_security"
    assert runner.calls == []


async def test_a_critical_finding_short_circuits_the_rest(workspace: Path) -> None:
    """A CRITICAL finding means the artifact is *dangerous*, not merely wrong.

    Continuing would mean reading more of it, dry-running its patch against
    the real workspace, and eventually executing it — all to produce a
    nicer report about something already refused.
    """
    engine = DeterministicValidationEngine()
    artifact = ValidationArtifact(
        source_files={"a.py": "import subprocess\n"},
        patch="--- a/file.txt\n+++ b/file.txt\n@@ -1,1 +1,2 @@\n hello\n+world\n",
        workspace_path=workspace,
    )

    report = await engine.validate(artifact)

    assert report.verdict is ValidationVerdict.FAIL
    assert report.short_circuited_at == "ast_security"
    assert [check.name for check in report.checks] == ["schema", "ast_security"]


async def test_an_ordinary_failure_accumulates_instead_of_short_circuiting(
    workspace: Path,
) -> None:
    """A patch that does not apply is wrong, not dangerous.

    Ordinary failures accumulate so one report can list everything wrong,
    rather than forcing a fix-one-rerun loop where each iteration costs an
    LLM call.
    """
    engine = DeterministicValidationEngine()
    artifact = ValidationArtifact(
        patch="--- a/file.txt\n+++ b/file.txt\n@@ -1,1 +1,2 @@\n nothing like it\n+world\n",
        workspace_path=workspace,
        expected_manifest_hash=WorkspaceSnapshot.capture(workspace).manifest_hash,
    )

    report = await engine.validate(artifact)

    assert report.verdict is ValidationVerdict.FAIL
    assert report.short_circuited_at is None
    assert report.failed_checks == ["patch"]
    assert "workspace_drift" in report.checks_run


async def test_a_finding_below_the_blocking_threshold_does_not_fail_the_check(
    workspace: Path,
) -> None:
    """A gate that fires on everything gets switched off.

    The default blocking severity is HIGH, so a MEDIUM smell is reported
    and the artifact still passes.
    """
    engine = DeterministicValidationEngine()
    artifact = ValidationArtifact(source_files={"a.py": "from os import *\n"})

    report = await engine.validate(artifact)

    assert report.verdict is ValidationVerdict.PASS
    assert report.findings
    assert report.failed_checks == []


# ---------------------------------------------------------------------------
# The accounting
# ---------------------------------------------------------------------------


async def test_a_skipped_check_is_recorded_with_its_reason() -> None:
    """What was not checked is as important as what was."""
    engine = DeterministicValidationEngine()

    report = await engine.validate(ValidationArtifact())

    skipped = {check.name: check.skip_reason for check in report.checks if check.skipped}
    assert skipped == {
        "schema": "artifact declares no payload schema",
        "ast_security": "artifact contains no Python source",
        "patch": "artifact contains no patch",
        "workspace_drift": "no expected manifest hash supplied",
    }
    assert report.checks_run == []


@pytest.mark.xfail(
    strict=True,
    reason=(
        "An artifact with nothing to check is reported as PASS. The "
        "ValidationArtifact docstring says 'an empty artifact cannot quietly PASS by "
        "having nothing to check', and the verdict is what the critic's adapter "
        "reads: CriticValidatorAdapter.validate builds an artifact from the "
        "worker's output dict, so an output with no 'source_files', no 'patch' and "
        "no 'payload_schema' reaches the engine with nothing to check and comes "
        "back as passed=True. src/paa/validation/engine.py is read-only for T-005; "
        "the fix is in DeterministicValidationEngine.validate — an artifact for "
        "which no check ran is a FAIL, not a PASS."
    ),
)
async def test_an_artifact_with_nothing_to_check_does_not_pass() -> None:
    """A gate that checked nothing has not passed anything.

    The accounting makes the emptiness visible in the report, but the
    *verdict* is the thing the caller acts on, and it says PASS.
    """
    engine = DeterministicValidationEngine()

    report = await engine.validate(ValidationArtifact())

    assert report.checks_run == []
    assert report.verdict is ValidationVerdict.FAIL


async def test_a_schema_failure_fails_the_schema_check(workspace: Path) -> None:
    """A payload that does not match its declared schema is rejected."""
    engine = DeterministicValidationEngine()
    artifact = ValidationArtifact(payload=5, payload_schema={"type": "string"})

    report = await engine.validate(artifact)

    assert report.verdict is ValidationVerdict.FAIL
    assert report.failed_checks == ["schema"]
    assert report.short_circuited_at == "schema"


async def test_a_malformed_schema_fails_the_gate_instead_of_crashing(workspace: Path) -> None:
    """A broken schema is a schema error, and it reaches the engine as one.

    The engine is where the crash used to land: the schema check calls the
    validator, and a validator that raises a ``TypeError`` takes the gate
    with it.
    """
    engine = DeterministicValidationEngine()
    artifact = ValidationArtifact(
        payload="x",
        payload_schema={"type": "string", "minLength": "abc"},
    )

    report = await engine.validate(artifact)

    assert report.verdict is ValidationVerdict.FAIL
    assert report.failed_checks == ["schema"]


async def test_a_patch_without_a_workspace_is_refused(workspace: Path) -> None:
    """A patch with nothing to check it against is not a clean patch."""
    engine = DeterministicValidationEngine()
    artifact = ValidationArtifact(patch="--- a/file.txt\n+++ b/file.txt\n@@ -1,1 +1,1 @@\n-x\n+y\n")

    report = await engine.validate(artifact)

    assert report.verdict is ValidationVerdict.FAIL
    assert report.failed_checks == ["patch"]
    assert report.short_circuited_at == "patch"


async def test_a_drifted_workspace_fails_the_drift_check(workspace: Path) -> None:
    """The drift check compares the live tree with the recorded hash."""
    engine = DeterministicValidationEngine()
    expected = WorkspaceSnapshot.capture(workspace).manifest_hash
    (workspace / "file.txt").write_text("changed\n", encoding="utf-8")
    artifact = ValidationArtifact(workspace_path=workspace, expected_manifest_hash=expected)

    report = await engine.validate(artifact)

    assert report.verdict is ValidationVerdict.FAIL
    assert report.failed_checks == ["workspace_drift"]


async def test_an_unchanged_workspace_passes_the_drift_check(workspace: Path) -> None:
    """The same workspace, unchanged, is not drift."""
    engine = DeterministicValidationEngine()
    expected = WorkspaceSnapshot.capture(workspace).manifest_hash
    artifact = ValidationArtifact(workspace_path=workspace, expected_manifest_hash=expected)

    report = await engine.validate(artifact)

    assert report.verdict is ValidationVerdict.PASS


async def test_a_test_suite_that_cannot_run_is_a_failure(workspace: Path) -> None:
    """An unrun suite is not a passing suite.

    The request to run tests is a request for *evidence*; with no sandbox
    and no workspace there is none, and the engine says so instead of
    quietly passing.
    """
    engine = DeterministicValidationEngine()
    artifact = ValidationArtifact(workspace_path=workspace, run_tests=True)

    report = await engine.validate(artifact)

    assert report.verdict is ValidationVerdict.FAIL
    assert report.failed_checks == ["tests"]


async def test_a_failing_suite_fails_the_artifact(workspace: Path) -> None:
    runner = _StubRunner(FAILING_RESULT)
    engine = DeterministicValidationEngine(test_runner=runner)
    artifact = ValidationArtifact(workspace_path=workspace, run_tests=True)

    report = await engine.validate(artifact)

    assert report.verdict is ValidationVerdict.FAIL
    assert report.failed_checks == ["tests"]
    assert runner.calls == [workspace]


async def test_a_passing_suite_passes_the_artifact(workspace: Path) -> None:
    runner = _StubRunner(PASSING_RESULT)
    engine = DeterministicValidationEngine(test_runner=runner)
    artifact = ValidationArtifact(workspace_path=workspace, run_tests=True)

    report = await engine.validate(artifact)

    assert report.verdict is ValidationVerdict.PASS
    assert runner.calls == [workspace]


# ---------------------------------------------------------------------------
# The report
# ---------------------------------------------------------------------------


async def test_the_report_is_ledger_shaped(workspace: Path) -> None:
    """The report is what a ``CRITIQUE_CONCLUDED`` payload carries."""
    engine = DeterministicValidationEngine()
    artifact = ValidationArtifact(
        correlation_id="corr-1",
        source_files={"a.py": "import subprocess\n"},
        workspace_path=workspace,
    )

    report = await engine.validate(artifact)
    payload = report.to_payload()

    assert payload["verdict"] == "FAIL"
    assert payload["correlation_id"] == "corr-1"
    assert payload["short_circuited_at"] == "ast_security"
    assert payload["failed_checks"] == ["ast_security"]
    assert payload["checks"][1]["check"] == "ast_security"
    assert payload["checks"][1]["findings"][0]["rule"] == "denied_import"


async def test_raising_for_the_verdict_raises_only_on_failure(workspace: Path) -> None:
    engine = DeterministicValidationEngine()

    good = await engine.validate(ValidationArtifact(source_files={"a.py": "x = 1\n"}))
    good.raise_for_verdict()

    bad = await engine.validate(ValidationArtifact(source_files={"a.py": "import subprocess\n"}))
    with pytest.raises(ValidationError):
        bad.raise_for_verdict()


async def test_the_verdict_is_the_same_every_time(workspace: Path) -> None:
    """A PASS means something only if it is the same PASS every time."""
    engine = DeterministicValidationEngine()
    artifact = ValidationArtifact(
        source_files={"a.py": "x = 1\n"},
        payload={"a": 1},
        payload_schema={"type": "object"},
        workspace_path=workspace,
    )

    first = await engine.validate(artifact)
    second = await engine.validate(artifact)

    assert first.verdict is second.verdict
    assert first.checks_run == second.checks_run


async def test_the_worst_severity_is_reported(workspace: Path) -> None:
    """The report leads with the finding that matters most."""
    engine = DeterministicValidationEngine()
    artifact = ValidationArtifact(source_files={"a.py": "import subprocess\n"})

    report = await engine.validate(artifact)

    assert report.findings
    assert report.findings[0].severity.value == "CRITICAL"
    assert report.findings[0].rule == "denied_import"
