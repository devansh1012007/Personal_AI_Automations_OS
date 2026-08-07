# The 10-Phase Vision

*Where PAA goes after the runtime spine — to become a coding agent as capable as
Claude Code, a skill platform richer than OpenClaw, and a personal assistant as
ambient as Hermes/Jarvis, on hardware you own.*

---

## The honest north star

You want three things at once, and they are genuinely different products:

| Archetype | What it is | What "better" means here |
|---|---|---|
| **Claude Code** | A terminal coding agent | Same coding loop **plus** crash-recovery, durable memory of your codebase, and an audit trail of every change |
| **OpenClaw** | An open skill/agent framework (Claw Hub) | A richer, *signed and sandboxed* skill ecosystem with a real marketplace and revenue |
| **Hermes / Jarvis** | An ambient personal assistant | Proactive, multimodal, always-on, reachable from anywhere, that *remembers your life* |

**The one thing to be honest about (ADR-0015):** architecture cannot close a
model-capability gap. A local 8B will not out-reason a frontier model no matter
how good the harness is. So the strategy is **not** "beat frontier models at
reasoning" — it is to win on the axes a stateless cloud agent *structurally
cannot*: recoverability, persistent memory, deterministic safety, auditability,
offline operation, an open economy, and ambient presence. Point the model layer
at a frontier API when you want peak coding IQ; run local when you want privacy.
The runtime is what makes either one *trustworthy and yours*.

## Where we are today (the foundation these phases build on)

~40k LOC, 1138 tests. Complete and tested: event-sourced ledger + crash
recovery, polyglot storage (SQLite/Postgres, vector, graph, cold lake, queue),
bounded context engine, the full memory system (decay, contradiction, creator,
curator, world model), sandbox + deterministic validation, the 9-agent hierarchy
+ orchestrator, model providers (local + 10 platforms), the Unified Skill Adapter
(Claw Hub + MCP), a signed-package marketplace, self-improvement loops, the
clean-room coder layer (permissions/hooks/loop/sessions), and the API + daemon +
CLI.

**The gap between "a substrate" and "a product you'd use daily" is the next ten
phases.** The single biggest missing thing: the runtime can *orchestrate* work
but ships almost no *concrete capabilities* — no real web search, file editing,
git, or browser as installed skills, and the coder loop isn't yet wired to drive
them. Phases 1–3 close that; 4–10 build the three products on top.

Effort key: **S** ≈ days · **M** ≈ 1–2 weeks · **L** ≈ 3–6 weeks · **XL** ≈ 2+ months.

---

## Phase 1 — Real capabilities: the tool library

*The runtime has a beautiful engine and no wheels. Give it hands.*

Right now there are zero shipped tools. Everything downstream is theoretical
until an agent can actually read a file, run a command, or search the web.

- **Core tool suite as native skills**: `read_file`, `write_file`,
  `edit_file` (structured/anchored edits, not full rewrites), `bash`/`shell`,
  `glob`, `grep` (ripgrep-backed), `list_dir`, `apply_patch`. Each a
  `SkillContract` with a permission profile, run through the sandbox.
- **Web tools**: `web_search` (pluggable: Brave/SearXNG/Tavily/DuckDuckGo),
  `web_fetch` (readability extraction → markdown), `browser` (Playwright, headed
  or headless, through the network-proxy sandbox).
- **Wire the coder loop to the orchestrator + USA registry** so `AgentLoop`
  actually dispatches these tools with the permission/hook gating already built.
  This is the single highest-leverage integration in the whole roadmap — it
  turns every existing subsystem into something a user can feel.
- **Creative options**: a `computer_use` tool (screenshot + click/type) for GUI
  automation; a `python_repl` stateful kernel skill; a `sql` tool over the user's
  own databases; an `http_api` generic REST caller with an OpenAPI importer that
  auto-generates a skill from any API spec.

**Exit:** `paa "fix the failing test in auth.py"` actually reads, edits, runs
pytest, and commits — end to end, gated, recovered on crash.

## Phase 2 — Codebase intelligence: the Claude Code core

*What makes a coding agent good is not the loop — it's knowing the code.*

- **Repository indexer**: walk a repo, chunk by symbol (tree-sitter), embed into
  the existing vector store, and map imports/calls into the existing graph store.
  You already have both engines; this feeds them.
