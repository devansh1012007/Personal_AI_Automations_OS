<!-- ORCH-DIGEST:START -->
agent: W1-IMPL-01 | domain: P-014 ADRs 0036-0043 + Settings surface + T13 | status: DONE
items: 8 flags/ADRs done; plan wrong twice (ADR-0042 offline claim, ADR-0041 rollback terminal claim) — corrected with evidence
proposed_tasks: 0 (this is the implementation)
top_risks: config.py line shifts break 9 pre-existing doc citations (FINDING — needs doc re-point task after waves settle); reflection.enabled + drain defaults were plan-unspecified (conservative + documented)
cross_domain_conflicts: none (config.py + docs/adr sole owner this wave)
out_of_plan_discoveries: 1 (docs-lock shift churn from parallel wave edits)
confidence: high (31 new tests green; ruff/mypy scoped 0)
last_comms_line_read: 2026-11-07T08:01:27Z ORCH -> ALL | DECISION | W1
<!-- ORCH-DIGEST:END -->

# Implementation report — W1-IMPL-01 — P-014 (T-014-P014)

## Status: DONE

## 0. Scope and method
Task: implement plan P-014 exactly per brief `docs/agent-ops/20261107-integration-fix/briefs/W1-IMPL-01.md`.
WRITE-SET only: `src/paa/config.py`, `docs/adr/0036-*.md`..`0043-*.md`, `docs/adr/README.md`, `tests/test_config_settings.py`.
Required reading done in brief order: STANDING-ORDERS, AGENTS.md (injected), 00-mission.md, plan rev5
`C:\Users\devan\.opencode\plan\2026-11-07-integration-fix.md`, W0-INV-05 §P-014 + §P-013, W0-INV-04 §P-001,
W0-INV-01 §P-009, W0-INV-03 (full), gate-report template, then code (config.py in full, ADR-0035 exemplar,
README index, `src/paa/storage/graph/base.py` EDGE_TYPES, Dockerfile stages, `src/paa/models/base.py` redact,
`src/paa/memory/world_model.py` enum, `src/paa/memory/creator.py` drain, `src/paa/improvement/reflection.py`,
`src/paa/ledger/replay.py` phase maps, docs-references lock).

Branch verified: `ops/20261107-integration-fix` [RAN git branch --show-current].
HEAD at start: `294fb31` [RAN git log --oneline -5]. Working tree clean at start [RAN git status --short -> empty].

COMMS read at start; last line: 2026-11-07T08:01:27Z ORCH -> ALL | DECISION | W1 (wave 1 start).

