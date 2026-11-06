# Roadmap

*Last reviewed: 2026-11-06 · Companion to [VISION.md](VISION.md) · Evidence base:
[research note on DeepSeek Super-agent](research/2026-11-deepseek-super-agent.md)*

This roadmap turns the [vision](VISION.md) into ordered work with an exit test per
item. It replaces the earlier phase-based roadmap; that version's item-level detail,
including the test-coverage notes for Phase 0, remains in git history.

---

## Contents

1. [Where things stand](#where-things-stand)
2. [How to read this](#how-to-read-this)
3. [Horizon 1: Hands, authority, and honesty](#horizon-1-hands-authority-and-honesty)
4. [Horizon 2: Seams](#horizon-2-seams)
5. [Horizon 3: Evidence and attention](#horizon-3-evidence-and-attention)
6. [Horizons 4 and 5 (conditional)](#horizons-4-and-5-conditional)
7. [Ideas imported from DeepSeek Super-agent](#ideas-imported-from-deepseek-super-agent)
8. [Sequencing and dependencies](#sequencing-and-dependencies)
9. [Decision gates and metrics](#decision-gates-and-metrics)
10. [Cross-cutting work](#cross-cutting-work)
11. [Decisions that need an owner](#decisions-that-need-an-owner)

---

## Where things stand

Measured on a clean Linux checkout at commit `4ac96db` (Python 3.12, `.[dev]`, no
Docker): about 52,000 lines of source, 3,145 tests collected, 2,899 passed, 241
skipped, 4 xfailed, 1 failed (environmental). Re-measure with the commands in the
[README](../README.md#status) before quoting any figure.

**True now.** The spine (ledger, replay, recovery, storage, context, sandbox,
validation, agents, memory, models, skills) is built and tested. One task flows from
request to commit through the composition root, survives a mid-execution stall, and
holds the token ceiling. The `paa` CLI, loopback API, daemon, and an experimental ACP
server exist.

**Not true yet.**

- No frontier model has driven a task end to end. Every end-to-end run uses an
  injected model, so passing tests establish the wiring, not model performance.
- There are no file, shell, or git tools. The coder agent can drive the runtime but
  cannot change a project.
- The coder loop is not ledger-integrated, so it gets neither crash recovery nor
  hash-chained history.
- Tasks do not record **who requested them or under which policy**.
- Third-party agents cannot be hosted, and no supported way exists to replace the
  agentic loop.

The honest summary: *a well-engineered runtime that has not yet done real work for a
real person.* The sequence below is ordered to fix that first.

## How to read this

- **Effort:** **S** hours · **M** 1–2 days · **L** 3–5 days · **XL** 1–2+ weeks.
  Solo-developer estimates; treat them as ordering aids, not commitments.
- **Done when:** an observable test, not a description of work.
- **Origin:** `PAA` for ideas from this project; `DS §n` for ideas taken or adapted from
  DeepSeek's Super-agent essay section *n*; `DS doc` for its prototype docs; `DS field`
  for failures independent users reported in the prototype.
- Horizons 1–3 are committed. Horizons 4–5 are **conditional** on the
  [validation test](VISION.md#validation-and-kill-criteria).

---

## Horizon 1: Hands, authority, and honesty

*Make PAA capable of real work, record who authorised it, and make its documentation
trustworthy.*

| # | Task | Effort | Done when | Origin |
|---|---|---|---|---|
| H1.1 | **File, shell, and git tools** as permissioned skills run through the sandbox: read, write, anchored edit, glob, grep, bash, git status/diff/commit. Destructive git operations require a gate. | **XL** | Each tool has permission-profile tests; path traversal is refused; a denied tool call is a ledger event; the tools work under the subprocess, WSL2, and Docker backends. | PAA |
| H1.2 | **Wire the coder loop into the ledger.** Every tool call and result becomes an event; the loop resumes after a crash. | **L** | `kill -9` mid-turn, restart, and `paa ledger <id>` shows an unbroken chain that resumes without repeating a completed side effect. | PAA, flexibility audit |
| H1.3 | **Real-model end to end.** Drive one task with a frontier provider through configuration, record cost and failure modes. | **M** | A documented run from request to `MUTATION_COMMITTED`, with the trace and the failures it hit. | PAA |
| H1.4 | **Mandates.** New events `TRIGGER_RECEIVED`, `ELIGIBILITY_DECIDED` (strength: explicit or inferred; matched rules), `MANDATE_GRANTED` (policy version, scope, requester identity). `POST /tasks` and the CLI require a requester and basis. | **M–L** | A task with no mandate is refused; a policy change that bears on a live task is recorded and delivered to it rather than silently ignored; `paa ledger` shows the mandate at the head of the chain. | DS doc 001, 002 |
| H1.5 | **Asynchronous tool calls with cancellation.** Long commands return immediately and deliver results as events; the loop accepts new input mid-run; a cancel stops running work. | **L** | A 10-minute command does not block a new instruction; cancel kills the process tree and writes a ledger event; ACP cancel works. | DS §4.1 |
| H1.6 | **Lifecycle fault tests** for the failure classes the prototype hit: retry after authorisation changed, stop while preparing a workspace, partial workspace after a failed prepare, event poll that reports success while skipping items, duplicate trigger. | **M** | Property and chaos tests fail on each injected fault and pass on the implementation. | DS field |
| H1.7 | **Documentation truth.** One script prints the status block (source size, test totals, outcome counts); README, VISION, and ROADMAP quote its output. | **S** | CI fails if the README's numbers disagree with the script. | PAA |

**Exit:** `paa agent "fix the failing test"` reads, edits, runs the tests, and commits;
each step and the task's mandate are in the ledger and survive `kill -9`.

---

## Horizon 2: Seams

*Make PAA connectable without widening the core.* Each seam follows the
[extension rules](VISION.md#extension-model): out of process, tighten-only, recorded
results, versioned envelope, rule of three.

| # | Task | Effort | Done when | Origin |
|---|---|---|---|---|
| H2.1 | **Harden the ACP server**: streaming, cancellation, editor context (mentions, working directory), a CI test that stdout stays protocol-clean. | **M** | Zed and one other ACP client drive a session with streamed output and working cancel. | PAA |
| H2.2 | **Event envelope v1**: versioned envelope, payload upcasters, a compatibility policy, JSONL export. | **M–L** | Golden-file tests for v1; a fixture ledger replays across a deliberate schema bump. | PAA |
| H2.3 | **Out-of-process hooks** (before/after tool use, at commit, at a gate), JSON in and out, tighten-only. | **M** | A hook can block a call but cannot unblock one policy denied. | PAA |
| H2.4 | **Per-task workspace isolation by default**: a worktree or directory per task, a recorded prepare step, detection of partial workspaces, cleanup on stop. | **M** | Stopping a task mid-prepare leaves no orphan process or half-built directory. | DS doc 003 |
| H2.5 | **Credential broker.** Shell, git, and CLI commands can use secrets the model never sees. | **L** | A canary secret never appears in the ledger, logs, or model context during a task that uses it. | DS §8.4, PAA secrets design |
| H2.6 | **First external agent under PAA** via an ACP client inside the sandbox; record file operations, commands, and permission requests. | **L** | A written measurement: what share of the agent's writes and commands were visible through the protocol versus only through the sandbox. **This decides whether belief B3 holds.** | PAA |
| H2.7 | **Executor-port spike.** Read the prototype's code and its executor interface; decide whether PAA can be the executor behind a front-half system, receiving a ticket and mandate and returning result and evidence. | **M** | A written go/no-go. If go: one ticket flows through PAA and its mandate lands in the ledger. | DS doc 001, 007 |

**Exit:** one external agent runs under PAA with its boundary activity recorded, and a
crash restores the workspace to the last checkpoint.

---

## Horizon 3: Evidence and attention

*Deliver the value the thesis claims and test it with outsiders.*

| # | Task | Effort | Done when | Origin |
|---|---|---|---|---|
| H3.1 | **Attested change bundle and `paa verify`.** Contains the mandate, diff, commands run, test results, approvals, and hash chain, and verifies offline without trusting the producer. | **L–XL** | A bundle altered by one byte fails verification; a reviewer can verify one on a machine without PAA's database. | PAA + DS doc 001 |
| H3.2 | **Accept modes per policy**: `off`, `observe` (record "would accept", start nothing), `auto`. Explicit requests work in every mode; unrequested work needs a written rule and starts in observe. | **M** | In observe mode a proactive candidate produces a ledger record and no task; switching to auto starts work under the matched rule. | DS doc 002 |
| H3.3 | **Watchdog v1 (deterministic).** From the ledger: no progress events within a window, repeated identical tool calls, spend without a changed diff, a task parked past its deadline. | **M** | Each condition is detected in a chaos test and produces an escalation event. | DS §6 |
| H3.4 | **Attention ledger.** Record every human interruption (question, gate, notification) as an event and report attention per accepted change. | **M** | `paa report attention` shows interruptions per task and per accepted change over a period. | DS §1, B6 |
| H3.5 | **Replay to regression test**: turn a recorded failure into a repeatable test. | **L** | A recorded failed run re-executes deterministically from the ledger and fails identically. | PAA |
| H3.6 | **Evaluation harness**: compare agents and configurations on your own repository with deterministic verification. | **XL** | Two agents run the same ten tasks and are scored by tests, not by self-report. | PAA |
| H3.7 | **Codebase intelligence, minimal**: symbol index and blast radius, only as far as H3.1 and H3.6 require. | **L** | Attested bundles list affected symbols for a change. | PAA |
| H3.8 | **Watchdog v2 (model-based drift judgement)**, only if H3.3 misses real stalls. | **L** | Drift detected in recorded runs where v1 was silent, with a measured false-positive rate. | DS §6 |
| H3.9 | **Validation trial** with about ten engineers. | calendar time | The [decision table](VISION.md#validation-and-kill-criteria) is applied. | PAA |

**Exit:** the validation trial is complete and a decision is recorded.

---

## Horizons 4 and 5 (conditional)

Start only if the trial supports it. Items are listed to show intent, not a plan.

**Horizon 4: Reach**

- Minimal GitHub event source (only if H2.7 shows integration is insufficient).
- Web dashboard listing what needs a person; ledger "time machine".
- Durable long-horizon tasks: milestone checkpoints, progressive summarisation,
  context objects with stable IDs for multi-hop recall (DS §3.3).
- Speculative execution in sandboxes with recorded, discardable branches (DS §3.4).
- Authentication, per-user isolation, team mode.
- One-command install and first-run setup.

**Horizon 5: Platform**

- Out-of-process pipeline SDK for custom loops, built to the
  [pipelines design](EXTENSIBILITY_PIPELINES.md).
- Executable package loading and marketplace publishing.
- Strong isolation as default where available; third-party security review.

---

## Ideas imported from DeepSeek Super-agent

| Idea | Decision | Where it lands |
|---|---|---|
| Mandate recording policy version, scope, strength, requester (DS doc 001) | **Adopt** | H1.4, H3.1 |
| Observe mode before autonomy (DS doc 002) | **Adopt** | H3.2 |
| Stored first, done afterwards; backoff retries (DS doc 003) | Already PAA's rule; **verify** against the lifecycle faults in H1.6 | H1.6 |
| Recovery note after restart (DS doc 003) | **Adapt**: PAA also verifies the workspace against a manifest | H1.2, H2.4 |
| One workspace per task (DS doc 003) | **Adopt** | H2.4 |
| Asynchronous start/stop tools (DS §4.1) | **Adopt** | H1.5 |
| Watchdog for stalls and drift (DS §6) | **Adopt**, deterministic first | H3.3, H3.8 |
| Attention as the product metric (DS §1, philosophy) | **Adopt** as a measurable | H3.4 |
| "Defaults, not a security boundary" (DS doc 003) | **Counter-position**: containment is PAA's reason to exist | H1.1, H2.4, H2.6 |
| Credentials usable without entering model context (DS §8.4) | **Adopt** | H2.5 |
| Context IDs and Chain of Recall (DS §3.3) | **Later** | Horizon 4 |
| Speculative execution (DS §3.4) | **Later**: safe only inside sandboxes with recorded branches | Horizon 4 |
| Resident LLM Router session, thread subscriptions (DS §2, §3.2) | **Skip for now**: commodity for a solo maintainer; integrate instead (H2.7) | Parked |
| Cloud agent loop with remote daemons (DS §5) | **Skip**: changes the threat model; PAA's local loop avoids the compromise path the authors flag | Parked |
| Feishu and chat-platform sources (DS doc 001) | **Skip** | Parked |
| Router sharding (DS §2.1) | **Skip**: not needed at PAA's scale | Parked |

Things *not* to take from the Super-agent evidence: its headline timing result
(12.4 vs 29.5 minutes p95) came from a synthetic stream with coding replaced by a
sleep tool, five runs per design, at about 3.8× the cache-read tokens of the neutral
single-session baseline. It says nothing about code quality and should not justify
roadmap choices.

---

## Sequencing and dependencies

```
H1.1 tools ──► H1.2 coder→ledger ──► H1.3 real-model run
   │                │
   │                └──► H1.5 async + cancel ──► H2.1 ACP hardening
   │
H1.4 mandates ──► H1.6 lifecycle fault tests
        │
        ├──► H2.2 event envelope ──► H2.3 hooks
        │
        └──► H2.7 executor-port spike ──┐
                                        ▼
H1.1 + H2.4 + H2.5 ──► H2.6 external agent under PAA ──► H3.1 attested change ──► H3.9 trial
                                        │                      ▲
                                        └──► H3.3 watchdog ────┤
                                             H3.2 observe mode ┤
                                             H3.4 attention    ┘
```

If you do nothing else, do **H1.1 and H1.2**. Everything later, including the evidence
claim, assumes PAA can change a project and record doing so.

## Decision gates and metrics

| Gate | After | Question | Evidence required |
|---|---|---|---|
| G1 | Horizon 1 | Is PAA useful to its own author on real work? | Two weeks of daily use; ledger shows tasks, mandates, recoveries |
| G2 | Horizon 2 | Does the boundary hold, and is integration with a front-half system possible? | H2.6 measurement; H2.7 go/no-go |
| G3 | Horizon 3 | Does anyone outside value authority and evidence? | Trial outcome per the decision table |

Metrics tracked from Horizon 1 onward, each from the ledger itself:

- **Mandate coverage:** share of accepted tasks with a recorded mandate (target 100%).
- **Recovery success:** share of injected crashes that resume without a repeated side
  effect (chaos suite).
- **Boundary coverage:** share of an external agent's writes and commands observed
  (H2.6).
- **Attention per accepted change:** interruptions per merged change (H3.4).
- **Verification time:** time for a reviewer to verify an attested bundle.

## Cross-cutting work

- **Security review** of the sandbox, credential broker, and hooks before any untrusted
  extension is accepted. The Windows backend is weaker than gVisor; gate hostile code
  on WSL2 or Docker.
- **Packaging:** `pipx install paa`, a first-run wizard, sane defaults.
- **Documentation:** fill the ADR bodies that are index-only; a getting-started guide;
  keep status figures generated, not typed.
- **Static analysis:** `mypy --strict` and `ruff` clean on every merge.
- **CI:** confirm the workflow's actual state in the Actions tab; treat it as
  unverified until green.

## Decisions that need an owner

1. **First external agent for H2.6.** Pick one (for example Codex or Claude Code
   through an ACP adapter) and one repository.
2. **Policy versioning scheme** for mandates (content hash, counter, or both).
3. **Requester identity model**: what counts as an identity for a CLI, an ACP client,
   and a webhook.
4. **Whether to read the prototype's source before H2.7**, or to wait until the spike.
5. **Trial participants** for H3.9, and the instrument for measuring review time.
