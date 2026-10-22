# Brief — W2-IMPL-04 (implementer) · wave 2 · run 20261022-w6-w7-w9

## 1. Role and mission

**Mission:** make `src/paa/models/` trustworthy by test — and **close a leak
of the API key** that the ROADMAP calls "the privacy promise".

**Why this task matters:** the ROADMAP says of this item: *"The escalation
boundary is the privacy promise. Assert it, don't hope."* The current state
is that the promise is **broken**, and the leak was reproduced:

```
StructuredOutputError.schema_errors is stored UNREDACTED   (models/base.py:108)
PaaError.__str__ renders details verbatim                    (core/errors.py:46-50)
PaaError.to_payload() merges details verbatim into the payload
router.py:623 copies str(local_error) into the MODEL_ESCALATED ledger payload
router.py:664-668 splats the same payload into the model.escalated log line
```

The same exception **redacts** its `raw_excerpt` and does **not** redact its
`schema_errors` — and `validate_against_schema` puts the model's *own output*
into those messages, which is exactly where a key can appear. The local-only
path was given `_redact_detail`; **the escalation path never was**.

**What failure looks like:** a test that asserts "no key in the logs" against
the path that already redacts, while the leaking path stays untested.

## 2. Required reading

1. `docs/agent-ops/20261022-w6-w7-w9/briefs/W2-CONTEXT.md` — **read first**.
2. `docs/agent-ops/20261022-w6-w7-w9/templates/STANDING-ORDERS.md` — binding.
3. `docs/plans/w6-w7-w9.md` — **§4 (0.3)** (your task, clause by clause), **§0**
   (the evidence).
4. `src/paa/models/base.py` — `ModelProvider`, `redact`, `StructuredOutputError`,
   `ModelUnavailableError`, `validate_against_schema`, `minimal_instance_for_schema`.
5. `src/paa/models/router.py` — `EscalatingModelRouter`: `block_reasons`,
   `evaluate`, `_route`, `_local_only`, `_record_escalation`, `_invoke` (the
   semaphore), `token_usage`.
6. `src/paa/models/echo_provider.py` — **the spy you should use**: it records
   `calls` and `call_count`, and `queue_failures()` makes it fail.
7. `tests/models/conftest.py` — **`assert_no_key_leak(*blobs, key=FAKE_KEY)`**,
   the reusable assertion. **Do not write a fourth variant of it.**
8. `tests/models/test_router_policy.py` and `test_escalation_audit.py` — the
   existing fakes (`_RaisingProvider`, `_RaisingStructuredProvider`,
   `_FrontierProbe`) and the established assertions.
9. `docs/adr/0021-transport-retry.md` — the ADR that says the
   `MODEL_ESCALATED` payload is unchanged *by the retry work*. Closing the
   leak is **not** a regression against it; it is a new decision.
10. `docs/agent-ops/20261022-w6-w7-w9/templates/report-implementation.md`.

## 3. The task

**T-007.** The key leaks on the escalation path; the semaphore and the
LOCKDOWN spy are unasserted.

### The four ROADMAP clauses, and what each needs

| Clause | State today | What you add |
|---|---|---|
| LOCKDOWN never escalates (**spy provider, zero frontier calls**) | the **policy** is covered (`test_router_policy.py:145`) — but every one of those is a **pure function call**. **No test anywhere makes a `complete()` under LOCKDOWN and observes the frontier provider.** | a **spy**: the frontier's `complete()` must be called **zero** times, the local's **exactly once**, and `force_escalate=True` must **not** bypass LOCKDOWN. And **zero `MODEL_ESCALATED` events** in the ledger. Use `EchoProvider` as the spy (it records `calls` / `call_count`) — and note that the router's `healthcheck()` calls the frontier **unconditionally** with no permission gate, so cover it too or say why not |
| escalation writes **exactly one** `MODEL_ESCALATED` event | the count is covered (`test_escalation_audit.py:55`), and the discriminator makes repeated escalations distinct | the **defect**: the discriminator is `f"escalation:{self._escalation_seq}"` and that counter is **per router instance** and never persisted — so a **restarted** router in the same correlation re-uses `escalation:1` and the ledger's idempotency key **silently suppresses** the second audit record. The audit trail under-counts across a restart. Decide the fix: a correlation-scoped discriminator (derived from the ledger's own `state_version`, or a persisted counter), and make the "exactly one per escalation" claim true **across a restart** |
| the **semaphore** caps concurrency | only the **configured number** is asserted (`test_settings_resolution.py:101`). **No test observes concurrency at all.** | a spy provider that records its **in-flight** count; `gather` N calls with N > the cap; assert `max_in_flight == 2` — **== the cap, not `<=`**: `<=` passes with a broken semaphore that serialises everything. And the cap is **one semaphore for the whole router**, so it gates the local and the frontier tiers alike — pin that too |
| **no API key** ever appears in logs or exceptions | **partial, and the leak is live** | the **fix** (redact `schema_errors`; apply the same treatment to the escalation payload as the local-only path already has), and the **test** that fails before it: a provider raising `StructuredOutputError` whose `schema_errors` contain the key ⟹ the key must be absent from the exception's `str()`, its `details`, the `MODEL_ESCALATED` payload, and **every** captured log line |

