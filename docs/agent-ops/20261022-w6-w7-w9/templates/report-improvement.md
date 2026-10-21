<!-- ORCH-DIGEST:START -->
agent: <ID> | scope: <tasks/findings addressed> | status: DONE|PARTIAL|BLOCKED|ESCALATE
findings: FIXED <n> | REJECTED <n> (with evidence) | DEFERRED <n>
blockers_major_open: none|<IDs>
code_commits: <sha...> | report_commit: <sha>
tests_added_or_strengthened: <n>
ownership_violations: none|<list>
confidence: high|med|low (<why>)
<!-- ORCH-DIGEST:END -->

# Improvement report — <ID>

## 1. Finding-by-finding disposition
| Finding | Severity | Disposition | What I did / why rejected or deferred (with evidence) | Commit |
|---|---|---|---|---|
## 2. Code changes (per file, with rationale; root-cause level, no band-aids)
## 3. Test changes (added edge cases from the test review, strengthened assertions, flake fixes, removed redundancy)
## 4. Optimizations applied and evidence of benefit (and ones declined, with reason)
## 5. Commands run and results (scoped)
## 6. Residual risks and anything the delta reviewer should check
