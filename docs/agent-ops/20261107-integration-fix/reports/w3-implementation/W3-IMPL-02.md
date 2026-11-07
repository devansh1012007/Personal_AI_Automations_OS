<!-- ORCH-DIGEST:START -->
agent: W3-IMPL-02 | domain: P-010 dual vault (T-010) | status: DONE
items: enum 4->15 (+11 RFC-only, additive); governance vault/governance/ identity.md+strategy.md via closed-set governance_path; fence conflict VaultConflictError(named path, ValueError subclass); reflection delegates to WorldModel, duplicate writer deleted (grep 0); source written into header; sync_enabled=False gate (VaultSyncSettings consume-only); T11 green: 35 new tests + fail-before shown
code_commit: dd7c3d6 | report_commit: (see code_commit; docs-only commit lists it)
proposed_tasks: 0 (this is the implementation)
top_risks: (1) fence/record crash window: doc committed before write-record -> next write raises false conflict (recoverable: delete record = re-adopt, documented in code); (2) anti_goals FINDING: runtime line-reader ingests fence markers once anti_goals.md is governed (pinned as test, runtime.py outside write-set); (3) my line shifts break docs-lock citations (follow-up docs task, precedent W1/W2-FIX-DOCS)
cross_domain_conflicts: none (world_model.py + reflection.py sole-owner this wave per brief; W3-IMPL-01 committed cda40f5 in parallel from same branch, untouched by me)
out_of_plan_discoveries: ADR filename discrepancy (brief says 0038-dual-vault-and-governance-sync.md; actual 0038-dual-vault-and-fence-precedence.md); anti_goals line-reader FINDING (below)
confidence: high (all acceptance criteria verified with commands below)
last_comms_line_read: 2026-11-07T10:55:00Z W3-IMPL-04 -> ORCH | DONE | T-004b-P004b (COMMS.md tail)
<!-- ORCH-DIGEST:END -->

# Implementation report — W3-IMPL-02 — P-010 dual vault (T-010-P010)

## Status: DONE

## 0. Scope and method
Task: implement plan P-010 per brief `docs/agent-ops/20261107-integration-fix/briefs/W3-IMPL-02.md`.
WRITE-SET only: `src/paa/memory/world_model.py`, `src/paa/improvement/reflection.py`,
`tests/memory/test_dual_vault_p010.py` (new). No edits to `config.py`/`runtime.py`/`cli/main.py`
[VERIFIED git diff --name-only HEAD for those three paths -> empty, before commit].

Required reading completed in brief order: STANDING-ORDERS, AGENTS.md, 00-mission.md,
plan rev5 `C:\Users\devan\.opencode\plan\2026-11-07-integration-fix.md` (P10 row, T11, §K),
ADR `docs/adr/0038-dual-vault-and-fence-precedence.md` (FULL), W0-INV-01 §P-010,
W1-IMPL-01 (VaultSyncSettings declared, config CONSUME-only),
code at HEAD (`world_model.py` full, `reflection.py` full, `cli/main.py` world-model,
`runtime.py._load_anti_goals`, `config.py` VaultSyncSettings), existing tests
(`tests/memory/test_world_model.py`, `tests/improvement/test_improvement.py`, `tests/cli/test_memory.py`).

Branch verified: `ops/20261107-integration-fix` [RAN git branch --show-current ->
ops/20261107-integration-fix]. HEAD at start: `7df6859` (after W3-IMPL-04).
COMMS read at start; my START+CLAIM lines posted 2026-11-07T10:40:37Z.

RESUME note: a prior instance of this agent wrote the code (uncommitted) and a skeleton
report before the server restart. On resume I inspected the full diff against the brief +
ADR-0038, kept the edits (they match the brief), completed verification, and committed.
No code was rewritten from scratch; all test/lint/commit steps below were executed by me
on this instance.

## 1. What was implemented (world_model.py)
- `BeliefDocument` 4 -> 15: added the 11 RFC-only members (constitution, principles,
  goals, anti_goals, projects, skills, playbooks, reflections, risk_rules,
  agent_hierarchy, prompt_contracts); zero removals.
  [RAN python -c enum dump -> len 15, all 15 values incl. original four].
  CLI `BeliefDocument(doc)` coercion (`cli/main.py:708` [VERIFIED]) now accepts the
  11 new names — additive widening, no CLI edit needed.
