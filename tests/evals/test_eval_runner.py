"""The runner's own contracts: the corpus row, refusals, report shape (C7.4).

Three things the matrix test cannot pin on its own:

* the ledger row's payload shape — ``v=1``, no free text, eval identity
  in ``metrics`` — and its idempotency through a real store;
* the refusal boundary: an adapter whose command factory raises is a
  *scored row with a recorded refusal*, never a crash that costs the
  matrix its other rows;
* the report's determinism boundary: ``scoring_view`` is the pure half
  (no clocks, no run ids), and the pair re-run proves the verdict itself
  reproduces across separate runs of the same pair.
"""

from __future__ import annotations

import uuid
from pathlib import Path

from paa.core.types import EventType
from paa.evals.harnesses import HarnessAdapter
from paa.evals.runner import EvalRunner, PairResult, eval_ledger_row
from paa.evals.scoring import CheckStatus, CheckVerdict, PairVerdict
from paa.evals.tasks import EvalError, EvalTask, load_tasks, task_set_root
from paa.ledger.events import BLOB_THRESHOLD_BYTES, canonical_json, payload_size_bytes
from paa.ledger.store import LedgerStore
from paa.validation.test_runner import PytestResult
from tests.evals.conftest import src_on_path


def _scratch_task() -> EvalTask:
    for task in load_tasks(task_set_root()):
        if task.task_id == "reverse-util":
            return task
    raise AssertionError("the shipped reverse-util task is missing")


def _pair_result(task: EvalTask, harness_id: str) -> PairResult:
    verdict = PairVerdict(
        task=task.task_id,
        harness=harness_id,
        mediation="mediated",
        checks=(
            CheckVerdict("tests_pass", CheckStatus.HELD, "5 passed, 0 failed, 0 skipped"),
            CheckVerdict("bundle_verifies", CheckStatus.HELD, "flight record final"),
            CheckVerdict("diff_matches_intent", CheckStatus.HELD, "exact diff"),
            CheckVerdict("forbidden_effects", CheckStatus.HELD, "all clean"),
        ),
    )
    return PairResult(
        verdict=verdict,
        task_sha256=task.manifest_digest(),
        wall_time_ms=1_234.5,
        subject_state="clean_exit",
        exit_code=0,
        tests=PytestResult(exit_code=0, passed=5, collected=5),
    )


class TestTheCorpusRow:
    async def test_the_row_is_a_v1_run_completion_with_the_eval_identity(
        self, ledger: LedgerStore
    ) -> None:
        task = _scratch_task()
        adapter = HarnessAdapter(
            harness_id="scratch-harness",
            mediation="mediated",
            description="scratch",
            command=lambda task, ws: ("true",),
            child_env=lambda task, ws: {},
        )
        result = _pair_result(task, "scratch-harness")
        correlation = uuid.uuid4()

        event = eval_ledger_row(correlation, "an-eval-id", task, adapter, result)
        assert event.event_type is EventType.PIPELINE_RUN_COMPLETED
        assert event.correlation_id == correlation
        assert event.discriminator == "eval:reverse-util:scratch-harness"

        # The golden shape: v first, then the run-completion vocabulary,
        # with the eval identity in metrics and nothing else.
        payload = event.payload
        assert set(payload) == {"v", "run_id", "status", "metrics"}
        assert payload["v"] == 1
        assert payload["run_id"] == "an-eval-id"
        assert payload["status"] == "pass"
        metrics = payload["metrics"]
        assert set(metrics) == {
            "kind",
            "task",
            "harness",
            "mediation",
            "task_sha256",
            "verdict_sha256",
            "checks",
        }
        assert metrics["kind"] == "eval"
        assert metrics["task"] == "reverse-util"
        assert metrics["harness"] == "scratch-harness"
        assert metrics["mediation"] == "mediated"
        assert metrics["task_sha256"] == task.manifest_digest()
        assert metrics["verdict_sha256"] == result.verdict.verdict_sha256()
        assert metrics["checks"] == {
            "tests_pass": "pass",
            "bundle_verifies": "pass",
            "diff_matches_intent": "pass",
            "forbidden_effects": "pass",
        }

        # ADR-0046 discipline, mechanically: small enough to stay inline,
        # and no check *reason* (free text) anywhere in the row.
        assert payload_size_bytes(payload) < BLOB_THRESHOLD_BYTES
        assert "reason" not in canonical_json(payload)
        assert "5 passed" not in canonical_json(payload)

    async def test_the_row_round_trips_through_a_real_store(
        self, ledger: LedgerStore
    ) -> None:
        task = _scratch_task()
        adapter = HarnessAdapter(
            harness_id="scratch-harness",
            mediation="observed",
            description="scratch",
            command=lambda task, ws: ("true",),
            child_env=lambda task, ws: {},
        )
        correlation = uuid.uuid4()
        sealed = await ledger.append(
            eval_ledger_row(
                correlation, "an-eval-id", task, adapter, _pair_result(task, "scratch-harness")
            )
        )
        assert sealed.sequence_id is not None

        rows = await ledger.read_correlation(correlation)
        assert len(rows) == 1
        assert rows[0].discriminator == "eval:reverse-util:scratch-harness"
        assert rows[0].payload["metrics"]["task"] == "reverse-util"

        # A redelivered append is suppressed, not duplicated: one pair,
        # one row, however many times the transport hands it over.
        again = await ledger.append(
            eval_ledger_row(
                correlation,
                "an-eval-id",
                task,
                adapter,
                _pair_result(task, "scratch-harness"),
            )
        )
        assert again.sequence_id == sealed.sequence_id
        assert len(await ledger.read_correlation(correlation)) == 1


