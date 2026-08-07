"""Composes the deterministic checks into one binary verdict. RFC §13.

The verdict is deliberately **binary**. A "mostly valid" artifact is not a
category the runtime can act on: either the mutation is committed or it is not,
and a confidence score at this boundary would just relocate the decision to
whoever reads it — usually an LLM, which is exactly the party RFC §13 says must
not make security decisions.

Cheap-to-expensive ordering
---------------------------
Checks run in ascending cost and short-circuit on the first *hard* failure:

===  ==========================  ============  ================================
  #  Check                       Typical cost  Why here
===  ==========================  ============  ================================
  1  Schema                      microseconds  Pure in-memory structural test
  2  AST security scan           milliseconds  Parse + walk, no IO
  3  Patch dry run               milliseconds  Reads only the touched files
  4  Workspace drift             ~100ms        Hashes the whole tree
  5  Test suite                  seconds       Executes code in a sandbox
===  ==========================  ============  ================================

The ordering is not merely an optimisation. Check 5 *runs the artifact*, and
checks 1-4 are what establish it is safe enough to run. Reordering them would
mean executing code that the security scan had not yet cleared, which inverts
the entire point of having a scanner.

Short-circuiting is on ``severity >= CRITICAL`` (a security-class failure) or a
structurally invalid artifact. Ordinary failures accumulate so one report can
list everything wrong, rather than forcing a fix-one-rerun loop where each
iteration costs an LLM call.
"""

from __future__ import annotations

import enum
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import structlog
from pydantic import BaseModel, ConfigDict, Field

from paa.core.errors import ValidationError
from paa.sandbox.base import Sandbox
from paa.validation.ast_scanner import AstSecurityScanner, Finding, ScannerPolicy, Severity
from paa.validation.patch import PatchPlan, UnifiedDiffValidator
from paa.validation.schema_validator import SchemaValidator
from paa.validation.test_runner import PytestResult, PytestRunner
from paa.validation.workspace import WorkspaceSnapshot

__all__ = [
    "CheckResult",
    "DeterministicValidationEngine",
    "ValidationArtifact",
    "ValidationReport",
    "ValidationVerdict",
]

log = structlog.get_logger(__name__)


class ValidationVerdict(str, enum.Enum):
    """Binary outcome. See the module docstring for why there is no middle."""

    PASS = "PASS"
    FAIL = "FAIL"


class ValidationArtifact(BaseModel):
    """Everything the engine may be asked to check about one candidate mutation.

    Every field is optional: a task that only produced structured JSON has no
    patch, and a task that only edited a config file has no Python source. The
    engine runs the checks the artifact actually enables and records which ones
    it skipped, so an empty artifact cannot quietly PASS by having nothing to
    check — see :attr:`ValidationReport.checks_run`.
    """

    model_config = ConfigDict(extra="forbid", arbitrary_types_allowed=True)

    correlation_id: str | None = None
    workspace_path: Path | None = None

    source_files: dict[str, str] = Field(default_factory=dict)
    """``{filename: python_source}`` for the AST scan."""

    patch: str | None = None
    """Unified diff to validate against ``workspace_path``."""

    payload: Any = None
    payload_schema: dict[str, Any] | None = None

    expected_manifest_hash: str | None = None
    """When set, the workspace is hashed and compared — the crash-recovery
    drift check from RFC §1.5."""

    run_tests: bool = False
    test_paths: tuple[str, ...] = ()
    test_timeout_seconds: float | None = None


@dataclass(slots=True)
class CheckResult:
    """Outcome of one named check."""

    name: str
    passed: bool
    duration_ms: float = 0.0
    findings: list[Finding] = field(default_factory=list)
    detail: dict[str, Any] = field(default_factory=dict)
    skipped: bool = False
    skip_reason: str | None = None

    @property
    def worst_severity(self) -> Severity | None:
        return max((f.severity for f in self.findings), key=lambda s: s.rank, default=None)

    def to_payload(self) -> dict[str, Any]:
        return {
            "check": self.name,
            "passed": self.passed,
            "skipped": self.skipped,
            "skip_reason": self.skip_reason,
            "duration_ms": round(self.duration_ms, 3),
            "finding_count": len(self.findings),
            "findings": [f.to_payload() for f in self.findings[:25]],
            "detail": self.detail,
        }


