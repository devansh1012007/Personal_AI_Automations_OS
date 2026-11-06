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
## 2. Verification that the problem exists (own evidence [VERIFIED], [RAN], deltas from investigation)
## 3. Root cause
## 4. Approach chosen and why (alternatives rejected; addresses cause not symptom)
## 5. What changed (per file) | File | Change | Why |
## 6. Blast radius and contracts (greps run, contracts changed before→after, backward-compat)
## 7. Tests (behavior covered, edge/abuse cases, confirmed fail-without-fix, commands+results, flakiness)
## 8. Long-run test: Source / Load / Hostile / Operability / Reversal
## 9. Edge cases and abuse scenarios (handled / not handled / unknown)
## 10. Performance and security considerations
## 11. Commands run and results (verbatim summaries)
## 12. Deviations from plan/brief; ownership issues; deferred dependencies
## 13. Out-of-scope findings
## 14. Known limitations, risks, rollback notes
## 15. Where I am least confident / what the reviewer should scrutinize
## 16. Self-review checklist
- [ ] Root cause, not symptom  - [ ] No band-aid patterns  - [ ] Tests fail without the fix
- [ ] Only write-set files changed  - [ ] No secrets/PII  - [ ] Conventions from AGENTS.md followed
- [ ] Commits contain only my files; trailers present
## 17. Commits
code: <sha(s)> | report: <sha>
