# Plan — user_guide.md dummy-proof guide (run 20261108)

**Status:** ready for dispatch (investigation done).
**Branch:** `docs/user-guide-20261108` from `main` @ `b8570a6` (clean; does NOT include AGENTS refresh until merged).
**Date:** 2026-11-08.

## 1. Problem statement and goal

There is no single beginner file: install, config, CLI, memory, coder, serve/TUI, API, and troubleshooting live across README.md, docs/operations, docs/flows, docs/modules/cli.md, docs/coder-agent.md, and src/paa/cli/*. Users get lost and guess flags, violate PAA_HOME/.env conventions, or hit footguns (DOCKER_GID, stale settings cache, bare `.` install skipping suites).

**Goal:** Create `docs/user_guide.md` (single file, dummy-proof, with copy-paste examples) covering: (1) what PAA is and is not, (2) install tiers (dev vs `.` vs all) + activation + first commands, (3) config essentials + OpenRouter 4-line recipe (Linux + Windows + `.env` file forms), (4) five worked workflows (submit→ledger→recover, human gate with default-reject warning, memory signals→drain→facts/search, coder agent modes with `paa mode` non-persistence trap, serve+TUI), (5) API quick map (3-row curl subset, not full duplicate of api-reference.md), (6) no-key behavior + link to fake-model test recipe (not developer tutorial), (7) troubleshooting table (10–12 rows, symptom→cause→fix→verify), (8) explicit NON-goals, (9) next-docs links. Every command/flag/setting/failure is traceable to code (file:line in plan evidence, kept as inline path:line cites verified against the docs-lock).

**Planner-reviewer resolutions (ses_e43cb1356...) — must-fix before/at dispatch:**
- R1 (BLOCKER gate): `paa gate` defaults to REJECT (`Option(False, "--approve/--reject")` cli/main.py:185) — every example must show `--approve` explicitly, and Workflow B must run `paa status <id>` first to read `awaiting`/`policy` rows (main.py:136-139).
- R2 (BLOCKER mode): `paa mode <x>` does NOT persist (prints `Set PAA_POLICY__MODE=...` main.py:354-366) vs `paa agent --mode ask|plan|accept_edits|bypass` (coder.py:61-65). Boxed warning: “pa mode auto changes nothing; edit .env/env, restart, re-run `paa doctor` to verify” — distinct from `agent --mode`. Also: `--max-steps 25` bounds the run while `ModelTurn.tokens` is always 0 (model_adapter.py:32-38).
- R3 (MAJOR A1 link): Keep `docs/user_guide.md` (docs-lock scope: tests/conventions/test_docs_references.py:97-100 excludes root *.md except README/AGENTS, so root would dodge the lock — rejected). Links in BOTH `docs/README.md` §B and one line in root README Quick Start/Further reading (README.md:39-55,335-341) — resolve L20 vs L34 conflict.
- R4 (MAJOR PATH): §2 starts with activation + `paa` PATH fallback (`.venv/bin/paa` / `.venv/Scripts/paa.exe`, entry pyproject.toml:81-82) + `paa --help` verification; cover `.env` must be in CWD, `PAA_HOME` outside-repo warning.
- R5 (MAJOR Win): OpenRouter recipe shows POSIX `export`, PowerShell `$env:OPENROUTER_API_KEY`, and `.env`-file form (no shell syntax).
- R6 (MAJOR workflows): teach `ledger` → `chain integrity: ok` (main.py:172-175); `serve` → `curl http://127.0.0.1:8787/health` (api/app.py:70-71); TUI quit `q/:q/:quit` (`src/paa/tui/__init__.py:21`, dispatcher.py:59); `signals --status` repeatable (main.py:590-592), `drain --limit` default 25 (main.py:660-662); gate path uses `submit --no-run` → `run`.
- R7 (MAJOR scope): §5 shrunk to 3-row curl (`GET /health`, `POST /tasks`, `GET /tasks/{id}`) + “full reference” link; §6 replaced by 3-line no-key behavior + link to tests/integration/conftest.py recipe. Frees ~60-80 lines.
- R8 (MAJOR length): Target 450–650 lines (hold 350-500 only if R7 applied AND troubleshooting ≥10 rows). Add prune rule: “link out, don’t duplicate tables.” DOCKER_GID includes the single `stat -c %g` one-liner + link, no deep-dive.

**Non-goals:** No code changes, no new CLI flags, no README.md rewrite, no ADR edits, no walkthrough videos/screenshots, no real-model end-to-end run (ROADMAP H1.3 not done), no Docker/gVisor deep-dive (pointer to docker.md only).

## 2. Facts from Step 1 vs assumptions

**Facts (investigator ses_e43cd959...):** install = `uv venv --python 3.12 && uv pip install -e ".[dev]"` (README.md:42, pyproject.toml:54-76; bare `.` skips api/s3 suites pyproject.toml:61-72); first cmd = `paa doctor` table (cli/main.py:237-288); CLI catalogue = version/submit/run/status/ledger/gate/recover/doctor/serve/mode/tui/memory{ facts,search,signals,drain,world-model}/agent with exact flags (cli/main.py:70-733, cli/coder.py:57-93); config = PAA_HOME ~/.paa (config.py:49-57), PAA_ + __ delimiter + .env from CWD (config.py:796-803), dotenv_value no os.environ write (config.py:896-922), lru_cache reset required (config.py:925-939); API loopback 127.0.0.1:8787 (config.py:822-859); OpenRouter recipe = OPENROUTER_API_KEY + 3×PAA_MODELS__* (AGENTS.md coder §, .env.example:76-104); workflows a–e verified in docs/flows + coder-agent.md; troubleshooting verified (DOCKER_GID, sandbox PATH python, port, .env cwd, stale cache); non-goals verified (no file/shell/git tools runtime_tools.py:90-112, tokens=0 model_adapter.py:32-38, EXECUTION_COMPLETED per-step non-terminal, queue derived); docs-lock checks every `path:line` (one file, line exists, non-blank; bare names ambiguous; fenced code skipped) — investigator Q7 evidence.

**Assumptions (explicit):**
- A1: Guide lives at `docs/user_guide.md` (not repo-root user_guide.md) so docs-lock scope is predictable; link from BOTH root README Quick Start/Further reading AND docs/README §B (R3). Alternative root placement recorded as rejected.
- A2: Examples use `paa doctor`, `paa submit "…"`, `paa ledger <id>`, `paa gate <id> --approve` (never bare `paa gate` — default is reject, R1), `paa agent --list-tools` — all runnable without keys. Real-model + docker examples marked NOT-RUN/CI-only.
- A3: Length target 450–650 lines (dummy-proof, not 15-line cheat), OR 350–500 only if R7 cuts AND troubleshooting ≥10 rows (R8). Exceeds AGENTS.md compact rule; appropriate for a user guide.
- A4: All path:line cites are verified by the coder reading each line (not blind copy); lock alone cannot catch "wrong without being broken." Base commit pinned to `b8570a6` (per Minor branch/base mismatch); re-verify all cites after rebase if agents-refresh merges first.

## 3. Approaches

- **A. Single mega-file `user_guide.md` (CHOSEN):** One file, §1..§9 per investigator outline, each § ends with “Try this” copy-paste block. Pros: single link from README, dummy can follow top-to-bottom. Cons: long file, must stay in sync — mitigated by verified cites.
- **B. Split into docs/user/*.md + index.** Rejected: user asked “make a file user_guide.md” (singular); split adds navigation overhead for dummies and delays linking.
- **C. Root `/user_guide.md`.** Rejected: docs-lock excludes root *.md except README/AGENTS; root path is non-idiomatic and future contributors will not find it under docs/. `docs/user_guide.md` is discoverable from docs/README.md index.

## 4. Atomic tasks

Single atomic task (docs only):
- **T-01 — Write `docs/user_guide.md`** (owner: coder, branch `docs/user-guide-20261108` from `main` @ `b8570a6`). Create the file + one-line link in `docs/README.md` §B **and** one line in root README Quick Start/Further reading (R3). Ensure lock green. No src/tests changes.

Dependency graph: T-01 alone. Then Step 4 independent code-reviewer pass → fix findings → Step 8 final review → commit/PR per git discipline. No parallel coders (single new file + index lines).

### T-01 spec
Sections: 1 What PAA is (ledger is truth, model proposes/policy decides, derived views); 2 Install + activation (3 tiers table, `.venv/bin/paa` fallback, doctor table decode, `paa --help` verify, Windows Activation.ps1, PAA_HOME outside-repo warning); 3 Config (PAA_HOME, `.env` CWD precedence, 3 must-know, OpenRouter 4 lines in POSIX + PowerShell + `.env`-file forms, loopback guard, `paa mode` non-persistence boxed warning); 4 Workflows A–E — A submit→status→ledger (`chain integrity: ok`)→recover, B `submit --no-run`→`run`→`status` (read awaiting/policy)→`gate --approve` explicitly (default = reject callout) →`ledger`, C memory `signals [--status …]` repeatable →`drain [--limit]` default 25 →`facts`/`search`/`world-model`, D `agent --list-tools` (free) →`--mode plan` →`--mode ask` →`--yes` →`--continue`/`--resume` + `--max-steps` bounds, E `serve` (`curl /health`) + `tui` (`new/drive/status/ledger/gate/doctor/q/:q/:quit`); 5 API quick map (3-row curl table: GET /health, POST /tasks, GET /tasks/{id} + full-reference link, loopback 8787 only); 6 No-key behavior (3 lines: what works offline, what errors) + link to fake-model test recipe; 7 Troubleshooting table (10–12 rows incl. backend-down, ledger verify, DOCKER_GID one-liner, sandbox python-is-python3, port/host, `.env` cwd, stale lru_cache, invalid-UUID/no-such-task, PAA_HOME git-clean, `paa` not on PATH — symptom→cause→fix→verify each); 8 What PAA does NOT do yet (no file/shell/git tools, tokens=0→max_steps, step-completion non-terminal, queue derived, no real-model e2e yet); 9 Where to go next (plain section links, NO new path:line cites except verified ones).
Each code block is a complete runnable snippet, marked `# Linux` or `# Windows` for paths AND env-var syntax (R5). No secret values; use placeholders.
Constraints: every path:line cite verified by reading; prefer section links over new cites; no line-number citations to AGENTS.md (link by section name); no claim of real-model run or Docker build; DOCKER_GID content is the single `stat -c %g /var/run/docker.sock` one-liner + link, no deep-dive; screenshots/video deliberately excluded (binary assets bloat diff).

## 5. Blast radius / risks / rollback

- **Blast:** new `docs/user_guide.md` only + `docs/README.md` one-line index link. No src/tests/CI/config. Rollback: `git revert` the commit (1 new file + 1 line).
- **Risk — docs-lock:** new file is in scope (docs/**/*.md). Coder must run `pytest tests/conventions -q` and fix every cite until green. Fenced code blocks are skipped by the lock — coder may use ``` blocks for shell examples safely, but prose path:line cites still must verify.
- **Risk — stale examples:** Examples must use `.[dev]` (not bare `.`), `paa doctor` first, `.venv/bin/python` vs `.venv/Scripts/python.exe` notation. Verify each example against cli/main.py + coder.py before writing.
- **Risk — overpromise:** Must not claim file/shell/git tools, real-model turnkey, Docker money path, or tokens accounting that does not exist. §8 is mandatory.

## 6. Risks, open questions, second-order effects

- Second file to keep in sync (user_guide + README index). Mitigation: guide links out to flows/modules as source of truth (“for detail see …”) instead of duplicating.
- VISION.md path ambiguity at HEAD (`D VISION.md` deleted; docs/VISION.md via ROADMAP links) — guide must not cite VISION.md:line until resolved; link by title only.
- What if `docs/user_guide.md` vs root `user_guide.md` expectation mismatch? Plan A1 chooses docs/. Surface in final report so user can relocate with a rename commit if needed.

## 7. Verification plan

- `pytest tests/conventions -q` green (docs+import locks) after T-01.
- `ruff check src tests` 0; `mypy src` baseline identity (Windows 3 at sandbox/subprocess_backend.py:548) — docs-only, no code touched.
- `python -c "from pathlib import Path; print(len(Path('docs/user_guide.md').read_text().splitlines()))"` reported; target 450–650 (or 350–500 iff R7+R8 applied).
- Done criteria (countable): 5 workflows each have a “Try this” block + expected output; troubleshooting rows ≥10; every `paa` example either smoke-run or explicitly marked NOT-RUN; every path:line cite clicked by reviewer.
- Human read-pass: Step 4 reviewer clicks every path:line cite and every example's expected output against code truth.
