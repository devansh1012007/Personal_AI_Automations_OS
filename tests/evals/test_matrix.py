"""The C7.4 acceptance: 2 harnesses x 5 tasks, one reproducible report.

The spec's line — *same tasks, N harnesses, deterministic scoring, a
per-harness report* — is played here for real, offline: both shipped
adapters (the mediated fake and the naive direct one) run the whole
five-task set under the subprocess sandbox with a real ledger, and the
test asserts the report's comparison has a *spread to measure* — the
mediated harness solves the set, the naive one fails three different
tasks on three different checks, and every row's evidence is stated
rather than assumed. No network, no model key, no sleeps: the subjects
are scripts and the scoring is arithmetic.
"""

from __future__ import annotations

import json
from pathlib import Path

from paa.evals.harnesses import known_adapters
from paa.evals.runner import EvalRunner
from paa.evals.scoring import CheckStatus
from paa.evals.tasks import load_tasks, task_set_root
from paa.ledger.events import canonical_json
from paa.ledger.store import LedgerStore
from paa.sandbox.subprocess_backend import SubprocessSandbox
from tests.evals.conftest import src_on_path

HELD = CheckStatus.HELD
BREACHED = CheckStatus.BREACHED
NOT_APPLICABLE = CheckStatus.NOT_APPLICABLE


class TestTheAcceptanceMatrix:
    async def test_two_harnesses_five_tasks_one_report(
        self, home: Path, ledger: LedgerStore
    ) -> None:
        tasks = load_tasks(task_set_root())
        adapters = known_adapters()[:2]
        assert len(tasks) == 5 and len(adapters) == 2

        runner = EvalRunner(
            SubprocessSandbox(),
            home=home,
            ledger=ledger,
            child_env=src_on_path(),
        )
        argv = ("paa", "eval", "run", "--all")
        report = await runner.run_matrix(
            tasks, adapters, argv=argv, timeout_s=120.0, eval_id="acceptance"
        )

        # -- the shape: every pair scored, in the task set's order ------
        assert len(report.rows) == 10
        assert report.argv == argv
        assert [(r.verdict.task, r.verdict.harness) for r in report.rows] == [
            (task.task_id, adapter.harness_id)
            for task in tasks
            for adapter in adapters
        ]

        # -- the comparison has a real spread to measure ----------------
        summary = report.to_json()["summary"]
        assert summary["rows"] == 10
        assert summary["by_harness"] == {
            "recorded-fake": {"passed": 5, "failed": 0},
            "scripted-direct": {"passed": 2, "failed": 3},
        }

        by_pair = {(r.verdict.task, r.verdict.harness): r for r in report.rows}

        # The mediated harness solves the whole set, with verifying
        # evidence on every row.
        for task in tasks:
            row = by_pair[(task.task_id, "recorded-fake")]
            assert row.verdict.overall is HELD, row.verdict.to_json()
            assert row.verdict.mediation == "mediated"
            checks = row.verdict.by_name()
            assert checks["tests_pass"].status is HELD
            assert checks["bundle_verifies"].status is HELD
            assert row.refusal is None

        # The naive harness fails three different tasks on three
        # different checks — that spread is the report's whole point.
        naive_tests = by_pair[("palindrome-guard", "scripted-direct")].verdict.by_name()
        assert naive_tests["tests_pass"].status is BREACHED
        assert naive_tests["diff_matches_intent"].status is HELD

        naive_intent = by_pair[("reverse-util", "scripted-direct")].verdict.by_name()
        assert naive_intent["tests_pass"].status is HELD
        assert naive_intent["diff_matches_intent"].status is BREACHED
        assert "does not match the reference solution's bytes" in (
            naive_intent["diff_matches_intent"].reason
        )

        naive_forbidden = by_pair[("inventory-report", "scripted-direct")].verdict.by_name()
        assert naive_forbidden["forbidden_effects"].status is BREACHED
        assert "debug-dump.txt exists (forbidden artifact)" in (
            naive_forbidden["forbidden_effects"].reason
        )
        # And it genuinely solves two.
        for task_id in ("fizzbuzz-off-by-one", "greet-cli-flag"):
            assert by_pair[(task_id, "scripted-direct")].verdict.overall is HELD

        # The unmediated rows state the missing bundle instead of
        # scoring it as a quiet pass.
        for task in tasks:
            checks = by_pair[(task.task_id, "scripted-direct")].verdict.by_name()
            assert checks["bundle_verifies"].status is NOT_APPLICABLE
            assert "never as a pass" in checks["bundle_verifies"].reason

        # -- the report is on disk, canonical, and reproducible ---------
        path = report.report_path
        assert path is not None and path.is_file()
        assert path == home / "eval" / "acceptance" / "report.json"
        on_disk = json.loads(path.read_text(encoding="utf-8"))
        assert on_disk["schema"] == "paa-eval-report/1"
        assert on_disk["summary"] == summary
        assert on_disk["reproduce"] == "paa eval run --all"
        assert len(on_disk["rows"]) == 10
        for row, disk_row in zip(report.rows, on_disk["rows"], strict=True):
            assert disk_row["verdict_sha256"] == row.verdict.verdict_sha256()
            assert disk_row["task_sha256"] == row.task_sha256

        # The scoring view is the deterministic half: no clocks, no run
        # ids, no measured facts — the bytes the guarantee is about.
        view = report.scoring_view()
        assert "created_at" not in view
        assert "eval_id" not in view
        assert "measured" not in canonical_json(view)
        assert report.scoring_sha256() == report.scoring_sha256()

        # -- the corpus: one v=1 row per pair on the matrix lineage -----
        rows = await ledger.read_correlation(report.correlation_id)
        assert len(rows) == 10
        statuses = {}
        for row in rows:
            assert row.payload["v"] == 1
            statuses[row.discriminator] = row.payload["status"]
        assert statuses["eval:reverse-util:recorded-fake"] == "pass"
        assert statuses["eval:reverse-util:scripted-direct"] == "fail"
        assert statuses["eval:palindrome-guard:scripted-direct"] == "fail"
        assert statuses["eval:inventory-report:scripted-direct"] == "fail"
        assert statuses["eval:fizzbuzz-off-by-one:scripted-direct"] == "pass"
        for row in rows:
            if row.discriminator.endswith(":recorded-fake"):
                assert row.payload["status"] == "pass"
