<!-- ORCH-DIGEST:START -->
agent: W0-INV-05 | domain: P-005/P-013/P-014 verification | status: DONE
items: 3 verified EXISTS / 0 ALREADY-FIXED / 0 NOT-REPRODUCIBLE / 1 PARTIAL / 0 MISCLASSIFIED
proposed_tasks: 3 (SURFACE 1, DEEP 2)
top_risks: P05 plan's --no-index claim contradicts derived-image layout; P13 has no last-run storage so /doctor hook needs new state; ADR 0036-0043 free but P14 Settings surface has 8 new sub-models to add atomically
cross_domain_conflicts: P05-ADR-0030 none; P14-DrainSettings-INV-01 overlap on drain.max_per_second owner
out_of_plan_discoveries: 2
confidence: high (all claims checked against live HEAD c15b665, 2 scoped pytest runs green)
last_comms_line_read: 2026-11-07T00:01:14Z ORCH -> ALL | DECISION | W0
<!-- ORCH-DIGEST:END -->

# Investigation report — W0-INV-05 — P-005/P-013/P-014 verification

## 0. Scope and method

Mission T-005: verify plan items P-005 (derived embed image), P-013 (weekly reflection wiring), P-014 (ADRs 0036–0043 + Settings surface) against live code at HEAD `88e648b` (= branch `ops/20261107-integration-fix`, current HEAD `c15b665` — docs-only delta: `4abf38b` init + `c15b665` DOCS-README-01, verified via `[RAN git log --oneline -5]`).

Read in order: brief `W0-INV-05.md`, `STANDING-ORDERS.md`, `AGENTS.md` (550 lines), `00-mission.md`, plan rev5 `2026-11-07-integration-fix.md` (195 lines, items P05/P13/P14 + T06/T14 + §K), `templates/report-investigation.md`, then code: `Dockerfile` (266 lines), `docker-compose.yml` (393 lines), `pyproject.toml` (160 lines), `.github/workflows/ci.yml` (433 lines), `src/paa/improvement/reflection.py` (270 lines), `src/paa/config.py` (802 lines), `docs/adr/README.md` + `docs/adr/0030-deployment-image-contract.md` (659 lines), `docs/runtime/background-jobs-and-events.md`, `src/paa/cli/main.py` doctor (lines 225–277), `src/paa/models/embeddings.py`, `src/paa/storage/vector/embeddings.py` (450 lines).

Commands run (read-only): `git branch --show-current` → `ops/20261107-integration-fix`; `git rev-parse HEAD` → `c15b66566af00f17e6ea7d289ed5c172a128e8a3`; grep sweeps for `run_weekly`, `ReflectionEngine`, `cron|schedule`, `sentence_transform`, `_try_`, new-flag names, `docker`; `Get-ChildItem docs/adr/*.md`; scoped pytest: `tests/improvement/test_improvement.py` → 22 passed; `tests/deployment/test_compose_config.py` → 11 passed. NOT run: full suite, `docker build` (docker absent on host), any grep outside listed scope.

Sibling reports `W0-INV-01..04.md` exist in `reports/w0-investigation/` (created in parallel) — not read, per ownership (only own report written).

## 1. Per plan item

### P-005 Embeddings without bloating base (`Dockerfile.embed`, derived)

