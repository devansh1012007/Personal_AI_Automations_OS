# Brief — W1-IMPR-06 (improver, narrowly-scoped repair) · wave 1 · run 20261022-w6-w7-w9

## 1. Role and mission

**Mission:** fix one finding that a previous agent **claimed** to fix and did
not. The claim is in the record and it is false; this task makes the record
true.

**Why this task matters:** `deploy/fetch-images.sh` is the thing an operator
runs on a bad day. A service whose keys sit at three spaces is **silently
dropped**: its image is never digest-checked, the plan is not refused, and the
script exits 0 saying it fetched everything. That is the single failure mode
the whole T-003 improvement exists to prevent, and it is still open.

**Your role:** improver, narrowly scoped. One finding, one file pair, one
proof. You are the only agent running.

## 2. Required reading

1. `docs/agent-ops/20261022-w6-w7-w9/templates/STANDING-ORDERS.md` — binding.
2. **The finding:** `docs/agent-ops/20261022-w6-w7-w9/reports/w1-review/W1-DREV-04-delta.md`
   §4, **R-W1-301** — read it in full: the location, the evidence, the
   reproduction, and the recommended fix.
3. `deploy/fetch-images.sh` — the reader, the plan, the refusals.
4. `tests/deployment/test_fetch_images.py` — the corpus, the independent
   Python oracle (`_compose_rows`), and the helpers (`_tool_path`, `_run`,
   `_list_rows`).
5. `docs/adr/0030-deployment-image-contract.md` — the decision, and the
   amendment that records the refusal set.
6. `docs/agent-ops/20261022-w6-w7-w9/templates/report-improvement.md` — your
   report template.

## 3. The task

**Finding R-W1-301 (MAJOR), and the false fix.**

A previous agent claimed this FIXED. **It was not.** It verified the
**single-service** case, where the empty-plan refusal fires by accident:

```yaml
services:
   postgres:
     image: postgres:16@sha256:0000...
```
⟹ "no images found … there is nothing to fetch", exit 1 ✓ — but for the wrong
reason: the service was not *refused*, it was *not seen*, and the refusal came
from the empty plan.

The **mixed** case is the real one, and the orchestrator reproduced it against
the tree at `90eda75`:

```yaml
services:
  postgres:
    image: postgres:16@sha256:0000000000000000000000000000000000000000000000000000000000000000
   qdrant:
     image: qdrant/qdrant:v1.19.2@sha256:1111111111111111111111111111111111111111111111111111111111111111
```

(`qdrant:` at **three** spaces) ⟹

```
pull  postgres:16@sha256:0000...  (postgres)
exit=0
```

The three-space service is **silently dropped**: its image is never
digest-pinned, the plan is not refused, and the script reports success. The
delta reviewer had already written the correction down: *"The empty-plan
refusal is not a substitute: it only fires when the hidden service is the
only one."*

**The fix, as the reviewer specified it — implement it, do not improvise a
different rule:**

* The test for "the reader cannot see this service's keys" must **not** be
  "deeper than four". It must be "**no key at four spaces, but at least one
  key at some other indent**": track `saw_key_any_indent` alongside
  `saw_key`, and refuse when `!saw_key && saw_key_any_indent`.
* Implement it **once** and teach **both** readers the same rule — the script's
  awk reader and the independent Python oracle in
  `tests/deployment/test_fetch_images.py`. The two readers must keep
  **agreeing**, and the oracle must not share a bug with the script: that is
  the whole value of having two.
* Add the **three-space** spelling to the corpus, and add a test for the
  **mixed** case above — a normal service *and* a hidden one — not only the
  single-service case.
* **The refusal must be loud**: a non-zero exit and a message that names the
  file and the shape it could not read. Not the generic "no images found"
  message.
* While you are in the file: the corpus carries **one spelling per refused
  shape**, which is how a second spelling of the same hole was missed. Add
  the other spellings of this shape (the five-space spelling was added
  earlier; three was the one that was missed).

## 4. Acceptance criteria (measurable)

* The **mixed** case above: the script **refuses**, exits non-zero, and its
  message names the file and the shape. **Paste the command, the output and
  the exit code into your report — before and after.**
* The single-service case still refuses, with the **shape-specific** message
  (not the empty-plan one).
* A four-space compose file still lists its services and exits 0.
* The corpus holds the three-space spelling **and** a mixed case, and **both**
  readers refuse the same shapes.
* `pytest tests/deployment -q` green; `ruff check tests/deployment` clean;
  `bash -n deploy/fetch-images.sh` clean.
* `mypy src` still 0 (you touch no Python in `src/`).

## 5. Ownership

**WRITE-SET (you may edit exactly these):**

```
deploy/fetch-images.sh
tests/deployment/test_fetch_images.py
docs/adr/0030-deployment-image-contract.md
```

**READ-ONLY:** `tests/deployment/test_compose_config.py`,
`tests/deployment/test_compose_env.py`, `tests/deployment/test_daemon_config.py`
— read them; do not edit them. If your change breaks one, post `FINDING` and
report it.

**FORBIDDEN:** everything else — `src/paa/**`, `tests/storage/**`,
`tests/conventions/**`, `pyproject.toml`, `docker-compose*.yml`,
`.env.example`, `docs/deployment/docker.md`, `docs/adr/README.md`,
`docs/plans/**`, `docs/MCP_AND_PLUGINS.md`, `AGENTS.md`.

## 6. Environment

* Scoped commands only. **Never** the full suite, never `docker compose up`,
  never a container restart or prune — the `paa-stack-*` stack is the
  operator's. `docker compose config` may be used **read-only on copies under
  /tmp** to check Compose's behaviour; never against the repository's compose
  files in place.
* Kill any process you start.

## 7. Deliverables

* **Report:** `docs/agent-ops/20261022-w6-w7-w9/reports/w1-improve/W1-IMPR-06-repair.md`
  — `templates/report-improvement.md`, ORCH-DIGEST first, appended as you
  work. Section 1 is the finding-by-finding disposition table.
* **Commits:** code first, then the report in a separate docs-only commit.
  Trailers: `Task: T-003`, `Agent: W1-IMPR-06`, `Wave: 1`, `Report: <path>`.
  Explicit paths only:
  ```bash
  test "$(git branch --show-current)" = "ops/20261022-w6-w7-w9" || { echo "wrong branch"; exit 1; }
  git add -- deploy/fetch-images.sh tests/deployment/test_fetch_images.py
  git commit -m "<msg>" -- deploy/fetch-images.sh tests/deployment/test_fetch_images.py
  ```
* **Final message (≤ 8 lines):** `STATUS | agent | task | report path | commit SHAs | findings FIXED/REJECTED/DEFERRED | blockers`.

## 8. Notes

* **A claim is not a fix.** The previous agent's report says FIXED; the code
  says otherwise. Your report must state the *observed* behaviour, with the
  transcript, not a summary of the intent.
* **The two readers must agree.** If your fix makes the script refuse a shape
  the oracle accepts, the parity test fails — and that failure is the design
  working, not a nuisance.