- **LSP bridge**: talk to language servers for go-to-definition, find-references,
  hover types, and diagnostics — so the agent reasons over *real* symbol
  relationships, not guesses.
- **Codebase-aware context builder**: a specialization of the bounded context
  gatherer that pulls the right files/symbols for a task within the token ceiling
  (the ceiling machinery already exists).
- **Git as a first-class subsystem**: branch-per-task (the RFC's session
  overlays), semantic commits, PR drafting, blame-aware editing, conflict-aware
  merges. Recovery already checkpoints workspaces; make git the checkpoint store.
- **Creative options**: a "codebase wiki" auto-generated from the graph +
  distilled facts (your earlier `llm wiki` idea); architectural-drift detection
  (flag when a change violates learned module boundaries); a "blast radius"
  estimator using the call graph before an edit.

**Exit:** on a large repo, retrieval quality and symbol awareness approach what a
frontier coding agent gets from a long context window — but persisted and reusable.

## Phase 3 — Interfaces: make it a thing people touch

*A CLI is the developer's door. Everyone else needs a room.*

- **A proper TUI** (Textual): live task tree, streaming agent output, the ledger
  timeline as a scrubber, permission prompts inline, plan-mode review — the
  Claude-Code terminal experience, but showing the recovery/audit machinery.
- **A local web dashboard** (the loopback FastAPI already exists): task board,
  memory browser, the world-model documents, the marketplace, cost/usage, and a
  live trace inspector over `execution_runs`.
- **Streaming everywhere**: SSE/WebSocket from the API so the TUI/web show tokens
  as they generate (the provider `stream()` method already exists).
- **Chat / history search "like xAI"** (your original ask): hybrid FTS5 + vector
  + graph search over the whole cold lake and ledger, ranked by the §15.8 hybrid
  score, with time/entity/project filters. Most of the substrate exists — this is
  assembly, and it is *the* feature that makes accumulated memory feel alive.
