# Roadmap

Where the runtime is, and where it should go — in priority order, with honest
effort and the reasoning behind the sequence.

## The one fact that shapes everything

The spine is strong and tested (ledger, storage, context, sandbox, memory:
**755 tests**). But:

- the orchestrator is **never instantiated** outside its own module,
- there is **no composition root** that wires the pieces together,
- there is **no entry point** — you cannot start the system or feed it a task,
- four written subsystems (agents, validation, models, observability) have
  **zero test coverage**.

So the honest status is: *a well-engineered collection of parts that has never
run as a whole.* The roadmap is ordered to fix that first, because every
aspirational feature in the brief is worthless until one real task can flow from
request to commit.

Effort key: **S** ≈ hours · **M** ≈ 1–2 days · **L** ≈ 3–5 days · **XL** ≈ 1–2+ weeks.
(Solo-developer estimates; halve with parallel agents when the network cooperates.)

---

## Phase 0 — Make the written code trustworthy

*Nothing new. Prove the code that already exists does what its docstrings claim.*
This is first because the untested modules carry the system's **security
properties**, and shipping security asserted only in prose is worse than not
shipping it.

| # | Task | Effort | Why it matters |
|---|------|--------|----------------|
| 0.1 | Test `validation/` — AST scanner catches each forbidden construct; unparseable source is *rejected* not skipped; patch apply+rollback restores exact bytes; path-traversal patches refused; workspace manifest is deterministic | **M** | This is the layer that stops agent-authored code from touching the host. Untested, it is decoration. |
| 0.2 | Test `agents/` — orchestrator emits the exact ledger event sequence end-to-end; delegation refuses a real 3-node cycle; policy gates (LOCKDOWN blocks egress, SAFE blocks deletion, anti-goal at 0.82); **critic's deterministic FAIL cannot be overridden by an LLM PASS** | **L** | The critic-override rule and the cycle guard are the two properties most likely to regress silently. |
| 0.3 | Test `models/` — LOCKDOWN never escalates (spy provider, assert zero frontier calls); escalation writes exactly one `MODEL_ESCALATED` event; semaphore caps concurrency; no API key ever appears in logs or exceptions | **M** | The escalation boundary is the privacy promise. Assert it, don't hope. |
| 0.4 | Test `observability/` — metric percentiles against hand-computed values; run repository round-trips; a failing exporter never breaks the traced operation | **S** | Cheap; unblocks trusting telemetry in the integration test. |
| 0.5 | Wire CI (GitHub Actions) — run the full suite + `ruff` + `mypy --strict` on every push, matrix over the optional extras | **S** | Makes "755 passing" a fact enforced on every commit, not a snapshot. |

**Exit:** every module has coverage; CI is green; the draft PR can leave draft.

---

## Phase 1 — Make it run end to end

*Turn the parts into a system you can actually start and drive.*

| # | Task | Effort | Notes |
|---|------|--------|-------|
| 1.1 | **Composition root** (`paa/runtime.py`) — one `Runtime.build(settings)` that constructs and wires ledger, storage, queue, model router, all agents, orchestrator, recovery. The missing centre. | **M** | Everything else in this phase depends on it. Use dependency injection so tests can swap fakes. |
| 1.2 | **End-to-end integration test** proving all five DoD items in one run: submit → hydrate → plan → policy → execute → validate → commit; then kill mid-execution and prove clean recovery; prove the 1500-token ceiling holds; prove an injection string is blocked with no model call | **L** | This is the test that proves the *thesis*. Until it's green, "it works" is a claim, not a fact. |
| 1.3 | **CLI** (`paa` via Typer) — `paa submit "<goal>"`, `paa status <id>`, `paa recover`, `paa gate <id> --approve`, `paa ledger <id>` (walk the causal chain), `paa doctor` (check which backends are live) | **M** | The first way a human drives the system. `paa ledger` is the "explain why it did that" DoD item made usable. |
| 1.4 | **FastAPI ingestion** — loopback-bound webhook → atomic `cold_lake.signals` write → orchestrator, with backpressure returning real HTTP 429 | **M** | The RFC's edge. The loopback guard is already enforced in config. |
| 1.5 | **Background daemon** — the async loop that drains queues, runs the memory creator, fires the decay sweep on its 6-hour cadence, and boots recovery on start | **M** | Makes the system *ambient* rather than one-shot. |
| 1.6 | **Markdown vault seeding** — create `constitution.md`, `principles.md`, `anti_goals.md`, `goals.md`, `identity.md` etc. on first run; the policy agent already reads anti-goals | **S** | The "strategic human interface" (RFC §9). Cheap and high-leverage. |

**Exit:** `paa submit "..."` runs a real task to `MUTATION_COMMITTED`, survives a
`kill -9` mid-run, and resumes on restart.

---

## Phase 2 — Complete the RFC subsystems

*The pieces the spec calls for that aren't built yet.*

