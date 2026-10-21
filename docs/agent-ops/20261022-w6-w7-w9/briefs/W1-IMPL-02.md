# Brief — W1-IMPL-02 (implementer) · wave 1 · run 20261022-w6-w7-w9

## 1. Role and mission

**Mission:** close the last gap between the deployment script and the compose
file — the script reads exactly **one** compose file, so the **merged** stack
the gVisor documentation tells operators to run is invisible to it and to the
parity test — and turn the parity test from a hard-coded expectation into a
derived one.

**Why this task matters:** the whole point of W4 was that the script and the
compose file cannot disagree. They still can, in one specific way: the
documented gVisor command is
`docker compose -f docker-compose.yml -f docker-compose.gvisor.yml up -d`,
which runs a **merged** stack, and the script — which has no merge semantics —
never sees the merge. Today the two coincide *by accident* (the override's
`paa` service declares no `image:`), and nothing tests that accident. The next
person who adds an image to the override gets a deployment that fetches
nothing, with a green test suite.

**What failure looks like:** a reviewer adds an `image:` to
`docker-compose.gvisor.yml` and the parity test still passes — the invariant
the whole script rests on is unenforced.

**Your role:** implementer.

## 2. Required reading (in this order)

1. `docs/agent-ops/20261022-w6-w7-w9/templates/STANDING-ORDERS.md` — binding.
2. `AGENTS.md` — the gate rules, the ADR rule, the docstring rule.
3. `docs/agent-ops/20261022-w6-w7-w9/00-mission.md` — commands, hotspots,
   baseline, environment rules.
4. `docs/plans/w6-w7-w9.md` — **§0**, **§2.2** (your task, with the eight gaps
   G1…G8), and **§6** (the decisions, of which **D-1 is yours**).
5. `docs/adr/0030-deployment-image-contract.md` — the decision you are
   extending. Read it fully: it records *why* compose is the single source of
   truth, why the digest is checked inside the retry unit, and why the script
   carries no image table. **Your change amends it** — an amendment section,
   not a rewrite of the original.
6. `deploy/fetch-images.sh` — the script, all 641 lines. Understand the awk
   compose reader, the three refusals, the retry unit, the mirror semantics,
   the `--list` / `--verify` / `--load` modes, and every exit path.
7. `tests/deployment/test_fetch_images.py` — the existing 35 tests, the stub
   `docker` binary, the `_tool_path` / `_run` / `_compose_rows` / `_list_rows`
   helpers. **Your new tests must reuse these helpers**, not re-implement them.
8. `docs/deployment/docker.md` §"the fetch-images section" — the operator's
   documentation, which must gain the new flag and the new env variables.
9. `.env.example` — the operator's contract with the compose file.
10. `docker-compose.yml` and `docker-compose.gvisor.yml` — the source of truth.
11. `docs/agent-ops/20261022-w6-w7-w9/COMMS.md` — from the `WAVE-START` line.
12. `docs/agent-ops/20261022-w6-w7-w9/templates/report-implementation.md` — the
    report shape.

## 3. Task and acceptance criteria

**Task T-003.** The script reads one compose file; the parity test hard-codes
the service set.

