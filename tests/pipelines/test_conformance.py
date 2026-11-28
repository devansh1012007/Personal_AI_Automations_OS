"""Conformance v0 run for every registered pipeline (C6.6, C7.1).

The G-LOOPS gate line: "conformance green for chat + coder + orchestrator
built-ins" now also covers the T1 observer adapter (C7.1, pipeline #4),
the composition manager (C8.2, pipeline #5) and the externally-driven
MCP session (W9-C9.4, pipeline #7 — P6's last adapter).
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

    async def test_the_report_count_follows_an_added_declaration(
        self, ledger: LedgerStore, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The derived count moves with the declaration table, not against it.

        The pin this proves honest (``len(reports) == len(BUILTIN_DECLARATIONS)``
        in the page-of-truth test below) replaced an exact ``== 7`` that
        broke by design the moment an eighth builtin landed — the
        closed-door failure the review named. Same monkeypatched extra
        as the test above: the reports grow by exactly the addition
        (the RED gap row), and the derived count follows the table
        rather than failing. No literal anywhere: the ninth builtin
        proves the same thing this one does.
        """
        from paa.pipelines import registry as registry_module
        from paa.pipelines.slots import PipelineDeclaration

        # Two scratch roots: the runners mkdir their working children
        # under the path they are handed (``one``/``two``/``three``...),
        # so a second run needs a second root, not the same one.
        scratch_before, scratch_after = tmp_path / "before", tmp_path / "after"
        scratch_before.mkdir()
        scratch_after.mkdir()
        before = await every_builtin_conformance(ledger, scratch_before)
        extra = PipelineDeclaration(
            name="test.eighth-builtin",
            version="1.0.0",
            requires_runtime=">=4.1,<5",
            description="registered to prove the derived count follows",
        )
        monkeypatch.setattr(
            registry_module,
            "BUILTIN_DECLARATIONS",
            {**registry_module.BUILTIN_DECLARATIONS, extra.name: extra},
        )
        after = await every_builtin_conformance(ledger, scratch_after)
        assert len(after) == len(before) + 1
        assert len(after) == len(registry_module.BUILTIN_DECLARATIONS)
        # The addition is a report (a RED one — no runner), never a
        # silent gap in the page.
        assert extra.name in {r.pipeline for r in after}

    async def test_the_rendered_report_is_one_page_of_truth(
        self, ledger: LedgerStore, tmp_path: Path
    ) -> None:
        reports = await every_builtin_conformance(ledger, tmp_path)
        text = render_reports(reports)
        # One verdict line per report: the count is the declarations' own,
        # read through the registry binding both ``list()`` and the
        # monkeypatch pattern above flow through — so a pipeline added
        # without a GREEN report shows up here as a missing/RED line, not
        # by breaking an exact count (this is the line that used to say
        # ``== 7`` and turned every eighth builtin into a red test).
        from paa.pipelines import registry as registry_module

        assert text.count("GREEN") == len(reports)
        assert len(reports) == len(registry_module.BUILTIN_DECLARATIONS)
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

    async def test_mcp_host_conformance_passes_real_checks(
        self, tmp_path: Path
    ) -> None:
        """The externally-driven session's real rows are real, over the wire.

        Four checks pass against the unchanged server driven in-process
        over its own framing: the deterministic session call, the
        classified transport death, the between-frames cancel, the
        no-secrets scan with the server's own attestation as the
        positive control. The two honest ``n/a`` rows are pinned so a
        future fake pass fails this test: no replay exists to diverge
        (``resume="none"``, by declaration), and no budget exists at the
        session layer to exhaust (the binding disciplines are the
        server's per-call policy gate and caps).
        """
        from paa.pipelines.conformance import mcp_host_conformance

        report = await mcp_host_conformance(tmp_path)
        assert report.passed, render_reports([report])
        statuses = {c.name: c.status for c in report.checks}
        real = (CHECK_NAMES[0], CHECK_NAMES[2], CHECK_NAMES[3], CHECK_NAMES[5])
        assert all(statuses[name] == "pass" for name in real), statuses
        assert statuses[CHECK_NAMES[1]] == "n/a", statuses  # resume="none"
        assert statuses[CHECK_NAMES[4]] == "n/a", statuses  # server-owned disciplines

    async def test_improve_conformance_passes_six_real_checks(
        self, ledger: LedgerStore, tmp_path: Path
    ) -> None:
        """The guarded loop's six rows are all real — including unknown-outcome.

        ``improve.reflection-loop`` is the first registered pipeline that
        journals a **mutating** call through the call journal, so the
        unknown-outcome check — an honest ``n/a`` with a pointer for every
        other pipeline — must be a genuine green here: a crashed
        ``improve.commit`` refuses the re-run with -32008 and the
        repository is untouched. Pin that it stays a real pass and never
        quietly degrades to an ``n/a`` row.
        """
        from paa.pipelines.conformance import improve_conformance

        report = await improve_conformance(ledger, tmp_path)
        assert report.passed, render_reports([report])
        statuses = {c.name: c.status for c in report.checks}
        assert all(status == "pass" for status in statuses.values()), statuses
        unknown_detail = next(
            c.detail for c in report.checks if c.name == CHECK_NAMES[2]
        )
        assert "-32008" in unknown_detail
