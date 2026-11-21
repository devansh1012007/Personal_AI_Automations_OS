"""The pipeline package: what a pipeline is, and the only door it gets.

P1.1 of docs/plans/2026-11-21-pipeline-platform-spec.md freezes the base
vocabulary here (ADR-0051); P1.2 adds the built-in pipeline that the
orchestrator delegates to (:mod:`paa.pipelines.builtin`). The registry,
selection and overlays are later phases and deliberately absent — extract,
don't design (the spec's §0 rule of three).
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

__all__ = [
    "FACADE_RUNTIME_VERSION",
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
]