class TestTheRefusalBoundary:
    async def test_an_adapter_that_cannot_run_is_a_scored_refusal_not_a_crash(
        self, home: Path
    ) -> None:
        from paa.sandbox.subprocess_backend import SubprocessSandbox

        task = _scratch_task()
        broken = HarnessAdapter(
            harness_id="broken-harness",
            mediation="mediated",
            description="a scratch adapter whose script is gone",
            command=_missing_script,
            child_env=lambda task, ws: {},
        )
        runner = EvalRunner(SubprocessSandbox(), home=home)
        result = await runner.run_pair(
            task, broken, eval_id="refusal-matrix", correlation_id=uuid.uuid4()
        )

        # The refusal is a measured fact on the row, and the row still
        # scores honestly against the untouched workspace.
        assert result.refusal is not None
        assert "does not exist" in result.refusal
        assert result.subject_state == "refused"
        by_name = result.verdict.by_name()
        assert by_name["tests_pass"].status is CheckStatus.BREACHED
        assert by_name["bundle_verifies"].status is CheckStatus.BREACHED
        assert "never happened" in by_name["bundle_verifies"].reason
        assert result.verdict.overall is CheckStatus.BREACHED


def _missing_script(task: EvalTask, workspace: Path) -> tuple[str, ...]:
    raise EvalError("the harness script /gone/harness.py does not exist")


class TestTheReportShape:
    async def test_a_re_run_of_the_same_pair_reproduces_the_verdict(
        self, home: Path
    ) -> None:
        """The determinism guarantee, on a real re-run of one observed pair.

        Two separate runs of the same task x harness pair — different
        eval ids, different workspaces, different wall times — must land
        the same verdict object and therefore the same verdict hash; the
        measured facts (which may vary) ride beside it, never inside it.
        """
        from paa.evals.harnesses import get_adapter
        from paa.sandbox.subprocess_backend import SubprocessSandbox

        task = _scratch_task()
        adapter = get_adapter("scripted-direct")
        runner = EvalRunner(
            SubprocessSandbox(), home=home, child_env=src_on_path()
        )
        first = await runner.run_pair(
            task, adapter, eval_id="rerun-one", correlation_id=uuid.uuid4()
        )
        second = await runner.run_pair(
            task, adapter, eval_id="rerun-two", correlation_id=uuid.uuid4()
        )

        assert first.verdict == second.verdict
        assert first.verdict.verdict_sha256() == second.verdict.verdict_sha256()
        assert first.task_sha256 == second.task_sha256
