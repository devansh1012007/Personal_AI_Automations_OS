"""The pytest runner — the check that must never report a false pass.

``src/paa/validation/test_runner.py`` exists for one reason, stated in its
own docstring: *"Never report a false pass."* Two states turn a naive runner
into one that always says yes, and both are common:

* **pytest is not installed.** ``python -m pytest`` exits ``1`` with
  ``No module named pytest``; a runner keying off "0 failures parsed" sees
  no failures and reports success. Nothing ran.
* **No tests were collected.** A typo'd path, a wrong rootdir, a workspace
  where the tests were never written: pytest exits ``5`` and prints
  ``no tests ran``. Zero failures again, and again nothing ran.

:attr:`PytestResult.ok` therefore requires *positive evidence* — pytest
present, tests collected, exit code 0 — rather than the absence of evidence
of failure. This file pins that rule, and it also pins the command the
runner builds, because one of its flags exists to keep the workspace's
manifest hash stable.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from paa.core.errors import SandboxError
from paa.sandbox.base import IsolationLevel, Sandbox, SandboxResult, SandboxSpec
from paa.validation.test_runner import PytestRunner


def _result(
    *,
    exit_code: int | None,
    stdout: str = "",
    stderr: str = "",
    timed_out: bool = False,
) -> SandboxResult:
    """A sandbox outcome, as a backend would hand it over."""
    return SandboxResult(
        exit_code=exit_code,
        stdout=stdout,
        stderr=stderr,
        timed_out=timed_out,
    )


class _RecordingSandbox(Sandbox):
    """Records the spec instead of running it.

    The command the runner builds is part of the contract: it decides
    whether the suite runs at all, in which environment, and with which
    flags — and one of those flags exists to keep the workspace's manifest
    hash stable.
    """

    def __init__(self, result: SandboxResult) -> None:
        self._result = result
        self.specs: list[SandboxSpec] = []

    @property
    def name(self) -> str:
        return "recording"

    @property
    def isolation_level(self) -> IsolationLevel:
        return IsolationLevel.PROCESS

    async def run(self, spec: SandboxSpec) -> SandboxResult:
        self.specs.append(spec)
        return self._result

    async def healthcheck(self) -> bool:
        return True


def _runner() -> tuple[PytestRunner, _RecordingSandbox]:
    """A runner over a sandbox that answers with a scripted outcome."""
    sandbox = _RecordingSandbox(_result(exit_code=0, stdout="3 passed in 0.1s\n"))
    return PytestRunner(sandbox), sandbox


# ---------------------------------------------------------------------------
# Never report a false pass
# ---------------------------------------------------------------------------


def test_a_missing_pytest_is_a_failure() -> None:
    """Nothing ran, so nothing passed.

    ``python -m pytest`` without pytest installed exits 1 with a greppable
    ``No module named pytest``; the runner's job is to notice that the
    *runner itself* is missing, not to count zero failures.
    """
    runner, _ = _runner()

    parsed = runner.parse(
        _result(
            exit_code=1,
            stdout="",
            stderr="No module named pytest",
        )
    )

    assert parsed.ok is False
    assert parsed.pytest_available is False
    assert parsed.tests_collected is False
    assert "pytest is not installed" in (parsed.failure_reason or "")


def test_no_tests_collected_is_a_failure() -> None:
    """Exit code 5 is pytest's "I collected nothing", which is not a pass."""
    runner, _ = _runner()

    parsed = runner.parse(
        _result(
            exit_code=5,
            stdout="no tests ran in 0.01s\n",
        )
    )

    assert parsed.ok is False
    assert parsed.tests_collected is False
    assert parsed.passed == 0
    assert "collected no tests" in (parsed.failure_reason or "")


def test_zero_collected_is_a_failure_even_with_a_zero_exit_code() -> None:
    """A suite that collected nothing proves nothing, whatever the exit code."""
    runner, _ = _runner()

    parsed = runner.parse(
        _result(
            exit_code=0,
            stdout="collected 0 items\n",
        )
    )

    assert parsed.ok is False
    assert parsed.tests_collected is False
    assert "collected no tests" in (parsed.failure_reason or "")


def test_a_timed_out_suite_is_a_failure() -> None:
    """A suite that was killed did not pass."""
    runner, _ = _runner()

    parsed = runner.parse(_result(exit_code=None, timed_out=True))

    assert parsed.ok is False
    assert parsed.timed_out is True
    assert "wall-clock budget" in (parsed.failure_reason or "")


def test_a_real_pass_is_a_pass() -> None:
    """The rule is not "everything fails" — positive evidence passes."""
    runner, _ = _runner()

    parsed = runner.parse(
        _result(
            exit_code=0,
            stdout="collected 3 items\n\n3 passed in 0.12s\n",
        )
    )

    assert parsed.ok is True
    assert parsed.passed == 3
    assert parsed.collected == 3
    assert parsed.failure_reason is None


