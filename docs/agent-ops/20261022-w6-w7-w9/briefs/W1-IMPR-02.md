# Brief — W1-IMPR-02 (improver) · wave 1 · run 20261022-w6-w7-w9

## 1. Role and mission

**Mission:** fix the wave-1 review's findings on the **deployment image
contract** — three MAJORs, all of them the same failure mode: *the script
reports success while planning a stack Compose will not run, or a stack it
will not refuse to run.*

**Your role:** improver. You are the **last change of wave 1** for this
write-set. You act on a reviewer's findings; you do not re-litigate them.
Every finding you were given ends **FIXED**, **REJECTED** (with evidence), or
**DEFERRED** (with a reason).

**You are not the implementer.** You did not write this code.

## 2. Required reading (in this order)

1. `docs/agent-ops/20261022-w6-w7-w9/templates/STANDING-ORDERS.md` — binding.
2. **The findings you must address, in full:**
   `docs/agent-ops/20261022-w6-w7-w9/reports/w1-review/W1-REV-02-code.md`
   §4 — findings **R-W1-101, R-W1-102, R-W1-103** (MAJOR), R-W1-104, R-W1-105,
   R-W1-106, R-W1-107, R-W1-113 (MINOR/NIT);
   and `docs/agent-ops/20261022-w6-w7-w9/reports/w1-review/W1-TREV-03-tests.md`
   §5 — finding **R-W1-213** (MINOR).
   **Read the findings in full — each carries the evidence, the reproduction
   and the recommended fix.** Do not work from this brief's summary.
3. `docs/adr/0030-deployment-image-contract.md` — the decision you are
   amending, **including the amendment T-003 added** (commit `db99c66`).
4. `docs/plans/w6-w7-w9.md` §2.2 — the design the task was given, including
   the G1–G8 gap table (G2 and G6 are *recorded, not fixed* — do not
   "improve" them into fixes).
5. `deploy/fetch-images.sh` — the script.
6. `tests/deployment/test_fetch_images.py` — the tests, the stub `docker`, and
   the helpers (`_tool_path`, `_run`, `_compose_rows`, `_list_rows`).
7. `docs/deployment/docker.md` (the fetch-images section) and `.env.example`.
8. `docs/agent-ops/20261022-w6-w7-w9/templates/report-improvement.md` — **your
   report template.**

## 3. The findings, and what "done" means for each