- **Existence verdict:** EXISTS (gap is real: no derived image, base deliberately torch-free)
- **Evidence:**
  - `[VERIFIED Dockerfile:9-18]` header documents wheelhouse→builder→runtime (downloads once, offline install, lean runtime).
  - `[VERIFIED Dockerfile:27-30]` "The server extras (postgres, s3, redis, vector/qdrant-client, api, otel) are installed here. The heavyweight embeddings extra (torch, ~2 GB) is left out deliberately — … a deployment that wants real embeddings should build a derived image." — plan P05 quotes this correctly; derived image is the pre-approved direction.
  - `[VERIFIED Dockerfile:65]` wheelhouse groups = `("vector","api","redis","postgres","s3","otel")` — `embeddings` deliberately absent; `[VERIFIED Dockerfile:137-140]` builder installs `".[$(cat /extras.txt)]"` offline (`--no-index`) + `[VERIFIED Dockerfile:144]` `pip check`.
  - `[VERIFIED docker-compose.yml:67]` `image: paa/runtime:4.1` (also `:86`) — plan's pin source is correct. Both services have `build: context:. dockerfile:Dockerfile` (lines 64–66, 83–85); tag is locally-built, not pulled.
  - `[VERIFIED pyproject.toml:42]` `embeddings = ["sentence-transformers>=3.3"]`; `[VERIFIED pyproject.toml:77-79]` `all = ["paa[vector,graph,embeddings,api,redis,postgres,s3,otel]"]`; `[VERIFIED pyproject.toml:97]` `requires_embeddings` marker exists.
  - `[VERIFIED Dockerfile.embed: does not exist]` — `[RAN Test-Path Dockerfile.embed -> False]`; only `Dockerfile` exists.
  - `[VERIFIED src/paa/storage/vector/embeddings.py:359-368]` `_sentence_transformers_available()` via `importlib.util.find_spec` (no torch import); `[VERIFIED :176-189]` `SentenceTransformer` imported lazily in `_load()` (worker thread); `[VERIFIED :386-433]` `get_embedder()` returns real embedder if spec found, else `HashEmbedder` fallback with `log.warning("embeddings.hash_fallback_active")`, raises only if `allow_hash_embedder_fallback=False`. `[VERIFIED src/paa/config.py:617]` `allow_hash_embedder_fallback: bool = True`.
  - `[VERIFIED src/paa/runtime.py:400-408]` `_try_vector()` wraps `get_embedder` in try/except → returns `(None, None)` + `log.warning("runtime.vector_unavailable")` (degrade-never-raise, per `AGENTS.md:104-108`). Same pattern `_try_graph` (`:413`), `_try_queue` (`:423`), `_acquire_sandbox` (`:496`).
  - `[VERIFIED docs/adr/0030-deployment-image-contract.md:108-116]` pulled images must be digest-pinned (tag-only = error); `[VERIFIED :295-299]` local builds are NOT fetched; `[VERIFIED :653-655]` "The Dockerfile base images are still not in the plan … Recorded, not fixed." P05 (new derived image + ADR-0042) amends this — does NOT contradict: derived image is a local build (`FROM paa/runtime:4.1` tag produced by base build), not a compose pull, so digest-pin rule does not apply (same class as `build:` services).
  - `[VERIFIED .github/workflows/ci.yml:95-115]` matrix `install: minimal/dev/all` with `extras: ""/"[dev]"/"[all]"`; gate steps `ruff`/`mypy`/`pytest` (`:398-433`). `all` shape installs torch (comment `:86-89` notes 45-min timeout for this). No docker-build job exists today — embed job would be net-new.