def test_a_failing_suite_is_a_failure() -> None:
    runner, _ = _runner()

    parsed = runner.parse(
        _result(
            exit_code=1,
            stdout="collected 3 items\n\n2 passed, 1 failed in 0.12s\n",
            stderr="FAILED tests/test_x.py::test_y - AssertionError\n",
        )
    )

    assert parsed.ok is False
    assert parsed.failed == 1
    assert parsed.passed == 2
    assert "1 failed" in (parsed.failure_reason or "")


def test_an_error_is_a_failure() -> None:
    """A collection error is a failure, not a skipped test."""
    runner, _ = _runner()

    parsed = runner.parse(
        _result(
            exit_code=1,
            stdout="collected 3 items\n\n1 passed, 2 errors in 0.12s\n",
            stderr="ERROR tests/test_x.py\n",
        )
    )

    assert parsed.ok is False
    assert parsed.errors == 2
    assert parsed.failed_tests == ("tests/test_x.py",)


@pytest.mark.parametrize(
    ("exit_code", "expected"),
    [
        (2, "interrupted"),
        (3, "internal error"),
        (4, "usage error"),
        (99, "unexpected pytest exit code"),
    ],
)
def test_an_unexpected_exit_code_is_a_failure(exit_code: int, expected: str) -> None:
    """Every exit code that is not 0 is a failure, and each has its own reason."""
    runner, _ = _runner()

    parsed = runner.parse(_result(exit_code=exit_code, stdout="3 passed in 0.1s\n"))

    assert parsed.ok is False
    assert expected in (parsed.failure_reason or "")


def test_the_skipped_tests_are_counted_but_do_not_fail_the_run() -> None:
    """A skipped test is not a failure — but it is not a pass either."""
    runner, _ = _runner()

    parsed = runner.parse(
        _result(
            exit_code=0,
            stdout="collected 3 items\n\n2 passed, 1 skipped in 0.12s\n",
        )
    )

    assert parsed.ok is True
    assert parsed.skipped == 1
    assert parsed.total == 3


def test_the_result_is_ledger_shaped() -> None:
    """The payload records why a run did not pass."""
    runner, _ = _runner()

    parsed = runner.parse(_result(exit_code=5, stdout="no tests ran\n"))
    payload = parsed.to_payload()

    assert payload["ok"] is False
    assert payload["exit_code"] == 5
    assert payload["pytest_available"] is True
    assert payload["tests_collected"] is False
    assert payload["failure_reason"]


# ---------------------------------------------------------------------------
# The command the runner builds
# ---------------------------------------------------------------------------


async def test_the_suite_is_run_as_a_module(tmp_path: Path) -> None:
    """``python -m pytest``, not the ``pytest`` script.

    The module form guarantees the interpreter and the test runner are the
    same environment, and it fails with a clean, greppable ``No module
    named pytest`` when the package is absent — where a missing console
    script fails with a shell-level "not found" that varies by platform.
    """
    runner, sandbox = _runner()

    await runner.run(tmp_path, test_paths=("tests/test_x.py",))

    command = sandbox.specs[0].command
    assert command[:3] == ("python", "-m", "pytest")
    assert command[-1] == "tests/test_x.py"


async def test_the_cache_provider_is_disabled(tmp_path: Path) -> None:
    """A ``.pytest_cache`` written into the workspace would be drift.

    The manifest hashes the workspace, and a cache directory the test run
    created is a change the workspace did not ask for — a false corruption
    alarm on the next restart.
    """
    runner, sandbox = _runner()

    await runner.run(tmp_path)

    assert "no:cacheprovider" in sandbox.specs[0].command


async def test_the_child_gets_no_network_and_no_ambient_environment(tmp_path: Path) -> None:
    """The environment is an explicit allowlist, and the network is off.

    A developer machine's environment routinely holds API keys and
    database DSNs, so the sandbox passes exactly what it is told to and
    nothing else.
    """
    runner, sandbox = _runner()

    await runner.run(tmp_path)

    spec = sandbox.specs[0]
    assert spec.env == {}
    assert spec.allow_network is False


async def test_the_workspace_and_the_timeout_reach_the_sandbox(tmp_path: Path) -> None:
    """The sandbox is told what to run, where, and for how long."""
    sandbox = _RecordingSandbox(_result(exit_code=0, stdout="3 passed in 0.1s\n"))
    runner = PytestRunner(sandbox, default_timeout_seconds=42.0)

    await runner.run(tmp_path, timeout_seconds=7.5)

    spec = sandbox.specs[0]
    assert spec.workspace_path == Path(tmp_path)
    assert spec.timeout_seconds == 7.5
    assert spec.memory_mb == 512


async def test_a_run_that_cannot_start_the_sandbox_raises(tmp_path: Path) -> None:
    """A sandbox that cannot be established is an exception, not a result.

    The distinction matters: "the workload failed" and "containment failed"
    demand opposite responses, and the second one must not be reported as a
    failing test suite.
    """

    class _BrokenSandbox(_RecordingSandbox):
        async def run(self, spec: SandboxSpec) -> SandboxResult:
            raise SandboxError("could not start")

    runner = PytestRunner(_BrokenSandbox(_result(exit_code=0)))

    with pytest.raises(SandboxError):
        await runner.run(tmp_path)
