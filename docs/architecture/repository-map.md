# Repository map

Directory-by-directory map of the PAA repository, with each directory's
responsibility and the files it contains. All paths are repository-relative.

## Top level

| Path | Responsibility |
|---|---|
| `src/paa/` | Application source (src-layout package) |
| `tests/` | Test suites (mirrors `src/paa/` package layout) |
| `docs/` | This documentation (plus pre-existing `adr/`, `coder-agent.md`, `EXTENSIBILITY_PIPELINES.md`, `ROADMAP.md`, `VISION.md`, `deployment/`) |
| `deploy/` | Deployment artifacts |
| `docker-compose.yml`, `docker-compose.gvisor.yml`, `Dockerfile` | Container / orchestration |
| `.env`, `.env.example` | Environment configuration (read from CWD) |
| `pyproject.toml` | Packaging, lint (ruff), type (mypy), test (pytest) config |
| `AGENTS.md` | Runtime instructions for coding agents (authoritative conventions) |
| `README.md` | Project README, build status, layout, quick start |

## src/paa/ — application source

| Path | Responsibility | Key public symbols |
|---|---|---|
| `src/paa/__init__.py` | Package marker; `__version__` | `__version__` |
| `src/paa/config.py` | All settings (`PAA_*`), `dotenv_value`, `get_settings` | `Settings`, `get_settings`, `reset_settings_cache`, `reset_dotenv_cache`, `dotenv_value` |
| `src/paa/runtime.py` | Composition root; `Runtime` build/close/submit/run/project/boot_recovery; optional-backend factories (`_try_vector/_try_graph/_try_queue`); `_acquire_sandbox` (5s probe) | `Runtime`, `RouterModelAdapter`, `CriticValidatorAdapter` |
| `src/paa/daemon.py` | Background daemon: boot recovery, dispatch, ingestion, maintenance loops | `Daemon` |
| `src/paa/core/types.py` | Domain types: IDs, `EventType`, `TERMINAL_EVENTS`, `Permission`, `PermissionMode`, `ComplexityModality`, `ModalityProfile`, `MODALITY_PROFILES`, `AgentRole` | `EventType`, `PermissionMode`, `ComplexityModality`, `MODALITY_PROFILES`, `AgentRole` |
| `src/paa/core/errors.py` | Exception hierarchy (`PaaError` and subclasses) | `PaaError`, `PolicyViolation`, `PermissionDeniedError`, `SecurityScanError`, `ValidationError`, `SchemaValidationError`, `SandboxError`, `SandboxTimeout`, `LedgerError`, `IdempotencyViolation`, `ReplayIntegrityError`, `BudgetExceededError`, `RecursionGuardError`, `ContextInsufficientError`, `ContradictionError`, `StorageError` |
| `src/paa/ledger/events.py` | Event envelope, `canonical_json`, idempotency key, digest, `LedgerEvent`, `GENESIS_HASH` | `LedgerEvent`, `canonical_json`, `compute_idempotency_key`, `compute_event_digest`, `GENESIS_HASH` |
| `src/paa/ledger/store.py` | Append-only store, `CorrelationHead`, `LedgerStore.append/append_many/read_correlation/head/open_correlations/verify_chain/save_snapshot/load_snapshot` | `LedgerStore`, `CorrelationHead` |
| `src/paa/ledger/replay.py` | `TaskPhase`, `TaskProjection`, `apply_event`, `replay`, `project` | `TaskProjection`, `TaskPhase`, `project` |
| `src/paa/ledger/recovery.py` | `RecoveryEngine`, `RecoveryReport`, `WorkspaceSnapshotter`/`Requeuer` protocols, `boot_sweep`, `checkpoint` | `RecoveryEngine`, `RecoveryOutcome` |
| `src/paa/storage/relational/database.py` | Async SQLite access layer: single write connection + read pool, WAL, schema apply, transactions | `Database`, `utc_now`, `to_iso`, `from_iso`, `dumps`, `loads` |
| `src/paa/storage/relational/schema_sqlite.sql` | SQLite DDL (26 tables) | — |
| `src/paa/storage/relational/schema_postgres.sql` | Postgres DDL (26 tables, column-identical to sqlite) | — |
| `src/paa/storage/coldlake/cas.py` | Content-addressed, zstd-compressed blob store | `ContentAddressedStore`, `BlobRef` |
| `src/paa/storage/coldlake/signals.py` | Raw signal intake repository, idempotent on (channel, external_id) | `SignalRepository`, `Signal` |
| `src/paa/storage/vector/` | Vector recall: Qdrant (embedded/server) or numpy brute-force fallback | `get_vector_store`, `get_embedder` |
| `src/paa/storage/graph/` | Graph store: Kuzu or SQLite recursive-CTE fallback | `get_graph_store` |
| `src/paa/storage/queue/` | Durable queue + distributed lock: SQLite or Redis | `get_queue`, `get_lock`, `StreamName`, `BackpressureController` |
| `src/paa/context/gatherer.py` | Bounded context gatherer with hard token ceiling | `BoundedContextGatherer` |
| `src/paa/context/budget.py` | `TokenBudget` with sub-budget halving per depth | `TokenBudget` |
| `src/paa/context/compaction.py` | Context compaction | — |
| `src/paa/context/metrics.py` | Context quality metrics (density, pollution, utility, entropy) | — |
| `src/paa/memory/domains.py` | 18 memory domains over 6 mechanisms; `DomainPolicy`, `decay_lambda` matrix | `MemoryDomain`, `MemoryMechanism`, `DomainPolicy`, `DOMAINS`, `narrative_domains` |
| `src/paa/memory/decay.py` | `DecaySweeper`, `effective_confidence` (C(t)=C₀·e⁻ˡᵃᵐᵇᵈᵃ·t) | `DecaySweeper`, `effective_confidence` |
| `src/paa/memory/facts.py` | `FactRepository`: entity/fact upsert, resolution, supersession chains | `FactRepository`, `Fact`, `Entity` |
| `src/paa/memory/creator.py` | `MemoryCreator`: ingest signals into hot serving facts | `MemoryCreator` |
| `src/paa/memory/curator.py` | `MemoryCurator`: nightly consolidation | `MemoryCurator` |
| `src/paa/memory/contradiction.py` | Contradiction detection and quarantine | — |
| `src/paa/memory/world_model.py` | Vault-based belief documents with marker-fenced managed blocks | `WorldModel` |
| `src/paa/agents/base.py` | `Agent`, `AgentContext`, `AgentMessage`, `AgentResult`, `MessageType`, `RiskLevel`, delegation entry point | `Agent`, `AgentContext`, `AgentMessage`, `AgentResult` |
| `src/paa/agents/orchestrator.py` | `ChiefOrchestrator`, `TaskRequest`, `TaskOutcome`; task lifecycle driver | `ChiefOrchestrator`, `TaskRequest` |
| `src/paa/agents/reasoning.py` | `ModelLike` protocol; `StrategicPlanner`, `CriticReviewer`, `TaskRouter`, `WorkerCell` | `ModelLike`, `StrategicPlanner`, `CriticReviewer`, `TaskRouter`, `WorkerCell` |
| `src/paa/agents/policy.py` | `PolicyRiskAgent` — deterministic risk gate, no model consulted | `PolicyRiskAgent`, `PolicyVerdict` |
| `src/paa/agents/delegation.py` | `DelegationEdge`, `DelegationGraph`, `DelegationRegistry` — depth/cycle/node bounds | `DelegationGraph`, `DelegationRegistry` |
| `src/paa/agents/context_agents.py` | `PlannerContextBuilder`, `WorkerContextBuilder`, `QueryExpander` | `PlannerContextBuilder`, `WorkerContextBuilder` |
| `src/paa/agents/specialists/` | Empty package (placeholder) | — |
| `src/paa/models/base.py` | `ModelProvider` contract, `CompletionRequest/Response`, `Message`, `ModelTier`, `validate_against_schema`, `redact`, `extract_json_object`, `minimal_instance_for_schema` | `ModelProvider`, `CompletionRequest`, `Message`, `ModelTier` |
| `src/paa/models/registry.py` | Provider specs (`ProviderSpec`), `build_provider`, `build_local_inference`, `KNOWN_PROVIDERS` | `build_provider`, `resolve_spec`, `KNOWN_PROVIDERS` |
| `src/paa/models/router.py` | `EscalatingModelRouter` — local-first, escalation with `MODEL_ESCALATED` audit | `EscalatingModelRouter` |
| `src/paa/models/*.py` | Concrete providers: `openai_compatible_provider`, `ollama_provider`, `anthropic_provider`, `gemini_provider`, `echo_provider`, `embeddings.py`, `_http.py` | — |
| `src/paa/sandbox/base.py` | `IsolationLevel`, `SandboxSpec`, `SandboxResult`, `Sandbox` ABC, `resolve_workspace`, `assert_inside_workspace`, `build_child_env` | `SandboxSpec`, `SandboxResult`, `IsolationLevel` |
| `src/paa/sandbox/subprocess_backend.py` | `SubprocessSandbox` (PROCESS isolation) | — |
| `src/paa/sandbox/wsl_backend.py` | `WslSandbox` (NAMESPACE isolation) | — |
| `src/paa/sandbox/docker_backend.py` | `DockerSandbox` (NAMESPACE/VM if runsc) | — |
| `src/paa/sandbox/dryrun_backend.py` | `DryRunSandbox` (NONE — executes nothing) | — |
| `src/paa/sandbox/recursion.py` | `DelegationGraph`, `RecursionGuard` — depth/cycle/node bounds | `RecursionGuard` |
| `src/paa/sandbox/watchdog.py` | `ResourceWatchdog`, `HeartbeatTracker` | — |
| `src/paa/sandbox/__init__.py` | `build_backend`, `get_sandbox` (auto-order docker→wsl→subprocess) | `get_sandbox` |
| `src/paa/validation/engine.py` | `DeterministicValidationEngine` — binary PASS/FAIL, ordered checks | `DeterministicValidationEngine`, `ValidationArtifact`, `ValidationReport` |
| `src/paa/validation/ast_scanner.py` | `AstSecurityScanner` — deny-by-default static scan | `AstSecurityScanner`, `Severity`, `Finding` |
| `src/paa/validation/patch.py` | Unified-diff parse/dry-run/apply/rollback with workspace jail | `UnifiedDiffValidator`, `PatchPlan`, `safe_relative_path` |
| `src/paa/validation/schema_validator.py` | `SchemaValidator` — pure-Python JSON Schema subset | `SchemaValidator` |
| `src/paa/validation/test_runner.py` | `PytestRunner` — run pytest inside a sandbox | `PytestRunner` |
| `src/paa/validation/workspace.py` | `WorkspaceSnapshot` — manifest capture/drift | `WorkspaceSnapshot` |
| `src/paa/coder/agent.py` | `CoderAgent.build` — composes runtime + tools + loop + permissions | `CoderAgent`, `MUTATING_RUNTIME_TOOLS` |
| `src/paa/coder/loop.py` | `AgentLoop` — think→act→observe under permissions & hooks | `AgentLoop`, `ToolCallingModel`, `Tool`, `LoopResult` |
| `src/paa/coder/model_adapter.py` | `RouterToolCallingModel` — bridges loop protocol to escalation router | `RouterToolCallingModel` |
| `src/paa/coder/permissions.py` | `ToolPermissionPolicy`, `DEFAULT_MUTATING_TOOLS` | `ToolPermissionPolicy` |
| `src/paa/coder/hooks.py` | `HookRegistry`, `HookEvent`, `HookContext`, `HookResult` | `HookRegistry`, `HookEvent` |
| `src/paa/coder/session.py` | `Session`, `TurnRole`, `Turn` — JSONL transcript, resume, compaction | `Session`, `TurnRole` |
| `src/paa/coder/runtime_tools.py` | `build_runtime_tools` — seven ledger-facing tools (check_runtime, list_tasks, task_status, submit_task, run_task, read_ledger, verify_ledger) | `build_runtime_tools` |
| `src/paa/coder/commands.py` | Slash commands (`/help`, `/clear`, `/model`, `/mode`, `/compact`, `/cost`) + markdown command loader | `CommandRegistry`, `default_registry` |
| `src/paa/skills/contracts.py` | `SkillContract`, `SkillInvocation`, `SkillResult`, contract parsing | `SkillContract`, `SkillInvocation`, `SkillResult` |
| `src/paa/skills/registry.py` | `SkillRegistry` — CRUD/discovery over `hot_serving_skill_registry` | `SkillRegistry` |
| `src/paa/skills/usa.py` | `UnifiedSkillAdapter` — dispatch state machine | `UnifiedSkillAdapter` |
| `src/paa/skills/secrets.py` | `SecretBroker`, `SecretValue`, `SecretAccess` — bound secret proxy | `SecretBroker`, `SecretValue` |
| `src/paa/skills/adapters/base.py` | `SkillAdapter`, `SecretProvider`, adapter `SkillResult` | `SkillAdapter` |
| `src/paa/skills/adapters/native.py` | `NativeAdapter` — in-process callables | — |
| `src/paa/skills/adapters/mcp.py` | `MCPClient`, `McpServerAdapter` — stdio JSON-RPC 2.0 | — |
| `src/paa/skills/adapters/claw_hub.py` | `ClawHubAdapter`, `parse_skill_md` — OpenClaw skill directories | — |
| `src/paa/marketplace/package.py` | `SkillPackage`, `PackageManifest` — .paapkg zip format, content hash, zip-slip guard | `SkillPackage` |
| `src/paa/marketplace/signing.py` | `sign`/`verify`, Ed25519 or HMAC dev fallback, `signing_is_secure` | `sign`, `verify` |
| `src/paa/marketplace/installer.py` | Five-gate installer (content hash → contract → signature → sandbox scan → validation → register) | `PackageInstaller`, `InstallGate` |
| `src/paa/marketplace/registry_client.py` | `HttpRegistry`, `LocalDirectoryRegistry`, `MarketplaceRegistry` | — |
| `src/paa/improvement/distillation.py` | Distillation (designed/partially built) | — |
| `src/paa/improvement/meta.py` | Meta-learning (designed/partially built) | — |
| `src/paa/improvement/optimizer.py` | Skill-weight optimizer | — |
| `src/paa/improvement/reflection.py` | Reflection over task windows; reuses `WorldModel` | — |
| `src/paa/observability/logging.py` | structlog configuration | — |
| `src/paa/observability/metrics.py` | Metrics registry | — |
| `src/paa/observability/tracing.py` | Tracing | — |
| `src/paa/observability/runs.py` | Execution runs | — |
| `src/paa/api/app.py` | FastAPI app: `/health`, `/tasks`, `/tasks/{id}`, `/tasks/{id}/gate`, `/tasks/{id}/correction`, `/ledger/{id}`, `/signals` | `create_app` |
| `src/paa/api/schemas.py` | Request/response pydantic models | `TaskRequestBody`, `TaskStatusResponse`, `SignalRequest`, … |
| `src/paa/cli/main.py` | `paa` Typer CLI: version, submit, run, status, ledger, gate, recover, doctor, serve, mode, tui | `app` |
| `src/paa/cli/coder.py` | `paa agent` command registration | `register_agent` |
| `src/paa/tui/runner.py` | Interactive dashboard runner | `run_tui` |
| `src/paa/tui/dashboard.py`, `dispatcher.py`, `runner.py`, `snapshot.py` | TUI components | — |

