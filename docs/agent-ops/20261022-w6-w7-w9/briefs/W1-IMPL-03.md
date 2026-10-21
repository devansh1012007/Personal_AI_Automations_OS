# Brief — W1-IMPL-03 (implementer) · wave 1 · run 20261022-w6-w7-w9

## 1. Role and mission

**Mission:** make one family of eight modules express the same dependency the
same way — as a **runtime** import — and add a convention lock so the family
cannot drift apart again.

**Why this task matters:** commit `1c9ffa3` ("a substrate contract, a neutral
error vocabulary, and a migration list") changed the storage boundary from
`Database` to `RelationalBackend`. It made **one** module's import a runtime
import (`src/paa/memory/creator.py`) and left **eight** siblings with the same
dependency under `if TYPE_CHECKING:`. The result is a codebase where the same
contract is resolved two different ways in nine places that were changed in the
same commit — and the difference is invisible until a runtime `isinstance`
check, a pydantic model resolution, or an annotation evaluation needs the real
class. It is the W7 item "*`TYPE_CHECKING` stubs are left over from a change
that made the imports runtime ones*".

**What failure looks like:** a reviewer finds that the eight modules still
resolve the dependency lazily, or that the conversion created an import cycle
that only manifests in a specific import order.

**Your role:** implementer.

## 2. Required reading (in this order)

1. `docs/agent-ops/20261022-w6-w7-w9/templates/STANDING-ORDERS.md` — binding.
2. `AGENTS.md` — the gate rules, the docstring rule, and the rule that a
   deviation from the RFC needs an ADR (this is not one — it is a consistency
   fix — but say so in the report).
3. `docs/agent-ops/20261022-w6-w7-w9/00-mission.md` — commands, hotspots,
   baseline.
4. `docs/plans/w6-w7-w9.md` — **§0** (the rules), **§3.2** (your task), and
   **§7** ("what this run will not do" — the ~22 blocks you are *not* touching).
5. `src/paa/memory/creator.py` — the module that was made a **runtime**
   import. **Your eight modules must match it.**
6. The eight modules themselves:
   `src/paa/memory/contradiction.py`, `src/paa/memory/decay.py`,
   `src/paa/memory/curator.py`, `src/paa/marketplace/installer.py`,
   `src/paa/skills/registry.py`, `src/paa/agents/policy.py`,
   `src/paa/agents/context_agents.py`, and — **the eighth** —
   `src/paa/observability/runs.py`.
   ⚠ **`src/paa/observability/runs.py` is FORBIDDEN for you** — wave 2's
   observability task (T-008) owns it, and it converts that one block itself.
   **Do not touch it.** Seven files, not eight.
7. `src/paa/storage/relational/base.py` — the module the family imports. Check
   what *it* imports at module scope (it must be light: no `psycopg`, no
   `boto3`, no `fastapi`).
8. `src/paa/storage/relational/__init__.py` — note that importing
   `paa.storage.relational.base` **executes the package `__init__`**. Read it
   and know what that pulls in. This is the whole risk of the task.
9. `git show 1c9ffa3 --stat` and `git show 1c9ffa3 -- src/paa/memory/creator.py`
   — the commit that created the inconsistency. Quote it in your report.
10. `docs/agent-ops/20261022-w6-w7-w9/COMMS.md` — from the `WAVE-START` line.
11. `docs/agent-ops/20261022-w6-w7-w9/templates/report-implementation.md` — the
    report shape.

## 3. Task and acceptance criteria

**Task T-004.** One commit left eight siblings expressing the same dependency
two different ways.

**Root cause (verified in the plan's §3.2):** `1c9ffa3` made
`memory/creator.py` import `RelationalBackend` at runtime and left the other
eight under `if TYPE_CHECKING:`. The family is provably cycle-free —
`paa.storage.relational.base` has no heavy dependency at module scope — so the
laziness buys nothing and costs consistency.

**The recommended approach (you may deviate with evidence):**

For each of the **seven** files you own, move the `RelationalBackend` import
out of the `if TYPE_CHECKING:` block to a normal runtime import, matching
`creator.py`'s form. Keep the other members of each `TYPE_CHECKING` block
where they are if they are still needed for annotations — or move them too if
they are the same kind of dependency. **Read each file and decide per
import**, and say what you decided in the report.

Then add a **convention lock**: a test that asserts the family resolves the
dependency at runtime, so it cannot drift apart again. The house has a precedent
for a source-shape assertion — W4's
`test_the_script_carries_no_hand_copied_image_table` asserts on the *content*
of a script — so this is the established kind of test here, not an oddity.

**Acceptance criteria (measurable):**

* All **seven** modules import `RelationalBackend` at runtime.
* The convention lock **fails if the change is reverted** — the test is a real
  regression test, not a description.
* **Every module in `src/paa` imports cleanly in a fresh interpreter.** This is
  the acceptance criterion that catches the one failure mode that matters: an
  import cycle that the test session never sees, because the session's
  `sys.modules` is already populated. Check it by **subprocess**, e.g.
  `subprocess.run([sys.executable, "-c", "import paa.<module>"], …)` for every
  module under `src/paa`, in a **fresh** interpreter each time — or one
  interpreter that imports them in a deliberately awkward order.
* `ruff` 0 and `mypy` 0 across `src` and `tests` — the conversion changes what
  the type checker can see, and a conversion that makes mypy *less* able to
  check something is a regression.
* **The tests that must exist and fail before the fix:**
  * the convention lock (fails on the un-converted tree, passes after);
  * a fresh-interpreter import of every module under `src/paa` (fails if a
    cycle was created);
  * `mypy src` reports **0** errors — and if the conversion makes mypy see
    *more*, that is a finding to report, not a problem to hide.
* **Commands that must pass (scoped):**
  ```bash
  .venv/bin/python -m pytest tests/conventions -q
  .venv/bin/python -m pytest tests/memory tests/agents/test_policy.py tests/agents/test_context_agents.py tests/skills tests/marketplace -q
  .venv/bin/python -m ruff check src tests
  .venv/bin/python -m mypy src
  ```
  The `mypy src` run is scoped by the *file set* it covers, but the **whole**
  `src` must be checked — a conversion in `src/paa/agents/policy.py` can
  change what mypy sees in `src/paa/runtime.py`.

**What NOT to do.** Do **not** convert the other ~22 `TYPE_CHECKING` blocks.
Nine are load-bearing (real runtime cycles) and nine are load-bearing for
another reason (optional extras — `fastapi`, `psycopg`, `boto3` — or
deliberate laziness: `paa.runtime` must not be imported by a CLI that prints
its version). The plan's §3.2 and §7 say the rest are **deferred with the
cycle proof**. Your report should state which blocks you found load-bearing
and why, so the deferral is on the record with evidence — and so a later
reader does not mistake "vestigial" for "safe to convert".

**One more thing to check and report:** `src/paa/storage/coldlake/__init__.py:38`
has a vestigial `TYPE_CHECKING` block (`paa.config.StorageSettings`) that you
**may not** touch — W1-IMPL-01 owns that file in this wave. **Do not edit it.**
Note it in your report as a deferred item.

## 4. Ownership

**WRITE-SET (you may edit exactly these):**

```
src/paa/memory/contradiction.py
src/paa/memory/decay.py
src/paa/memory/curator.py
src/paa/marketplace/installer.py
src/paa/skills/registry.py
src/paa/agents/policy.py
src/paa/agents/context_agents.py
tests/conventions/__init__.py                   (new)
tests/conventions/test_import_conventions.py    (new)
```

**READ-ONLY:** `src/paa/memory/creator.py` (the model — **do not edit it**),
`src/paa/storage/relational/**`, `src/paa/config.py`, `src/paa/observability/**`
(T-008 owns it in wave 2 — **do not edit `runs.py`**).

**FORBIDDEN:** everything else. In particular
`src/paa/storage/coldlake/**` (W1-IMPL-01 owns it in this wave — including
`coldlake/__init__.py`), `src/paa/observability/runs.py`, `src/paa/agents/{base,orchestrator,reasoning,delegation}.py` (wave 2's T-006 owns those),
`src/paa/models/**`, `src/paa/validation/**`, `src/paa/marketplace/{registry_client,package,signing}.py`
(T-011 owns `registry_client.py` in wave 2), `deploy/**`, `docker-compose*.yml`,
`docs/**` (no ADR is needed for a consistency fix — **say so in your report**
rather than writing one; if you think one is needed, `ESCALATE` and explain).

**Need something outside?** Do not edit it. Post `NEED` in `COMMS.md`, finish
the safe part, and report `DEFERRED-DEPENDENCY` with the exact change required.

## 5. Parallel siblings this wave

| Agent | Task | Owns | Contracts it changes |
|---|---|---|---|
| `W1-IMPL-01` | T-001 — the blob error vocabulary | `src/paa/storage/coldlake/**`, `tests/storage/{test_blob_errors,test_minio_backend,test_blobstore_contract}.py`, `docs/adr/0032-*` | the `BlobStore` error clause |
| `W1-IMPL-02` | T-003 — the fetch-images ↔ compose parity | `deploy/fetch-images.sh`, `tests/deployment/*`, `docker-compose*.yml`, `docs/adr/0030-*`, `docs/deployment/docker.md`, `.env.example` | the script's CLI and env surface |

**Known interactions — this one matters for you.** W1-IMPL-01 adds a **new
module** `src/paa/storage/coldlake/errors.py` and imports it at runtime from
`minio_backend.py` and `cas.py`. **The import graph is changing under you.**
That is exactly why the fresh-interpreter acceptance criterion exists: your
check must run against a tree that includes their work, or it must be
re-runnable after they land. **Run your fresh-interpreter check again at the
end of your work, after the wave settles, and report what you saw** — or
explicitly report that you could not and why. You cannot see their
uncommitted work.

## 6. Skip list and known traps

* **Already fixed — do not redo:** the `BlobStore` signature mismatch (fixed in
  `03f635a`); the `minio` → `s3` rename (ADR-0026; the alias and the filename
  are deliberate).
* **False leads:** "the `TYPE_CHECKING` blocks are all removable" — **nine are
  not.** The ones that are load-bearing for a *cycle*:
  `storage/coldlake/cas.py` (the `base`↔`cas` cycle — `base.py` imports `cas`
  for `BlobRef`), `memory/creator.py` (the `paa.memory.__init__` →
  `contradiction` cycle), `skills/adapters/{base,native,claw_hub}.py` and
  `skills/usa.py` (the `paa.skills.__init__` → `adapters.base` cycle), and
  `observability/tracing.py` (the `paa.observability.__init__` → `metrics`
  cycle). The ones that are load-bearing for an **optional extra** or a
  deliberate laziness: `api/app.py` (`fastapi`), `cli/main.py` and
  `coder/agent.py` and `daemon.py` (`paa.runtime`),
  `storage/relational/__init__.py` (`psycopg`), `models/embeddings.py`.
  **Do not touch any of these.** Your report should record the classification
  with the reason.
* **Traps:**
  * A module's `if TYPE_CHECKING:` block may hold **more than** the
    `RelationalBackend` import, and some of the other members may be
    **load-bearing for a different reason** — e.g. a `TYPE_CHECKING` import of
    a module that pulls in `numpy` or `sentence-transformers`. **Read each
    block; move only what is safe.**
  * `src/paa/agents/policy.py` and `src/paa/agents/context_agents.py` import
    `RelationalBackend` **for annotations only** — but pydantic models
    resolve annotations at class-creation time, and `AgentContext` and
    friends are pydantic models. Check whether the annotation resolution
    changes when the import becomes real.
  * A cycle can be introduced by importing a module whose **package
    `__init__`** imports back. `paa.storage.relational.__init__` imports
    `paa.config` and lazily `postgres`; `paa.memory.__init__` imports
    `contradiction`. **Read the package `__init__` of every package you
    touch** before you make the import real.
  * `tests/conventions/` does not exist yet — you create it, with an
    `__init__.py` (the other test packages have one).
* **Flaky / pre-existing failures:** none. The suite is green at the base SHA.
* **Do not run the full suite.** The gate is the orchestrator's job.

## 7. Environment and tests

* **Scoped commands only** (the ones in §3). Never the full suite, never a
  container restart, never a migration, never a fixed port.
* Your fresh-interpreter check is the one check that legitimately needs to
  enumerate `src/paa` — do it with a **subprocess**, not by importing in the
  test session.
* No environment variable, port, container or database is needed.
* Kill any process you start.

## 8. Deliverables

* **Report:** `docs/agent-ops/20261022-w6-w7-w9/reports/w1-impl/W1-IMPL-03-T-004.md`
  — using `templates/report-implementation.md`. **Create it at the start and
  append as you work.**
* **Commits:** code first, then the report in a **separate docs-only commit**
  that lists the code commit's SHA. Trailers: `Task: T-004`, `Agent: W1-IMPL-03`,
  `Wave: 1`, `Report: <path>`. Explicit paths only:
  ```bash
  test "$(git branch --show-current)" = "ops/20261022-w6-w7-w9" || { echo "wrong branch"; exit 1; }
  git add -- src/paa/memory/contradiction.py src/paa/memory/decay.py …
  git commit -m "<msg>" -- src/paa/memory/contradiction.py src/paa/memory/decay.py …
  ```
* **Final message (≤ 8 lines):** `STATUS | agent | task | report path | commit SHAs | blockers`.

## 9. Role-specific notes

* **Answer the long-run test** (section 8 of the standing orders) in your
  report: Source / Load / Hostile / Operability / Reversal. For this task the
  *Reversal* answer is: **which test fails if the conversion is reverted?**
  Name it, and show that it does fail — the convention lock is the answer.
* **This is a small, surgical change.** It is tempting to tidy the other ~22
  blocks while you are here. **Do not.** The deferral is recorded in the plan,
  and a reviewer will check that you did not exceed the write-set.
* **The report is more valuable than the diff here.** The classification of
  the blocks you did *not* touch — with the reason for each — is the part that
  saves the next reader the analysis.