- `GOVERNANCE_FILES = frozenset({"identity.md", "strategy.md"})` + `WorldModel.governance_path()`:
  resolves under `vault/governance/`, closed-set membership test rejects unknown names
  and traversal (`../identity.md`, separators) with ValueError. Governance files are
  NOT enum members (per brief: do not invent RFC membership). Managed 15 stay flat
  under vault root; `path_for` unchanged (flat join), all existing callers unaffected.
- Conflict semantics (the core, ADR-0038 "read-compare against the last atomic write"):
  - Derived write record `.paa-managed-hashes.json` in vault root, keyed
    `filename:section`, SHA-256 of the block body the last atomic write placed in the
    fence. Hidden dotfile + JSON (never `.md`), so vault .md listings are undisturbed.
  - `_check_conflict` runs in front of the unconditional `_replace_block` (single choke
    point, called from `update_belief_state`): on-disk block body hash != record ->
    `VaultConflictError(path, section, detail)` naming the path, files NOT overwritten.
    `VaultConflictError` subclasses ValueError so existing error paths keep working.
  - No record (pre-tracking file / lost record) -> adopt-on-first-write (one-time
    baseline), conflicts detected from then on. Corrupt/wrong-shaped record degrades to
    "no record" with a warning log, never bricks the writer [test_corrupt_write_record...].
  - DECISION (crash ordering): record is written AFTER the document commits
    (data first, bookkeeping second). A crash between the two yields a *false conflict*
    on the next write — loud and recoverable (delete the record to re-adopt) — never a
    lost human edit. The alternative (record first) would silently overwrite the very
    edit the fence exists to protect. Documented in `_record_hash`/`update_belief_state`.
  - Edits OUTSIDE the block: merge-preserve as before (only the block body is hashed
    and replaced). [test_edit_outside_block_merges_and_preserves].
- Marker hygiene `_check_marker_hygiene`: exactly 0 or 1 BEGIN/END pair per section;
  duplicate/pasted markers or a dangling marker raise VaultConflictError (ambiguous
  surgery refused). Managed `content` carrying fence markers is rejected at the
  boundary (SECURITY comment: prevents smuggling markers via untrusted payload).
- `_new_document(..., source)`: header now reads
  "Managed block below is maintained by the runtime (source: {source})."
- `WorldModel(vault_path, *, sync_enabled: bool | None = None)`: consume-only gate for
  `VaultSyncSettings.enabled`. `False` -> `update_belief_state` is a silent no-op
  (files and record untouched). `None` (default) preserves existing caller behavior
  (reflection playbook writes are internal learning writes, not vault sync — a future
  sync loop wires `settings.vault_sync.enabled` in). DECISION, recorded in the
  docstring; config.py untouched.
- Atomic write unchanged (temp + `os.replace`); temp cleanup on failure added
  (best-effort unlink, original error re-raised).
- Vault seed: ADR-0038 is silent on a seed mechanism for the 11 new files. DECISION:
  no seeding on construction (a fresh vault stays empty — seeding `anti_goals.md`
  headers would feed header prose to the runtime line reader, see FINDING-2).
  Governance files are operator-placed; WorldModel resolves but never creates them.
  New managed files materialise on first `update_belief_state` / sync-loop write.
  The dead-writer wiring decision (ADR: a sync loop will call `update_belief_state`)
  is NOT implemented here — no daemon loop invented, per the brief's dead-writer trap.
- Trace tag `PLAN:integration-fix:P010` present 8x across the two source files
  [RAN Select-String count -> 8].

## 2. What was implemented (reflection.py)
- `ReflectionEngine.apply_to_playbook` delegates to
  `WorldModel.update_belief_state(BeliefDocument.PLAYBOOKS, combined, section="reflections",
  source="reflection")`. Reads current block via public `read_managed` (no private
  `_extract_block` use). Signatures of `apply_to_playbook`/`run_weekly` unchanged
  (P-013 W4 precondition preserved).
- Duplicate `_write_managed_block` module function DELETED.
  [RAN grep `def _write_managed_block` over src/ -> 0 matches;
   grep `_write_managed_block` over src/ -> 0 matches]. Single-writer proven.
- `filename` parameter now closed-set validated: only `"playbooks.md"` accepted, else
  ValueError. This tightens the old behavior (arbitrary filename joined onto the
  vault) — the only production caller is `run_weekly` (no filename arg)
  [VERIFIED grep apply_to_playbook callers -> only reflection.py:182 + tests],
  so no caller breaks; the tightening is deliberate (closed set, consistent with the
  CLI coercion) and pinned by `test_apply_to_playbook_rejects_non_playbook_filename`.