## Context Report (pre-implementation, per mandate section 1)
Root cause: plan P-014 is the cross-cutting lock item — every later wave reads Settings flags + ADR decisions
declared here. No defect; additive surface. Evidence (INV-05 verified): ADR 0036-0043 free, Settings pattern
established (PAA_ + nested __, per-sub-model BaseModel, #:-comments, __all__ alphabetical), flags absent from code.
Affected files: config.py (6 new Settings fields, 7 new classes, MemorySettings +1 field, __all__ +7),
8 ADR files + index, tests/test_config_settings.py (new). Downstream: P07-P13 implementers consume these flags.
Invariants: env_prefix PAA_ + env_nested_delimiter __ unchanged; get_settings lru_cache + reset_settings_cache
unchanged; no path-bearing new setting (no _propagate_home change); class X(str,enum.Enum) untouched;
zero-baseline ruff 0 / mypy 3-error gate (subprocess_backend.py:548, untouched — must stay 3).
Risks/edge cases: (a) plan names no default for reflection.enabled (P13 row) nor drain.max_per_second (P07 row) —
brief instructs: conservative positive default for drain, stated in ADR-0036, do not invent silently; same
treatment for reflection (documented in config comment + this report); (b) specialists nested env shape
PAA_SPECIALISTS__CODING__ENABLED; (c) docs path:line lock — ADRs are in lock scope (docs/adr/*), every
path:line must resolve to exactly one file + existing non-blank line; use full repo-relative paths, avoid
bare filenames and :NNN continuation shorthand; (d) T13 EDGE_TYPES pin must live in tests/test_config_settings.py
(write-set allows no other test file).
Expected behavior: Settings() defaults == plan §K (allocator.enabled=False, max_results=5, tier.dry_run=True,
cost_gate.enabled=False, threshold=1.0, vault_sync.enabled=False, specialists.coding.enabled=False), env overrides
round-trip via PAA_<SUB>__<FIELD> + reset_settings_cache(), invalid values raise ValidationError.
Required tests: per-flag default/env/invalid in tests/test_config_settings.py; T13 set-equality vs ADR-0043 table.

Phases (atomic):
- Phase A: config.py Settings surface.
- Phase B: tests/test_config_settings.py (per-flag + T13).
- Phase C: ADRs 0036-0043 + README index.
- Phase D: scoped verification (ruff/mypy scoped, pytest scoped incl. docs lock + schema parity no-op check).
- Phase E: self-review + final review, commit code, commit report separately.

## Phase log
### Phase A — config.py Settings surface (DONE, self-reviewed)
- `__all__`: +7 entries, alphabetical (AllocatorSettings, CodingSettings, CostGateSettings, ReflectionSettings,
  SpecialistsSettings, TierSettings, VaultSyncSettings).
- `MemorySettings.drain_max_per_second: int = 100 (ge=1)` — plan P07 names the Setting, no default; 100 ==
  current `drain_pending_embeddings(limit=100)` batch at ~1 Hz daemon cadence (no behaviour change when unset).
  Declared only; P07 wires the throttle.
- New sub-models after ObservabilitySettings: AllocatorSettings (enabled=False, max_results=5 ge=1),
  TierSettings (dry_run=True), CostGateSettings (enabled=False, threshold=1.0 gt=0), VaultSyncSettings
  (enabled=False), CodingSettings (enabled=False) nested in SpecialistsSettings (coding field),
  ReflectionSettings (enabled=False — plan P13 names the flag, no default; conservative opt-in, documented
  in field comment). Each field has a `#:` rationale comment citing its plan item / ADR.
- Settings gained 6 fields (allocator, tier, cost_gate, vault_sync, specialists, reflection); no path-bearing
  setting, so `_propagate_home` untouched — verified: no new field stores a `Path`.
- Self-review (re-read diff): defaults match plan §K/§E; nested specialists env = PAA_SPECIALISTS__CODING__ENABLED;
  constraints (ge=1/gt=0) reject hostile values at validation instead of at use. No hardcoded defaults elsewhere
  (config.py is the single declaration site). No severity findings.
- [RAN .venv\Scripts\python.exe -c Settings defaults assert -> "defaults OK"] (covers all 8 flags incl.
  specialists/reflection/drain per brief acceptance).

### Phase B — tests/test_config_settings.py (DONE, self-reviewed)
- 31 tests, all passing [RAN .venv\Scripts\python.exe -m pytest tests/test_config_settings.py -q -> 31 passed]:
  per-flag defaults (plan §K/§E), env overrides with opposite-of-default raw values (6 booleans + max_results/
  threshold/drain), invalid values raise ValidationError (9 cases), get_settings cache-reset behaviour,
  __all__ membership, specialist nested shape, T13 EDGE_TYPES set-equality vs ADR-0043 table (12 members,
  MUTATES = the non-§7.3 member).
- Mutation check: changing any default, field constraint, or EDGE_TYPES membership fails at least one test;
  env tests use opposite-of-default raw values so a pass proves the variable was actually read.
- Convention: `paa_home` fixture (PAA_HOME isolation + reset_settings_cache), monkeypatch.setenv per repo pattern.
- Self-review: no type:ignore, no noqa, line-length 100 respected. No severity findings.

### Phase C — ADRs 0036-0043 + README index (DONE, pending lock verification)
- 0036 allocator (StreamName StrEnum waiver §, starvation fallback, K/F/R derivation rules, allocator event
  -> _NO_PHASE_CHANGE/non-terminal, drain default 100, §P01 200-char spec).
- 0037 tiers-replay (projection target = tier-dict helper apply_tier_event, NOT TaskProjection; union precedence;
  T2 backfill; dry_run; full ADR-0008 key w/ discriminator=fact_id+to_tier; sweeper-side already-done).
- 0038 dual vault (15 managed = 13 RFC §9 + 2 live-only; conflict = human edit inside block since last atomic
  write -> named-path error; dead-writer wiring decided; governance plan-original; source header write).
- 0039 coder fallback (memory/touch.py, Runtime.embedder field, keyword-first, degrade when absent).
- 0040 cost gate (advisory only, utility=0 -> inf, storage+maintenance source gaps named, threshold positive scale).
- 0041 specialists (coding-only pilot, RFC §11.8 = 10, "21" phantom closed, STATE_ROLLBACK_TRIGGERED reuse —
  existing resumable wiring cited src/paa/core/types.py:165, src/paa/ledger/replay.py:166, TERMINAL_EVENTS
  src/paa/core/types.py:187; no new terminal event claimed).
- 0042 embeddings image (FROM paa/runtime:4.1 pinned per docker-compose.yml:67; plan --no-index corrected:
  derived image has no wheelhouse -> online install with PIP_RETRIES + retry loop; digest rule n/a for local builds).
- 0043 edges-phantom (exact 12 table; T13 pin).
- README index +8 rows; "files on disk" line updated to include 0036-0043.
- ADR citations: full repo-relative path:line only, each verified non-blank in a fresh read before writing;
  no bare filenames, no :NNN shorthand. To be proven by the docs-references lock in Phase D.

### Phase D — scoped verification (DONE)
- [RAN .venv\Scripts\python.exe -m pytest tests/test_config_settings.py tests/storage/test_schema_parity.py -q -> 37 passed, exit 0] (31 new + schema parity green, proving nothing touched there).
- [RAN .venv\Scripts\python.exe -m ruff check src/paa/config.py tests/test_config_settings.py -> "All checks passed!", exit 0] (baseline 0 held).
- [RAN .venv\Scripts\python.exe -m mypy src/paa/config.py -> "Success: no issues found in 1 source file", exit 0] (adds 0 errors; total stays 3 elsewhere).
- [RAN .venv\Scripts\python.exe -m pytest tests/conventions/test_docs_references.py -q -> 2/3 pass, 1 fail] — see FINDING below.
- Acceptance grep: 8 ADR files on disk + 8 index rows (+ "0036–0043" in the files-on-disk line) [RAN via glob + Select-String -> 8 files, 9 matches].
- Full suite NOT run (forbidden by standing orders §16).

**FINDING (docs lock, outside write-set — reported, not fixed).** `test_every_referenced_line_is_not_blank`
fails with 60+ violations. Decomposition, proven with `git show HEAD:` comparisons: (a) pre-existing
(w6-w7-w9 citations, mission category C); (b) 9 citations to `src/paa/config.py` (lines 97,133,137,381,450,
460,491,495,630) were non-blank at HEAD and are blank now — broken by my insertions (+137 lines, forced:
sub-models must precede `Settings`, `__all__` must stay sorted). Any config.py change unavoidably shifts
these; the lock is designed for a follow-up doc re-point task. (c) remaining `cli/main.py`, `runtime.py`,
`context_agents.py` citations broken by W1-IMPL-02/03's parallel edits (working tree shows their files
modified). **My contribution is zero:** none of the 60+ violations names `docs/adr/*.md` 0036–0043, and the
lock's other two tests (names exactly one file / within file — which cover my 8 ADRs' citations) pass.
Needed: a docs re-point task after all waves' code settles (or accept per-wave lock churn). Posted as NEED/COMMS FINDING.

### Self-review (final, per mandate)
Re-read the full config.py diff from scratch: 6 new Settings fields + 7 new classes + MemorySettings drain
field + sorted __all__; no path-bearing setting (proves `_propagate_home` untouched); constraints reject
hostile values at validation; `#:` comments carry plan/ADR rationale per convention. ADRs re-checked against
brief mandates: INV-05 offline contradiction resolved in ADR-0042 (online+retries, plan claim corrected with
evidence); tier projection target decided in ADR-0037 (tier-dict helper, not TaskProjection); ADR-0041 states
existing `STATE_ROLLBACK_TRIGGERED` wiring and claims no new terminal event. No CRITICAL/MAJOR findings.
Independent reviewer: none available in-subagent mode (rule requires report-or-blocker instead of fake pass) —
status DONE with the docs-lock FINDING surfaced; final gate will re-run the lock on the settled tree.

### Long-run test (standing order 8)
- **Source:** every default / count / key in this report is verified against live code, not plan text
  (plan was wrong twice and is corrected in ADR-0042 + report: offline claim, rollback terminal claim).
- **Load:** Settings construction is O(1) extra work (7 sub-models); no loops, no I/O, no locks; concurrent
  `get_settings()` is the existing lru_cache shape, unchanged.
- **Hostile input:** invalid env values (0/negative/non-numeric/bad bool) raise ValidationError at load —
  pinned by 9 tests. `src/paa/config.py` stage: nothing new executes on environment beyond pydantic parsing.
- **Operability:** every flag is independently flippable by env var (`PAA_<SUB>__<FIELD>`) with
  `reset_settings_cache()` picking it up (pinned); rollback = flag flip, nothing migrated.
- **Reversal:** reverting any default breaks its per-flag test; deleting/adding an edge breaks T13; restoring
  a second `_touch_recalled` def is what ADR-0039's structural test (P-004's) guards.

## Status: DONE (code + ADRs + tests committed separately from this report; see Git State)

## Git State
- Branch: `ops/20261107-integration-fix` [RAN git branch --show-current].
- Code commit: `d1278dd` (11 files: src/paa/config.py, docs/adr/0036-0043 + README.md, tests/test_config_settings.py).
- Report commit: separate docs-only commit below listing `d1278dd`.
- Working tree after my commits: sibling files (runtime.py/cli/main.py/context_agents.py/creator.py/COMMS.md)
  still modified — not mine, not committed by me. COMMS.md appends are ORCH-committed per standing order.

## Verification (gate-report shape)
- Unit: [RAN pytest tests/test_config_settings.py -q -> 31 passed].
- Integration-adjacent: [RAN pytest tests/storage/test_schema_parity.py -> green (in 37-count run)].
- Typecheck: [RAN mypy src/paa/config.py -> Success, 0 errors]. Lint: [RAN ruff scoped -> All checks passed!].
- Docs lock: 2/3 tests pass; blank-line test fails with pre-existing + shift-caused citations only (see FINDING).
- Full suite NOT RUN (standalone orders §16).
- New failures introduced by me: the 9 config.py doc citations now landing on blank lines (decomposition
  documented above). Pre-existing failures unchanged: w6-w7-w9 blank citations (mission category C).
