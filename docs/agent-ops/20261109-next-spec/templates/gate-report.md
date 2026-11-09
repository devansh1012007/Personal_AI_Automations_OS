# Gate — wave <N> run <k> — <UTC time>
- Tree state: `git status --porcelain` → clean | <exceptions>
- HEAD: <sha> · Baseline: see 00-mission.md
| Step | Command | Result | vs baseline |
|---|---|---|---|
| Build | | | |
| Typecheck | .venv/Scripts/python.exe -m mypy src | | |
| Lint | .venv/Scripts/python.exe -m ruff check src tests | | |
| Unit | pytest tests/ -q | | |
| Integration/e2e | pytest tests/ -q (full) | | |
| Fresh container restart + health + smoke | | | |
| New/changed tests ×3 (flakiness) | | | |
New failures (regressions): … · Pre-existing failures unchanged: … · Flaky: …
Triage/action: …
