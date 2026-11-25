# The observation bundle (`paa-observe-bundle/1`)

**Status: built (SPEC 2 §3.1 C7.1, H3.2 observer mode).** `paa observe -- <cmd>`
runs any CLI in a workspace under the sandbox's one-shot contract and leaves a
complete **T1 evidence bundle** on disk: the child's transcript, the file
manifest's before/after diff, git's own view of the changes, and a named
classification of the state the run left behind. The writer and reader live in
`src/paa/observe/bundle.py`; the command entry is `src/paa/cli/observe.py`.

The bundle is the spec's *cheapest honest* evidence about what an external
harness did (pipeline-platform spec §6): it does not claim the harness behaved
— only that these bytes came out of it, these files changed under it, and this
is the state it left the workspace in. What it deliberately never claims is a
resume: every observe bundle says `resume = "none"`, and the recovery sweep
refuses to re-drive the lineage.

## Where bundles live

`$PAA_HOME/observe/<run-id>/bundle.json`. The run id is the ledger lineage's
correlation id, so a run, its trace (`paa pipeline trace <run-id>`) and its
artifact share one name.

## The JSON shape

```json
{
  "schema": "paa-observe-bundle/1",
  "phase": "final",
  "run": {
    "run_id": "0f1e…",
    "command": ["pytest", "-q"],
    "workspace": "/abs/path",
    "started_at": "2026-11-24T17:05:12.301+00:00",
    "ended_at": "2026-11-24T17:05:14.882+00:00",
    "resume": "none",
    "evidence_tier": "T1",
    "observer_version": "4.1.0"
  },
  "child": {
    "exit_code": 0,
    "timed_out": false,
    "killed_reason": null,
    "duration_ms": 2581.0,
    "peak_rss_mb": 88.5,
    "backend": "subprocess",
    "isolation_level": "PROCESS",
    "truncated_output": false
  },
  "transcript": {
    "stdout": {
      "stream": "stdout",
      "sha256": "…",
      "bytes": 20481,
      "truncated": false,
      "text": "the full text, or the head under the cap"
    },
    "stderr": { "stream": "stderr", "…": "…" }
  },
  "manifest": {
    "before_count": 41,
    "after_count": 42,
    "created":   [{ "path": "new.txt", "size": 12, "sha256": "…" }],
    "modified":  [{ "before": {…}, "after": {…} }],
    "deleted":   [{ "path": "old.txt", "size": 3, "sha256": "…" }]
  },
  "git": {
    "available": true,
    "reason": "",
    "status": " M src/x.py\n?? new.txt",
    "diff": "diff --git a/… (capped at 256 KiB)",
    "head_before": "a1b2…",
    "head_after": "c3d4…",
    "commits": [{ "line": "c3d4… by harness" }],
    "dirty": true
  },
  "classification": {
    "state": "clean_exit",
    "exit_code": 0,
    "signal": null,
    "signal_name": null,
    "duration_ms": 2581.0,
    "workspace_changed": true,
    "files_created": 1,
    "files_modified": 1,
    "files_deleted": 0,
    "git_available": true,
    "git_dirty": true,
    "commits": 0,
    "notes": "…",
    "resume": "none",
    "recoverable": false
  }
}
```

## The two-phase write

The file is written twice. A skeleton with `phase = "started"` lands **before**
the child is spawned; the final record with `phase = "final"` lands after
classification. A bundle that still says `started` is the crash marker for an
observer or child that never came back — the file itself is the evidence, and
nothing silently upgrades it. Writes are atomic (`<path>.tmp` + rename), so a
reader never sees a splice from a killed process. This is not a power-loss
fsync contract: a host crash can still lose an unflushed rename.

## The honesty rules, field by field

| Field | Rule |
|---|---|
| `run.resume` | Always `"none"` (spec §6: external-harness adapters cannot honour the replay contract). |
| `run.evidence_tier` | Always `"T1"` — observed from outside. Mediated (`T2`) and contained (`T3`) belong to later adapters. |
| `classification.state` | One of `clean_exit`, `error_exit`, `signalled`, `timed_out`, `resource_killed`. The sandbox's `killed_reason` outranks the exit code; a negative POSIX exit is decoded to its signal number. |
| `classification.recoverable` | Always `false`, and exists to be read: the answer to "can I resume this?" comes from the data, not from a doc. |
| `transcript.*.truncated` | The cap (`--transcript-cap-kb`, default 2048) is applied to the encoded length with an explicit marker — never a silent tail drop. The hash covers the kept text; `bytes` records what the child actually produced. |
| `git.available` | `false` with a `reason` is an honest answer (not a repo, or no git) — the bundle says so rather than inventing a diff. |
| `git.diff` | Capped at 256 KiB with an explicit truncation line. |

## Consuming bundles

`read_bundle(path)` (same module) validates the schema tag and refuses a file
that is not a `paa-observe-bundle/1` — a foreign file must not be silently
reinterpreted. `bundle.sha256()` is the canonical-JSON content hash the
ledger's `observe.run` COMPLETED row attests (`result_sha256` /
`bundle_sha256`) — it hashes sorted-key JSON, **not** the indented bytes of
the file on disk, so `sha256sum bundle.json` is a different digest. The
transcript text never rides the ledger.

## The pipeline door

`builtin.observe` is pipeline #4 in the registry (`paa pipeline list`,
`paa pipeline inspect builtin.observe`) — same facade contract as the other
three, zero capabilities asked, and exactly two declared differences:
`resume = "none"`, `evidence_tier = "T1"`. `paa pipeline run builtin.observe
--goal "pytest -q"` and `paa observe -- pytest -q` journal identically and
leave the same bundle.

## CLI quick reference

```bash
paa observe -- pytest -q                       # observe, print the verdict
paa observe --json -- make build               # metrics as JSON
paa observe -w src/ --timeout-s 60 -- go test  # workspace + wall clock
paa pipeline trace <run-id>                    # the lineage's rows
```

The exit code is the observed CLI's fate: `0` only for a clean exit. A crashed
or failed harness is a *successful observation* with a failed subject — the
command says so and exits `1`, never pretending the harness succeeded.
