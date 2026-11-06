# Brief — W3-REV-01 (REV) · wave 3 review · run 20261106-memory-remembering-agent

## 1. Role and mission
Mission: Independently audit Wave 3 (T-005 extractor counters + pronoun mapping, T-006 explicit-only prune floor + decay_lambda deprecation). Try to break both. Note: orphan-guard regression (gate-w3-1) is already known — confirm root cause, do not spend the report re-checking it; focus on NEW defects the implementers may have missed.
Role: code reviewer (READ-ONLY on source).

## 2. Required reading (in order)
1. docs/agent-ops/20261106-memory-remembering-agent/templates/STANDING-ORDERS.md (binding)
2. AGENTS.md (gate, config rules, conventions)
3. docs/agent-ops/20261106-memory-remembering-agent/00-mission.md (baseline: mypy 3, pytest 12; gate-w3-1 known regressions excluded)
4. Plan C:\Users\devan\.opencode\plan\2026-11-06-memory-remembering-agent.md — items C05, C06, T06, T07
5. Template: docs/agent-ops/20261106-memory-remembering-agent/templates/review-code.md
6. COMMS.md (read at start)

## 3. Task
Review diff: `git diff 38cda61..HEAD -- src/paa/memory/creator.py src/paa/config.py src/paa/memory/decay.py tests/memory/test_creator.py tests/memory/test_decay.py tests/memory/test_llm_extractor.py` (code commits 648f979, f4d0ca8).
Method: BLIND PASS first (diff + surrounding code, findings before implementer reports), THEN claims audit (reports: reports/w3-impl/W3-IMPL-05-T-005.md, reports/w3-impl/W3-IMPL-06-T-006.md).
T-005 angles: pronoun entity `user` collision with real users named "user"? I/we mapping scope (case, punctuation, words containing I/we)? Counter double-counting between extractor and _is_storable? LLM extractor parity (do LLM-extracted facts get counters?)? ProcessingResult consumer break (API BatchReport shape)? Hostile: "I" inside object_value false-fire?
T-006 angles: `model_fields_set` with .env vs env-var vs kwargs (known least-confident); DeprecationWarning visibility (AGENTS.md filterwarnings error for paa.* — does warning break tests under config?); unknown-domain keys warn-and-ignore is swallowed-error band-aid or honest?; DecaySweeper construction lazy-settings import cycle (smoke test enough?); sweep signature unchanged — is constructor floor injection thread-safe/minimal?; DOMAINS untouched — verified?
Run scoped tests only.
Note: gate failures R-W3-REG1 (orphan guard) and R-W3-DOCS (8 citations) are already triaged to improver — do not re-report them as your findings; only add detail if you find NEW defects in the same code.

## 4. Ownership
WRITE-SET: only docs/agent-ops/20261106-memory-remembering-agent/reports/w3-review/REV-code.md
READ-ONLY: everything else.

## 5. Deliverables
Report with ORCH-DIGEST verdict (APPROVE/APPROVE-WITH-CHANGES/REJECT), findings by severity with file:line evidence + root-cause fixes, optimizations DO NOW/LATER/NOT WORTH IT.
Final ≤8 lines.
