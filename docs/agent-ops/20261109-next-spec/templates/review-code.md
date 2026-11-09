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
## 1. Blind-pass findings
## 2. Claims audit
## 3. Long-run test (independent answers)
## 4. Findings
## 5. Attempts to break it
## 6. Dimension checklist
## 7. Optimization and simplification opportunities
## 8. Cross-agent interactions
## 9. What I could not verify
