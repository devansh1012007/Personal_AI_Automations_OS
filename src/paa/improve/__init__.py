"""The guarded self-improvement loop — SPEC 2 Wave 8, row C8.1.

The owner's "after MVP" item, made concrete: **reflection → patch
proposal → deterministic validation → human gate → commit**, as a
pipeline (:class:`~paa.improve.pipeline.ImproveReflectionLoop`,
registered as ``improve.reflection-loop``). It is wired from the
existing :mod:`paa.improvement` primitives — the Weekly Reflection
Engine above all, whose ``analyze_window`` is the first stage unchanged
— rather than forking them, and every improvement lands **as an
attested change bundle** (C7.2): self-modification inherits the
evidence chain instead of bypassing it.

The pieces:

``proposal``
    the deterministic middle artifact — described change (files, intent,
    rationale), never applied by the stage that produced it;
``validation``
    the proposal against a scratch copy, the repository's own gate
    commands as subprocesses, exit codes as the verdict — no model
    anywhere in the decision;
``gate``
    the human decision artifact and the approver seam — approval and
    refusal both first-class, an unwired gate refuses;
``gitcommit``
    the guarded host-side git commit, behind the ADR-0047 integrity
    gate the coder package owns;
``pipeline``
    the five-stage loop and its facade, journalled through the call
    journal and replay-aware.

**The honesty line (ADR-0017):** prototypical few-shot classification
≠ gradient meta-learning. Nothing in this package is gradient
meta-learning, and the prototypical classifier it refers to lives in
``paa.improvement.meta`` and is analysis-only; this loop is a
deterministic function of the ledger's friction numbers driving a
guarded change process. **No gate is ever model-decided** (SPEC 2 §9
rule 2): the pipeline declares no ``think``, validation is exit codes,
and the only approval that exists is a human's.
"""

from __future__ import annotations

from paa.improve.gate import (
    DECISION_APPROVED,
    DECISION_REJECTED,
    Approver,
    HumanDecision,
    refuse_all,
    terminal_approver,
)
from paa.improve.gitcommit import GitCommitResult, guarded_git_commit, is_git_repository
from paa.improve.pipeline import (
    IMPROVE_CAPABILITIES,
    ImproveFacade,
    ImproveReflectionLoop,
)
from paa.improve.proposal import (
    DEFAULT_TARGET_FILE,
    PROPOSAL_SCHEMA,
    FileChange,
    PatchProposal,
    proposal_from_report,
    read_proposal,
    write_proposal,
)
from paa.improve.validation import (
    CommandOutcome,
    ValidationVerdict,
    run_validation,
)

__all__ = [
    "DECISION_APPROVED",
    "DECISION_REJECTED",
    "DEFAULT_TARGET_FILE",
    "IMPROVE_CAPABILITIES",
    "PROPOSAL_SCHEMA",
    "Approver",
    "CommandOutcome",
    "FileChange",
    "GitCommitResult",
    "HumanDecision",
    "ImproveFacade",
    "ImproveReflectionLoop",
    "PatchProposal",
    "ValidationVerdict",
    "guarded_git_commit",
    "is_git_repository",
    "proposal_from_report",
    "read_proposal",
    "refuse_all",
    "run_validation",
    "terminal_approver",
    "write_proposal",
]