| # | Task | Effort | Notes |
|---|------|--------|-------|
| 2.1 | **Memory creator finish + test** — `creator.py` and `facts.py` exist but are untested; the ETL from cold-lake signal → extracted fact → hot serving needs the malformed-input containment and contradiction path proven | **M** | The real-time ingestion half of memory. |
| 2.2 | **Memory curator** (`curator.py`) — nightly maintenance: decay sweep + relationship pruning + duplicate consolidation + orphan cleanup + vector reconciliation, under a wall-clock budget that stops cleanly mid-pass | **L** | The low-frequency optimizer (RFC §2.1 agent 9). Hardcoded refusal to auto-resolve conflicts. |
| 2.3 | **World model** (`world_model.py`) — maintain the four belief-state docs (`current_focus`, `strategic_risks`, `active_constraints`, `operating_themes`) with atomic marker-bounded writes that never clobber human text; episodic compression ladder (RFC §12) | **M** | The "cognitive world model" — raw logs → patterns → principles → playbooks. |
| 2.4 | **Unified Skill Adapter runtime** — `contracts.py` and `secrets.py` exist; still need the registry, Claw-Hub adapter (parse `SKILL.md`), MCP client (JSON-RPC over stdio), and the §8.2 state machine with the secret proxy | **L** | Turns the system from "runs its own code" into "runs the ecosystem's tools." The secret broker is the security-critical piece. |
| 2.5 | **Self-improvement loops** (`improvement/`) — weekly reflection engine (friction score, anti-pattern extraction → playbooks), skill distillation on clean commits, tool-weight EWMA optimizer | **L** | The "Hermes-class" self-optimization (RFC §3). Depends on 2.3 for playbook writes. |
| 2.6 | **Multi-session isolation** — session-scoped workspace overlays, cross-session query tool gated by the policy agent (RFC §7) | **M** | Concurrent projects without context bleed. |

**Exit:** the runtime learns from its own history and can run community skills
safely.

---

## Phase 3 — The brief's new capabilities

*The things you asked for beyond the RFC.*

| # | Task | Effort | Notes |
|---|------|--------|-------|
| 3.1 | **Chat / history search ("like XAI")** — hybrid search over the cold lake: FTS5 trigram + vector recall + graph expansion, ranked by the §15.8 hybrid score, with time and entity filters. A `paa search "..."` command and an API route. | **L** | High user value, and most of the substrate (vector store, FTS, graph, hybrid-score metric) already exists — this is assembly, not new infrastructure. |
| 3.2 | **Marketplace** (`marketplace/`) — signed skill/agent packages (`.paapkg`), Ed25519 verification, a 5-gate installer (signature → hash → AST scan → permission check → sandbox smoke-test), a `LocalDirectoryRegistry` default + optional `HttpRegistry` | **XL** | The platform play. The installer's five gates are the whole security model — a marketplace is the largest attack surface in the design. Build the gates before the storefront. |
| 3.3 | **Prototypical few-shot task classification** (`improvement/meta.py`) — class prototypes as mean embeddings of past task exemplars; classify a new task by nearest prototype with a confidence margin; return "unknown" rather than guess | **M** | The real, honest member of the meta-learning family (ADR-0017). Feeds the router and the modality classifier so the system adapts from few examples with no training loop. |
| 3.4 | **LLM wiki with context** — a browsable, linkable knowledge base built from the graph + distilled facts + markdown vault, cross-referenced and served locally; agents can read and write entries under curator approval | **L** | Turns accumulated memory into something a human can browse and an agent can cite. Depends on 2.3. |
| 3.5 | **Specialist agent ecosystem** — the 21 RFC specialists as data-driven configs (tools/permissions/budgets/schema), seeded into the agent registry, with the coding/research/writing three fully wired first | **M** | Data-driven so adding a specialist is a row, not a class. Communication + robotics carry mandatory human gates. |
| 3.6 | **Notification & bridge adapters** — Telegram / email / desktop ingestion into the cold lake; the outbound communication agent already mandates a human gate | **M** | Makes the system reachable from where you already are. |

**Exit:** the system is searchable, extensible by a community, adapts from few
examples, and reachable from your existing tools.

---

## Phase 4 — Long-horizon tasks (your stated v-next)

*The hardest and most valuable frontier.*

| # | Task | Effort | Notes |
|---|------|--------|-------|
| 4.1 | **Durable task graphs** — a task spanning weeks is a lineage that must survive dozens of restarts. Extend the ledger's snapshot machinery to milestone-level checkpoints; model dependencies as graph edges (`DEPENDS_ON`, `BLOCKS`) | **XL** | The ledger + snapshots are already the right foundation — this is why crash recovery was built first. |
| 4.2 | **Time-aware scheduling** — deadline tracking, the §15.2 attention-allocation score to pick what to work on next, calendar integration | **L** | The productivity agent's real job. |
| 4.3 | **Forecasting** — schedule-slip prediction and resource-usage estimates from historical execution logs | **M** | The forecasting specialist, made real. |
| 4.4 | **Progressive summarization for very long lineages** — so a months-long task's context stays under the token ceiling as its history grows | **L** | Where the context engine's compaction meets the world model's episodic compression. |

---

## Cross-cutting (do continuously, not as a phase)

- **Security review** of the sandbox and marketplace before either is exposed to
  anything untrusted. The Windows subprocess backend is honestly weaker than
  gVisor — for genuinely hostile code, gate on the WSL2 or Docker backend.
- **Packaging** — `pipx install paa`, a first-run wizard, sane defaults so it
  works before any optional extra is installed.
- **Docs** — fill in the ADR bodies that are currently index-only (0001–0007,
  0009, 0011–0014, 0016–0018); a getting-started guide once the CLI exists.
- **`mypy --strict` to zero** across the untested modules as they gain coverage.

---

## Suggested order, and why

```
Phase 0 (trust) ──► Phase 1 (make it run) ──► Phase 2 (complete RFC)
                          │                          │
                          └──► Phase 3.1 (search) ◄──┘   ← highest value / cost
                                     │
                          Phase 3.2–3.6 (platform, agents, bridges)
                                     │
                          Phase 4 (long-horizon)
```

The through-line: **you cannot improve what you cannot run, and you should not
run what you cannot trust.** Phase 0 buys trust, Phase 1 buys a running system,
and only then does building features stop being building on sand.

If you want the single highest-leverage next step: **Phase 0.2 + Phase 1.1 +
1.2** — test the agents, write the composition root, and prove one task
end-to-end. That converts "755 unit tests" into "the thing works," which is the
claim everything else rests on.
