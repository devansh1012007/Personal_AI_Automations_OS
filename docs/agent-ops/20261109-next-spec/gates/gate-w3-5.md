# Gate — Real-model rung — 2026-11-10

- HEAD: 9005a3e · Scope: WSL2 live `paa agent --workspace` against the
  operator's real OpenRouter key, fixture `examples/fixtures/buggy_calc`,
  throwaway `PAA_HOME=/home/devansh/.paa-s1`.
- Result: **SUCCESS** — live run under the free frontier model
  `nvidia/nemotron-3-ultra-550b:a55b:free` (tier FRONTIER) exited 0, fixed
  the fixture, re-ran the suite green, ledger chain ok; later paid-model
  attempts the same window were **BLOCKED by the provider** (HTTP 403, key
  limit exceeded).

| Step | Result |
|---|---|
| live run, free frontier model `nvidia/nemotron-3-ultra-550b:a55b:free` | exit 0; fix applied (`validation.patch.applied files=1 patch_sha256=53de650bf588`); suite green (5 passed); 12 tool calls, 0 denied, `stopped_reason=model_finished`; mandate `MANDATE_GRANTED`, chain ok; session `1063b64cece64dfcaa4dd7d004e03e74` |
| live attempts, paid models | exit 1 at provider call; clean CLI error render; mandate `MANDATE_GRANTED`; chain ok; session id printed |
| workspace side effects | fix applied in throwaway `/tmp/buggy_calc`; agent did not stage/commit |
| escalation arm | unavailable (`PAA_MODELS__ESCALATION_PROVIDER=none`; modality STANDARD < COMPLEX floor) — not needed on the successful run |

Conclusion: the real-model rung **passed** — `paa agent --workspace` fixed
the `buggy_calc` failing test under a live frontier model, with the ledger
evidence holding. The fix itself stays proven by the dry-run rung (47/47,
real bash) and the scripted N2.3 recovery chaos suite.

Follow-up: paid-model attempts were provider-blocked (key limit); the
escalation arm was not exercised on the successful run.
