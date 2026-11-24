"""The pipeline package: what a pipeline is, and the only door it gets.

P1.1 of docs/plans/2026-11-21-pipeline-platform-spec.md freezes the base
vocabulary here (ADR-0051); P1.2 adds the built-in pipeline that the
orchestrator delegates to (:mod:`paa.pipelines.builtin`). Wave 6 adds the
generalised call journal (:mod:`paa.pipelines.journal`, C6.2/P2.1) — the
seq/fingerprint/outcome record ADR-0050 names the checkpoint. The registry,
selection and overlays arrive with C6.4/C6.5 in the same wave.
"""

from paa.pipelines.base import (
    FACADE_RUNTIME_VERSION,
    Capability,
    CapabilityNotGrantedError,
    CapabilitySet,
    CapabilityToken,
    GateQuestion,
    GateRejectedError,
    InvalidCallParamsError,
    OutcomeUnknownError,
    Pipeline,
    PipelineCancelledError,
    PipelineDivergedError,
    PipelineError,
    PipelineResult,
    PipelineStatus,
    PipelineTask,
    RunLimits,
    compute_grants,
)
from paa.pipelines.journal import (
    PIPELINE_JOURNAL_EVENT_TYPES,
    CallJournal,
    CallOutcome,
    CallRecord,
    fingerprint,
    fold_call_journal,
    params_shape,
    read_call_journal,
)

__all__ = [
    "FACADE_RUNTIME_VERSION",
    "PIPELINE_JOURNAL_EVENT_TYPES",
    "CallJournal",
    "CallOutcome",
    "CallRecord",
    "Capability",
    "CapabilityNotGrantedError",
    "CapabilitySet",
    "CapabilityToken",
    "GateQuestion",
    "GateRejectedError",
    "InvalidCallParamsError",
    "OutcomeUnknownError",
    "Pipeline",
    "PipelineCancelledError",
    "PipelineDivergedError",
    "PipelineError",
    "PipelineResult",
    "PipelineStatus",
    "PipelineTask",
    "RunLimits",
    "compute_grants",
    "fingerprint",
    "fold_call_journal",
    "params_shape",
    "read_call_journal",
]
