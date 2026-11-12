# Brief — W0-IMPR-01 (IMPLEMENTER) · wave 0 improve · run 20261109-next-spec

## 1. Role and mission
Mission: Fix reviewer MAJOR-1 + MAJOR-2 in the new markdown-link lock (T-N0-1) with minimal changes, re-green the gate. Why: lock must not miss malformed links or fragmented allowlist links — those are exactly the broken-link classes it exists to catch. Failure looks like: malformed or fragmented-allowlist links pass CI silently.
Your role: implementer (coder), read review first.

## 2. Required reading
1. STANDING-ORDERS (binding)
2. 00-mission.md, docs/plans/2026-11-09-next-spec-revised.md N0.1
3. W0-REV-01 review: docs/agent-ops/20261109-next-spec/reports/w0-review/W0-REV-01-review.md (§4 MAJOR-1/MAJOR-2, §5 probe table, §7 DO-NOW)
4. W0-IMPL-01 report §4/§7/§11
5. Template: report-implementation.md
6. COMMS.md

## 3. Task and acceptance criteria
Task T-N0-1-IMPR-01: Fix two MAJOR false-negatives in tests/conventions/test_markdown_links.py:
- MAJOR-1: `test_every_relative_link_names_an_existing_path` skips `<unparseable-link>` sentinel (line ~500) — must report as defect (mirror check_link lines 434-438). Verify `[x](<unterminated` and `[x](guide.md` (missing paren) now FAIL the existence test (or _violations) instead of skipping. Ideally delegate to check_link/_violations so sentinel handling lives in one place (MINOR-4 meta-fix) — if full unify is risky, minimal one-line report-as-defect is acceptable provided MAJOR-2 also fixed and synthetic test covers sentinel.
- MAJOR-2: existence test allowlist skip lacks `and not fragment` (line ~503) — add the fragment check so `0001-...#foo` is NOT allowlisted (it must be reported, not skipped). Verify fragmented allowlist now fails existence as check_link does.
Acceptance: both probes from review §5 (#1, #2, #6) now produce violations in the real suite (not just _violations); pytest tests/conventions -q green (34+); ruff 0; mypy src exactly 3. Do NOT attempt MAJOR-3 (../ containment — file as follow-up, reviewer recommends follow-up with regression test, not bolted on). Do NOT attempt MINOR-1 nested brackets, MINOR-3 dedup, NIT-1/3/4 (file or leave; optional if 3-line change each — only if no corpus impact).
Commands: pytest tests/conventions/test_markdown_links.py -q, pytest tests/conventions/test_docs_references.py -q, ruff, mypy.

## 4. Ownership
WRITE-SET: tests/conventions/test_markdown_links.py only
READ-ONLY: docs/VISION.md, docs/README.md, docs/ROADMAP.md, other tests/*
FORBIDDEN: docs/* links (unless a test fix breaks them — then NEED), scripts/*, src/**

## 5. Siblings
No parallel implementers this sub-wave (single-file fix). Current HEAD includes W0-REV-01 1ccf3f0, W0-REV-02 b1779e3.

## 6. Skip list
MAJOR-3 (root containment) is explicitly out of scope for this task — document as DEFERRED-DEPENDENCY in report.

## 7. Environment
Scoped pytest only. Do NOT run full suite, docker, etc.

## 8. Deliverables
Report: reports/w0-review/W0-IMPR-01-improve.md (ORCH-DIGEST first). Commits: code then report. Branch ops/20261109-next-spec from main @ b8570a6, worktree D:\dev work\coding\Projects\Personal_AI_Automations_OS. Explicit paths.
Final message ≤8 lines.

## 9. Role notes
Implementer: minimal fix, prove with probes, file MAJOR-3 as follow-up.
