"""The recorder — ``paa record``, the harness-neutral flight recorder (C7.3).

SPEC 2 §6 / ROADMAP H3.5's distribution half: a harness (Claude Code,
Codex, Gemini CLI, anything that runs a command) runs *inside* PAA's
evidence boundary instead of beside it. The recorder is three doors
over one record:

* :mod:`paa.recorder.record` — the hash-chained flight record,
  ``paa-record/1``: append-only, crash-safe, tamper-evident;
* :mod:`paa.recorder.proxy` — the MCP policy proxy: the harness's tool
  calls, journalled, decided by the run's policy, executed under
  containment;
* :mod:`paa.recorder.core` — the sequencing: the harness under a
  :class:`~paa.sandbox.base.SandboxSession`, the snapshots, the
  classification, the observe bundle (T1 floor) and the attested
  change bundle (the reviewer's artifact, ``paa verify``-able offline).

The honesty line the whole package holds: **mediated** and **observed**
are never collapsed. Tool calls that rode the proxy are facts with
hash-chained witnesses; everything else the harness did is visible only
as manifest and git deltas — stated as such, never inferred.
"""

from paa.recorder.core import (
    POLICY_MODES,
    PolicySpec,
    RecorderCore,
    RecordOutcome,
    child_env_for_run,
)
from paa.recorder.record import (
    EVENT_KINDS,
    RECORD_SCHEMA,
    RecordError,
    RecordVerdict,
    RunRecord,
    read_summary,
    verify_record,
)

__all__ = [
    "EVENT_KINDS",
    "POLICY_MODES",
    "RECORD_SCHEMA",
    "PolicySpec",
    "RecordError",
    "RecordOutcome",
    "RecordVerdict",
    "RecorderCore",
    "RunRecord",
    "child_env_for_run",
    "read_summary",
    "verify_record",
]
