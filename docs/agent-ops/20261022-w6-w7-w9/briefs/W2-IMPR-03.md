# Brief — W2-IMPR-03 (fixer) · wave 2 improve · run 20261022-w6-w7-w9

**Task:** fix **R-W2-203**, **F-4** and **F-7** of the wave-2 reviews.
Read all three in full before you start:

* `docs/agent-ops/20261022-w6-w7-w9/reports/w2-review/W2-REV-02-tests.md`
  ⟹ **R-W2-203** (MAJOR, the key-leak assertion).
* `docs/agent-ops/20261022-w6-w7-w9/reports/w2-review/W2-REV-01-code.md`
  ⟹ **F-4** (MINOR, the escalation ordinal) and **F-7** (MINOR, the
  LOCKDOWN-safe healthcheck).

**You are the fixer**, not the original implementer.

## R-W2-203 — the claim is stronger than the truth (MAJOR)

`tests/models/test_key_secrecy.py` claims *"no API key ever appears in logs
or exceptions"*. The assertion looks at `str(error)` and `error.details`.
It does **not** look at the exception **chain**: the reviewer measured
`key in error.__cause__` ⟹ **True**, and the key is in the **formatted
traceback** — which is the surface an operator actually pastes into a bug
report. The cause comes from `router.py::_local_only`, which chains the
provider's own unscrubbed exception (`from exc`).

The reviewer's note: the chain is **pre-existing** (it dates from the first
commit) — wave 2 did not introduce it — but wave 2 is the wave that wrote the
file claiming the stronger property, and the wave that wrote the test that
cannot see the difference.

**Decide, and say which you chose and why:**

* **(a)** close it — the raised error must not carry an unscrubbed
  credential, whether in `__cause__`, in `__context__`, or in the rendered
  traceback; **or**
* **(b)** narrow the claim — the file says exactly what is guaranteed (no key
  reaches a log line, a ledger payload, or the raised error's own message and
  details) and says plainly that the provider's original exception is
  retained as the `__cause__` **for diagnosis**.

(a) and (b) cannot both be skipped; the current state is a claim stronger
than the truth, asserted by a test that cannot see the difference. If you
choose (b), the retention must be **deliberate and documented**, and the
house rule from `AGENTS.md` — *"no secrets in code or logs"* — must still
hold for the surfaces it covers.

**The test must be able to see the difference** either way: extend the
assertion to the chain (`assert_no_key_leak(str(error), error.details,
error.__cause__, getattr(error.__cause__, "details", None))`) and, if you
choose (a), also assert the rendered traceback is clean
(`traceback.format_exception`).

## F-4 — the escalation ordinal is a decorative extra read (MINOR)

`router.py:714` reads the per-correlation ordinal from the ledger to build
the escalation discriminator, and `:774` reads it again. It is an extra read
on the hot path and a new failure point. The reviewer also notes that under
concurrency two writers can both get ordinal 1 (both records survive, so it
is not a suppression risk — but it is noise).

Make it **one** read, or make it unnecessary; and make the concurrency case
**deterministic** rather than accidental. Say which and why. Do not make the
discriminator weaker than it is now — the ledger must not silently suppress
the second audit record, which is the defect wave 2 fixed.

## F-7 — the LOCKDOWN-safe healthcheck is opt-in (MINOR)

`router.py:266-296`: the healthcheck now refuses to probe the frontier under
LOCKDOWN, but it **defaults** to `ASK` and the mode is available at
construction (`Settings.policy.mode`) and **discarded**. So the safe
behaviour is opt-in by accident, and a router built with LOCKDOWN still
probes the frontier unless the caller passes the mode.

Make the router use **the mode it was given**. If the mode is not available
at construction, say so and choose the safe default explicitly — and write
down which.

## Also in your write-set (a record defect, not a code defect)

`docs/agent-ops/20261022-w6-w7-w9/reports/w2-imimpl/`… the wave-2 T-007
report's §7.4 claims a *"measured flakiness range"*; the reviewer found it
is **five green runs plus a duration range**, which is not a flake
measurement. Correct the wording in
`docs/agent-ops/20261022-w6-w7-w9/reports/w2-impl/W2-IMPL-04-T-007.md`
(or measure it properly — a flake rate needs more than five runs, and a
claim you cannot support must be narrowed to what you can).

## Acceptance criteria

* The key-leak claim matches the truth, and the test can **see** the
  difference it asserts.
* The escalation path performs **no redundant ledger read**, and the
  discriminator is no weaker than wave 2 made it.
* The router's healthcheck honours **the mode it was constructed with**.
* Scoped: `.venv/bin/python -m pytest tests/models -q`,
  `ruff check src/paa/models tests/models`, `mypy src/paa/models` — green,
  and the gate must not regress.
* **Five consecutive green runs** of `tests/models`, because you are touching
  a concurrency-sensitive path — and the report states the **actual** result.

## Ownership

**WRITE-SET:** `src/paa/models/router.py`, `tests/models/conftest.py`,
`tests/models/test_key_secrecy.py`, `tests/models/test_router_lockdown.py`,
`tests/models/test_escalation_audit.py`, `tests/models/test_router_concurrency.py`,
`docs/agent-ops/20261022-w6-w7-w9/reports/w2-impl/W2-IMPL-04-T-007.md`.

**READ-ONLY:** `src/paa/models/base.py` (another agent owns it this wave — do
not edit it), `src/paa/core/errors.py` (the `PaaError.__str__` /
`to_payload` behaviour is read-only; if the fix needs a change there, post a
`NEED` with the exact change).

**FORBIDDEN:** `src/paa/**` outside `models/router.py`, `tests/**` outside
`tests/models/`, `pyproject.toml`, `deploy/**`, `docs/adr/**` (no ADR is
needed — say so in your report), `docs/plans/**`, `AGENTS.md`.

## Deliverables

Report `docs/agent-ops/20261022-w6-w7-w9/reports/w2-improve/W2-IMPR-03-R-W2-203-F-4-F-7.md`
(ORCH-DIGEST first). Commit code first, then the report, docs-only, explicit
paths, on branch `ops/20261022-w6-w7-w9`, trailers `Task: R-W2-203,F-4,F-7`,
`Agent: W2-IMPR-03`, `Wave: 2-improve`.
Final message ≤ 8 lines: `STATUS | agent | findings | report path | commit SHAs | blockers`.
