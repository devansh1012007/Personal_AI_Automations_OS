"""``paa observe`` — the T1 observer: run any CLI, watch honestly (C7.1).

The spec's cheapest honest evidence about what an external harness
actually did: run the CLI in a workspace, record the manifest diff,
git's own view of the changes and the child's transcript, and classify
the state a crash leaves behind. The bundle
(:mod:`paa.observe.bundle`) is the artifact, the core
(:mod:`paa.observe.observer`) is the sequencing and the crash
discipline, and :mod:`paa.observe.pipeline` is the adapter as pipeline
#4 — ``resume = "none"``, ``evidence_tier = "T1"``, on the same facade
contract as the other three.
"""

from paa.observe.bundle import (
    BUNDLE_SCHEMA,
    EVIDENCE_TIER,
    GitChanges,
    ManifestDiff,
    ManifestSnapshot,
    ObserveBundle,
    read_bundle,
)
from paa.observe.observer import (
    CrashClass,
    CrashClassification,
    ObserveOutcome,
    ObserverCore,
    classify_outcome,
)
from paa.observe.pipeline import (
    OBSERVE_CAPABILITIES,
    ObserveFacade,
    ObservePipeline,
)

__all__ = [
    "BUNDLE_SCHEMA",
    "EVIDENCE_TIER",
    "OBSERVE_CAPABILITIES",
    "CrashClass",
    "CrashClassification",
    "GitChanges",
    "ManifestDiff",
    "ManifestSnapshot",
    "ObserveBundle",
    "ObserveFacade",
    "ObserveOutcome",
    "ObservePipeline",
    "ObserverCore",
    "classify_outcome",
    "read_bundle",
]