@dataclass(slots=True)
class ValidationReport:
    """The engine's answer, ready for a ``CRITIQUE_CONCLUDED`` payload."""

    verdict: ValidationVerdict
    checks: list[CheckResult] = field(default_factory=list)
    duration_ms: float = 0.0
    short_circuited_at: str | None = None
    correlation_id: str | None = None

    @property
    def passed(self) -> bool:
        return self.verdict is ValidationVerdict.PASS

    @property
    def findings(self) -> list[Finding]:
        """Every finding across every check, worst first."""
        merged = [f for check in self.checks for f in check.findings]
        merged.sort(key=lambda f: (-f.severity.rank, f.rule, f.lineno))
        return merged

    @property
    def checks_run(self) -> list[str]:
        return [c.name for c in self.checks if not c.skipped]

    @property
    def failed_checks(self) -> list[str]:
        return [c.name for c in self.checks if not c.passed and not c.skipped]

    def to_payload(self) -> dict[str, Any]:
        return {
            "verdict": self.verdict.value,
            "correlation_id": self.correlation_id,
            "duration_ms": round(self.duration_ms, 3),
            "short_circuited_at": self.short_circuited_at,
            "checks_run": self.checks_run,
            "failed_checks": self.failed_checks,
            "checks": [c.to_payload() for c in self.checks],
        }

    def raise_for_verdict(self) -> None:
        """Raise :class:`~paa.core.errors.ValidationError` unless the verdict passed."""
        if self.passed:
            return
        raise ValidationError(
            "deterministic validation rejected the artifact",
            failed_checks=self.failed_checks,
            short_circuited_at=self.short_circuited_at,
            finding_count=len(self.findings),
        )


