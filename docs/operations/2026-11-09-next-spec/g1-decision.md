# G1 decision — the real-model S1 gate (H1.3)

**Status: PENDING — operator run required.** Not approved, and not faked: the
live frontier-model run this decision feeds on has not happened in an
environment that holds a model API key. Every field below is empty on
purpose, and names who fills it and when.

- **Written:** 2026-11-22.
- **Scope:** G1, the repo's own Horizon 1 gate. S1 is the real-model end of
  H1.3 — the ROADMAP row reads "**Real-model end to end.** Drive one task
  with a frontier provider through configuration, record cost and failure
  modes," done when there is "A documented run from request to
  `MUTATION_COMMITTED`, with the trace and the failures it hit." The gate
  itself asks "Is PAA useful to its own author on real work?" and its evidence
  row is "Two weeks of daily use; ledger shows tasks, mandates, recoveries."

## Why it is pending

The S1 run needs a Linux/WSL2 host and a frontier-model API key in the
operator's environment; this sandbox has no model key, so the run is
operator-blocking. Everything the gate sits on is landed and tested — the
fixture, the coder loop, the work tools, the ledger-before-effect journal
(the S1 dry-run rung: 47/47 with real bash), and the Wave 4 surfaces
(`paa chat`, `paa mcp serve`) — so the real-model run is the gate's only
open row. The evidence file, `s1-report.md` in this directory, already
exists and is honest about what it holds: the 2026-11-10 live run under the
free frontier model `nvidia/nemotron-3-ultra-550b:a55b:free` SUCCEEDED (fix
applied, suite green, 12 + 12 tool rows, chain ok) — but the paid-model
attempts were BLOCKED at the provider (HTTP 403, key limit exceeded), the
cost figures and ledger export were never captured (the report's own
"honest gap"), and the live run made no `git_stage`/`git_commit` calls, so
the fix sits as an uncommitted working-tree change and H1.3's "request to
`MUTATION_COMMITTED`" has no live evidence yet. The run book below closes
those three gaps; the template records what follows.

## Operator run book

Linux/WSL2, Python 3.12 venv, `.[dev]` extra. Model selection is entirely
`PAA_MODELS__*` configuration — `paa agent` takes no model flag (see
[`configuration-and-environment.md`](../configuration-and-environment.md),
`ModelSettings`):

```bash
export OPENROUTER_API_KEY=sk-or-...              # named by the registry row's api_key_env
export PAA_MODELS__LOCAL_PROVIDER=openrouter
export PAA_MODELS__LOCAL_MODEL=nvidia/nemotron-3-ultra-550b:a55b:free
export PAA_MODELS__ESCALATION_PROVIDER=none
export PAA_HOME=$HOME/.paa-s1                    # throwaway home; evidence stays out of daily state

cp -r examples/fixtures/buggy_calc /tmp/buggy_calc
cd /tmp/buggy_calc
python -m pytest test_calc.py -q                # pre-run state: 1 failed, 4 passed
git init && git add -A && git commit -m "buggy head"   # git tools need a repo; mirrors the dry-run harness
paa agent --list-tools                           # catalogue + resolved model line, no tokens spent
paa agent --workspace /tmp/buggy_calc --mode ask --yes "fix the failing test"
```

Capture (win or lose — a provider-blocked run is still a recorded failure
mode, as the 2026-11-10 403 pair was), appended to `s1-report.md`'s
real-model rung. That file exists; record against what it actually holds,
not a hypothetical:

1. **Ledger evidence.** `paa ledger <correlation-id>` for the mandate
   lineage (`MANDATE_GRANTED` head, `basis=explicit`) and the tool-call
   lineage (one `TOOL_CALL_PROPOSED` + `TOOL_CALL_COMPLETED` pair per call,
   `chain integrity: ok`, `bash` rows attesting `PROCESS`, in-process rows
   `NONE`). The run prints the session id but not the lineage ids; read
   them from the throwaway ledger's `system_state_correlation_head` table
   under `$PAA_HOME/state/paa.db`.
2. **Cost.** This seam writes no token-cost rows to the ledger
   (`ModelTurn.tokens` is always 0 on the coder path; the router's usage
   tally lives in-process), so take the cost rows from the provider's own
   usage report for the run window, with the ledger's tool-call count as the
   denominator — exactly the gap the 2026-11-10 report left open.
3. **Failure modes.** Every refusal, provider error, escalation block, and
   recovery classification — plus whether the run ends in `git_commit` (the
   `MUTATION_COMMITTED` end of H1.3's done-when; the 2026-11-10 live run
   stopped short of it).

## Decision template

To be filled by the operator after the run. Two outcomes only (ROADMAP
sequencing; SPEC 2 §11 open decision 1):

- **A — continue to Horizon 2** (the ROADMAP's H2 rows: event envelope,
  out-of-process hooks, workspace isolation, credential broker, first
  external agent, executor-port spike): pick when the run's cost and failure
  modes are inside what the author will tolerate daily.
- **B — fix daily-use blockers first**: pick when the run surfaced a
  blocker that makes daily use worse than not using PAA.

| Field | Entry |
|---|---|
| Cost of the S1 run | — pending, to be filled by the operator after the run |
| Failure modes observed | — pending, to be filled by the operator after the run |
| Daily-use blockers | — pending, to be filled by the operator after the run |
| **Decision (A or B)** | — pending, to be filled by the operator after the run |
| Decided by / date | — pending, to be filled by the operator after the run |

Until those fields carry a run that actually happened, this document asserts
nothing beyond its status line.