- **Creative options**: a VS Code / JetBrains extension speaking to the local
  daemon; a "time machine" UI that replays a task's ledger step by step; a
  natural-language query over your own history ("what did I decide about auth in
  March?").

**Exit:** a non-terminal user can drive, watch, and search the system.

## Phase 4 — Ambient presence: the Hermes turn

*Stop being a tool you invoke. Start being an assistant that is just there.*

- **Ingestion bridges**: Telegram, WhatsApp, email (IMAP/SMTP), Slack/Discord,
  browser-extension capture, filesystem watchers, calendar. Everything lands in
  the cold lake (the ingestion edge exists); the memory creator already turns
  signals into facts.
- **Proactivity engine**: scheduled + event-triggered tasks (cron already in the
  daemon), plus a "notice and offer" loop — the attention-allocation score
  (§15.2) decides *what's worth surfacing* rather than waiting to be asked.
- **Notifications & digests**: a morning brief, deadline nudges, "you said you'd
  follow up with X," anomaly alerts — all gated by the communication agent's
  mandatory human-approval rule.
- **Creative options**: a "life graph" over the KuzuDB store (people, projects,
  commitments, threads) with relationship-decay reminders; a **do-not-disturb /
  focus mode** driven by `active_constraints.md`; an inbox-zero agent that triages
  but never sends without a gate.

**Exit:** it messages *you* first, usefully, and remembers the context when it does.

## Phase 5 — Long-horizon autonomy (your stated v-next)

*The hardest, most valuable frontier: tasks that span weeks and survive dozens of
restarts.*

- **Durable task graphs**: milestone-level checkpoints on top of the ledger's
  snapshot machinery; dependency edges (`DEPENDS_ON`, `BLOCKS`) as first-class
  graph relations; sub-task trees that a crash reconstructs exactly.
- **Progressive summarization for long lineages** so a months-long task's context
  stays under the token ceiling as its history grows (where compaction meets the
  world model's episodic ladder).
- **Goal maintenance**: `goals.md` / `strategy.md` as living objectives the
  planner re-checks; drift detection when day-to-day work diverges from stated
  goals (the narrative-coherence score §15.13).
- **Autonomous project management**: forecasting (schedule-slip prediction from
  execution history), resource budgeting, and a weekly self-review that reports
  progress against milestones.
- **Creative options**: a "dead man's switch" — if a long task stalls N days it
  escalates to you; branch-and-merge *strategies* (try three approaches in
  parallel sandboxes, keep the best); a negotiation loop where the agent proposes
  a plan, you edit it, and it re-plans around your edits.

**Exit:** hand it "get my side project to a v1 launch" and it works it over weeks,
surviving reboots, checking in, never losing the thread.

## Phase 6 — Multimodal senses

*A personal OS that can only read text is half-blind.*

- **Vision**: screenshot understanding (computer-use), image/diagram/PDF ingestion
  into the cold lake, chart/table extraction. Route to a vision-capable provider
  via the registry.
- **Voice**: local STT (whisper.cpp) + TTS (Piper) for a hands-free assistant;
  wake-word optional; everything transcribed into episodic memory.
- **Documents**: first-class PDF/DOCX/XLSX/PPTX ingestion and generation (there
  are skills for these patterns already in the ecosystem to wrap).
- **Creative options**: a "watch my screen and help" mode; meeting transcription →
  action items → tasks; a camera/OCR intake for physical documents; audio diary →
  structured memory.

**Exit:** you can talk to it, show it things, and hand it files of any kind.

## Phase 7 — The learning system that actually learns

*Self-improvement today is heuristics. Make it measurable and real.*

- **Evaluation harness**: a benchmark runner (SWE-bench-style for coding, plus
  your own task replays from the ledger) so every change to prompts, routing, or
  models is measured, not guessed. This is the prerequisite for all real learning.
- **Prompt/playbook optimization** driven by eval scores, not just friction — the
  reflection engine proposes changes, the harness proves them before they land.
- **Local adaptation**: LoRA fine-tuning of a small local model on *your* accepted
  outputs and corrections — the honest version of "it learns your style,"
  distinct from the prototypical few-shot already built for fast task routing.
- **Retrieval quality loop**: the retrieval-precision metric (§10) feeds back to
  re-rank and prune what the context builder surfaces.
- **Creative options**: A/B routing (send a task to two model tiers, learn which
  wins per task class); a "confidence calibration" pass so the system knows when
  it's likely wrong and asks; distillation of frontier-model traces into local
  recipes (the skill distillation engine already exists — feed it eval-verified
  runs).

**Exit:** measurable month-over-month improvement on your own workload, provable
from the eval harness.

## Phase 8 — The platform & economy: better than OpenClaw

*Turn the marketplace from a mechanism into an ecosystem.*

- **Marketplace maturity**: reviews/ratings, versioning + dependency resolution,
  a trust/reputation score (the 5-gate installer is the safety floor), publisher
  identity, and semantic discovery over skills.
- **Monetization**: paid skills/agents/configs, revenue split, license
  enforcement, usage metering — the "sell your AI" ask made real.
- **Skill authoring kit**: `paa skill new`, a local test harness, a linter for
  contracts, and a one-command publish — lower the barrier so the ecosystem grows.
- **Config/agent bundles**: sell a whole configured assistant ("the indie-hacker
  setup," "the researcher setup") as an installable bundle.
- **Creative options**: a federation protocol so private/team marketplaces can
  share; "skill recipes" that compose existing skills without code; a bounty board
  where users post tasks and skill-authors fulfill them; verifiable-build
  attestations so a buyer knows the package matches its source.

**Exit:** a stranger can publish a signed skill, another can safely install and
pay for it, and both trust the five gates between them.

## Phase 9 — Advanced reasoning & multi-agent depth

*Squeeze more capability out of whatever model you point at it.*

- **Verified multi-agent patterns**: debate (N agents argue, a judge decides),
  adversarial self-critique before commit, and ensemble planning — all bounded by
  the existing delegation graph + recursion guards, so it can't run away.
- **Tree-of-thought / graph-of-thought planning** using the node/token budgets
  already enforced (§11), with the planner exploring and pruning branches.
- **Neuro-symbolic reasoning**: push more decisions into the deterministic layer
  (policy, validation, graph queries) and use the model only where symbols can't
  reach — the highest-leverage way to make a weaker model behave reliably.
- **Speculative execution**: run the likely-next steps in parallel sandboxes while
  waiting on a gate, discard the losers.
- **Creative options**: a "second opinion" mode that escalates only the *uncertain*
  sub-steps to a frontier model (cost-aware); causal reasoning over the graph
  ("if I change X, what breaks?"); a constitution-checker that formally verifies a
  plan against `constitution.md` before execution.

**Exit:** on hard tasks, the *system* is meaningfully smarter than the raw model
it runs, because structure does work the model would otherwise fumble.

## Phase 10 — Trust, scale, and the moat

*Make the guarantees provable and the deployment serious.*

- **Security hardening**: real gVisor/Firecracker isolation on Linux, secret
  rotation, a capability audit, and a third-party security review before the
  marketplace is public. The Windows subprocess sandbox is honestly weak; this
  makes strong isolation the default where it can run.
- **Formal-ish guarantees**: property-based and chaos testing expanded into a
  continuous suite; a provable "no network egress in LOCKDOWN" test; signed,
  tamper-evident audit exports for compliance.
- **Distribution & scale**: optional multi-machine (a beefy home server does
  inference, the laptop is a thin client); encrypted state sync across your own
  devices; team mode with per-user isolation over the session layering that
  already exists.
- **Packaging**: one-command install (`pipx`/Homebrew/installer), a first-run
  wizard, and sane defaults so a non-expert is productive in minutes.
- **Creative options**: end-to-end encrypted memory (you hold the key, even a
  cloud sync can't read it); a "portable brain" export so you can move your entire
  memory + config to a new machine; an offline-first PWA client; hardware
  appliance (a Raspberry-Pi-class always-on node).

**Exit:** trustworthy enough to run your life on, portable enough to take with
you, and safe enough to open to others.

---

## Missing today (the honest gap list)

**Load-bearing gaps** (block the vision): no concrete tools; coder loop not wired
to the orchestrator; no codebase indexing/LSP; no UI beyond the CLI; observability
untested; live-Postgres serialization pass unfinished.

**Capability gaps**: no multimodal (vision/voice/docs); no git integration; no
proactive/scheduled intelligence beyond the raw daemon; no ingestion bridges; no
eval harness; no local fine-tuning.

**Product gaps**: no marketplace UX/economy; no reputation/reviews; no skill
authoring kit; no cost/budget dashboard; no auth/multi-user; no mobile/remote.

**Nice-to-haves**: theming, plugin hot-reload, a rules/automation builder UI,
templated workflows, import from other agent frameworks, an "explain this
decision" natural-language view over the causal chain, keyboard-driven everything.

## Moonshots (worth dreaming about)

- **A truly personal model**: continual local fine-tuning so a small model, tuned
  on your corrections, beats a generic large one *on your specific work*.
- **Self-authoring skills**: the system notices a repeated manual workflow and
  writes, tests, and installs a new skill for it — distillation taken to its end.
- **A memory you can trust for a decade**: the event ledger as a lifelong,
  queryable, tamper-evident record of everything you've done and decided.
- **Agent-to-agent economy**: your assistant hires other people's specialist
  agents through the marketplace to complete sub-tasks, paying per use.

## Guiding principles for all of it (don't lose these)

1. **The ledger is truth; everything else is a cache.** Never add a feature that
   can't be recovered from the log.
2. **Deterministic where it matters.** Safety, validation, and money never depend
   on a model's mood.
3. **Local-first, escalate on purpose.** Privacy is the default; the network is a
   deliberate, logged choice.
4. **No silent capability without a gate.** Every new power comes with a permission
   and an audit trail.
5. **Honesty over hype.** If a weaker model can't do something, say so and route
   around it — don't pretend the harness is magic.

---

## Suggested sequencing

```
Phase 1 (tools) ─► Phase 2 (codebase IQ) ─► Phase 3 (interfaces)
                                                │
        ┌───────────────────────────────────────┼──────────────┐
        ▼                                        ▼              ▼
  Phase 4 (ambient)                       Phase 5 (long-horizon)  Phase 7 (learning)
        │                                        │              │
        ▼                                        ▼              ▼
  Phase 6 (multimodal)                    Phase 9 (reasoning)  Phase 8 (economy)
        └───────────────────────┬────────────────────────────────┘
                                 ▼
                        Phase 10 (trust & scale)
```

**If you do nothing else, do Phase 1.** Concrete tools + wiring the coder loop is
the step that converts this from an impressive substrate into something you use
every day — and every later phase is more valuable once it's real.
