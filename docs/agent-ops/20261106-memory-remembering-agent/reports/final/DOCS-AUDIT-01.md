<!-- ORCH-DIGEST:START -->
agent: DOCS-AUDIT-01 | domain: docs-tree cleanup audit | status: DONE
items: 282 files classified (a-live 66 / b-run-trail 213 / c-dated-historical 3 / d-useless-generated 0)
proposed_tasks: 0 (SURFACE 0, DEEP 0)
top_risks: (1) no .gitignore change justified — enabling ignores for never-observed patterns would be speculative; (2) 2 untracked substantive docs at docs/ root need operator track/ignore decision, not an auditor delete; (3) editor/.tmp patterns unignored but with zero occurrences in docs/
cross_domain_conflicts: none (no files shared with FINAL-IMPR-01; read-only on its 4 docs)
out_of_plan_discoveries: 2 (untracked root docs; .tmp not covered by .gitignore — both documented, neither actioned)
confidence: high (full enumeration via glob + git ls-files; zero-junk verdict is a counted result, not a sample)
last_comms_line_read: [2026-11-06T21:14:26Z] ORCH -> ALL | DECISION | WAVE-END | FACT Wave 4 closed: delta APPROVE, gate-w4-2 green; W4-IMPL-07 BLOCKED superseded by W4-IMPL-08 (COMMS.md:59, end of file)
<!-- ORCH-DIGEST:END -->

# Report — DOCS-AUDIT-01 — docs tree cleanup audit

## 0. Scope and method

**Deliverable.** Audit the `/docs` tree (all subfolders) for useless/generated items and tidy `.gitignore` so generated junk is not committed, without touching files owned by the parallel FINAL-IMPR-01 agent. Verdict on `.gitignore` and on deletions is evidence-gated by the brief (§3): additions only for patterns *already present/observed in /docs* with paths + counts; tracked useless files are listed as DELETE-CANDIDATES for the operator, never deleted/untracked by this agent.

**Write-set honored.** Only `.gitignore` (read, not modified — see §2 decision) and this report were edited. The parallel agent's four operator docs (`docs/modules/daemon.md`, `docs/modules/memory.md`, `docs/operations/deployment-and-runtime.md`, `docs/deployment/docker.md`) and all other files under `docs/agent-ops/` were read read-only. No docs content or line refs touched, so the conventions lock (`tests/conventions/test_docs_references.py`) cannot shift from this task. No file deleted, no `git rm`, no moves/renames. [VERIFIED] `git branch --show-current` → `ops/20261106-memory-remembering-agent` before any write.

**Method (evidence, not sampling).**
- Enumerated every file under `docs/` with `pathlib.Path('docs').rglob('*')` filtered to files, and cross-checked against `git ls-files docs/` (tracked vs untracked). [RAN `git ls-files docs/` -> 278 tracked; pathlib on disk -> 282 files; diff = 4 untracked (see §1).]
- Classified each of the 282 files into the brief's four buckets (a) live docs, (b) run-trail `docs/agent-ops/`, (c) dated/historical proposals, (d) useless/generated — using path prefix + file inspection for the two ambiguous untracked root docs (head-read only).
- Searched for junk signatures exhaustively: hidden files/dirs, suffixes `.pyc/.tmp/.swp/.bak/.orig/.rej/.log/.pid`, `__pycache__` path parts, zero-byte files, files <50 bytes, exact-duplicate content by SHA-256, and generated-site dirs (`site/_build/build/dist/.jekyll*/mkdocs.yml/docusaurus*`). [RAN python enumeration -> 0 hits on every junk signature; extensions only `.md` (270) + `.tsv` (12, ownership tables).]
- Checked `.gitignore` coverage with `git check-ignore` probes (present vs absent patterns) rather than assuming. Did NOT run pytest/ruff/mypy — out of lane per brief §6; the conventions checker was read, not executed.

**What I did not do.** No full-test runs (brief forbids; no code changed, so no regression surface). No deletion recommendation beyond an explicit 0-item DELETE-CANDIDATE list. No speculative `.gitignore` patterns. No COMMS append (see §4: brief §4 makes all of `docs/agent-ops/` read-only except this report, and COMMS Rule 8 reserves COMMS commits to ORCH).

**COMMS read points.** Read at start through line 59 (WAVE-END, end of file). Re-read before committing/finalizing (see §3). Last line read recorded in ORCH-DIGEST above.

## 1. Classification of all 282 files under /docs

### Summary counts [RAN python enumeration]

