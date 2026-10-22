# Brief — W1-REV-02 (code reviewer) · wave 1 · run 20261022-w6-w7-w9

## 1. Role and mission

**Mission:** audit the wave-1 **deployment** and **import-consistency** changes
for correctness and contract soundness — and try to break them.

**Your lens:** **correctness and design**, on the two smaller clusters. You are
the reviewer who asks "does this hold under conditions the tests do not reach?"

**Your role:** code reviewer. **Read-only on source.** You never fix; the
improvement step is a different agent. You write only your report.

## 2. Required reading

1. `docs/agent-ops/20261022-w6-w7-w9/briefs/W1-REVIEW-CONTEXT.md` — **the
   shared context: the wave, the diff range, the gate, and the claims to
   audit.** Read it first.
2. `docs/agent-ops/20261022-w6-w7-w9/templates/STANDING-ORDERS.md` — binding.
3. `docs/agent-ops/20261022-w6-w7-w9/templates/review-code.md` — **your report
   template.** Use it.
4. `AGENTS.md` — the house rules.
5. `docs/plans/w6-w7-w9.md` — §0 (the evidence), §2.2 (T-003's design and the
   G1–G8 gap table), §3.2 (T-004's design), §6 (the decisions, which are
   *given*).
6. `docs/adr/0030-deployment-image-contract.md` — the decision T-003 amends.
7. `docs/adr/0026-s3-cold-lake-substrate.md` — background only.
8. The diff: `git diff 02f3636..3ac9d6b -- . ':!docs/agent-ops'`
   (T-003: `954705d` + `db99c66`; T-004: `7747fa0`)

## 3. Your task

**The order matters: blind pass first.**

1. **Blind pass.** Read the diff and the surrounding code. Form your own
   findings **before** you read any implementer report.
2. **Claims audit.** Now read the implementer reports (paths in the shared
   context) and check **every** T-003 and T-004 claim. An unsupported claim is a
   finding.
3. **Try to break it.** Concrete failing inputs, in a temp worktree if you need
   one — **not** the shared tree.
4. **The long-run test**, answered independently.
5. **The optimisation pass**, with `DO NOW` / `DO LATER` / `NOT WORTH IT`.

**Specifically, and at minimum:**

* **T-003 — the merge.** The script now reads several compose files. Ask:
  * Is the merge *order* right, and is "later wins" true for **every** key it
    merges (a service's `image:` and a service's `build:` are different keys —
    what happens when file A has `image:` and file B has `build:` for the
    **same** service)?
  * Does the merge **refuse** what it does not support — `extends:`, anchors,
    aliases, a multi-line `image:`, a custom tag, a top-level `include:`? Is
    the refusal loud (non-zero exit, a message naming the file and the
    shape)?
  * Is the digest pinning still enforced for **every** image the merged stack
    runs — including an image that appears **only** in the override, and
    including a service that has `image:` in one file and `build:` in another?
  * Does the flag actually beat `PAA_COMPOSE_FILE`, and does the script still
    work when the flag is absent (the documented single-file path)?
  * Is the refusal for a `profiles:`-gated service the **right** one? A
    profile-gated service that is off by default is not what the deployment
    runs — but the script's job is to fetch what a run *could* run. Say which
    you think is right and why.
  * The parity test now **derives** the expected service set. Check that the
    derivation is not circular — that the test would still fail if the script
    and the compose file disagreed.
* **T-004 — the import conversion.** Ask:
  * Is the conversion safe? Did it trade a lazy import for an eager one that
    costs import time, or that pulls in an optional dependency the module
    deliberately avoided?
  * Does the fresh-interpreter check cover the modules it claims, and would it
    actually catch a cycle?
  * Are the two locks **real regression tests** — do they fail when the change
    is reverted? (The implementer says they tested the revert by hand twice.)
  * **The plan's evidence is claimed to be wrong.** The implementer says only
    **one** `TYPE_CHECKING` block in the tree is load-bearing for a real
    runtime cycle, not nine. **Verify that measurement** — the plan's §0 and
    §3.2 carry the wrong number, and the records must be corrected.
  * The implementer found a **new** defect it could not fix:
    `storage/relational/__init__.py:42-43` keeps `PostgresDatabase` and
    `Settings` behind `TYPE_CHECKING` while `get_database`'s return annotation
    names them, so `typing.get_type_hints(get_database)` raises `NameError`.
    **Is that a real defect?** What should be done about it, and by whom?
  * Did the conversion make `mypy` see *more* or *less*? (The gate is
    `mypy src` = 0 errors; the implementer reports `mypy src tests` = 184
    errors, the same as the pre-change baseline. Confirm the "same as
    baseline" claim rather than taking it.)

## 4. Ownership

**You may write exactly one file:** `docs/agent-ops/20261022-w6-w7-w9/reports/w1-review/W1-REV-02-code.md`
(and append to `COMMS.md`, which the orchestrator commits).

**READ-ONLY:** everything else, including all source. **You never fix.**

## 5. Deliverables

* **Report:** `reports/w1-review/W1-REV-02-code.md`, using
  `templates/review-code.md`. The ORCH-DIGEST block first.
* **Findings** with an ID (`R-W1-…`; keep yours distinct from REV-01's — use
  `R-W1-1xx` if you need to), severity, `file:line`, evidence, why it
  matters, and a recommended fix **at the root-cause level**.
* **COMMS:** at most 6 lines.
* **Final message (≤ 8 lines):** `STATUS | agent | scope | report path | verdict | findings by severity | blockers`.

## 6. Notes

* **A finding you cannot evidence is not a finding.**
* **"Missing tests for a behaviour change" is an automatic MAJOR.**
* The deployment script is the thing an operator runs on a bad day. The
  failure mode that matters most is not a crash — it is a script that
  **reports success** while having fetched the wrong thing. Look for that.
