# Brief — W3-IMPL-03 (implementer) · wave 3 · run 20261022-w6-w7-w9

**Task T-011 — the marketplace's untrusted HTTP body.** The remote registry
chooses the local exception type.

## The defect (the plan's §3.4, and the recon that found it)

`src/paa/marketplace/registry_client.py`'s `HttpRegistry` fetches a remote
body and lets the **server** decide which local exception is raised. The
recon reproduced it: a malformed body produces an `AttributeError` or a
`TypeError` from **inside** the client's own code — not one of the project's
errors. So:

* the caller cannot tell a malformed remote body from a bug in the client,
  because the exception type is whatever the payload happened to provoke;
* the **server's text** ends up in the exception's message and details, and
  from there in a log line and possibly in a ledger payload — an untrusted
  body writing into the project's logs.

The plan's requirement, verbatim:

> The remote body is untrusted: a malformed body must produce **the project's
> own error**, with the server's text **bounded** and **never interpolated
> into a message that reaches a log**.

And the reason it is in this run: *"It is also the largest attack surface in
the design, per the ROADMAP — worth closing now rather than after a
storefront exists."*

## What to do

1. **One error type for a malformed remote body**, from the project's own
   vocabulary. Read `src/paa/core/errors.py` and use the error the house
   style would use — not a bare `ValueError`, and not the server's choice.
2. **Bound the server's text.** Say what "bounded" means concretely: a
   maximum length, and — the part that matters — the text must not reach a
   **message** that reaches a log unescaped. A remote string is data; a log
   line is a format. Decide where the remote text is allowed to appear (a
   bounded, quoted detail?) and where it is not, and **write the rule down**.
3. **Every** path that reads the body is covered: the success path's parse, the
   error path's error body, a body that is not JSON, a body that is JSON but
   not the expected shape, a body that is empty, and a body that is enormous.
   Enumerate them from the code — do not fix the one the recon happened to
   hit.
4. **The status code** is the server's too. A 500 with a valid JSON body is
   still a failure, and it must fail with the project's error.

## What to test

`tests/marketplace/` — the project's error for each malformed shape, the
**bounded** text (a long body is truncated, and the truncation is visible in
the test, not implied), and the guarantee that the remote text does **not**
reach a log line unescaped. If the client logs the remote body, the test
must show what reaches the log.

**Do not mock away the thing under test.** The HTTP call itself should be
real-ish (the repo's pattern is `httpx.MockTransport` — read
`tests/models/` for the house style); what is under test is the client's
handling of the body, not the socket.

## Constraints

* **Never** reach the network: no live registry, no live HTTP. `httpx.MockTransport`
  only.
* The fix must not change the client's **contract** for callers in a way that
  breaks them silently — if an existing caller catches the old exception
  types, find them and say what happens to them. The repo's grep is your
  friend; report what you find.
* Root cause, not symptom: the client parses a body it has not validated.

## Ownership

**WRITE-SET:** `src/paa/marketplace/registry_client.py`,
`src/paa/marketplace/*.py` (only if another module in the package does the
same parsing — and say so), `tests/marketplace/*.py`.

**READ-ONLY:** `src/paa/core/errors.py` (use its vocabulary; if the error
you need does not exist and creating one is the honest fix, propose it in
your report and **do not** edit `core/errors.py` — it is not yours),
`src/paa/marketplace/installer.py` (a wave-1 file), `docs/ROADMAP.md` (it
names this as the largest attack surface — read it for the context).

**FORBIDDEN:** `src/paa/**` outside `marketplace/`, `src/paa/core/**`,
`tests/**` outside `tests/marketplace/`, `pyproject.toml`, `deploy/**`,
`docs/**` (no ADR is needed for a fix of this kind — say so in your report;
if you think one is, `ESCALATE` and explain), `docs/adr/**`, `docs/plans/**`,
`AGENTS.md`.

## Acceptance criteria

* A malformed remote body raises **the project's own error**, on every path
  that reads a body.
* The server's text is **bounded**, and the rule for where it may appear is
  stated in the code.
* The remote text does not reach a log line unescaped — with a test that
  shows exactly what does.
* Each malformed shape has a test that fails without the fix.
* The callers that catch the old exception types are identified, and what
  happens to them is stated.
* Scoped: `.venv/bin/python -m pytest tests/marketplace -q`,
  `ruff check src/paa/marketplace tests/marketplace`, `mypy src/paa/marketplace`
  — green, and the gate must not regress.

## Deliverables

Report `docs/agent-ops/20261022-w6-w7-w9/reports/w3-impl/W3-IMPL-03-T-011.md`
(ORCH-DIGEST first). Commit code first, then the report, docs-only, explicit
paths, on branch `ops/20261022-w6-w7-w9`, trailers `Task: T-011`,
`Agent: W3-IMPL-03`, `Wave: 3`.
Final message ≤ 8 lines: `STATUS | agent | task | report path | commit SHAs | blockers`.
