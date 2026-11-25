"""Conformance v0 run for every registered pipeline (C6.6, C7.1).

The G-LOOPS gate line: "conformance green for chat + coder + orchestrator
built-ins" now also covers the T1 observer adapter (C7.1, pipeline #4).
Green includes the honest ``n/a`` rows — a check a vocabulary cannot
express is reported with the reason and a pointer to where the behaviour
IS pinned, never faked as a pass. The test also pins that the report
renderer says GREEN/RED and that every pipeline reports all six checks
in the standard order.
"""

from __future__ import annotations

import uuid
from pathlib import Path

import pytest

from paa.ledger.store import LedgerStore
from paa.pipelines.conformance import (
    CHECK_NAMES,
    every_builtin_conformance,
    render_reports,
)


class TestConformanceV0:
    async def test_every_registered_pipeline_is_green(
        self, ledger: LedgerStore, tmp_path: Path
    ) -> None:
        from paa.pipelines.registry import PipelineRegistry

        reports = await every_builtin_conformance(ledger, tmp_path)
        # Derive the expectation from the registry: a new runnable builtin
        # must either gain a conformance runner or fail this gate as RED.
        registered = tuple(
            row.name
            for row in PipelineRegistry(local_dir=None).list()
            if row.runnable
        )
        assert tuple(r.pipeline for r in reports) == registered
        assert "builtin.observe" in registered  # C7.1: pipeline #4 is registered
        for report in reports:
            assert report.passed, f"{report.pipeline} failed:\n{render_reports([report])}"
            # The standard is six checks, in the standard order, for
            # every pipeline — no silent omissions.
            assert tuple(c.name for c in report.checks) == CHECK_NAMES

    async def test_a_new_builtin_without_a_runner_is_red_not_silent(
        self, ledger: LedgerStore, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A registered builtin with no runner must fail the gate by name."""
        from paa.pipelines import registry as registry_module
        from paa.pipelines.conformance import every_builtin_conformance
        from paa.pipelines.slots import PipelineDeclaration

        extra = PipelineDeclaration(
            name="test.unrunnable-conformance",
            version="1.0.0",
            requires_runtime=">=4.1,<5",
            description="registered without a conformance runner",
        )
        monkeypatch.setattr(
            registry_module,
            "BUILTIN_DECLARATIONS",
            {**registry_module.BUILTIN_DECLARATIONS, extra.name: extra},
        )
        reports = await every_builtin_conformance(ledger, tmp_path)
        gap = next(r for r in reports if r.pipeline == extra.name)
        assert not gap.passed
        assert all(c.status == "fail" for c in gap.checks)
        assert "no conformance runner" in render_reports([gap])

    async def test_the_rendered_report_is_one_page_of_truth(
        self, ledger: LedgerStore, tmp_path: Path
    ) -> None:
        reports = await every_builtin_conformance(ledger, tmp_path)
        text = render_reports(reports)
        assert text.count("GREEN") == 4
        assert "double-run-sequence-equality" in text
        assert "n/a" not in text.splitlines()[0]  # verdict lines stay verdicts
        # The n/a rows are visible, not hidden: tie the rendered count to
        # the data so an added or dropped row cannot rot a magic number.
        expected_na = sum(c.status == "n/a" for r in reports for c in r.checks)
        assert text.count("[   -]") == expected_na

    async def test_coder_conformance_is_strong_not_ceremonial(
        self, ledger: LedgerStore, tmp_path: Path
    ) -> None:
        """Five checks pass for real; the sixth is an honest, pinned n/a.

        A hand-written `commit` row used to make unknown-outcome a fake
        pass — no registered pipeline journals a mutating call, so a green
        there proved the fold in isolation and nothing about the pipeline.
        Pin the n/a explicitly so a future fake pass fails this test.
        """
        from paa.pipelines.conformance import coder_conformance

        report = await coder_conformance(ledger, tmp_path)
        assert report.passed
        statuses = {c.name: c.status for c in report.checks}
        assert statuses[CHECK_NAMES[2]] == "n/a", statuses
        assert "test_interrupted_mutating_call_never_auto_repeats" in (
            next(c.detail for c in report.checks if c.name == CHECK_NAMES[2])
        )
        assert all(
            status == "pass"
            for name, status in statuses.items()
            if name != CHECK_NAMES[2]
        ), statuses

    async def test_failed_builtin_stage_is_a_red_row_not_a_raised_assert(
        self, ledger: LedgerStore, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A failed stage must be visible to the renderer as RED."""
        from paa.agents.orchestrator import ChiefOrchestrator
        from paa.ledger.replay import TaskPhase, TaskProjection
        from paa.pipelines.conformance import builtin_conformance

        async def failed_run(
            self: ChiefOrchestrator, cid: uuid.UUID
        ) -> TaskProjection:
            return TaskProjection(correlation_id=str(cid), phase=TaskPhase.FAILED)

        monkeypatch.setattr(ChiefOrchestrator, "run", failed_run)
        report = await builtin_conformance(ledger)
        assert not report.passed
        assert report.checks[0].status == "fail"
        assert "RED" in render_reports([report])

    async def test_observe_conformance_passes_real_checks(
        self, ledger: LedgerStore, tmp_path: Path
    ) -> None:
        """The T1 adapter's real rows are real: killed child, timeout, secrets."""
        from paa.pipelines.conformance import observe_conformance

        report = await observe_conformance(ledger, tmp_path)
        assert report.passed, render_reports([report])
        statuses = {c.name: c.status for c in report.checks}
        real = (CHECK_NAMES[0], CHECK_NAMES[2], CHECK_NAMES[3], CHECK_NAMES[5])
        assert all(statuses[name] == "pass" for name in real), statuses
        assert statuses[CHECK_NAMES[1]] == "n/a", statuses  # resume="none"
        assert statuses[CHECK_NAMES[4]] == "n/a", statuses  # sandbox-owned budgets
