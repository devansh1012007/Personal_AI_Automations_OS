<!-- ORCH-DIGEST:START -->
agent: <ID> | scope: <wave/cluster/tasks> | verdict: APPROVE|APPROVE-WITH-CHANGES|REJECT
findings: BLOCKER <n> | MAJOR <n> | MINOR <n> | NIT <n>
claims_audit: <n claims checked> / <n unsupported or false>
band_aids_found: none|<list>
top_3_risks: <short>
optimizations: DO-NOW <n> | DO-LATER <n> | NOT-WORTH-IT <n>
reversal_check: done|reasoned-only|not-possible
confidence: high|med|low (<why>)
<!-- ORCH-DIGEST:END -->

# Code review — <ID>

## 0. Method
Diff range reviewed, files read in context, commands run. Order followed: blind pass first, then implementer report.
## 1. Blind-pass findings (before reading implementer reports)
## 2. Claims audit
| Implementer claim | Verified? | Evidence / finding |
|---|---|---|
## 3. Long-run test (independent answers)
Source / Load / Hostile / Operability / Reversal
## 4. Findings
### R-W<N>-001 [BLOCKER|MAJOR|MINOR|NIT] <title>
- Location: `path:line` · Evidence: … · Why it matters: … · Recommended fix: … (root-cause level)
## 5. Attempts to break it
Concrete inputs/interleavings tried or reasoned, outcomes.
## 6. Dimension checklist
Correctness · root cause vs symptom · regression/compat · concurrency/idempotency · error handling · security/abuse · performance/scalability · migration/rollback · observability · maintainability/conventions · docs impact — each with verdict and notes.
## 7. Optimization and simplification opportunities
| # | Location | Evidence | Impact | Effort | Risk | Verdict (DO NOW / DO LATER / NOT WORTH IT) |
|---|---|---|---|---|---|---|
## 8. Cross-agent / cross-task interactions observed
## 9. What I could not verify
