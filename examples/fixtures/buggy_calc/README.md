# buggy_calc — the S1 demo fixture (N3.1)

One failing test, one line bug, recorded fix. `divide()` promises
"`a` divided by `b` as a float" and floors the result, so
`test_divide_returns_fraction` fails while the other four tests
pass. The fix is one character (`//` → `/`), pinned as
`fix.patch`.

This directory is intentionally **outside** the repo's pytest
suite: `pyproject.toml` sets `testpaths = ["tests"]`, so the
failing test can never make the main suite red. It is a target
for `paa agent --workspace`, not a test of this repository.

## Real-model S1 run (Linux/WSL2, needs a key)

From the repo root, with the dev venv active:

```bash
export OPENROUTER_API_KEY=sk-or-...
export PAA_MODELS__LOCAL_PROVIDER=openrouter
export PAA_MODELS__LOCAL_MODEL=openai/gpt-4o-mini
export PAA_MODELS__ESCALATION_PROVIDER=none
export PAA_HOME="$HOME/.paa-s1"   # throwaway home, never the real one

cp -r examples/fixtures/buggy_calc /tmp/buggy_calc
cd /tmp/buggy_calc

paa agent --workspace /tmp/buggy_calc --mode ask "fix the failing test"
paa ledger <correlation-id>   # printed by the agent run
```

Expected: the agent reads `calc.py`, runs the suite, edits the
one line, re-runs to green, stages and commits — each mutating
call prompting in ASK mode. The ledger lineage begins with an
explicit `MANDATE_GRANTED` and every call carries `args_sha256`
plus the actual `isolation_level` (`PROCESS` with the subprocess
sandbox; `NAMESPACE`/`VM` where a container backend is live).
Results go in `docs/operations/2026-11-09-next-spec/s1-report.md`.

## ScriptedModel dry-run (any host, no spend)

The harness proof — same fixture, same tool sequence, no model:

```bash
.venv/Scripts/python.exe scripts/drill_kill_resume.py --dry-run
```

Runs the fixture against a real `CoderAgent` with a scripted
tool-calling model, and asserts the S1 evidence: mandate head,
per-call hash + isolation rows, persisted intent for the edit,
ASK prompts on mutating calls only, chain verification, green
suite, commit, clean tree, and `fix.patch` reproducing the fix
on a pristine copy. Exit 0 means every check held.

## Applying the fix by hand (operator baseline)

```bash
git apply fix.patch
python -m pytest test_calc.py -q
```

`git apply --check fix.patch` must succeed against the shipped
`calc.py`, and the suite must then report 5 passed.
