# Brief — W0-IMPR-02 (IMPLEMENTER) · wave 0b improve · run 20261109-next-spec

## 1. Role and mission
Mission: Fix reviewer MAJOR-1: all 14 docs/VISION.md line refs in docs/reviews/2026-11-09-composability-review.md are stale by +4 (banner inserted 4 lines). Why: audit trail pointers are wrong on committed tree — reader follows ref and lands 4 lines early. Failure looks like: next reader cannot verify verdicts from tree.
Your role: implementer (coder), docs fix only.

## 2. Required reading
1. STANDING-ORDERS
2. 00-mission.md, plan N0.3
3. W0-REV-04 review: reports/w0-review/W0-REV-04-review.md §2.4 (exact per-ref offsets), §4 MAJOR-1, §11 suggested task
4. docs/reviews/2026-11-09-composability-review.md (review note), docs/VISION.md (post-banner 968 lines, appendix 401-968)
5. Template: report-implementation.md
6. COMMS.md

## 3. Task and acceptance criteria
Task T-N0-3-IMPR: Shift every docs/VISION.md ref in docs/reviews/2026-11-09-composability-review.md by +4 (14 table entries + 3 in-text L refs + scope 401–964 → 401–968). Verify each new ref lands on non-blank line (docs-references lock). Add one sentence noting numbering is post-banner (e.g., after scope line). Optionally pin version to 2608.25512v1 and fix appendix count prose (NIT-1: state inclusive span 401–968 = 568) and align banner link text to target (NIT-2: link text docs/reviews/... vs target reviews/... — align text to target if trivial). Do NOT touch verdicts, wording, or appendix content. Do NOT attempt PDF-body verification (DEFERRED follow-up). Do NOT rewrite W0-IMPL-03 report history — MINOR-1 (lock-scope misstatement in impl report §6/§14) is handled by ORCH annotation, not by editing the report.
Acceptance: pytest tests/conventions -q green (34 passed), ruff/mypy baseline identity, every ref checked manually against current lines (document how checked in report).
Commands: pytest tests/conventions -q, .venv/Scripts/python -m ruff check src tests, .venv/Scripts/python -m mypy src, python scripts/status.py --check (expect 0)

## 4. Ownership
WRITE-SET: docs/reviews/2026-11-09-composability-review.md only
READ-ONLY: docs/VISION.md, docs/reviews/documentation-audit.md, tests/*
FORBIDDEN: editing docs/VISION.md, scripts/status.py, .github/workflows/ci.yml, src/**, tests/*, impl reports

## 5. Siblings
No parallel implementers (single-file fix). Current HEAD includes W0-REV-04 7a81404, W0-REV-05 8387ae4.

## 6. Skip list
PDF body verification is DEFERRED-DEPENDENCY (file in report). MINOR-1 report correction is ORCH-owned.

## 7. Environment
Scoped pytest/ruff/mypy only.

## 8. Deliverables
Report: reports/w0-review/W0-IMPR-02-improve.md (ORCH-DIGEST). Commits: code then report. Branch ops/20261109-next-spec, worktree D:\dev work\coding\Projects\Personal_AI_Automations_OS. Explicit paths.
Final message ≤8 lines.

## 9. Role notes
Implementer: mechanical +4, prove each ref, file PDF follow-up as DEFERRED.
