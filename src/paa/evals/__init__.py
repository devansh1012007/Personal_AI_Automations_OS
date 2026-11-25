"""The eval harness — same tasks, N harnesses, deterministic scoring (C7.4).

SPEC 2 §6 / ROADMAP H3.6: the cross-harness eval harness. One task set
(:mod:`paa.evals.tasks`, repository data under ``evals/``), N registered
harness adapters (:mod:`paa.evals.harnesses`), one runner
(:mod:`paa.evals.runner`) that runs each task x harness pair inside
PAA's evidence boundary and scores it with checks that are machine
verdicts end to end — tests pass, bundle verifies, diff matches intent,
no forbidden effects — never a model judgment (the spec's rule 2).

The two shipped adapters are scripted fakes (deterministic, offline,
no model key); the registry is where real harnesses plug in by name.
Every result is a ledger row (``PIPELINE_RUN_COMPLETED`` on the matrix's
own lineage — see :mod:`paa.evals.runner` for why that member and not a
new one), which is the compounding corpus the spec calls the data bet.
"""

from paa.evals.harnesses import (
    HarnessAdapter,
    get_adapter,
    known_adapters,
    register_adapter,
)
from paa.evals.runner import EVAL_POLICY, REPORT_SCHEMA, EvalReport, EvalRunner
from paa.evals.scoring import (
    CHECK_BUNDLE,
    CHECK_FORBIDDEN,
    CHECK_INTENT,
    CHECK_TESTS,
    CheckStatus,
    CheckVerdict,
    PairVerdict,
)
from paa.evals.tasks import (
    TASK_SCHEMA,
    EvalError,
    EvalTask,
    load_task,
    load_tasks,
    task_set_root,
)

__all__ = [
    "CHECK_BUNDLE",
    "CHECK_FORBIDDEN",
    "CHECK_INTENT",
    "CHECK_TESTS",
    "EVAL_POLICY",
    "REPORT_SCHEMA",
    "TASK_SCHEMA",
    "CheckStatus",
    "CheckVerdict",
    "EvalError",
    "EvalReport",
    "EvalRunner",
    "EvalTask",
    "HarnessAdapter",
    "PairVerdict",
    "get_adapter",
    "known_adapters",
    "load_task",
    "load_tasks",
    "register_adapter",
    "task_set_root",
]