| Bucket | Meaning | Count | Conventions-lock status |
|---|---|---:|---|
| (a) live docs | Current architecture/modules/operations docs, ADRs, glossary | 66 | IN SCOPE (`path:line` lock active) |
| (b) run-trail | Everything under `docs/agent-ops/` (3 runs) | 213 | EXCLUDED as a whole directory [VERIFIED tests/conventions/test_docs_references.py:108-114,540] |
| (c) dated/historical | `docs/plans/` ×2 + `docs/memory-polish/plans/Plan1.md` ×1 | 3 | Split — see below |
| (d) useless/generated | pycache, editor artifacts, generated indexes, empties, checkpoint dups | **0** | n/a |

Total on disk 282 (270 `.md` + 12 `.tsv`); tracked 278; untracked 4. No file on disk missing from tracking other than those 4; no tracked file missing from disk. [RAN diff of `git ls-files docs/` vs disk listing]

#### (a) Live docs — 66 files, KEEP

- Root index/planning: `docs/README.md`, `docs/ROADMAP.md`, `docs/VISION.md`, `docs/EXTENSIBILITY_PIPELINES.md`, `docs/MCP_AND_PLUGINS.md`, `docs/flexability-audit.md`, `docs/glossary.md`, `docs/reviews/documentation-audit.md` — the review audit itself cross-references the subsystem coverage and confirms the tree structure; retained as live.
- `docs/adr/` (21 files: README + ADRs 0008,0010,0015,0019–0026,0028–0035) — decision record; AGENTS.md notes ADRs 0001–0007/0009/0011–0014/0016–0018 are index-only. Index-only ADR entries are *declared* gaps, not generated junk; deleting them would break `docs/adr/README.md` links and is explicitly flagged in AGENTS.md as "don't chase". KEEP.
- `docs/architecture/`, `docs/backend/`, `docs/data/`, `docs/development/`, `docs/flows/`, `docs/interfaces/`, `docs/modules/` (incl. the 4 FINAL-IMPR-01 files — read-only here), `docs/operations/`, `docs/runtime/`, `docs/security/` — all cited, in-scope `path:line` documents. The `path:line` lock scopes `docs/` + root `README.md`/`AGENTS.md` [VERIFIED tests/conventions/test_docs_references.py:96-100]; edits to these are owned by FINAL-IMPR-01 this wave. KEEP, no action.
- `docs/deployment/docker.md` — FINAL-IMPR-01-owned this wave (in the §4 never-touch list). KEEP, no action.

#### (b) Run-trail `docs/agent-ops/` — 213 files, KEEP (this run's memory)

