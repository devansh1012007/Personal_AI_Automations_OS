"""Run a project's pytest suite inside a sandbox and parse the result.

The expensive validation check, and the only one that executes the artifact —
so it runs last (see :mod:`paa.validation.engine`) and always inside a
:class:`~paa.sandbox.base.Sandbox`.

Never report a false pass
-------------------------
The whole value of this check is that a ``PASS`` means something. Two states
turn a naive runner into one that always says yes, and both are common:

**pytest is not installed.** ``python -m pytest`` then exits ``1`` with
``No module named pytest``. A runner keying off "0 failures parsed" sees no
failures and reports success. Nothing ran.

**No tests were collected.** A typo'd path, a wrong ``rootdir``, or a workspace
where the tests never got written: pytest exits ``5`` and prints
``no tests ran``. Zero failures again, and again nothing ran.

Both cases are treated as **explicit failures** here, not as passes.
:attr:`PytestResult.ok` requires positive evidence — pytest present, tests
collected, exit code 0 — rather than the absence of evidence of failure.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import structlog

from paa.sandbox.base import Sandbox, SandboxResult, SandboxSpec

__all__ = ["PytestResult", "PytestRunner"]

log = structlog.get_logger(__name__)

#: pytest's documented exit codes.
EXIT_OK = 0
EXIT_TESTS_FAILED = 1
EXIT_INTERRUPTED = 2
EXIT_INTERNAL_ERROR = 3
EXIT_USAGE_ERROR = 4
EXIT_NO_TESTS_COLLECTED = 5

_COUNT = re.compile(
    r"(\d+)\s+(passed|failed|error|errors|skipped|xfailed|xpassed|deselected|warnings?)"
)
_SUMMARY_LINE = re.compile(r"^=+\s.*\s(in|\bno tests ran\b).*=+$", re.MULTILINE)
_COLLECTED = re.compile(r"collected\s+(\d+)\s+item")
_NO_TESTS = re.compile(r"no tests ran|collected 0 items", re.IGNORECASE)
_MISSING_PYTEST = re.compile(
    r"No module named pytest|ModuleNotFoundError: No module named ['\"]pytest",
    re.IGNORECASE,
)


@dataclass(frozen=True, slots=True)
class PytestResult:
    """Structured outcome of a suite run."""

    exit_code: int | None
    passed: int = 0
    failed: int = 0
    errors: int = 0
    skipped: int = 0
    collected: int = 0

    pytest_available: bool = True
    tests_collected: bool = True
    timed_out: bool = False
    duration_ms: float = 0.0
    summary_line: str = ""
    failure_reason: str | None = None
    stdout_tail: str = ""
    stderr_tail: str = ""
    failed_tests: tuple[str, ...] = field(default_factory=tuple)

    @property
    def ok(self) -> bool:
        """PASS requires positive evidence that tests actually ran and passed."""
        return (
            self.pytest_available
            and self.tests_collected
            and not self.timed_out
            and self.exit_code == EXIT_OK
            and self.failed == 0
            and self.errors == 0
            and self.passed > 0
        )

    @property
    def total(self) -> int:
        return self.passed + self.failed + self.errors + self.skipped

    def to_payload(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "exit_code": self.exit_code,
            "passed": self.passed,
            "failed": self.failed,
            "errors": self.errors,
            "skipped": self.skipped,
            "collected": self.collected,
            "pytest_available": self.pytest_available,
            "tests_collected": self.tests_collected,
            "timed_out": self.timed_out,
            "duration_ms": round(self.duration_ms, 2),
            "summary_line": self.summary_line,
            "failure_reason": self.failure_reason,
            "failed_tests": list(self.failed_tests),
        }


class PytestRunner:
    """Executes pytest inside a sandbox and parses its output."""

    def __init__(
        self,
        sandbox: Sandbox,
        *,
        python_executable: str = "python",
        default_timeout_seconds: float = 300.0,
        memory_mb: int = 512,
    ) -> None:
        self._sandbox = sandbox
        self._python = python_executable
        self._timeout = default_timeout_seconds
        self._memory_mb = memory_mb

    async def run(
        self,
        workspace: Path | str,
        *,
        test_paths: tuple[str, ...] = (),
        extra_args: tuple[str, ...] = (),
        timeout_seconds: float | None = None,
        env: dict[str, str] | None = None,
    ) -> PytestResult:
        """Run the suite in ``workspace``.

        Invoked as ``python -m pytest`` rather than the ``pytest`` script:
        the module form guarantees the interpreter and the test runner are the
        same environment, and it fails with a clean, greppable
        ``No module named pytest`` when the package is absent — where a missing
        console script fails with a shell-level "not found" that varies by
        platform and is far harder to distinguish from a real error.
        """
        command = [
            self._python,
            "-m",
            "pytest",
            # -p no:cacheprovider: a .pytest_cache written into the workspace
            # would change its manifest hash and register as filesystem drift.
            "-p",
            "no:cacheprovider",
            "-q",
            "--no-header",
            "--tb=short",
        ]
        command.extend(extra_args)
        command.extend(test_paths)

        spec = SandboxSpec(
            command=tuple(command),
            workspace_path=Path(workspace),
            env=env or {},
            memory_mb=self._memory_mb,
            timeout_seconds=timeout_seconds or self._timeout,
            allow_network=False,
        )

        result = await self._sandbox.run(spec)
        parsed = self.parse(result)
        log.info(
            "validation.pytest.completed",
            ok=parsed.ok,
            passed=parsed.passed,
            failed=parsed.failed,
            errors=parsed.errors,
            exit_code=parsed.exit_code,
            reason=parsed.failure_reason,
        )
        return parsed

    def parse(self, result: SandboxResult) -> PytestResult:
        """Turn raw sandbox output into a structured result.

        Separate from :meth:`run` so the parsing rules are unit-testable
        against captured output without needing a live sandbox.
        """
        combined = f"{result.stdout}\n{result.stderr}"

        if _MISSING_PYTEST.search(combined):
            return PytestResult(
                exit_code=result.exit_code,
                pytest_available=False,
                tests_collected=False,
                duration_ms=result.duration_ms,
                failure_reason=(
                    "pytest is not installed in the sandbox environment; "
                    "no tests ran, so this is a FAIL rather than a pass"
                ),
                stdout_tail=result.stdout[-4000:],
                stderr_tail=result.stderr[-4000:],
            )

        if result.timed_out:
            return PytestResult(
                exit_code=result.exit_code,
                timed_out=True,
                duration_ms=result.duration_ms,
                failure_reason="test suite exceeded its wall-clock budget and was killed",
                stdout_tail=result.stdout[-4000:],
                stderr_tail=result.stderr[-4000:],
            )

        counts = {name: 0 for name in ("passed", "failed", "errors", "skipped")}
        for raw_count, label in _COUNT.findall(combined):
            key = "errors" if label.startswith("error") else label
            if key in counts:
                counts[key] += int(raw_count)

        collected = 0
        if (match := _COLLECTED.search(combined)) is not None:
            collected = int(match.group(1))

        no_tests = (
            result.exit_code == EXIT_NO_TESTS_COLLECTED
            or (_NO_TESTS.search(combined) is not None and counts["passed"] == 0)
        )

        summary = ""
        if (match := _SUMMARY_LINE.search(combined)) is not None:
            summary = match.group(0).strip()

        failure_reason: str | None = None
        if no_tests:
            failure_reason = (
                "pytest collected no tests; an empty suite proves nothing and is "
                "reported as FAIL rather than as a pass"
            )
        elif result.exit_code == EXIT_USAGE_ERROR:
            failure_reason = "pytest usage error — check the arguments and rootdir"
        elif result.exit_code == EXIT_INTERNAL_ERROR:
            failure_reason = "pytest internal error"
        elif result.exit_code == EXIT_INTERRUPTED:
            failure_reason = "pytest was interrupted before finishing"
        elif counts["failed"] or counts["errors"]:
            failure_reason = f"{counts['failed']} failed, {counts['errors']} errored"
        elif result.exit_code not in (EXIT_OK, EXIT_TESTS_FAILED):
            failure_reason = f"unexpected pytest exit code {result.exit_code}"

        return PytestResult(
            exit_code=result.exit_code,
            passed=counts["passed"],
            failed=counts["failed"],
            errors=counts["errors"],
            skipped=counts["skipped"],
            collected=collected,
            pytest_available=True,
            tests_collected=not no_tests,
            timed_out=False,
            duration_ms=result.duration_ms,
            summary_line=summary,
            failure_reason=failure_reason,
            stdout_tail=result.stdout[-4000:],
            stderr_tail=result.stderr[-4000:],
            failed_tests=tuple(_extract_failed_tests(combined)),
        )


def _extract_failed_tests(output: str) -> list[str]:
    """Pull node ids out of pytest's ``FAILED``/``ERROR`` short-summary lines."""
    found: list[str] = []
    for line in output.splitlines():
        stripped = line.strip()
        if stripped.startswith(("FAILED ", "ERROR ")):
            node = stripped.split(" ", 1)[1].split(" - ")[0].strip()
            if node and node not in found:
                found.append(node)
    return found[:50]
