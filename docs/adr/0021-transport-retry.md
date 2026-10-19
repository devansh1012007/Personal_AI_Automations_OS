# ADR-0021: Transport retry for transient model-endpoint failures

## Status

Accepted. Correctness class — the configuration's retry story, read as
written, did not cover the network, and one transient failure ended runs it
should have absorbed.

## Context

`ModelSettings.max_retries` (default 2) sits under the "Timeouts, retries,
idempotency" heading of `docs/interfaces/integrations-and-communication.md`
next to `request_timeout_seconds`, which reads as transport protection. It is
not. The value is consumed only by `ModelProvider.complete_structured`
(`base.py:631-666`), the validate-and-repair loop that re-asks after a
*malformed structured answer* — and it can never fire on a transport failure,
because there is no answer to validate in the first place.

The transport itself was single-shot. `models/_http.py::_post_json` issued
exactly one POST and raised `ModelUnavailableError` on any failure: timeout,
connection reset, 429, 5xx. So one transient hiccup from a hosted gateway
during `paa agent` killed the whole run. The escalation router could not
absorb it either — escalation is a policy decision (`escalation_forbidden_modes`,
`escalate_at_or_above`, the permission mode), and a task in a mode that
forbids escalation has nowhere else to go. The transient failure became fatal
at precisely the moment the right move was to wait a moment and ask again.

A blanket retry would have been the wrong fix alongside the right one: HTTP
400/401/403 are deterministic — the same bytes come back — so retrying them
only turns a fast config error into a slow one, and a non-JSON body or a
non-object JSON value is an answer, not a transport event.

## Decision

- A separate knob, `ModelSettings.max_transport_retries` (default 2,
  `ge=0`), threaded through `registry.build_provider` — and through the
  `build_local_provider` / `build_frontier_provider` fast paths — into every
  provider constructor and stored on `HttpModelProvider`. It is the *only*
  knob of this ADR that comes from settings; the backoff pair below is a
  constructor argument and nothing threads it from configuration, so there is
  no `PAA_MODELS__*` setting to look for. `max_retries` stays what it
  was — structured-output repair only. Two failure classes, two budgets,
  because they answer different questions ("can this provider be reached
  right now" vs "can this provider follow instructions").
- The retry loop lives **inside the provider** (`_post_json`), not in the
  router. The retryable class is narrowed to `httpx.TimeoutException`,
  `httpx.HTTPError` (connection errors and the rest of the transport family),
  and HTTP status 408, 429 or >= 500. Every other 4xx, a non-JSON body and a
  JSON value that is not an object raise on the first response, with the same
  message, `status_code` and redacted `body_excerpt` they have always had.
- Backoff is `base * 2**attempt`, base 0.5 s, capped at 5 s; a `Retry-After`
  on 429 overrides it in either of its two legal forms — delta-seconds and an
  HTTP-date — and is clamped to 10 s in both, the date form included. A date
  already in the past means *no wait* (the retry is due), never a negative
  sleep. Base and cap are constructor arguments on `HttpModelProvider` and on
  every concrete provider, deliberately *not* settings: they are timing
  constants, not operational choices, and no construction path threads them
  from configuration. Tests set them to ~0 so retry coverage runs instantly.
- A wall-clock deadline bounds the whole loop. The request timeout bounds a
  *single attempt*; the loop's budget is that timeout times the number of
  attempts it may make, so the worst case is
  `request_timeout_seconds × (max_transport_retries + 1)`. Each attempt's
  timeout is clamped to the remaining budget, and a backoff that would leave
  no time for another attempt raises the last error instead of sleeping. The
  two quantities must stay distinct: an attempt that genuinely times out
  spends its whole timeout, so a deadline equal to that timeout would be
  spent by the first attempt — and the timeout half of the retryable class
  would be advertised and never actually retried.
- Each retry logs `model.http_retry` with provider, attempt, status code,
  delay and scrubbed URL — never the response body, because a rejection body
  can quote the request (and its key) back. The final raised error still
  carries the redacted `body_excerpt`.

## Consequences

- Worst-case latency for a provider that fails every attempt is bounded by
  `request_timeout_seconds × (max_transport_retries + 1)` — 360 s with the
  default 120 s timeout and two transport retries — and the backoff sleeps
  are spent *inside* that budget, not added on top of it. A flapping
  endpoint adds at most the capped schedule (0.5 + 1 + 2 ... up to 5 s per
  wait) before the same `ModelUnavailableError` the router already handles
  surfaces as before.
- Auth and configuration errors stay fast: non-transient 4xx are never
  retried, so a wrong key still fails on the first response with an
  unchanged message and `status_code`.
- Because the retry stays inside the provider, the router's escalation
  timing, audit trail and `MODEL_ESCALATED` payloads are unchanged — the
  router still sees exactly one `ModelUnavailableError`, only after the
  transport budget is genuinely exhausted. Escalation timing shifts by the
  retried attempts' elapsed time, which is the honest cost: the system waited
  before giving up, and the ledger's timestamps say so.
- `build_local_provider` / `build_frontier_provider` in
  `paa/models/__init__.py` construct the legacy `ollama`/`llamacpp`/`echo`/
  `anthropic` fast paths directly rather than through the registry, so they
  thread `max_transport_retries` from settings themselves. Every construction
  path — the registry rows and these fast paths — now honours one knob, and a
  non-default `max_transport_retries` can no longer diverge between them.
  Neither path threads the backoff pair: that stays a constructor argument,
  so the schedule is a property of the code, not of the configuration.
- Covered by `tests/models/test_http_retry.py`: the retryable class (500
  recovers, 400 and non-JSON do not retry), attempt counts against the
  budget, `Retry-After`, timeout and connection-error translation, the
  `model.http_retry` log line, and that redaction holds across retries. The
  budget itself is pinned as well: a timeout that consumes its attempt is
  still retried, the wall-clock bound stops the loop before the attempt count
  does, and both legal `Retry-After` forms are honoured — delta-seconds and
  HTTP-date, including a date already in the past.