Three run directories, all tracked except this run's own in-flight briefs/reports:
- `20261022-w6-w7-w9/` (~107 files: mission, ledger, COMMS, 34 briefs, 5 gates, 3 ownership TSVs, ~40 reports, 12 templates)
- `20261024-memory-polish/` (~35 files: mission, ledger, COMMS, 9 briefs, 5 ownership TSVs, ~11 reports, 4 templates)
- `20261106-memory-remembering-agent/` (~71 files + 2 untracked: this run's briefs/gates/ownership/reports/templates)

All 213 are excluded from the `path:line` conventions lock as a whole directory with a stated reason ("a record of what a run found, allowed to have rotted") [VERIFIED tests/conventions/test_docs_references.py:33-39,108-114,540]. Identical template files across runs (4 SHA-256-identical groups: `report-implementation.md`, `review-code.md`, `review-tests.md`, `STANDING-ORDERS.md` between the 2026-10-22 and 2026-10-24 template dirs) are intentional per-run copies, not duplicated checkpoints; repeated basenames across runs (`W1-IMPL-01.md` ×3, `DELTA-review.md` ×4, etc.) are per-run namespaces, not dupes. KEEP as the run memory; deleting history would erase the audit trail the repo deliberately keeps. No action.

#### (c) Dated/historical proposals — 3 files, KEEP with lock-status note

| File | Tracked | Lock status | Note |
|---|---|---|---|
| `docs/plans/w3-w5.md` | yes | EXCLUDED (whole `docs/plans/` dir, [VERIFIED tests/conventions/test_docs_references.py:41-46,115-120,541]) | dated proposal; keep |
| `docs/plans/w6-w7-w9.md` | yes | EXCLUDED (same) | dated proposal; keep |
| `docs/memory-polish/plans/Plan1.md` | yes | **IN SCOPE** — exclusion matches only the `docs/plans/` prefix, and `docs/memory-polish/plans/` does not match it [VERIFIED tests/conventions/test_docs_references.py:203-208 `_is_excluded` prefix logic] | dated proposal but lock-active; keep; operator note: its citations must stay fresh (a prior wave NEED'd its `creator.py:799-830` citation after a line shift — COMMS records this; not this task's fix) |

Declaring (c) as "keep but note exclusion" per the brief: only the two `docs/plans/` files are lock-excluded; `Plan1.md` is not, and this report corrects the bucket premise for it explicitly rather than mislabeling it as excluded. [VERIFIED path per file above]

#### (d) Useless/generated — 0 files

Exhaustive signature search found nothing: no `__pycache__`/`.pyc` in docs, no `*.tmp/*.swp/*.bak/*.orig/*.rej/*.log/*.pid`, no hidden files, no zero-byte or <50-byte files, no generated docs-site output (`site/`, `_build/`, `build/`, `dist/`, `.jekyll*`, `mkdocs.yml`, `docusaurus*` all absent), no empty placeholder dirs that are files. [RAN python enumeration -> `junk-suffix: []`, `hidden: []`, `0 bytes: []`, site-glob `[]` across all patterns]

**Useless-items table:** empty by counted evidence. There is nothing to ignore and nothing to delete.

**DELETE-CANDIDATES (tracked but useless, for operator approval): 0.** No tracked file meets the (d) definition. The brief forbids deleting/untracking committed files regardless; with zero candidates, no operator approval is requested. [ASSUMED none hidden under an innocent name — mitigation: full extension + size + hash census, not filename-only inspection; a file misnamed as `.md` with real content would still have been counted as (a)/(b)/(c), which is the safe direction for an auditor]

**Untracked files (4) — distinguished, none deleted, none ignored:**
1. `docs/agent-ops/20261106-memory-remembering-agent/briefs/DOCS-AUDIT-01.md` — this task's brief (untracked pending the run's commit cadence). Not junk.
2. `docs/agent-ops/20261106-memory-remembering-agent/reports/final/IMPR-01.md` — FINAL-IMPR-01's in-flight report. Not junk; outside my write-set.
3. `docs/A Disciplined System for Computational Attention Allocation.md` — untracked substantive proposal ("Personal AI Operating System v2 — A Disciplined System for Computational Attention Allocation", core philosophy/invariant/production sections, head-read verified). NOT junk. Filing it under docs root with spaces is untidy but the content is a real document; ignore would hide it. **Operator decision needed:** track (possibly after renaming to a space-free name, which is also an operator call — I did not rename) or leave untracked. I recommend **track**, because an unignored untracked doc is the stable state; ignoring substantive markdown would set a precedent for silently dropping proposals. [VERIFIED head read lines 1-30]
4. `docs/Core Architecture Specification and Production Runtime RFC.md` — untracked substantive RFC (v4.0 polyglot storage topology, ledger/sandbox/self-improvement spec, head-read verified). Same disposition: NOT junk; **operator decision needed: track**. [VERIFIED head read lines 1-30]

## 2. .gitignore verdict — NO CHANGE, evidence-gated

### What `.gitignore` covers today [VERIFIED read .gitignore:1-9]

```
.venv/
__pycache__/
*.py[cod]
.pytest_cache/
.ruff_cache/
.mypy_cache/
*.egg-info/
.hypothesis/
.env
```

Python cache/tooling only — which correctly and already covers any Python junk that might appear under docs (probed below). There is no docs-scoped section and no editor/backup pattern.

### Probes (evidence)

- [RAN `git check-ignore -v docs/x/__pycache__/a.pyc` -> match `.gitignore:2:__pycache__/`, exit 0] — Python-generated junk under docs IS already ignored repo-wide. No action needed.
- [RAN `git check-ignore -v docs/nonexistent.tmp` -> no match, exit 1] — `*.tmp` is NOT ignored. Same holds by inspection for `*.swp`, `*.bak`, `*~`, `*.orig`, `*.rej` (none of them appears in `.gitignore`).

### Why no additions despite the gap

Brief §3 gates additions on patterns *already present/observed in /docs* with paths + counts. The census gives **zero** occurrences of any of those patterns in docs/ (282 files, all `.md`/`.tsv`, no hidden files). Adding `*.tmp / *.swp / *.bak` etc. would therefore be speculative hardening, not a tidy-up of observed junk, and it violates the fix-root-cause discipline: an ignore for a never-seen artifact tests nothing, hides nothing, and (if later copied by another agent into a broader pattern) risks swallowing a real file. The standing orders ban fixing at the wrong level and inventing scope; this audit's evidence gate is the brief's deliberate firewall against exactly that.

**Decision:** `.gitignore` is left byte-identical. This is a verified NO-OP, not an oversight. Nothing in /docs is currently at risk of committing generated junk: the tree contains only deliberate markdown + ownership TSVs. If editor/backup artifacts ever appear under docs, the operator's correct action then is to add the minimal observed pattern with its path evidence — I record the probe methodology so that future evidence can be produced the same way.

### Residual risk (not actioned, operator-visible)