| Finding | Severity | What "done" means |
|---|---|---|
| **R-W1-101** | **MAJOR** | The YAML merge key `<<:` is **not refused** and is silently mis-read — the script reports success while planning a stack Compose will not run. **Refuse it, loudly**, with the same refusal discipline the other unsupported shapes get: a non-zero exit and a message naming the file and the shape. |
| **R-W1-102** | **MAJOR** | The top-level `include:` refusal catches only the **block** form; the **flow** form `include: [other.yml]` is silently ignored. Refuse both. |
| **R-W1-103** | **MAJOR** | A profile-gated service is dropped from the plan **before** the pin rule runs, so an **unpinned** image passes the gate. **The pin rule must see every service the plan considers**, or the exclusion must happen after the pin check. The reviewer identified the exact ordering that lets an unpinned image through — fix that ordering, and add the test that would have caught it. |
| R-W1-104 | MINOR | The success message and the `--list` output both claim the plan is the whole image set, which is false when a service was skipped. **Say what was skipped**, or stop claiming completeness. |
| R-W1-105 | MINOR | `profiles: []` over-excludes: Compose starts the service, the script does not fetch it. Fix the semantics. |
| R-W1-106 | MINOR | The record (ADR-0030's amendment) says Compose *replaces* a `profiles:` key when a later file declares it; it **concatenates** the sequences. **The ADR must be true** — check it against `docker compose config` and write down what Compose actually does. |
| R-W1-107 | NIT | The refusal corpus tests one spelling per refused shape; each of the two hole-making shapes has more than one spelling. Add the spellings. |
| R-W1-213 | MINOR | The deployment refusals assert "non-zero", not a **code**. Assert the specific exit code. |

## 4. Constraints

* **A refusal must be loud.** A shape the reader does not understand must
  produce a **non-zero exit** and a message that names the file and the shape
  — never a silently wrong plan. This is the whole point of the change, and
  it is the failure the reviewer is hunting.
* **Do not widen the reader into a YAML engine.** You are implementing a
  documented *subset* with loud refusals for everything else. The ADR already
  says so; keep it true.
* **The existing test structure is load-bearing.** The script and the test
  must keep using **independent** readers — a shared reader makes the test
  tautological. The stub `docker` on the test's PATH must answer any new
  subcommand the script uses.
* **The test must scrub the developer's environment** — `PAA_FETCH_*`,
  `PAA_COMPOSE_FILE`, `PAA_IMAGE_MIRROR`, `STUB_*` and **any new variable you
  introduce** — so a developer's shell cannot change what the test exercises.
* **Do not rename `minio_backend.py`**, do not touch the MinIO alias, do not
  change the digest-pinning rule (it is the decision ADR-0030 exists for).
* **The gate must not regress.** `ruff` 0, `mypy src` 0, the full suite green
  — the orchestrator runs the full suite; you run your scoped files.
* **No new dependencies**, no `pyproject.toml` change, no `src/paa/**` change.

## 5. Ownership

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
— they read the compose files you may change. **Read them; do not edit them.**
If your change breaks one, post `FINDING` and report it (it is not yours to
fix, and the fix may be in a file you do not own).

**FORBIDDEN:** everything else — `src/paa/**`, `tests/storage/**`,
`tests/conventions/**`, `pyproject.toml`, `docs/adr/README.md`,
`docs/plans/**`, `docs/MCP_AND_PLUGINS.md`, `AGENTS.md`, `docs/adr/0031-*`,
`docs/adr/0032-*`.

## 6. Siblings in this wave

`W1-IMPR-01` (the blob-substrate findings: `src/paa/storage/coldlake/**`,
`tests/storage/{test_blob_errors,test_minio_backend,test_blobstore_contract}.py`,
`docs/adr/0032-*`) and `W1-IMPR-03` (the import-lock findings). **No overlap
with your write-set.** You cannot see their uncommitted work.

## 7. Environment and tests

* **Scoped commands only:**
  ```bash
  .venv/bin/python -m pytest tests/deployment -q
  .venv/bin/python -m ruff check tests/deployment
  bash -n deploy/fetch-images.sh
  ```
* **Never** the full suite. **Never** `docker compose up`, never a container
  restart, never a prune — the deployment stack (`paa-stack-*`) belongs to
  the operator and is **not yours to touch**. Your tests use a stub `docker`
  binary and never talk to a daemon.
* The reviewer used `docker compose config` (v5.6.0) **read-only**, on copies
  under `/tmp`, to check the merge semantics. That is allowed and is the right
  way to settle a question about Compose's behaviour — **but never against the
  repository's compose files in place**, and never with a `docker compose up`.
* Kill any process you start.

## 8. Deliverables

* **Report:** `docs/agent-ops/20261022-w6-w7-w9/reports/w1-improve/W1-IMPR-02-T-003.md`
  — using `templates/report-improvement.md`. **Create it early and append as
  you work.** Section 1 is the **finding-by-finding disposition table**.
* **Commits:** code first, then the report in a **separate docs-only commit**
  listing the code commit's SHA. Trailers: `Task: T-003`, `Agent: W1-IMPR-02`,
  `Wave: 1`, `Report: <path>`. Explicit paths only:
  ```bash
  test "$(git branch --show-current)" = "ops/20261022-w6-w7-w9" || { echo "wrong branch"; exit 1; }
  git add -- deploy/fetch-images.sh tests/deployment/test_fetch_images.py …
  git commit -m "<msg>" -- deploy/fetch-images.sh tests/deployment/test_fetch_images.py …
  ```
* **Final message (≤ 8 lines):** `STATUS | agent | task | report path | commit SHAs | findings FIXED/REJECTED/DEFERRED | blockers`.

## 9. Notes

* **The failure mode you are fixing is the quiet one.** A script that fails
  loudly is a nuisance; a script that **succeeds** while having fetched the
  wrong images is the thing that costs an operator an afternoon. Every finding
  above is an instance of the second kind.
* **The ADR must be true when you are done.** If Compose's real behaviour
  differs from what the amendment says (R-W1-106), correct the ADR — with the
  evidence, and say how you checked it.
* **Answer the long-run test** in your report: Source / Load / Hostile /
  Operability / Reversal.
