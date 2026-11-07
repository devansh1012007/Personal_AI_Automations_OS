<!-- ORCH-DIGEST:START -->
agent: W2-FIX-DOCS | task: W2-FIX-DOCS (R-W2-001) | status: COMPLETE
broken_at_start: 42 (lock blank-line failures; brief estimated ~44 — RUN list governed) | broken_at_end: 0
lock: pytest tests/conventions/test_docs_references.py -q -> 5 passed (EXIT=0)
docs_commit: 8d39e5c (34 docs files, 138 insertions/deletions) | report_commit: (this commit, next)
last_comms_line_read: 2026-11-07T09:49:27Z W2-FIX-01 -> ORCH DONE (line 80; FIX-01 landed f009484 mid-task — replay mappings finalised AFTER its landing)
<!-- ORCH-DIGEST:END -->

# W2-FIX-DOCS report

## Context
- Branch verified `ops/20261107-integration-fix` before committing [VERIFIED git branch --show-current].
- [RAN .venv/Scripts/python -m pytest tests/conventions/test_docs_references.py -q -> 1 failed, 42 broken citations at start; final re-run -> 5 passed, EXIT=0.]
- Root cause: wave-2 mid-file insertions into heavily-cited files. (a) Touch unification W2-IMPL-01 added +1 imports to `api/app.py`, `cli/main.py`, `coder/runtime_tools.py`, `agents/context_agents.py` (one-line shift across all of each file) and deleted trailing `_touch_recalled` defs. (b) Tier layers W2-IMPL-02/03 + W2-FIX-01 inserted ~90 lines into `ledger/replay.py` (tier fold + `TierPolicy` import) and +1 into `memory/domains.py` (`__all__` entry, TierPolicy appended). Every broken citation was re-pointed by reading its citing sentence and locating the current line holding the claim (symbol def, never decorator, where the sentence names the symbol; same-content line for comment/body/range anchors) — never blind +N. Where the sentence named a symbol and the old anchor was a body/decorator line in the *wrong* function (or decorator where the def is the claim), the anchor was moved to the correct def (the W1 REV R-W1-001 "def, not decorator" rule). Ranges were re-ended on real content (Plan1 ranges, MCP range).
- Sequencing: W2-FIX-01 landed mid-task (code f009484, report 0546308, COMMS line 80) adding a second replay.py shift (+1 import, +docstring/logic). Replay citations were therefore finalised AFTER its commit against the committed tree (`git log --oneline -3` check before the final lock run; FIX-01's own report notes "docs lock 44->48 for DOCS residual (replay shifts)"). Final lock re-run is green, so no residual from the sibling remain. No src/** or tests/** file was edited by this agent; `git diff --stat` is docs-only (34 docs files; COMMS.md/01-work-ledger.md modified in working tree by others/orchestrator but deliberately NOT committed).
- Scratch files (`w2-*.txt`, `w2*.py`) deleted before commit; none present at commit.

## Citation mapping (old → new, one-line evidence each)
Evidence = current target line content [VERIFIED by grep/read at final line numbers after FIX-01 f009484 landed]. Base = wave-2 base 32b3138 (`git show 32b3138:<path>`), compared per-citation (CUR vs BASE) to distinguish wave-caused from pre-existing stale.

### cli/main.py — command defs (BLANK class + lock-blind decorator class: sentence names the command, anchor moves to `def`, never `@app.command()`)
- docs/README.md:46-50, repository-map.md:118-123/125, backend/overview.md:13/15/18/19, request-lifecycle.md:11, workflow-01-submit-and-run.md:10, workflow-03-crash-recovery.md:11, documentation-audit.md:59 — `:76→:77` [def submit], `:292→:293` [def serve], `:227→:228` [def doctor], `:209→:210` [def recover], `:147→:148` [def ledger], `:121→:122` [def status], `:182→:183` [def gate], `:355→:356` [def tui]. All VERIFIED as `def <name>(` at new line; old line is now `@app.command()` (decorator).
- backend/overview.md:14, request-lifecycle.md:12 — `:89→:106` [def run; was body line inside submit — pre-existing wrong-without-broken, same class as W1 FINDING, fixed by claim].
- backend/overview.md:16 — `:134→:148` [def ledger]; backend/overview.md:17, workflow-02-human-gate.md:50 — `:164→:183` [def gate]; backend/overview.md:20 — `:248→:293` [def serve]; backend/overview.md:22 — `:307→:356` [def tui]. (All were body lines inside previous commands at base — pre-existing wrong-without-broken, fixed by claim; the W1 FINDING rows are now resolved for these files.)
- repository-map.md:117, cli.md:10 — `cli/main.py:38→:42` [sentence names `app`; `app = typer.Typer(` at 42; base 38 was an import].
- backend/overview.md:54, cli.md:36, deployment-and-runtime.md:46 — `:54→:55` [async def _build_runtime].
- cli.md:51 — `:95→:99`, `:111→:115` [the `finally:` lines of submit/run; sentence names `finally`].
- cli.md:55 — `:372→:377` [raise typer.Exit(1) in correlation_id_uuid; sentence names typer.Exit]; cli.md:58 — `:343→:348` [Exit after "unknown mode" in mode()].
- backend/overview.md:56, deployment-and-runtime.md:47 — `:254→:293` [def serve; sentence: "paa serve builds one Runtime, starts the Daemon and a FastAPI app"].
- integrations-and-communication.md:118 — `:266→:316` [config = uvicorn.Config(; sentence names uvicorn].
- daemon.md:69 — `:310→:311` [rt = await _build_runtime(run_recovery=False) inside serve].
- Plan1.md:79 — `cli/main.py:69-99→:70-100` [range: version decorator 70 through submit's `await rt.close()` 100].
- ADR-0036:115 — `:635→:636` [_MAX_DRAIN_ERROR_CHARS = 200].
- w6-w7-w9.md:237 — `:208→:209` [same-content decorator; the sentence quotes the cite as an example].

### api/app.py — handler defs (BLANK class + lock-blind off-by-one-endpoint class: every Handler anchor pointed inside the previous endpoint's body at base; fixed to the correct handler `def`)
- component-interactions.md:229, repository-map.md:127, api-reference.md:9 — `:53→:54` [def create_app].
- api-reference.md:25 (GET /health), deployment-and-runtime.md:145 — `:68→:71` [async def health].
- api-reference.md:43, request-lifecycle.md:13-14, workflow-01-submit-and-run.md:11 — `:85→:94` [async def submit_task].
- api-reference.md:72 — `:104→:114` [async def task_status]; api-reference.md:94, workflow-02-human-gate.md:51 — `:126→:136` [async def clear_gate].
- api-reference.md:104 — `:139→:149` [async def record_correction]; api-reference.md:110 — `:147→:156` [async def ledger]; ledger.md:131 — `:149→:156` [async def ledger; sentence: "API /ledger/{id}"].
- api-reference.md:122, persistence-and-storage.md:82, documentation-audit.md:62 — `:165→:175` [async def ingest_signal].
- api-reference.md:134 — `:315→:316` [def _signal_repo]; api-reference.md:159 — `:337→:338` [def _parse_uuid].
- api-reference.md:160 — `:109→:114` [task_status def], `:150→:156` [ledger def]; :161 — `:135→:136` [clear_gate def].
- api-reference.md:58, :162 — `:87→:295`, `:168→:295` [async def _admit; sentence names _admit; base anchors were Health `},` / ledger `)`]. The duplicate continuation cite was collapsed to a single `api/app.py:295`.
- api-reference.md:163, Plan1.md:93 — `:172→:175` [ingest_signal def].
- persistence-and-storage.md:72 — `:179→:180` [if repo is None:; same-content]; persistence-and-storage.md:136, api-reference.md:167 — `:199→:200` [# -- memory recall section comment; same-content].
- deployment-and-runtime.md:103 — `:272→:274` [async def drain_memory; sentence names POST /memory/drain].
- w6-w7-w9.md:74 — `:43→:44` [if TYPE_CHECKING:; sentence names it (fastapi import next line)].
- Plan1.md:64 — `src/paa/api/app.py:172-196→:173-197` [section comment through `duplicate=pre_existing is not None,`]; :80 — `api/app.py:91-170→:92-171` [task-submission comment through `]`].

### context_agents.py — handle/method defs (BLANK class) + same-content body/comment anchors (lock-blind)
- agents.md:114 (trio), storage.md:75 — `:143→:144` [async def _hot_facts], `:243→:244` [async def _semantic], `:340→:341` [async def _graph_neighbours].
- request-lifecycle.md:33, data-lifecycle.md:142, workflow-01-submit-and-run.md:47 — `:376→:377` [Planner handle def]; data-lifecycle.md:168 — `:427→:428` [Worker handle def].
- memory.md:142 — `:189→:190` [predicate = like_predicate(...)].
- component-interactions.md:94 — `:295→:296` [initial = float(row["initial_confidence"]); same-content body anchor inside shared builder path].
- modules-and-services.md:176, context.md:78 — `:122→:123` [super().__init__(**kwargs); Expander-fallback anchor inside __init__].
- data-lifecycle.md:171, agents.md:92, modules-and-services.md:177 — `:303→:446` [if packet.vacant_slots: — the hydration_required failure in Worker handle; base 303 was `)` inside _semantic].
- data-lifecycle.md:120 — `:223→:224` [confidence = effective_confidence(]; persistence-and-storage.md:103 — `:182→:183`, :158 — `:183→:184` [LIKE-escape comment block; same-content]; :113 — `:205→:206` [tiebreaker comment; same-content]; :159 — `:214→:215` [ORDER BY line]; memory.md:149 — `:221→:222` [for row in rows:]; context.md:61/62 — `:309→:310`, `:296→:297` [importance/domain lines; same-content].
- UNRESOLVED (left as-is, lock-ok): agents.md:90 `:236`/`:271` (Planner/Worker class rows point inside method bodies at base and now; no single def line names both — ambiguous, see FINDINGs); workflow-01-submit-and-run.md:55 `:379` (continuation resolves to context_agents.py; the gate-parking claim lives in the orchestrator — mis-resolved pre-existing).

### memory/domains.py — symbol defs (+1 shift from `__all__`)
- architecture/overview.md:203 — `:78→:79` [WORKING]; memory.md:26 — `:75→:76` [class MemoryDomain]; memory.md:28 — `:43→:44` [VOLATILE]; memory.md:29 — `:99→:100` [class DomainPolicy — def, not the @dataclass decorator at 99], `:137→:138` [DOMAINS dict] x2 (memory.md:29 and :168), `:250→:251` [def domain_policy] x2 (memory.md:29 and :163); memory.md:30 — `:260→:261` [def narrative_domains].

### coder/runtime_tools.py — catalogue + tool classes (+1)
- coder.md:76 — `:47→:85` [def build_runtime_tools; sentence lists the catalogue — base 47 was an unrelated import line, pre-existing wrong-without-broken, fixed by claim].
- coder.md:102 — `:222→:223` [class _TaskStatus], `:269→:270` [class _SubmitTask].
- MCP_AND_PLUGINS.md:83 — `runtime_tools.py:84-106→:85-107` [def build_runtime_tools through catalogue `}`; range stays a range of real content].

### ledger/replay.py — FINALISED AFTER W2-FIX-01 f009484 landed (committed tree; +1 TierPolicy import + tier-fold body)
- modules-and-services.md:76, ledger.md:82 — `:170→:171` [def apply_event].
- data-lifecycle.md:40, workflow-01-submit-and-run.md:37, workflow-03-crash-recovery.md:39, ledger.md:91, documentation-audit.md:63 — `:322→:412` [async def project; sentence names project()].
- data-lifecycle.md:54 — `:266→:267` [case EventType.STATE_ROLLBACK_TRIGGERED:]; workflow-02-human-gate.md:55 — `:261→:262` [case EventType.HUMAN_GATE_CLEARED:].
- data-models-and-relationships.md:185, ledger.md:79 — `:88→:89` [class TaskProjection]; data-models-and-relationships.md:185 (:34→:35), glossary.md:53, background-jobs.md:97, ledger.md:80 — `:34→:35` [class TaskPhase].
- data-models-and-relationships.md:92, persistence-and-storage.md:176 — `:92→:93` [as a replay optimisation and are safe to delete.; same-content docstring line].
- glossary.md:65, ledger.md:83 — `:149→:150` [_PHASE_MAP def].
- workflow-02-human-gate.md:61 — `:163→:164` [EventType.HUMAN_GATE_REJECTED: TaskPhase.BLOCKED,; same-content _PHASE_MAP member].
- ADR-0041:31/:65 — `:166→:167` [EventType.STATE_ROLLBACK_TRIGGERED: TaskPhase.ROLLED_BACK,; same-content].
- background-jobs.md:98 — `:219→:220` [updates["attempts"] = state.attempts + 1; same-content]; workflow-01-submit-and-run.md:111, ledger.md:88 — `:239→:240` [if len(done) < len(state.plan_steps):; same-content].
- ADR-0036:77, ADR-0037:25 — `:291→:379` [_NO_PHASE_CHANGE def; W1 had pinned 291 as correct].
- ledger.md:130 — `:347→:437` [events = await store.read_correlation(; same-content inside project].
- replay.py:62 (is_resumable cites: request-lifecycle.md:67, workflow-02-human-gate.md:26/47, security:152) — UNCHANGED: current line 62 is `def is_resumable(self) -> bool:` (the cited docstring line at base is the def now; anchor holds the claim).
- UNRESOLVABLE (left as-is, lock-ok, NEED for orchestrator): workflow-01-submit-and-run.md:38 `:295` (continuation carried from replay.py:412; "If state.is_terminal, return immediately" — replay.py stores no such early-return at :295; the check lives in the orchestrator's run()) and workflow-01:40 `:327` (continuation "(_context_for, :327)" — no _context_for in replay.py; also orchestrator-resident). Converting these to full `orchestrator.py:NNN` citations needs locating exact orchestrator lines and changes doc resolution — outside "citation lines only" repair; recorded as residual semantic drift, not blank-lock failures.

## ADR scan (W1-method semantic read of ADR cites into the 6 files)
- ADR-0036:77 replay:291→:379 ✓ (_NO_PHASE_CHANGE); :115 cli:635→:636 ✓ (_MAX_DRAIN_ERROR_CHARS); creator:1497 verified correct (W1), unchanged.
- ADR-0037:25 replay:291→:379 ✓.
- ADR-0038/0041/0042/0043 (W1 scan): replay:166→167 ✓ (ROLLED_BACK); 0041 replay:166 both →167 ✓; other ADR cites into the 6 files found none stale besides the above. (0038 world_model, 0041 core/types, 0042 compose/pyproject, 0043 graph — untouched by wave-2, re-verified non-blank by the lock.)

## Verification
- [RAN .venv/Scripts/python -m pytest tests/conventions/test_docs_references.py -q -> 5 passed (EXIT=0)] at end, after FIX-01 f009484 landed and against the committed tree. Zero blank citations into the 6 files (audit script: 161 refs into 6 files, 0 BLANK).
- Self-review: re-read the full git diff (34 docs files). All changes are citation numbers on existing lines (plus collapsing one duplicate `_admit` continuation to a single cite); no prose rewrites; no src/**, tests/**, agent-ops/** or plans/** edits (memory-polish/plans/Plan1.md is in scope — the exclusion is only docs/plans/**; "docs/plans and audits/" is outside the lock's excluded directories and is editable).
- Mutation mental frame: reverting any single re-point to its old number re-blanks the lock where the old line is blank (api/cli/ca/rt), or re-ages the anchor (def cites revert to decorator/comment/docstring lines). The fix-by-claim def moves (api Handlers, cli body rows) do not fail the lock if reverted (old lines are non-blank) — their correctness rests on the endpoint→handler-def readings documented above.

## Residual / blockers
- **Lock residual: none (5 passed).** UNRESOLVABLE semantic items (lock-ok, 3): workflow-01-submit-and-run.md `:295`/`:327` (mis-resolved continuations to replay.py; claims live in orchestrator.py) and `context_agents.py:379` (same). These need an orchestrator decision to re-target to orchestrator.py lines; left untouched per "UNRESOLVABLE + NEED, not an invented line."
- No NEED posted to COMMS for this (recorded here; orchestrator reads reports). No unresolvable blank citation.

## Out-of-scope findings (not fixed; for orchestrator)
- FINDING (pre-existing, wrong-without-broken, lock-ok): docs/modules/agents.md:90 `:236`/`:271` point inside method bodies for the Planner/Worker class rows (ambiguous two-class anchor; no single line names both). Left untouched.
- FINDING (pre-existing, lock-ok, outside the 6 files): docs/modules/cli.md:56 `:180` resolves to `cli/coder.py:180` (section_path) for "Task not awaiting gate" (the gate handling is in cli/main.py's gate command region) and :48 `:64` resolves to `cli/coder.py:64` for submit's `run=True` default. Mis-resolution by heading scope; separate sweep.
- FINDING (wave +1 shift, lock-blind, same class as W1): citations into the 4 touch files that point at *body* lines (not defs) remain off-by-one where the preceding line is non-blank (e.g. persistence `api/app.py:8`/`api/app.py:11` are pre-shift and exact — those two are fine; other body anchors shifted with content and were same-content repaired). Only def-anchored and blank cites were re-pointed; the remaining non-blank body anchors were content-verified same-line (hash-line diff audit) rather than exhaustively re-read.
- FINDING (bare-number sweep, lock-invisible): docs/modules/cli.md command table bare numbers (mode 336, tui 352) and repository-map.md "__main__ at line 467" are stale by the wave +1 shift (lock does not see bare numbers; W1 noted the same class).

## Long-run test answers
- Source: every new citation was verified against its target line's actual content (grep/read evidence above), not arithmetic; ADR scan included.
- Load/Hostile/Operability: docs-only change; no runtime behavior, no new failure modes. Reversal: reverting a re-point re-breaks the lock (except the claim-based def moves, reasoned above).
- What was NOT verified: full test suite (standing order 16, scoped check only); non-blank citations into files other than the 6 wave-2 files (assumed stable — the wave-2 diff proves they were not edited).
