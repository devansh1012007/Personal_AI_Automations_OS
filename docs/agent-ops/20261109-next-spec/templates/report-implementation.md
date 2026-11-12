<!-- ORCH-DIGEST:START -->
agent: <ID> | task: <T-xxx> | status: DONE|PARTIAL|BLOCKED|ESCALATE
code_commits: <sha...> | report_commit: <sha>
files_changed: <n> (<≤10 paths>)
contracts_changed: none|<list>
tests: added <n>, changed <n>; scoped run: pass|fail|NOT-RUN(<why>)
ownership_violations: none|<list>
deferred_dependencies: none|<list>
out_of_scope_findings: <n>
temporary_mitigation: no|yes(<what, removal condition>)
deep_issue_flag: yes|no
least_confident_about: <1 line>
confidence: high|med|low (<why>)
last_comms_line_read: <timestamp>
<!-- ORCH-DIGEST:END -->
# Implementation report — <ID> — <T-xxx> <title>
## 1. Task and acceptance criteria (restated)
## 2. Verification that the problem exists (evidence gathered myself)
## 3. Root cause
## 4. Approach chosen and why (alternatives rejected)
## 5. What changed (per file)
## 6. Blast radius and contracts
## 7. Tests (how each confirmed to fail without fix)
## 8. Long-run test (Source/Load/Hostile/Operability/Reversal)
## 9. Edge cases and abuse scenarios
## 10. Performance and security
## 11. Commands run and results (verbatim summaries)
## 12. Deviations from plan/brief
## 13. Out-of-scope findings
## 14. Known limitations, risks, rollback notes
## 15. Where I am least confident
## 16. Self-review checklist
## 17. Commits
