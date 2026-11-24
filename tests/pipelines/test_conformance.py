"""Conformance v0 run for every registered pipeline (C6.6; observe C7.1).

The G-LOOPS gate line: "conformance green for chat + coder + orchestrator
built-ins". Green includes the honest ``n/a`` rows — a check a vocabulary
cannot express is reported with the reason and a pointer to where the
behaviour IS pinned, never faked as a pass. The test also pins that the
report renderer says GREEN/RED and that every pipeline reports all six
checks in the standard order. Wave 7 adds the fourth row: ``builtin.observe``
(the T1 adapter, pipeline #4) runs the same standard in its own vocabulary.
"""

from __future__ import annotations

from pathlib import Path

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
        reports = await every_builtin_conformance(ledger, tmp_path)
        assert [r.pipeline for r in reports] == [
            "builtin.plan-execute-critique",
            "builtin.chat",
            "coder.tool-loop",
            "builtin.observe",
        ]
        for report in reports:
            assert report.passed, f"{report.pipeline} failed:\n{render_reports([report])}"
            # The standard is six checks, in the standard order, for
            # every pipeline — no silent omissions.
            assert tuple(c.name for c in report.checks) == CHECK_NAMES

    async def test_the_rendered_report_is_one_page_of_truth(
        self, ledger: LedgerStore, tmp_path: Path
    ) -> None:
        reports = await every_builtin_conformance(ledger, tmp_path)
        text = render_reports(reports)
        assert text.count("GREEN") == 4
        assert "double-run-sequence-equality" in text
        assert "n/a" not in text.splitlines()[0]  # verdict lines stay verdicts
        # The n/a rows are visible, not hidden: the standard shows them.
        assert text.count("[   -]") >= 5

    async def test_coder_conformance_is_strong_not_ceremonial(
        self, ledger: LedgerStore, tmp_path: Path
    ) -> None:
        """The pipeline with the full vocabulary passes all six for real."""
        from paa.pipelines.conformance import coder_conformance

        report = await coder_conformance(ledger, tmp_path)
        assert report.passed
        statuses = {c.name: c.status for c in report.checks}
        assert all(status == "pass" for status in statuses.values()), statuses

    async def test_observe_conformance_passes_real_checks(
        self, ledger: LedgerStore, tmp_path: Path
    ) -> None:
        """The T1 adapter's real rows are real: killed child, timeout, secrets."""
        from paa.pipelines.conformance import observe_conformance

        report = await observe_conformance(ledger, tmp_path)
        assert report.passed, render_reports([report])
        statuses = {c.name: c.status for c in report.checks}
        # Four rows are checked for real against the subprocess backend;
        # the two n/a rows are the adapter's own declared limits.
        real = [
            "double-run-sequence-equality",
            "unknown-outcome-never-auto-repeats",
            "cancel-within-grace",
            "no-secrets-in-journal",
        ]
        assert all(statuses[name] == "pass" for name in real), statuses
        assert statuses["divergence-halt"] == "n/a"
        assert statuses["budget-exhaustion"] == "n/a"
