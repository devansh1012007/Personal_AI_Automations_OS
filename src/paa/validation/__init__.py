"""Deterministic, host-side validation. RFC §13.

Nothing in this package consults a model. Every check is a pure function of its
input, runs on the host, and returns the same verdict every time — which is the
property that lets the runtime treat a PASS as a security decision rather than
an opinion.

The division of labour is deliberate: an LLM critic judges whether an artifact
is *good* (does it solve the task, is it idiomatic, will it be maintainable),
and this package judges whether it is *safe and structurally valid*. A critic
can be argued out of its position by the text it is reviewing; an AST walk
cannot.

SPEC DEVIATION (docs/adr/0010): the JSON Schema validator implements a
documented subset in pure Python rather than depending on ``jsonschema`` — see
:mod:`paa.validation.schema_validator` for why an optional dependency would
make validation strictness machine-dependent.
"""

from __future__ import annotations

from paa.validation.ast_scanner import (
    AstSecurityScanner,
    Finding,
    ScannerPolicy,
    Severity,
)
from paa.validation.engine import (
    CheckResult,
    DeterministicValidationEngine,
    ValidationArtifact,
    ValidationReport,
    ValidationVerdict,
)
from paa.validation.patch import (
    FilePatch,
    Hunk,
    PatchAction,
    PatchApplier,
    PatchJournal,
    PatchPlan,
    PatchPlanEntry,
    UnifiedDiffValidator,
    compute_patch_sha256,
    safe_relative_path,
)
from paa.validation.schema_validator import SchemaError, SchemaValidator, validate_instance
from paa.validation.test_runner import PytestResult, PytestRunner
from paa.validation.workspace import (
    DEFAULT_EXCLUDES,
    ManifestDiff,
    RestoreReport,
    WorkspaceSnapshot,
    hash_file,
)

__all__ = [
    "DEFAULT_EXCLUDES",
    "AstSecurityScanner",
    "CheckResult",
    "DeterministicValidationEngine",
    "FilePatch",
    "Finding",
    "Hunk",
    "ManifestDiff",
    "PatchAction",
    "PatchApplier",
    "PatchJournal",
    "PatchPlan",
    "PatchPlanEntry",
    "PytestResult",
    "PytestRunner",
    "RestoreReport",
    "ScannerPolicy",
    "SchemaError",
    "SchemaValidator",
    "Severity",
    "UnifiedDiffValidator",
    "ValidationArtifact",
    "ValidationReport",
    "ValidationVerdict",
    "WorkspaceSnapshot",
    "compute_patch_sha256",
    "hash_file",
    "safe_relative_path",
    "validate_instance",
]
