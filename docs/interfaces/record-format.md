# The flight record (`paa-record/1`)

**Status: built (SPEC 2 §6 C7.3, H3.5's recorder half).** `paa record run --
<harness-cmd>` runs a harness inside PAA's evidence boundary and journals
everything the boundary sees into one **hash-chained, append-only** file,
`record.jsonl`, as it happens — not reconstructed after the fact. The writer
and verifier live in `src/paa/recorder/record.py`; the command entries are
`src/paa/cli/record.py` (`run`, `proxy`, `verify`).

The record is the *mediation* artifact, one tier up from the observe bundle:
where `paa observe` watches a harness it cannot influence, the recorder is the
boundary the harness's tool calls pass through. Three doors share one record:

| Door | What it does |
|---|---|
| `paa record run -- <cmd>` | Wraps the harness in a `SandboxSession` (lifetime budget, whole-tree kill), relays its stdout live, snapshots the workspace before/after, classifies the aftermath, writes the observe bundle (the T1 floor) and the attested change bundle at close. |
| `paa record proxy --run-dir <dir>` | The **MCP policy proxy** the harness spawns (its tool calls ride it): every call journalled → decided by the run's recorded policy → executed under containment → answered. |
| `paa record verify <run-dir>` | The reviewer's door over the chain itself: schema, sequence, every link's digest, the summary, the phase. |

## Where records live

`$PAA_HOME/record/<run-id>/`:

```
<run-dir>/
  record.json             the summary (phase, policy in force, counts, chain root)
  record.jsonl            the hash-chained events, append-only
  observe-bundle.json     the T1 floor for the same child (paa-observe-bundle/1)
```

The attested bundle built at close lands under `$PAA_HOME/attest/record-<run
id prefix>/` and cites the run in its evidence section.

## The event envelope

Every line of `record.jsonl` is one event:

```json
{
  "schema": "paa-record/1",
  "seq": 4,
  "at": "2026-11-25T04:12:09.301+00:00",
  "kind": "tool_result",
  "data": { "tool": "bash", "argv": ["git", "status"], "ok": true, "…": "…" },
  "prev": "<sha256 of the previous event>",
  "sha256": "<sha256 of this event's canonical JSON, sans the sha256 field>"
}
```

`sha256` covers exactly `{schema, seq, at, kind, data, prev}` in canonical
JSON (sorted keys, no whitespace). `prev` is the previous event's `sha256`;
the first event's `prev` is the word `genesis` — a word, not a hash, so it can
never collide with a real predecessor's digest. It is the same construction
the attested bundle's section chain rides, so one mental model serves both:
substitute a payload, reorder, truncate or forge a line, and the chain breaks
at the first divergence.

### The event kinds (closed vocabulary)

| Kind | Data (essentials) |
|---|---|
| `run_started` | run id, harness argv, workspace, recorder version, pid |
| `policy_config` | mode, allow/deny/ask rules, the ask resolution |
| `snapshot_before` | file count, manifest digest, git head |
| `child_spawned` | pid, backend, isolation level, budgets |
| `proxy_started` | proxy name, protocol version, client version, policy mode |
| `tool_call` | tool name, journalled arguments (capped), `via: "proxy"` |
| `tool_result` | tool, normalised argv, policy reason, execution facts |
| `policy_denied` | tool, raw decision (`deny`/`ask`), resolution, reason, rule |
| `proxy_closed` | reason (`eof`), tool call and denial counts |
| `child_exit` | exit code, killed reason, duration, peak RSS, backend facts |
| `snapshot_after` | file counts, manifest diff, git head/dirty/commits, state |
| `record_finalised` | counts, classification, artifact citations |

A `tool_result` with `executed: "sandbox"` ran contained (the `bash` tool);
`executed: "in-process"` ran jailed in-process (the file tools);
`executed: "refused"` never ran (the workspace jail refused the target). A
`policy_denied` with `decision: "ask"` and `resolution: "denied_unattended"`
is the unattended ask: the policy wanted a human, the stdio proxy has no
interactive door, and the refusal is recorded with the raw decision kept
beside it — never hidden, never upgraded to a quiet allow.

## The summary (`record.json`)

Written twice, atomically: `phase: "started"` before the harness spawns (the
crash marker — a record still saying `started` is a run that never came
back), `phase: "final"` after the last event. Carries the run identity, the
**policy that was in force** (the single source the proxy rebuilds its
engine from — two proxies serving one run cannot disagree), the `honesty`
block (mediated vs observed, stated once), the `containment` block (what was
actually in force, and its limits), event counts, the chain root and the
artifact paths.

## Properties the format holds by construction

* **Append-only, crash-safe.** Each event is one newline-terminated line,
  flushed and `fsync`-ed before the writer proceeds. A final line without
  its newline is a **torn write** and `paa record verify` reports it as such,
  never silently drops it.
* **One chain, sequential writers.** The core writes before the harness
  spawns and after it exits; the proxy writes in between. Each append takes
  an exclusive `flock`, re-reads the on-disk tail, verifies that tail's own
  digest, and extends it — a second writer's events extend the same chain,
  and a tail that does not verify is refused, loudly.
* **Mediated ≠ observed, never collapsed.** Tool events exist only for calls
  that rode the proxy. Everything the harness did outside it is visible only
  as the observe bundle's manifest/git deltas — the T1 floor. The record
  never infers a tool call from a file change.

## The MCP policy proxy

The harness (or its MCP config — Claude Code's `.mcp.json`, Codex's config,
Cursor's, OpenCode's) spawns:

```
paa record proxy --run-dir <dir>
```

The run's env carries everything a cooperating harness needs:
`PAA_RECORD_RUN_ID`, `PAA_RECORD_RUN_DIR`, `PAA_RECORD_WORKSPACE`, and
`PAA_RECORD_PROXY_CMD` (the ready-to-paste stanza). The proxy speaks MCP
`2024-11-05` over stdio (NDJSON or Content-Length framing, autodetected per
message — the same framing kit as `paa mcp serve`), serves three tools
(`bash`, `read_file`, `write_file`), and answers `initialize`, `tools/list`,
`tools/call`, `ping`.

Per call, in order: **journal** (`tool_call`) → **validate** (shape) →
**decide** (the run's `ToolPermissionPolicy`: allow / deny / ask) →
**execute** (sandbox one-shot for `bash`; jailed in-process for the file
tools) → **journal** (`tool_result` or `policy_denied`) → **answer**. Policy
refusals are JSON-RPC `-32002`; tool execution failures are MCP
`isError: true` results — the same convention the C4.5 server states.

No auth token, on purpose: the proxy is an ephemeral per-run stdio child, and
whoever can spawn it already holds local process authority in the workspace.
The integrity boundary for what it journalled is the hash chain and the
attested bundle's signature, both of which a hostile local writer breaks only
visibly.

## The attested bundle at close

Unless `--no-attest`, the recorder builds an attested change bundle
(`paa-attest-bundle/1`) at close: the run's mandate (the `--goal`, defaulting
to the recorded command line), the workspace diff, the observe bundle as
evidence, **the flight record itself as an evidence blob** (`kind:
"paa-record/1"`, carrying its event count and chain root), and one *observed*
command row per `bash` call the record witnessed executing — each citing the
record blob's digest, never asserted. The `record_finalised` event cites the
attested bundle and the embedded record blob, so the live chain's last word
covers the exact bytes a reviewer will `paa verify`.

The embedded record is the flight record **as of the attestation point** —
the events up to `snapshot_after`; the finalise event that cites it is the
live record's visible extension of that prefix.

## Verification

```
paa record verify <run-dir> [--json]
```

Six checks, each reported independently: schema, sequence, chain, summary,
torn tail, finalised. Exit 0 only when every door held *and* the run
finalised. A record at `phase: "started"` verifies as a chain but reports the
crash marker — the honest answer to "did this run finish?" is data, not a
guess.