**Root cause (verified in the plan's §2.2):** `PAA_COMPOSE_FILE` is a single
path (`fetch-images.sh:109-113`); the reader handles exactly one file; the
parity test (`test_fetch_images.py:568`) compares the script's `--list` against
an independent reader over the **base** file only; and the expected service set
is duplicated in the test (`:618-619`).

**Decision D-1, already taken:** the script takes **more than one** compose
file — a repeatable `--compose <path>` flag, merged in file order, later wins.
`PAA_COMPOSE_FILE` stays as the single-file spelling (backwards compatible).
*You may deviate with evidence, but you must say why in the report.*

**The gaps, and their dispositions (D-5, D-6, and the plan's §2.2):**

| Gap | Disposition |
|---|---|
| G1 — the gVisor override is invisible to the script and to the parity test | **fix**: the script merges; the parity test drives the merged stack |
| G2 — the test compares two hand-rolled readers, not Compose | **record, do not fix** — the module's own docstring already calls it "a shape lock, not an equivalence proof". Say so in your report; do not pretend it is an equivalence proof |
| G3 — the service-name set is duplicated in the test | **fix**: derive the expected set from the compose files instead of hard-coding it |
| G4 — nothing pins the image strings | **fix**: assert that every pulled image **is** digest-pinned (not *which* digest — a digest bump is a legitimate change) |
| G5 — the `image:`+`build:` exemption from the pin rule is untested | **fix**: pin the exemption as intended |
| G6 — the Dockerfile base images are in neither the compose file nor the plan | **record** — they are fetched by `docker build`, not by the script; ADR-0030 already scopes it. Note it in the report |
| G7 — the readers' blind spots (`profiles:`, `extends:`, anchors, a multi-line `image:`) are untested | **fix**: the reader corpus in `test_fetch_images.py` gains those shapes |
| G8 — `PAA_COMPOSE_FILE` and `PAA_FETCH_*` are documented nowhere outside the script's own header | **fix**: `.env.example` and `docs/deployment/docker.md` |

**Acceptance criteria (measurable):**

* The script accepts several compose files and merges them in file order; the
  **gVisor override** is covered by the parity test, and an `image:` added to
  the override **changes the plan** (a test that fails before the fix).
* The expected service set in the parity test is **derived**, not hard-coded.
* Every pulled image is asserted to be digest-pinned; the `image:`+`build:`
  exemption is asserted to be deliberate.
* The reader corpus covers the blind spots: a `profiles:`-gated service, an
  `extends:`, a YAML anchor, a multi-line `image:`.
* `PAA_COMPOSE_FILE`, `PAA_FETCH_MAX_ATTEMPTS`, `PAA_FETCH_BASE_DELAY`,
  `PAA_FETCH_TIMEOUT` and `PAA_IMAGE_MIRROR` are documented in
  `docs/deployment/docker.md` and `.env.example`.
* **The tests that must exist and fail before the fix:**
  * a compose file set whose merged image set differs from the base file's
    alone ⟹ the script's plan covers the difference;
  * the expected service set is derived ⟹ adding a service to the compose
    file does **not** require editing the test (the test still passes, and it
    would have failed before, because the set was hard-coded);
  * a `profiles:`-gated service ⟹ the plan's treatment is pinned (either it is
    included and that is asserted, or it is excluded and that is asserted —
    **decide, and say which**; the reader's job is to fetch what the deployment
    runs, and a profile-gated service that is off by default is *not* what it
    runs);
  * a multi-line `image:` and an `extends:` ⟹ the reader's behaviour is
    pinned, whatever it is — an **error** is an acceptable answer, a silent
    wrong answer is not.
* **Commands that must pass (scoped):**
  ```bash
  .venv/bin/python -m pytest tests/deployment -q
  .venv/bin/python -m ruff check tests/deployment
  bash -n deploy/fetch-images.sh
  ```
  and the script's own modes must still work:
  ```bash
  PAA_COMPOSE_FILE=docker-compose.yml deploy/fetch-images.sh --list
  ```
* The gate must not regress: `ruff` 0 and `mypy` 0.

**Note on the merge semantics.** Compose's own merge is a deep, well-specified
algorithm. You are **not** re-implementing it. What you need is the subset
that the repository's compose files actually use, and a **refusal** for the
shapes you do not support — because a script that silently mis-merges is worse
than one that says "I do not understand this file". State in the ADR amendment
exactly what subset you implemented and what you refuse.

## 4. Ownership

**WRITE-SET (you may edit exactly these):**

```
deploy/fetch-images.sh
tests/deployment/test_fetch_images.py
docker-compose.yml
docker-compose.gvisor.yml
docs/adr/0030-deployment-image-contract.md
docs/deployment/docker.md
.env.example
```

**READ-ONLY:** `tests/deployment/test_compose_config.py`,
`tests/deployment/test_compose_env.py`, `tests/deployment/test_daemon_config.py`
(they read the compose files you may change — **read them, do not edit them**;
if a change of yours breaks one, post `FINDING` and report it).

**FORBIDDEN:** everything else. In particular `src/paa/**`,
`tests/storage/**`, `tests/conventions/**`, `pyproject.toml` (T-002 owns it in
wave 2), `docs/adr/README.md`, `docs/MCP_AND_PLUGINS.md`, `AGENTS.md`,
`docs/adr/0031-*`, `docs/adr/0032-*`.

**Need something outside?** Do not edit it. Post `NEED` in `COMMS.md`, finish
the safe part, and report `DEFERRED-DEPENDENCY` with the exact change required.

## 5. Parallel siblings this wave

| Agent | Task | Owns | Contracts it changes |
|---|---|---|---|
| `W1-IMPL-01` | T-001 — the blob error vocabulary | `src/paa/storage/coldlake/**`, `tests/storage/{test_blob_errors,test_minio_backend,test_blobstore_contract}.py`, `docs/adr/0032-*` | the `BlobStore` error clause |
| `W1-IMPL-03` | T-004 — the `TYPE_CHECKING` leftover | `src/paa/memory/{contradiction,decay,curator}.py`, `src/paa/marketplace/installer.py`, `src/paa/skills/registry.py`, `src/paa/agents/{policy,context_agents}.py`, `tests/conventions/**` | the import graph of eight modules |

**Known interactions.** Neither sibling touches the deployment, and you do not
touch the cold lake. W1-IMPL-01 writes a **new** ADR (`0032`); you **amend**
`0030`. Neither of you edits `docs/adr/README.md` — the orchestrator does, at
the wave boundary. You cannot see their uncommitted work.

## 6. Skip list and known traps

* **Already fixed — do not redo:** the script already reads the compose file
  (there is no image table); the digest is already verified **inside** the
  retry unit; the presence check already uses the full reference; `--load`
  works and stops; `--list` needs neither the CLI nor a daemon; the three
  refusals (unpinned, interpolated, unnamed) exist; the GNU-`timeout`
  requirement is detected. **W4 landed all of this. Read ADR-0030 before you
  change anything.**
* **False leads:** "the script fetches older images than compose runs" — fixed
  in W4. "the `--load` branch is unreachable" — fixed in W4. "the presence
  check uses `:latest`" — fixed in W4.
* **Traps:**
  * The awk reader is **hand-rolled on purpose** (PyYAML is not a dependency).
    A hand-rolled parser is only safe if something holds it to the file — the
    existing test drives the real script against a stub `docker` and compares
    its list with an **independent** reader. **Keep that structure.** Do not
    make the script and the test share a reader; that would make the test
    tautological.
  * The stub `docker` is a `#!/bin/sh` script on a **PATH built from symlinks**
    into the real PATH minus one program (`_tool_path`, `_TOOLS`). The stub dir
    is appended so the real `docker` is shadowed. If you add a `docker`
    subcommand the script now uses, **the stub must answer it** — or the test
    will pass for the wrong reason.
  * `tests/deployment/` has **no `conftest.py`**; the tests are deliberately
    self-contained. Keep them so.
  * The test scrubs `PAA_FETCH_*`, `PAA_COMPOSE_FILE`, `PAA_IMAGE_MIRROR` and
    `STUB_*` from the developer's environment before running the script
    (`:420-424`) — a developer's shell must not change what the test
    exercises. **Your new flag and any new env var must be scrubbed the same
    way.**
  * `docker-compose.gvisor.yml`'s `paa` service has **no** `image:` and **no**
    `build:`. The reader currently *skips* a service with neither. If the
    merge changes what "skip" means, check that path.
  * The `image:`+`build:` exemption means `paa` and `paa-init` carry a **tag**
    (`paa/runtime:4.1`) and are never digest-checked. That is deliberate
    (ADR-0030) — **pin it as intended**, do not "fix" it.
* **Flaky / pre-existing failures:** none. The suite is green at the base SHA.
* **Do not run the full suite.** The gate is the orchestrator's job.

## 7. Environment and tests

* **Scoped commands only** (the ones in §3). Never the full suite, never a
  container restart, never a `docker compose up`, never a prune.
* **Do not stop or restart any container.** The deployment stack
  (`paa-stack-*`) belongs to the operator and is **not** yours to touch. Your
  tests use a **stub** `docker` binary; they never talk to a daemon.
* `tests/deployment/` currently has 61 tests; they must all still pass.
* Kill any process you start.

## 8. Deliverables

* **Report:** `docs/agent-ops/20261022-w6-w7-w9/reports/w1-impl/W1-IMPL-02-T-003.md`
  — using `templates/report-implementation.md`. **Create it at the start and
  append as you work.**
* **Commits:** code first, then the report in a **separate docs-only commit**
  that lists the code commit's SHA. Trailers: `Task: T-003`, `Agent: W1-IMPL-02`,
  `Wave: 1`, `Report: <path>`. Explicit paths only:
  ```bash
  test "$(git branch --show-current)" = "ops/20261022-w6-w7-w9" || { echo "wrong branch"; exit 1; }
  git add -- deploy/fetch-images.sh tests/deployment/test_fetch_images.py …
  git commit -m "<msg>" -- deploy/fetch-images.sh tests/deployment/test_fetch_images.py …
  ```
  The ADR amendment and the docs may ride in the same code commit, or in their
  own docs-only commit — your choice, but the report is always last.
* **Final message (≤ 8 lines):** `STATUS | agent | task | report path | commit SHAs | blockers`.

## 9. Role-specific notes

* **Answer the long-run test** (section 8 of the standing orders) in your
  report: Source / Load / Hostile / Operability / Reversal. For this task the
  *Reversal* answer is the important one: **which test fails if the merge is
  reverted?** Name it.
* **Amend, do not rewrite, ADR-0030.** The original decision and its reasoning
  are still true; your change extends it. Add an "Amendment" section that
  states what changed, why, and what it costs.
* **Say what you deliberately did not do.** G2 and G6 are recorded, not fixed.
  A reviewer will look for the honest scope statement.