## Entry points (verified)

| Entry point | File | Function |
|---|---|---|
| `paa` CLI | `src/paa/cli/main.py:38` | `app = typer.Typer(...)`; `__main__` at line 467 |
| `paa submit` | `src/paa/cli/main.py:73` | `submit` |
| `paa serve` | `src/paa/cli/main.py:289` | `serve` |
| `paa doctor` | `src/paa/cli/main.py:224` | `doctor` |
| `paa recover` | `src/paa/cli/main.py:206` | `recover` |
| `paa ledger` | `src/paa/cli/main.py:144` | `ledger` |
| `paa gate` | `src/paa/cli/main.py:179` | `gate` |
| `paa agent` | `src/paa/cli/coder.py:53` | `register_agent` → `agent` |
| `paa tui` | `src/paa/cli/main.py:352` | `tui` |
| Programmatic runtime | `src/paa/runtime.py:260` | `Runtime.build()` |
| API factory | `src/paa/api/app.py:53` | `create_app(runtime)` |
| Daemon | `src/paa/daemon.py:44` | `Daemon(runtime)` |

## tests/

| Path | Covers |
|---|---|
| `tests/conftest.py` | `paa_home` fixture, `snapshotter` fake, `reset_dotenv_cache` |
| `tests/ledger/` | Ledger suite (71 tests per README) |
| `tests/context/` | Context engine (353 tests) |
| `tests/storage/` | Storage (255 + 5 skipped Redis) |
| `tests/sandbox/` | Sandbox (39 tests) |
| `tests/memory/` | Memory decay/domains/contradiction (37 tests) |
| `tests/agents/` | Agents suite (70 tests) |
| `tests/integration/` | End-to-end integration (16 tests) |
| `tests/models/` | Model providers + escalation router (71 tests) |
| `tests/coder/` | Coder agent (91 tests) |
| `tests/skills/` | Skills (51 tests) |
| `tests/marketplace/` | Marketplace (18 tests) |
| `tests/improvement/` | Self-improvement (22 tests) |
| `tests/api/` | FastAPI ingestion (27 tests) |
| `tests/validation/` | Schema validator suite |
| `tests/tui/`, `tests/cli/`, `tests/context/` | CLI/TUI/context |

