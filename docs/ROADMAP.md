# Roadmap

Where the runtime is, and where it should go — in priority order, with honest
effort and the reasoning behind the sequence.

## The one fact that shapes everything

The spine is strong and tested (ledger, storage, context, sandbox, memory). The suite
**collected 3,145 tests** when this file was last written — the ROADMAP's own "755
tests" figure predates the substrate track and Phase 0.

**What is true now:**

- the four subsystems that used to have **zero test coverage** — agents, validation,
  models, observability — have suites, and the security properties they carry are
  asserted rather than promised (§"Phase 0");
- the orchestrator **is** instantiated outside its own module, and one real task
  flows from request to commit: `tests/integration/test_end_to_end.py` drives it
  through the composition root, survives a stall mid-execution, and holds the token
  ceiling;
- the composition root is `Runtime.build` (`src/paa/runtime.py`) and the entry point
  is the `paa` CLI (`src/paa/cli/main.py`) — `submit`, `status`, `ledger`, `gate`,
  `recover`, `doctor` — with the FastAPI ingestion app beside it.

**What is still not true:**

- the whole run happens against an **injected** model. A frontier provider has never
  driven a task here, so "the thing works" is a fact about the wiring and not about
  the models;
- the optional substrates are exercised by the suite — a live PostgreSQL, two live
  S3 servers — but a deployment that wires every substrate together and drives a
  task through it is still a thing an operator does, not a thing the suite asserts.

So the honest status is: *a well-engineered collection of parts that now runs as a
whole against an injected model.* The roadmap is still ordered to fix the rest
first, because every aspirational feature in the brief is worthless until one real
task can flow from request to commit.

Effort key: **S** ≈ hours · **M** ≈ 1–2 days · **L** ≈ 3–5 days · **XL** ≈ 1–2+ weeks.
(Solo-developer estimates; halve with parallel agents when the network cooperates.)

---

## Phase 0 — Make the written code trustworthy

*Nothing new. Prove the code that already exists does what its docstrings claim.*
This is first because the untested modules carry the system's **security
properties**, and shipping security asserted only in prose is worse than not
shipping it.

**State: done, and the exit criterion is met for four of the five items.** The
clauses below were the ROADMAP's own trust clause, and each one now has a suite that
fails if the behaviour it names regresses. That is not the same as total coverage,
and the second table names, per item, what covers the clause and **what is still
open** — the parts that are a policy decision, a known defect, or a substrate that
needs a live server.

| # | Task | Effort | State | Why it matters |
|---|------|--------|-------|----------------|
| 0.1 | Test `validation/` — AST scanner catches each forbidden construct; unparseable source is *rejected* not skipped; patch apply+rollback restores exact bytes; path-traversal patches refused; workspace manifest is deterministic | **M** | **done** | This is the layer that stops agent-authored code from touching the host. Untested, it is decoration. |
| 0.2 | Test `agents/` — orchestrator emits the exact ledger event sequence end-to-end; delegation refuses a real 3-node cycle; policy gates (LOCKDOWN blocks egress, SAFE blocks deletion, anti-goal at 0.82); **critic's deterministic FAIL cannot be overridden by an LLM PASS** | **L** | **done** | The critic-override rule and the cycle guard are the two properties most likely to regress silently. |
| 0.3 | Test `models/` — LOCKDOWN never escalates (spy provider, assert zero frontier calls); escalation writes exactly one `MODEL_ESCALATED` event; semaphore caps concurrency; no API key ever appears in logs or exceptions | **M** | **done** | The escalation boundary is the privacy promise. Assert it, don't hope. |
| 0.4 | Test `observability/` — metric percentiles against hand-computed values; run repository round-trips; a failing exporter never breaks the traced operation | **S** | **done** | Cheap; unblocks trusting telemetry in the integration test. |
| 0.5 | Wire CI (GitHub Actions) — run the full suite + `ruff` + `mypy --strict` on every push, matrix over the optional extras | **S** | **done** | Makes the gate a fact enforced on every commit, not a snapshot somebody remembers to run. |

### What covers each clause, and what does not