- Editor/backup junk (`*.swp`, `*~`, `*.bak`, `*.tmp`) is currently committable if it ever appears [VERIFIED probe above]. Risk is low-probability and fully recoverable (untracked junk does not harm the conventions lock; the lock only reads tracked-format markdown by extension). No action per evidence gate; flagged here so the operator can pre-approve a future minimal pattern without re-auditing.
- A docs-generating tool (MkDocs/Sphinx/Docusaurus) would create exactly the `site/`-style output probed above; none exists, so no derived-output ignore is justified today.

## 3. Out-of-scope findings (not actioned)

1. **Two untracked substantive docs at `docs/` root** (§1, items 3–4). Out of scope to track/rename (write-set covers `.gitignore` + this report only; renaming = forbidden by §4). Carried as the operator decision above. Not a FINDING against any agent — they pre-date this audit (present in `git status` at first inspection).
2. **`.tmp` not ignored** (§2 residual risk). Documented, not "fixed" with a speculative pattern.
3. **`docs/memory-polish/plans/Plan1.md` is lock-active, not excluded** (§1c). Any future line shift in `src/` that it cites needs the citation updated in that docs file (owned outside this audit's write-set). Noted for the operator; no edit made.
4. No autocorrect/junk doc found, so the brief's "update a near-certain autocorrect/junk doc" exception was not exercised — no docs content edited at all.

**Self-review (audit diff).** Re-read the planned diff from scratch as a reviewer: the only writable diff is this report (`.gitignore` untouched). Checking: (i) does the report claim anything unverified? Every factual claim above carries `[VERIFIED path:line]` or `[RAN ...]` with the command and outcome; the two `[ASSUMED]`/`[UNKNOWN]` points (innocent-named junk, Plan1 future drift) are labeled as such; (ii) any line-ref shift risk? The report itself lives in `docs/agent-ops/` (lock-excluded) so its `path:line` strings are safe, and I changed zero in-scope docs; (iii) scope creep? No deletes, no renames, no speculative ignores, no edits to FINAL-IMPR-01's files; (iv) mutation test: if a junk file were added tomorrow, would this audit's method catch it? Yes — the census is rerunnable verbatim, and §2 records the exact enumeration + probe commands. No CRITICAL/MAJOR findings. Independent code review is not applicable (no code changed; report-only audit) — ORCH/FINAL-REV remains the reviewer of record.

**Long-run test.** Source: full census, not a sample. Load (100×): enumeration is O(n) over 282 files, rerunnable in seconds. Hostile input: n/a for docs audit, but the .gitignore decision is hostile-input-safe — it refuses to guess patterns. Operability: if junk appears, the same commands in §0 produce the evidence needed to add a pattern safely; rollback is `git checkout -- .gitignore` (no change, nothing to roll back). Reversal: this audit changes no behavior, so no test could fail on revert; the reversal probe is `git status --porcelain` still showing the same 4 untracked docs and `.gitignore` untouched.

## 4. Assumptions and unknowns

- [ASSUMED] The two untracked root docs' full content matches their heads (philosophy/RFC). I head-read 30 lines each; classification as substantive does not depend on the remainder. Full-content read would change nothing in the keep/track recommendation (generated junk has no such prose structure).
- [ASSUMED] `git status --porcelain` at audit start (4 modified docs including FINAL-IMPR-01's live edits + dirty src/tests from the parallel wave) reflects the parallel agent's in-flight work. I did not diff those files (out of write-set and out of lane) and committed none of them; explicit-path commits below avoid them entirely.
- [UNKNOWN] Whether the operator intends the two root docs to be tracked or removed. Left as an explicit decision, not a default.
- Self-caught error, recorded honestly: one exploratory `dart` invocation failed (`dart: command not found`) — this is a Python project (`uv`/`pytest` lane); no Dart toolchain exists or is needed. It was a wrong-lane probe, produced no change, and is not repeated. No other tool errors except PowerShell lacking `head` (piped instead via Python); both are documented so the ORCH does not mistake them for hidden verification gaps. All load-bearing evidence above comes from `git` + `pathlib` enumeration that did run.

## 5. Recommended task breakdown and ordering

**No new tasks proposed.** The audit closes clean: zero useless items, zero delete-candidates, zero justified `.gitignore` lines. Future ordering if the operator wants hygiene anyway (NOT recommended as blocking work):
1. (Operator, one command) `git add` the two root docs or move them into `docs/plans/`/`docs/reviews/` with names that match the tree's kebab-case convention — a rename changes `path:line`-adjacent navigation only if linkers exist; the conventions lock would verify. Audit cannot do this (write-set).
2. (Only if artifacts are ever observed) Add the observed pattern to `.gitignore` with path evidence, reusing §0's probes.

My commit is report-only (no `.gitignore` change, per §2). Per brief §7/standing orders #15, this report commit is a docs-only commit with explicit paths; there is no preceding code commit because there is no code change.