## Important pre-existing documents (retained)

| Path | Responsibility |
|---|---|
| `docs/coder-agent.md` | The `paa agent` tool loop design |
| `docs/EXTENSIBILITY_PIPELINES.md` | Pluggable agentic loops |
| `docs/ROADMAP.md` | 10-phase roadmap |
| `docs/VISION.md` | Product vision |
| `docs/deployment/docker.md` | Docker/Postgres/S3 deployment topology |
| `docs/adr/README.md` | ADR index (claims 0001–0020; only 0008, 0010, 0015, 0019, 0020 exist) |
| `docs/adr/0008-idempotency-key-with-attempt.md` | Idempotency deviation |
| `docs/adr/0010-head-projection-not-partial-index.md` | Head projection deviation |
| `docs/adr/0015-capability-ceiling.md` | Escalation ceiling deviation |
| `docs/adr/0019-docker-server-deployment.md` | Docker deployment deviation |
| `docs/adr/0020-provider-keys-from-dotenv.md` | Provider keys from dotenv |

## High-coupling areas (where many modules converge)

1. `src/paa/storage/relational/database.py::Database` — imported by ledger,
   recovery, cold lake, graph, queue, policy, context agents, memory
   (facts/creator/decay/curator/contradiction), observability runs, skills
   registry, improvement modules, marketplace installer, runtime. The single
   relational access point.
2. `src/paa/config.py::get_settings()` — `lru_cache`d singleton read by nearly
   every subsystem; `PAA_HOME` changes require `reset_settings_cache()`.
3. `src/paa/runtime.py::Runtime.build()` — single composition root; everything
   flows through it.
4. `src/paa/core/types.py` — imported everywhere; the vocabulary layer.