## 3. Tests (tests/memory/test_dual_vault_p010.py, 35 tests)
Enum set (6): len==15; exact 15-file union; original four preserved; CLI coercion of
new names; rejection of unknown/traversal/empty; governance not enum members.
Governance path (3): subdir resolution; unknown/traversal rejection; managed flat.
Materialization (3): all 15 written via update_belief_state; governance operator-placed
and readable; fresh WorldModel seeds nothing.
Fence conflict (4): inside-edit -> VaultConflictError naming path + no overwrite;
outside-edit merges/preserves; adopt-then-detect upgrade path; corrupt record rebuilt.
Idempotency/crash (2): double write byte-identical (file + record); os.replace crash
-> old bytes intact, no `.*.tmp` residue, next write proceeds (no false conflict).
Marker injection (4): duplicate BEGIN rejected; BEGIN-without-END rejected; content
carrying markers rejected (no file created); marker spelling pinned once (exactly one
BEGIN/END pair in file).
Source header (2): explicit source + default `memory_curator` in header.
Sync gate (3): disabled -> nothing written; disabled preserves existing bytes; enabled
writes normally.
Single writer (2): src-wide scan for `def _write_managed_block` == []; reflection.py
contains no `_write_managed_block`.
Reflection delegation (4): writes through WorldModel accumulating; human text
preserved; inherits conflict semantics (ValueError naming playbooks.md); non-playbook
filename rejected.
Anti-goals agreement (2): human-only file agrees with line reader; managed-block
injection pinned as FINDING (below).

## 4. FINDINGs (documented, not fixed — outside write-set)
- FINDING-1 (ADR filename): the brief names the ADR
  `0038-dual-vault-and-governance-sync.md`; the file on disk is
  `0038-dual-vault-and-fence-precedence.md`. Content matches the brief's description;
  proceeded with the on-disk ADR. ADR not edited (forbidden).
- FINDING-2 (anti_goals fence-vs-line-reader): `runtime.py:607-621 _load_anti_goals`
  is a fence-unaware line reader — it skips blanks and `#` lines but ingests
  `<!-- paa:managed:BEGIN/END -->` markers (and the managed body) as anti-goals once
  `anti_goals.md` is governed. Human-only files agree with the reader
  [test_human_only_file_agrees_with_line_reader]; a governed block pollutes the list
  [test_managed_block_injection_pinned_as_finding asserts the marker IS in goals].
  ADR-0038 leaves "fence-aware reading or an explicit note" to the implementer;
  runtime.py is outside my write-set, so I pin the behavior as a test and report it.
  Owner follow-up: make `_load_anti_goals` fence-aware (strip the managed block
  before line-reading) or gate governing anti_goals on that fix. Same pattern likely
  applies to any other direct vault readers of newly-governed files.
- FINDING-3 (docs lock): my line shifts move citations in ADR-0038 and
  `docs/modules/memory.md` `path:line` references. The docs-references lock will fail
  until a docs task re-points them (precedent: W1/W2-FIX-DOCS). Not mine to fix
  (write-set); flagged for the follow-up docs task.

## 5. Verification evidence (all [RAN <cmd> -> <result>] by me on this instance)
- `.venv\Scripts\python.exe -m pytest tests/memory/test_dual_vault_p010.py -q`
  -> 35 passed.
- `.venv\Scripts\python.exe -m pytest tests/improvement/test_improvement.py
  tests/memory/test_world_model.py tests/memory/test_decay.py tests/cli/test_memory.py -q`
  -> all passed (72 + 42 lines of dots, 0 failures). Reflection/world-model/cli
  suites green unchanged.
- `.venv\Scripts\python.exe -m ruff check src/paa/memory/world_model.py
  src/paa/improvement/reflection.py tests/memory/test_dual_vault_p010.py`
  -> All checks passed!
- `.venv\Scripts\python.exe -m mypy src/paa/memory/world_model.py
  src/paa/improvement/reflection.py` -> Success: no issues in 2 files.
- `.venv\Scripts\python.exe -m mypy src` -> Found 3 errors, all pre-existing in
  `src/paa/sandbox/subprocess_backend.py:548` (killpg/getpgid/SIGKILL attr-defined,
  Windows platform) — zero in my files. Matches the brief's "mypy total 3".