### The two other things the plan's evidence found

* `EscalatingModelRouter.healthcheck()` calls `await self._frontier.healthcheck()`
  with **no permission-mode gate** — and `OpenAICompatibleProvider.healthcheck()`
  issues a **real HTTP GET** carrying the `Authorization` header. It has no
  caller in `src/` today, so the vector is dormant. **Report it**; fix it only
  if the fix is small and obvious, and say which you did.
* `ModelProvider.__repr__` is untested; `EchoProvider`'s determinism claim
  (*"Nothing here reads a clock, generates a random number, or touches a
  socket"*) is untested; `token_usage()` / `reset_usage()` are untested.

## 4. Acceptance criteria (measurable)

* The **leak** is closed: a key inside `schema_errors` is absent from the
  exception's `str()`, from `details`, from the `MODEL_ESCALATED` payload and
  from every captured log line. **The test must fail against the pre-fix
  code** — prove it, and put the transcript in the report.
* The **spy** test asserts the frontier's call count is **zero** under
  LOCKDOWN (for every modality, and with `force_escalate=True`), the local's
  is exactly one, and the ledger holds no `MODEL_ESCALATED`.
* The **semaphore** test asserts `max_in_flight == the cap`, with N > the cap
  in flight.
* The escalation counter survives a **router restart** in the same
  correlation, and the test says what the discriminator is now.
* `ruff` 0, `mypy src` 0, and the wave-1 gate must not regress.
* **Commands that must pass (scoped):**
  ```bash
  .venv/bin/python -m pytest tests/models -q
  .venv/bin/python -m ruff check src/paa/models tests/models
  .venv/bin/python -m mypy src/paa/models
  ```

## 5. Ownership

**WRITE-SET (you may edit exactly these):**

```
src/paa/models/router.py
src/paa/models/base.py
tests/models/__init__.py
tests/models/conftest.py
tests/models/test_router_policy.py
tests/models/test_escalation_audit.py
tests/models/test_router_lockdown.py      (new)
tests/models/test_router_concurrency.py   (new)
tests/models/test_key_secrecy.py          (new)
```

**READ-ONLY:** `src/paa/models/{registry,_http,echo_provider,embeddings,__init__}.py`
and the other `src/paa/models/*.py` — **read them, do not edit them**. The
providers are not yours; if a test proves a defect in one, report it as a
`FINDING` with the reproduction.

**FORBIDDEN:** `src/paa/**` outside `models/{router,base}.py`,
`src/paa/core/errors.py` (the `PaaError.__str__` / `to_payload` behaviour is
**read-only** for you — if the fix needs a change there, post a `NEED` with
the exact change; the redaction belongs at the model boundary, and the
`_redact_detail` helper already exists there), `tests/**` outside
`tests/models/`, `pyproject.toml`, `deploy/**`, `docs/**` (if you believe the
redaction policy needs an ADR, `ESCALATE` and explain — do not write one
unasked), `docs/adr/README.md`, `docs/plans/**`, `AGENTS.md`.

## 6. Environment

* Scoped checks only. **Never** the full suite, never a container, never a
  port, never a network call to a real provider. Every provider test in this
  repo uses `httpx.MockTransport` — keep it that way.
* The repo's logging is **structlog**, and `caplog` is used **nowhere** — use
  `structlog.testing.capture_logs()`, which the existing tests already do.
* `assert_no_key_leak(*blobs, key=FAKE_KEY)` in `tests/models/conftest.py` is
  the house assertion — **use it**, and extend it only if you need a case it
  does not cover.
* Kill any process you start.

## 7. Deliverables

* **Report:** `docs/agent-ops/20261022-w6-w7-w9/reports/w2-impl/W2-IMPL-04-T-007.md`
  — `templates/report-implementation.md`, ORCH-DIGEST first, appended as you
  work.
* **Commits:** code first, then the report in a separate docs-only commit.
  Trailers: `Task: T-007`, `Agent: W2-IMPL-04`, `Wave: 2`, `Report: <path>`.
  Explicit paths only:
  ```bash
  test "$(git branch --show-current)" = "ops/20261022-w6-w7-w9" || { echo "wrong branch"; exit 1; }
  git add -- src/paa/models/router.py src/paa/models/base.py tests/models/
  git commit -m "<msg>" -- src/paa/models/router.py src/paa/models/base.py tests/models/
  ```
* **Final message (≤ 8 lines):** `STATUS | agent | task | report path | commit SHAs | blockers`.
  **It must not be empty.**