- **Plan discrepancy (PARTIAL):** Plan P05 HOW says "`pip install --no-index` from wheelhouse (offline, per Dockerfile convention)". A derived image `FROM paa/runtime:4.1` has NO wheelhouse (runtime stage contains only venv + docker client, `[VERIFIED Dockerfile:246]` `COPY --from=builder /opt/venv`). Offline install is impossible without either (a) extending base `Dockerfile` wheelhouse groups to include `embeddings` (bloats wheelhouse layer but keeps derived offline), or (b) online `pip install "paa[embeddings]"` with retry loop in derived image (network-fragile convention permits: `AGENTS.md:288-300` requires every network op retries; pip needs `PIP_RETRIES=10` + `retry()` loop + SHA verify). Plan as written conflates the two. ADR-0042 must pick one; do not silently claim offline.
- **Root cause chain:** symptom (paraphrase recall fails under hash fallback) → cause (torch excluded from base by design) → origin (ADR-0007 hardware constraint + ADR-0030 last-chapter "not fixed" note). Not a bug; P05 is additive.
- **Blast radius:** files: `Dockerfile.embed` (new), `pyproject.toml` (extras, possibly wheelhouse groups), `docs/adr/0042-*.md`, `docs/adr/README.md` (index), `.github/workflows/ci.yml` (new job), `docs/deployment/docker.md`. Callers: `get_embedder` selection is transparent (spec-found → real). Tests: `tests/storage/test_vector.py:43` `HAS_SENTENCE_TRANSFORMERS` guard (skips when absent — plan's A5b `requires_embeddings` gated paraphrase test fits here). No ledger/schema impact.
- **Fix options:** A — derived `Dockerfile.embed` online pip + retries (lean, needs network, must retry-loop); B — extend wheelhouse groups + derived copies wheelhouse (offline, larger wheelhouse layer, but keeps base build offline-green). **Recommended:** A with full retry convention + CI import assertion (`python -c "import sentence_transformers"` inside embed image only; base asserts hash fallback). **Why:** matches Dockerfile header intent ("build a derived image"), avoids bloating base wheelhouse for torch, and AGENTS.md network-fragility rule already covers online retry.
- **Long-run test:** Source (plan units: derived Dockerfile exists, FROM pinned `paa/runtime:4.1` never `latest`); Load (embed image builds under flaky mirror — retries absorb transient); Hostile (corrupt download → SHA/retry fails loudly, never silently partial); Operability (embed image opt-in, base unchanged, rollback = delete derived); Reversal (remove derived image → A5b skips, A5a still green).
- **Classification:** SURFACE (additive, isolated, no state)
- **WRITE-SET:** `Dockerfile.embed`, `docs/adr/0042-*.md`, `docs/adr/README.md`, CI job in `.github/workflows/ci.yml`. **CONTRACT-SET:** embedder selection (unchanged); image tag `paa/runtime:4.1` (read-only). **DEPENDS-ON:** P04 (code path) — code side only; A5b needs P05. **ENV-SET:** none (no new Settings).
- **Test plan:** T06 CI: (1) build embed image succeeds; (2) `import sentence_transformers` exit 0 inside; (3) base image import fails / hash fallback marker present; (4) `pip check` passes in embed. Boundary: base without extra → degraded marker, not crash. NOT RUN here (docker absent).
- **Conflicts/ordering:** ADR-0042 amends ADR-0030 (add amendment note, don't rewrite 0030). CI job slots as new `jobs.embed-image` after `gate` or as 4th matrix row — must NOT widen sandbox PATH or install (landmines at `ci.yml:21-36,458-466`). If option B chosen, touches `Dockerfile` (hotspot-adjacent; single-writer).
- **Acceptance criteria (measurable):** `Dockerfile.embed` first non-comment line pins `FROM paa/runtime:4.1`; all network ops retry-looped; CI embed job green with import assertion; `ruff/mypy/pytest` unchanged.

### P-013 Weekly reflection wiring (`improvement/reflection.py`, scheduler, `/doctor`)

- **Existence verdict:** EXISTS (engine real, wiring absent)
- **Evidence:**
  - `[VERIFIED src/paa/improvement/reflection.py:101]` `class ReflectionEngine`; `[VERIFIED :169]` `async def run_weekly(self, *, now=None)` analyses last 7 days, writes anti-patterns to playbook.
  - `[RAN grep run_weekly in src/paa/**/*.py -> only definition at reflection.py:169; zero callers]` — no daemon, CLI, or orchestrator invocation. `[RAN grep in tests -> only tests/improvement/test_improvement.py:128,154]`.
  - `[VERIFIED docs/runtime/background-jobs-and-events.md:5]` "There is no separate worker process, cron scheduler, or external message broker"; `[VERIFIED :131]` "No cron/scheduled jobs beyond the 6h decay sweep." Daemon loops are exactly 3: `[VERIFIED src/paa/daemon.py:52-54]` dispatch 0.5s, ingestion 1.0s, maintenance 6h (`DecaySweeper`); `[VERIFIED daemon.py:170-182]` maintenance = curation tick only. No weekly/reflection loop.
  - `[VERIFIED src/paa/cli/main.py:225-277]` `/doctor` builds Rich table rows: version, home, permission mode, relational, vector, graph, queue, model router, sandbox facts, open tasks. No reflection row, no `last_run_age_days`. Tests: `[VERIFIED tests/cli/test_cli.py:60-139]` doctor backends/sandbox.
  - `[RAN grep last.*reflection|reflection.*last|REFLECTION_RUN -> none]` — no last-run timestamp stored anywhere (no ledger event, no file, no Settings field). `[RAN grep REFLECT in core/types.py -> none]` — no `EventType` for reflection runs.
  - `[RAN pytest tests/improvement/test_improvement.py -q -> 22 passed (dots only)]`; `[RAN pytest tests/deployment/test_compose_config.py -q -> 11 passed]`. Improvement tests pin `run_weekly()` behaviour (high-friction flagged, playbook preserves human text) but not scheduling or staleness.
- **Root cause chain:** symptom (doc claims "once a week" at `reflection.py:1-3` RFC §3.1) → mechanism (engine implemented + tested, never scheduled) → origin (manual-only by omission; no cron infra exists in repo to wire into). Plan P13 "Cron/scheduler step" assumes OS cron or daemon loop — repo has neither; choice is new.
- **Blast radius:** `src/paa/improvement/reflection.py` (add `last_run` tracking), `src/paa/daemon.py` (4th loop OR CLI cron note), `src/paa/cli/main.py` (doctor row), `src/paa/config.py` (ReflectionSettings), possibly `src/paa/core/types.py` + `ledger/replay.py` if event-based (EventType checklist per AGENTS.md:88-93). TUI `render_doctor` (`tui/dashboard.py:176`) mirrors CLI — consider both.
- **Fix options:** A — daemon 4th loop (weekly interval, supervised like others) + ledger/file last-run + doctor row; B — OS cron invoking new `paa reflection` CLI + doctor row (no new daemon loop, but requires operator cron setup); C — document manual-only (plan rejects: "Leave manual = doc claim false"). **Recommended:** A (matches repo's single-daemon philosophy, no external scheduler dependency). **Why:** daemon already supervises loops; cron would be first external scheduler and untestable in CI.
- **Long-run test:** Source (T14: last run 8d ago → doctor stale; manual run clears); Load (weekly loop fires once per interval, not tight loop); Hostile (empty ledger → no-op report, no crash); Operability (kill daemon mid-reflection → next tick re-runs idempotently; flag `reflection.enabled=False` stops loop); Reversal (disable flag → doctor shows "disabled", engine still callable).
- **Classification:** SURFACE (additive loop + row; no schema/ledger invariant change unless event-based — prefer file/ledger-read over new EventType to stay SURFACE; if new EventType chosen, becomes DEEP upfront — flag for P14 writer)
- **WRITE-SET:** `src/paa/improvement/reflection.py`, `src/paa/daemon.py`, `src/paa/cli/main.py`, `src/paa/config.py` (ReflectionSettings), `docs/adr/?-reflection-wiring.md` (which number TBD — see P14). **CONTRACT-SET:** `run_weekly()` signature (unchanged); doctor table (additive row). **DEPENDS-ON:** P14 Settings. **ENV-SET:** `PAA_REFLECTION__ENABLED` (+ optional interval).
- **Test plan:** T14 unit (due → stale; run → clears); boundary (exactly 7d → not stale; 7d+1s → stale); hostile (no vault → report still produced, no playbook write); concurrency (two ticks same week → playbook appends once — marker-fenced `_write_managed_block` already handles). Must fail before fix: doctor lacks reflection row; new test asserts row exists.
- **Conflicts/ordering:** Scheduler loop vs maintenance loop ordering (run after curator DECAY phase, same tick discipline as P09 note — no parallel prune race). Observability metric `reflection.last_run_age_days` (§K) needs last-run source — decide FIRST (ledger query `events_since` vs file) before doctor row.
- **Acceptance criteria (measurable):** `run_weekly` has exactly one production caller; `/doctor` prints `reflection.last_run_age_days` ≤7 when enabled; T14 green; `grep run_weekly` hits beyond definition+tests.

### P-014 ADRs 0036–0043 + Settings surface (`docs/adr/`, `config.py`)

- **Existence verdict:** EXISTS (both sequences free; Settings pattern established; new flags absent, as expected pre-P14)
- **Evidence:**
  - `[RAN Get-ChildItem docs/adr/*.md -> 0008,0010,0015,0019-0026,0028-0035 + README.md]`; `[VERIFIED docs/adr/README.md:55]` last index row is 0035. **ADR 0036–0043 are FREE** — no files, no index rows, no `SPEC DEVIATION (docs/adr/0036+)` citations found (grep of src for those numbers implied by glob; full-tree grep not run — noted as residual risk). Note: AGENTS.md known-stale section lists same disk set. Plan mapping (0036 allocator / 0037 tiers / 0038 dual vault / 0039 coder fallback / 0040 cost gate / 0041 specialists / 0042 embeddings image / 0043 edges-phantom) is consistent with free sequence. **Do not share** — one ADR per item per plan header.
  - `[VERIFIED src/paa/config.py:662-683]` `Settings(BaseSettings)` with `env_prefix="PAA_"`, `env_nested_delimiter="__"`, `env_file=".env"`, `extra="ignore"`. Sub-models: `StorageSettings(:56)`, `ContextSettings(:177)`, `MemorySettings(:215)`, `PolicySettings(:336)`, `SandboxSettings(:358)`, `ModelSettings(:553)`, `QueueSettings(:620)`, `ObservabilitySettings(:645)` — each `BaseModel` with `Field(default=..., ge/le/gt)` constraints, `#:` rationale comments. No `Locator` — every new flag per convention goes in a NEW `BaseModel` subclass field-registered on `Settings` (then `PAA_<SUB>__<FIELD>` works automatically).
  - Flag mapping (brief's 8 names → proposed declaration, all `[ASSUMED]` for exact class names since P14 not yet written; pattern `[VERIFIED]`):
    - `allocator.enabled=False`, `allocator.max_results=5` → new `AllocatorSettings(BaseModel)` field `allocator` on `Settings` → `PAA_ALLOCATOR__ENABLED`, `PAA_ALLOCATOR__MAX_RESULTS` [plan P07 states these defaults].
    - `tier.dry_run=True` → new `TierSettings` field `tier` → `PAA_TIER__DRY_RUN` (plan P09 default `True`; backfill is migration step, NOT a setting — plan P14 explicit).
    - `cost_gate.enabled=False`, `cost_gate.threshold=1.0` → new `CostGateSettings` field `cost_gate` → `PAA_COST_GATE__ENABLED`.
    - `vault_sync.enabled=False` → new `VaultSyncSettings` field `vault_sync` → `PAA_VAULT_SYNC__ENABLED` (rollout §K: `vault_sync.enabled=False`).
    - `specialists.coding.enabled` (nested) → new `SpecialistsSettings` with `coding: SpecialistToggle(enabled=...)` or flat `coding_enabled`? Convention (nested delimiter) favours sub-BaseModel `CodingSettings(enabled: bool)` so env = `PAA_SPECIALISTS__CODING__ENABLED`. `[ASSUMED]` — simpler flat `PAA_SPECIALISTS__CODING_ENABLED` also works but breaks `specialists.<name>.enabled` symmetry for pilot→4→5 rollout. Recommend nested.
    - `reflection.enabled` → new `ReflectionSettings` field `reflection` → `PAA_REFLECTION__ENABLED` (P13).
    - `drain.max_per_second` → owner per plan P07: `memory/creator.py:1490` — declare in `MemorySettings` (existing class, memory domain) OR new `DrainSettings`. `[ASSUMED]` prefer `MemorySettings` (already governs decay/curation cadence) unless INV-01 claims otherwise — flag overlap: plan P07 says "P07 is the sole owner" so INV-01's flags must not double-declare. Brief notes INV-01..04 consume these findings.
  - `[RAN grep for those 8 names in src -> none (only "reflection":0.002 decay lambda and word "Frontier")]` — confirms flags not yet in code, P14 genuinely unbuilt.
  - `[VERIFIED src/paa/config.py:788-791]` `get_settings()` is `lru_cache`d; `reset_settings_cache()` for tests (AGENTS.md:164-165). Any new sub-model must also be considered in `_propagate_home`? No — only storage paths re-root; new settings hold no paths except possibly `vault_sync` (if it grows paths, extend `_propagate_home`).
  - Settings tests: `[RAN no tests/config/ dir]`; settings exercised via `Settings(home=...)` in `tests/api/conftest.py`, `tests/cli/test_memory.py`, `tests/coder/test_agent.py`, plus `tests/deployment/test_compose_config.py` (compose↔code parity). Plan P14 "parity + settings tests" fits there; `test_compose_config.py` pattern is the model for adding env-parity assertions.
  - `[VERIFIED AGENTS.md:323-327]` ADR index-only rows are normal (0001–0007, 0009, 0011–0014, 0016–0018 index-only). New ADRs should land as full files (like 0030), not index-only.
- **Root cause chain:** symptom (plan needs 8 new flags + 8 ADRs) → mechanism (config surface is additive by convention; ADR sequence free) → no defect; risk is collision (parallel investigators/writers) and atomicity (all 8 flags + 8 ADRs in one writer's wave).
- **Blast radius:** `docs/adr/0036–0043` (8 new files) + `README.md` index (8 rows), `src/paa/config.py` (8 new classes + 8 fields + `__all__` additions). Hotspot per `00-mission.md:30` — P14 sole owner of `config.py`. Downstream: every P07–P13 writer reads these flags; INV-01..04 parallel reports may assume shapes — P14 must be single-writer, waves-gated.
- **Fix options:** A — one P14 writer adds all 8 sub-models + 8 ADRs atomically (plan §F "main→P14 locks"); B — per-item writers add their own flag+ADR. **Recommended:** A (avoids concurrent `config.py`/`README.md` edits, hotspot single-writer rule). **Why:** `config.py` is mission-hotspot; concurrent edits risk churn/lock files.
- **Long-run test:** Source (each flag default asserts §K values; env override round-trips); Load (first import cost negligible — no heavy imports in config); Hostile (bad env value → pydantic ValidationError at boot, not silent fallback); Operability (toggle flag → feature off without code change; rollback = flag flip); Reversal (remove new sub-model → old env vars ignored via `extra="ignore"`, no crash).
- **Classification:** DEEP (touches hotspot, gates 5 plan items, ADR chain semantics)
- **WRITE-SET:** `src/paa/config.py`, `docs/adr/0036-*.md` … `0043-*.md`, `docs/adr/README.md`. **CONTRACT-SET:** all `PAA_*__*` env names (new, additive); `get_settings`/`reset_settings_cache` (unchanged). **DEPENDS-ON:** P07–P13 (read flags after declared). **ENV-SET:** the 8 new prefixes above.
- **Test plan:** per-flag: (1) default value == §K; (2) env override via monkeypatched `PAA_<SUB>__<FIELD>` + `reset_settings_cache()`; (3) invalid value raises; (4) `test_compose_config.py`-style parity if compose surfaces any flag (today compose sets none of these 8 — parity is no-op unless P10/P13 env forwarding added). Must fail before fix: env var currently ignored (extra="ignore") → assertion flips.
- **Conflicts/ordering:** P14 AFTER P07–P13 code reads flags (plan table Depends). `drain.max_per_second` ownership: P07 sole owner — P14 only declares, P07 wires. `specialists.coding.enabled` nested shape must match P11 `AgentRole.CODING_SPECIALIST` wiring — coordinate shape now, write later.
- **Acceptance criteria (measurable):** 8 ADR files exist + indexed; 8 Settings sub-models import clean; `Settings().allocator.enabled is False`, `Settings().tier.dry_run is True`, `Settings().cost_gate.enabled is False`, `Settings().vault_sync.enabled is False`, `Settings().reflection.enabled` per P13 default; env override test green; `ruff/mypy 0` kept.

## 2. Duplicates / shared root causes

- P05 + P14 share ADR/README write-set (0042 + index). P14 (single writer) files all 8; P05 writer must NOT create 0042 independently or index in parallel — coordinate via P14's wave.
- P13 + P14 share `config.py` (ReflectionSettings) — same single-writer constraint.
- P05 offline-install discrepancy (derived image has no wheelhouse) is the only plan-vs-code contradiction found. Everything else (tags, stages, extras, markers, ADR numbers, daemon loops absence) verified consistent.

## 3. Out-of-plan discoveries

1. `[VERIFIED src/paa/improvement/reflection.py:176-182]` `run_weekly()` calls `analyze_window` which reads `store.events_since` — reflection has zero ledger write (no `REFLECTION_COMPLETED` event). A doctor staleness query built on ledger reads would need either a new read path (scan `REFLECTION_*` events that don't exist) or file-based last-run. File-based is safer (no EventType/replay work). Not plan-breaking; worth a P13 design note. Evidence: global grep for `REFLECTION_` event = zero hits.
2. `[VERIFIED .github/workflows/ci.yml:86-89]` `all` shape timeout 45 min justified by torch download; P05 CI embed job would add a SECOND torch download path (derived image build) — total CI minutes grow. Plan should scope embed job to `all`-only or nightly, not every push × 3 shapes. [ASSUMED] — needs orchestrator decision, not a code fact.

## 4. Assumptions and unknowns

- [ASSUMED] Settings class names in §P14 mapping (`AllocatorSettings`, `TierSettings`, etc.) — pattern is `[VERIFIED]`, exact names not yet fixed by any writer. Resolvable by P14 single writer.
- [ASSUMED] `specialists` nested env shape (`PAA_SPECIALISTS__CODING__ENABLED` vs flat) — resolvable by P14 writer in coordination with P11.
- [UNKNOWN] Docker availability: `[RAN Get-Command docker -> CommandNotFoundException; docker version -> not recognized]` on this Windows host. Cannot verify read-only whether a daemon exists or whether private registry holds `paa/runtime:4.1`. Resolvable by operator/CI healthcheck (plan RS2).
- [UNKNOWN] Whether any `SPEC DEVIATION (docs/adr/0036+)` citations exist outside `src/paa` (only `src` grepped for flag names). Resolvable: full-tree grep for `0036|0037|0038|0039|0040|0041|0042|0043` by P14 writer before creating files. Glob + index evidence strongly suggests free; exact residual risk low.
- [UNKNOWN] Python `requires_embeddings` parity claim in plan success criteria A5b: marker exists `[VERIFIED pyproject.toml:97]` but no test currently uses it (grep `requires_embeddings` in tests → only `test_vector.py:43` find_spec guard). P05's T06 will create the first consumer; A5b criterion unprovable until then. Not a contradiction, just unbuilt.

## 5. Recommended task breakdown and ordering

1. Wave +1 (P14 writer, single): `config.py` 8 sub-models + 8 ADR files (0036–0043) + index rows; parity tests. Order inside: Settings first (code consumers compile), ADRs second (record reasons).
2. P05 (after P14 ADR-0042 exists): `Dockerfile.embed` option A (online retry) + CI job; T06.
3. P13 (after P14 ReflectionSettings): daemon weekly loop + doctor row using file-based last-run; T14.
   ⚠️ Do NOT run P05/P13 config edits concurrently with P14 writer (hotspot).

## Re-verification (per METHOD §11)

- Re-read `Dockerfile:27-30` + `storage/vector/embeddings.py:386-433` — derived-image direction and degraded-fallback claims still hold.
- Re-ran `grep run_weekly` — still zero production callers; doctor still lacks reflection row (`cli/main.py:246-272`).
- Re-globbed `docs/adr/` — still max 0035; P14 sequence-free claim holds.

## Scratch files

none

## Baselines (scoped, per brief)

- `[RAN .venv\Scripts\python.exe -m pytest tests/improvement/test_improvement.py -q -> 22 passed]`
- `[RAN .venv\Scripts\python.exe -m pytest tests/deployment/test_compose_config.py -q -> 11 passed]`
- `[RAN docker version -> CommandNotFoundException (not installed on this host)]` → Docker availability UNKNOWN (recorded, no build attempted per brief).
- Full suite NOT run (forbidden by brief §7).