- Single-writer: grep `def _write_managed_block` and `_write_managed_block` over
  `src/**/*.py` -> 0 matches (both).
- Fail-before: `git worktree add --detach <tmp> HEAD` + copied test file +
  `PYTHONPATH=<tmp>/src pytest test_dual_vault_p010.py` -> collection ERROR:
  `ImportError: cannot import name 'VaultConflictError' from
  'paa.memory.world_model'` — the new surface does not exist at HEAD, so the suite
  fails before (as required), then passes after. Worktree removed afterwards
  [RAN git worktree remove -> list shows only the main tree].
- No forbidden files touched: `git diff --name-only HEAD -- src/paa/config.py
  src/paa/runtime.py src/paa/cli/main.py` -> empty (pre-commit).
- No vault files written to the repo or `~/.paa/vault`: all tests use `tmp_path`;
  `git status` shows no `.md` vault files under the repo.

## 6. Long-run test (Source / Load / Hostile / Operability / Reversal)
- Source: conflict authority is the derived write record (SHA-256 of last-written
  block body) — not mtime (mtime changes on any outside edit and cannot distinguish
  inside/outside), not filesystem metadata (not portable), not ledger events (vault is
  filesystem state, no EventType per AGENTS.md). Record lives next to the data it
  describes, is rebuildable, and degrades to adopt-on-next-write on corruption —
  the standard "derived view" posture of this repo.
- Load: per-write cost is O(file size) read + hash compare + one extra small atomic
  write (the record). 100x sequential writes are idempotent (byte-identical from
  write 2 on, test-pinned). Concurrency: two writers share one record via atomic
  replace — last-writer on the record; the conflict check is a guard, not a lock
  (documented in `_record_hash`). The vault is single-node filesystem state; a real
  lock would be a new subsystem, out of scope and rejected.
- Hostile: marker injection in file (duplicate/dangling fences) and in managed
  content both rejected before any write; traversal rejected in governance_path,
  CLI coercion (closed enum), and apply_to_playbook (closed filename); corrupt
  record cannot brick the writer. ReDoS: block regex is non-greedy DOTALL over a
  single small document, no nested quantifiers.
- Operability: `sync_enabled=False` (VaultSyncSettings.enabled) is the kill switch —
  files stay byte-identical [test]. Rollback: removing the 11 enum members restores
  the four-file CLI exactly (nothing reads the new files yet, ADR-0038). False
  conflicts (crash in the record window) are recoverable by deleting the record —
  documented in code. Observability: `world_model.fence_conflict` /
  `hashes_unreadable` / `sync_disabled` log lines via structlog; exceptions carry
  path + section.
- Reversal: reverting world_model.py makes the whole new test file fail at import
  (VaultConflictError/governance_path/sync_enabled absent) and
  `len(BeliefDocument)==15` fail; reverting reflection.py alone fails
  `test_no_duplicate_fence_writer_in_src` + the 4 delegation tests. Existing
  world-model/reflection/cli suites would still pass on revert — the new file is
  the sole guard, which is why it pins markers, paths, and the single-writer grep.

## 7. Unverified assumptions & remaining risks
- [ASSUMED] Sequential single-process vault access (no concurrent multi-writer stress
  test run; the record is last-writer-wins by design). No concurrency test beyond
  the crash-mid-write one.
- NOT-RUN: full pytest suite (out of scope per brief §7) and the docs-references
  lock (expected to fail on my line shifts until the docs follow-up — FINDING-3).
  The `requires_postgres`/`requires_s3` suites are unaffected by these files.
- Residual: the first tracked write to a pre-existing hand-written block is an
  adopt (no conflict even if the human just edited inside) — the one-time cost of
  turning tracking on, inherent to any read-compare design, surfaced in tests.
- The ADR's dead-writer wiring (sync loop calling update_belief_state) is future
  work; until then the 11 new files are written only on demand (reflection
  playbooks + any WorldModel caller). This is ADR-by-design, recorded per the
  brief's dead-writer trap.

## 8. Git state
Branch `ops/20261107-integration-fix` [VERIFIED]. Code commit `dd7c3d6`
(3 files, explicit paths). Other agents' uncommitted work in the tree
(api/tui/facts/W3-IMPL-01 report) was never touched, staged, or stashed by me.
Report committed separately (docs-only), listing `dd7c3d6` above.