| # | Clause | What covers it now | What is **not** covered |
|---|---|---|---|
| 0.1 | the scanner, the patch, the workspace | `tests/validation/test_ast_scanner.py` derives the rule inventory **from the scanner's own source** and checks it against all **19** rule names, each with a case; the default policy's **42** denied modules (in both spellings), **38** denied attributes and **7** denied builtins are each refused. `unparseable_source` is one of the 19, so "rejected, never skipped" is asserted. `test_patch.py` refuses traversal, absolute paths, control characters and the two symlink escapes a string-only check misses, and `test_apply_then_rollback_restores_the_exact_bytes` pins the rollback. `test_workspace.py` exercises the **real** `WorkspaceSnapshot` — the manifest does not depend on the write order or on the root, paths use `/`, CRLF and LF hash differently, a symlink is skipped and not followed. `test_engine.py` pins the gate's order and its short-circuits; `test_test_runner.py` pins that a run never reports a false pass. | Three behaviours are **xfail** — asserted to fail, each naming the file and the fix: the scanner's *walk* escapes a 2000-deep parse as `RecursionError` (`ast_scanner.py`); an artifact with nothing to check is reported as PASS (`engine.py`); a created file gains a trailing newline it was never given (`patch.py`). `paa.validation` is at **94%** line coverage (`pytest tests/validation --cov=paa.validation`). |
| 0.2 | the orchestrator, the cycle guard, the policy gates | `tests/agents/test_orchestrator.py` asserts the **exact** ordered event list for six paths — happy, failure, block, anti-goal block, gate, released gate — and `tests/integration/test_end_to_end.py` asserts the same through the real wiring. `tests/agents/test_delegation.py` refuses a **mediated** three-node cycle and pins that the *cycle guard* fires, not the depth ceiling: at the default modality the depth ceiling fires first, so a test written at the default would pass even if the guard were deleted. `tests/agents/test_policy.py` refuses network egress under LOCKDOWN, refuses deletion under SAFE, and asserts the anti-goal cosine path on **both** sides of 0.82 with an injected embedder. The nine irreversible patterns each have a case that fails when that pattern is removed, and the inventory is derived from the module's own source. `tests/integration/test_end_to_end.py::test_model_pass_cannot_override_a_deterministic_fail` pins the critic's rule. | **Two operator decisions are open** and are not this phase's to close: the **declared-permission half** of `_check_lockdown` cannot fire, and making it reachable changes *which* refusal the ledger records — a judgement about what the policy should say, not a defect; and four **pattern-semantics** findings (`git push -f`, the `rm` flag spellings, `DELETE FROM public.users`, `TRUNCATE` without `TABLE`) each widen a security rule over prose, so each is a policy decision. Both are named in `AGENTS.md` §"The rules this tree holds itself to", and carried with their reproductions in the run's work ledger. `paa.agents` is at **89%** line coverage; `context_agents.py` is the thinnest at **70%**. |
| 0.3 | the escalation boundary | `tests/models/test_router_lockdown.py` uses a spy provider and asserts **zero** frontier calls and exactly one local call, and that `force_escalate=True` does not bypass LOCKDOWN. `tests/models/test_escalation_audit.py` asserts exactly one `MODEL_ESCALATED` per escalation, and `tests/models/test_router_concurrency.py` asserts that two routers under one correlation write **two** records, because the audit's identity is the event's own id. The same file asserts `max_in_flight == cap` — `==`, not `<=`, because a semaphore that serialised everything would pass `<=`. `tests/models/test_key_secrecy.py` asserts the key reaches no log line, no ledger payload, no exception message, no details **and no exception chain**; the router's errors carry no chain at all. | The no-key guarantee is a guarantee about **the router's** errors: a provider that raises an error carrying its own key still has that key in that error, and the test file says so. `paa.models` is at **85%**; the provider *transports* are the thin part — `anthropic_provider.py` at **43%** and `ollama_provider.py` at **44%** — because they need a live endpoint. |
| 0.4 | the telemetry | `tests/observability/test_metrics.py` asserts hand-computed percentiles (p95 of 1..5 is 4.8), pins the rank formula with a property, and refuses a quantile outside the unit interval. `tests/observability/test_runs.py` round-trips the repository — the row that comes back is the row that was written, a run is in flight until it is finished, a non-zero exit code is an error, and an unfinished run is excluded from an aggregate rather than counted as a failure. `tests/observability/test_tracing.py` asserts a failing exporter never breaks the traced operation, and pins the `BaseException` asymmetry: an *interruption* propagates while the span is still finished and retained. `logging.py`, which had **zero** coverage, now has `tests/observability/test_logging.py`. | `paa.observability` is at **90%** line coverage; `tracing.py` is at **84%** and `metrics.py` at **85%**. |
| 0.5 | CI | `.github/workflows/ci.yml` runs `ruff check src tests`, `mypy src` (which is `strict = true` in the project's own config) and the whole `pytest` suite with `--strict-markers --strict-config` on every push and pull request. The matrix is over **install shapes** — a minimal one, the documented `.[dev]` one and `.[all]` — because "never import one at module scope in a core path" is a claim a minimal install has to be tested against. The developer shape installs `.[dev]`, which carries `paa[api]` and `paa[s3]`; without them the API suite and the live S3 suite skip themselves at import. A **PostgreSQL service container** and two **S3 service containers** (SeaweedFS and RustFS) are declared with health checks, and the suites' own variable names are exported: `PAA_TEST_POSTGRES_DSN` and the *labelled* `PAA_TEST_S3_*` spellings. The workflow installs **`python-is-python3`**, because the sandbox builds its child `PATH` as `/usr/local/bin:/usr/bin:/bin` and `actions/setup-python`'s tool-cache interpreter is not on it; without that line six assertions fail on the runner for a reason that has nothing to do with the code. The install uses the retry convention the Dockerfiles already encode. | **The workflow has never run** — there is no GitHub here, so it was parsed and its steps were executed locally as far as they could be, and the run itself is UNVERIFIED. The `requires_postgres` suite only *runs* in the `.[all]` shape (`paa[postgres]` is not in `dev`), so in the other two shapes it skips even though the server is there. A green run without the services says only that the suites skipped. |

**Exit:** every module has coverage; CI is green; the draft PR can leave draft.
**Read that as it stands:** the four subsystems have coverage, and it is *not*
total — the coverage column above is the honest figure, and the named gaps are the
known defects rather than the unmeasured remainder. The exit criterion's own words
("every module has coverage") were not met in the strict sense and are not claimed
here; what is met is that the four subsystems' headline clauses are asserted.

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
| 2.1 | **Memory creator finish + test** — `creator.py` and `facts.py` exist and are tested: `tests/memory/test_creator.py` covers signal → extracted fact → hot serving, malformed-input containment and contradiction quarantine; `tests/memory/test_facts.py` covers idempotent upsert, alias resolution and NUL refusal. | **M** | Done; real-time ingestion half of memory (RFC §2.1 agent 8). |
| 2.2 | **Memory curator** (`curator.py`) — nightly maintenance: decay sweep + relationship pruning + duplicate consolidation + orphan cleanup + vector reconciliation, under a wall-clock budget that stops cleanly mid-pass | **L** | Tested in `tests/memory/test_curator.py`. Done; low-frequency optimizer (RFC §2.1 agent 9). Hardcoded refusal to auto-resolve conflicts. |
| 2.3 | **World model** (`world_model.py`) — maintain the four belief-state docs (`current_focus`, `strategic_risks`, `active_constraints`, `operating_themes`) with atomic marker-bounded writes that never clobber human text; episodic compression ladder (RFC §12) | **M** | Tested in `tests/memory/test_world_model.py`. Done; the "cognitive world model" — raw logs → patterns → principles → playbooks. |
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
| 3.5 | **Specialist agent ecosystem** — the 21 RFC specialists as data-driven configs (tools/permissions/budgets/schema), seeded into the agent registry, with the coding/research/writing three fully wired first | **M** | Data-driven so adding a specialist is a row, not a class. Communication + robotics carry mandatory human gates. Progress: the *coding* one exists today as `paa agent` (`src/paa/coder/`, see `docs/coder-agent.md`) — but it is wired through its own loop rather than the specialist registry, so the registry/seed half of this task is still open. |
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

If you want the single highest-leverage next step: **the rest of Phase 1.** Phase 0
is done — the agents, validation, models and observability are asserted rather than
promised — and the composition root, the `paa` CLI and the end-to-end test are
already in the tree, so what remains of Phase 1 is the part that has never run
against anything but an injected model. That is what converts "3,107 collected
tests" into "the thing works," which is the claim everything else rests on.