class DeterministicValidationEngine:
    """Runs the host-side checks that decide whether a mutation may commit.

    Every collaborator is injectable so a caller can tighten a policy without
    subclassing, and so the engine is testable without a sandbox.
    """

    def __init__(
        self,
        *,
        scanner: AstSecurityScanner | None = None,
        schema_validator: SchemaValidator | None = None,
        patch_validator: UnifiedDiffValidator | None = None,
        sandbox: Sandbox | None = None,
        test_runner: PytestRunner | None = None,
        scanner_policy: ScannerPolicy | None = None,
    ) -> None:
        self.scanner = scanner or AstSecurityScanner(scanner_policy)
        self.schema_validator = schema_validator or SchemaValidator()
        self.patch_validator = patch_validator or UnifiedDiffValidator()
        self.sandbox = sandbox
        self.test_runner = test_runner or (PytestRunner(sandbox) if sandbox else None)

    async def validate(self, artifact: ValidationArtifact) -> ValidationReport:
        """Run every applicable check and return a binary verdict."""
        started = time.perf_counter()
        report = ValidationReport(
            verdict=ValidationVerdict.PASS, correlation_id=artifact.correlation_id
        )

        for check in (
            self._check_schema,
            self._check_ast,
            self._check_patch,
            self._check_drift,
        ):
            result = check(artifact)
            report.checks.append(result)
            if not result.passed:
                report.verdict = ValidationVerdict.FAIL
                if self._is_hard_failure(result):
                    report.short_circuited_at = result.name
                    report.duration_ms = (time.perf_counter() - started) * 1000.0
                    self._log(report)
                    return report

        # Tests run last and only if everything above cleared — this is the
        # only check that executes the artifact.
        if artifact.run_tests:
            result = await self._check_tests(artifact)
            report.checks.append(result)
            if not result.passed:
                report.verdict = ValidationVerdict.FAIL

        report.duration_ms = (time.perf_counter() - started) * 1000.0
        self._log(report)
        return report

    @staticmethod
    def _is_hard_failure(result: CheckResult) -> bool:
        """Whether to stop immediately rather than collect more findings.

        A CRITICAL finding means the artifact is *dangerous*, not merely wrong.
        Continuing would mean reading more of it, dry-running its patch against
        the real workspace, and eventually executing it — all to produce a
        nicer report about something already refused.
        """
        worst = result.worst_severity
        return (worst is not None and worst.rank >= Severity.CRITICAL.rank) or result.detail.get(
            "structural_failure", False
        )

    @staticmethod
    def _log(report: ValidationReport) -> None:
        log.info(
            "validation.completed",
            verdict=report.verdict.value,
            checks=report.checks_run,
            failed=report.failed_checks,
            short_circuited_at=report.short_circuited_at,
            duration_ms=round(report.duration_ms, 2),
        )

    # -- individual checks -------------------------------------------------

    def _check_schema(self, artifact: ValidationArtifact) -> CheckResult:
        started = time.perf_counter()
        if artifact.payload_schema is None:
            return CheckResult(
                name="schema",
                passed=True,
                skipped=True,
                skip_reason="artifact declares no payload schema",
            )
        errors = self.schema_validator.check(artifact.payload, artifact.payload_schema)
        return CheckResult(
            name="schema",
            passed=not errors,
            duration_ms=(time.perf_counter() - started) * 1000.0,
            detail={
                "error_count": len(errors),
                "errors": list(errors[:25]),
                "structural_failure": bool(errors),
            },
        )

    def _check_ast(self, artifact: ValidationArtifact) -> CheckResult:
        started = time.perf_counter()
        if not artifact.source_files:
            return CheckResult(
                name="ast_security",
                passed=True,
                skipped=True,
                skip_reason="artifact contains no Python source",
            )

        findings: list[Finding] = []
        for filename, source in sorted(artifact.source_files.items()):
            findings.extend(self.scanner.scan(source, filename=filename))

        blocking = [
            f for f in findings if f.severity.rank >= self.scanner.policy.blocking_severity.rank
        ]
        return CheckResult(
            name="ast_security",
            passed=not blocking,
            duration_ms=(time.perf_counter() - started) * 1000.0,
            findings=findings,
            detail={
                "files_scanned": len(artifact.source_files),
                "blocking_count": len(blocking),
            },
        )

    def _check_patch(self, artifact: ValidationArtifact) -> CheckResult:
        started = time.perf_counter()
        if artifact.patch is None:
            return CheckResult(
                name="patch",
                passed=True,
                skipped=True,
                skip_reason="artifact contains no patch",
            )
        if artifact.workspace_path is None:
            return CheckResult(
                name="patch",
                passed=False,
                duration_ms=(time.perf_counter() - started) * 1000.0,
                detail={
                    "reason": "a patch was supplied without a workspace to apply it against",
                    "structural_failure": True,
                },
            )

        plan: PatchPlan = self.patch_validator.dry_run(artifact.patch, artifact.workspace_path)
        return CheckResult(
            name="patch",
            passed=plan.ok,
            duration_ms=(time.perf_counter() - started) * 1000.0,
            detail={
                **plan.to_payload(),
                # A rejected path is a security event, not a mere mismatch:
                # the patch tried to write outside the workspace.
                "structural_failure": bool(plan.rejected),
            },
        )

    def _check_drift(self, artifact: ValidationArtifact) -> CheckResult:
        started = time.perf_counter()
        if artifact.expected_manifest_hash is None or artifact.workspace_path is None:
            return CheckResult(
                name="workspace_drift",
                passed=True,
                skipped=True,
                skip_reason="no expected manifest hash supplied",
            )
        snapshot = WorkspaceSnapshot.capture(artifact.workspace_path)
        drifted = snapshot.has_drifted(artifact.expected_manifest_hash)
        return CheckResult(
            name="workspace_drift",
            passed=not drifted,
            duration_ms=(time.perf_counter() - started) * 1000.0,
            detail={
                "expected_manifest_hash": artifact.expected_manifest_hash,
                "actual_manifest_hash": snapshot.manifest_hash,
                "file_count": snapshot.file_count,
                "drifted": drifted,
            },
        )

    async def _check_tests(self, artifact: ValidationArtifact) -> CheckResult:
        started = time.perf_counter()
        if self.test_runner is None or artifact.workspace_path is None:
            return CheckResult(
                name="tests",
                passed=False,
                duration_ms=(time.perf_counter() - started) * 1000.0,
                detail={
                    "reason": (
                        "tests were requested but no sandbox/workspace is configured; "
                        "reported as FAIL because an unrun suite is not a passing suite"
                    ),
                    "structural_failure": True,
                },
            )

        result: PytestResult = await self.test_runner.run(
            artifact.workspace_path,
            test_paths=artifact.test_paths,
            timeout_seconds=artifact.test_timeout_seconds,
        )
        return CheckResult(
            name="tests",
            passed=result.ok,
            duration_ms=(time.perf_counter() - started) * 1000.0,
            detail=result.to_payload(),
        )
