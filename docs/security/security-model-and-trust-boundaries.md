# Security model and trust boundaries

This document describes PAA's security controls as implemented, and separates
**confirmed controls** (verified in source) from **known limitations** and
**unverified assumptions**. No secrets or credentials are included.

## Trust model

PAA is **single-user, single-machine, local-first**. There is no authentication,
no multi-tenancy, and no network-facing edge beyond the loopback ingestion API.
The threat model is expressed in RFC §17.6 and implemented as: **a model must
never decide whether an action is safe** — that decision is deterministic Python
(`agents/policy.py:7`). LLMs generate content; Python enforces control.

## Confirmed security controls

### 1. No model in the control path / security loop

- `PolicyRiskAgent` (`src/paa/agents/policy.py`) has **no model/provider
  dependency**. Every check is deterministic: set membership, regex, path
  arithmetic, cosine threshold (`policy.py:7`–`:11`).
- The critic runs deterministic validation first; the model's verdict can only
  *downgrade*, never upgrade, a failing check. A model saying PASS cannot
  overturn an AST rejection or failing test suite (`agents/reasoning.py:194`,
  `validation/engine.py:226`).
- `StructuredOutputError` and AST rejections are authoritative (`base.py`,
  `engine.py`).

### 2. Loopback-only ingestion

- `Settings.api_host` validator (`_enforce_loopback`, `config.py:409`) refuses
  non-loopback binds unless `PAA_ALLOW_NON_LOOPBACK=1`. This is a **startup
  check**, not a deployment note (`api/app.py:11`).
- Docker publishes no host ports for Postgres/Redis/Qdrant/the object store; only the
  ingestion API on `127.0.0.1:8787` (`AGENTS.md:132`).

### 3. Tamper-evident ledger

- Every event commits to its predecessor's digest (`prev_hash` → `event_hash`);
  altering any historical row invalidates every digest after it
  (`ledger/events.py:105`). Verified by `LedgerStore.verify_chain`
  (`ledger/store.py:315`).
- Ledger events are immutable (`LedgerEvent` is frozen pydantic, `events.py:
  132`).
- The ledger is append-only; there is no delete operation on events
  (`data/data-lifecycle.md`).

### 4. Ledger-before-side-effect

- Every orchestrator phase writes its event before the work it describes
  (`agents/orchestrator.py:9`, `runtime.py` docstring). A crash between event
  and work is recoverable; the reverse is not, and that ordering is chosen
  deliberately (`AGENTS.md:36`).

### 5. Sandbox containment (honestly declared)

`IsolationLevel` (`sandbox/base.py:54`) ranks backends honestly:

| Level | Backend | Real containment? | Stops |
|---|---|---|---|
| NONE (0) | DryRun | No | Nothing executes |
| PROCESS (1) | Subprocess | **No** | CPU/wall-clock, memory, accidental writes outside workspace |
| NAMESPACE (2) | WSL, Docker | Yes (process-level) | Plus filesystem visibility, (with `--network=none`) network |
| VM (3) | Docker + runsc/gVisor | Yes (syscall boundary) | Syscall interception — **not available on target hardware** |

`is_real_containment` is true only at `NAMESPACE` and above (`base.py:93`).
The subprocess backend runs as the **same OS user** with the same network stack;
it is "a guard against a confused agent, not a hostile one" (`base.py:26`).

### 6. Workspace jail

`resolve_workspace` / `assert_inside_workspace` resolve paths (collapse `..`,
follow symlinks) and confine writes to one directory (`sandbox/base.py:282`,
`:305`). Comparison on resolved paths defeats `..`/symlink escapes.

### 7. Environment allowlist (no ambient secret leakage)

`build_child_env` builds the child environment from empty; it never inherits
`os.environ` by copy-and-delete. Only minimal OS bootstrap keys (`PATH`,
`SystemRoot`, ...) plus the caller's explicit `spec.env` allowlist
(`sandbox/base.py:337`). `SandboxSpec._reject_ambient_env_smell` trips on
`env=dict(os.environ)` (`base.py:170`).

### 8. AST pre-scan (deny-by-default)

`AstSecurityScanner` (`validation/ast_scanner.py`) statically rejects dangerous
Python before execution:
- Denied modules (subprocess, ctypes, socket/urllib/requests/httpx, boto3,
  pickle, importlib, os.system/popen, shutil.rmtree, eval/exec/compile, …)
  (`ast_scanner.py:122`–`:244`).
- Unparseable source → CRITICAL finding, **never skipped** — a scanner that
  skips unreadable source gives an attacker a bypass (`ast_scanner.py:342`).
- NUL byte, `SyntaxError`, `RecursionError`, `MemoryError` → CRITICAL
  (`ast_scanner.py`).

### 9. Deterministic validation (binary verdict)

`DeterministicValidationEngine` (`validation/engine.py`): schema → AST → patch
dry-run → workspace drift → tests (runs last, inside sandbox). Binary PASS/FAIL;
short-circuits on CRITICAL or structural failure; never acts on "mostly valid"
(`engine.py:10`).

### 10. Policy checks (defence in depth)

`PolicyRiskAgent` (`agents/policy.py`):
- Permissions: every declared `required_permissions` must be granted by the
  active `PermissionMode`; unknown permission → block (`policy.py:174`).
- LOCKDOWN: network patterns and `NET_EGRESS`/`EXTERNAL_WRITE` declarations
  blocked regardless of what the plan declares (`policy.py:194`).
- SAFE: irreversible operations (`rm -rf`, `DROP TABLE`, `git push --force`,
  …) hard-blocked (`policy.py:213`, `_IRREVERSIBLE_PATTERNS`).
- Paths: forbidden write roots (`/etc`, `/usr`, `C:\Windows`, …) and `..`
  traversal blocked (`policy.py:222`).
- Operator rules (regex) from `hot_serving_policy_rules`; malformed rules
  logged and skipped (fail open on a *single* rule, not the whole gate)
  (`policy.py:243`).
- Anti-goals: cosine similarity ≥ 0.82 (vector) or phrase match (fallback)
  blocks the task (`policy.py:262`).
- Risk score [0,1] decides human-gate need; `always_gate_risk_profile=0.90`
  (`config.py:224`).

### 11. Delegation safety

`DelegationGraph` per correlation enforces depth, cycle, and node bounds before
the callee runs (`agents/delegation.py:87`). The wait graph is a DAG — no cycle
of waiters (`agents/base.py:20`). `DelegationRegistry` discards finished
correlations (`delegation.py:167`).

### 12. Secret hygiene

- Model provider errors and exceptions redact secrets via `redact()`
  (`models/base.py:139`, patterns for `sk-`, Bearer, `api_key=`).
- `CompletionResponse.to_payload()` excludes generated text (`base.py:283`).
- Skills never receive secrets; they emit a `get_secret` system call resolved by
  the host `SecretBroker` after checking `PERM_SECRET_READ`
  (`skills/secrets.py:304`). Plaintext exists only in the host process for one
  call; `SecretValue.__str__`/`__repr__` return `"***"` (`secrets.py:106`).
- `SecretBroker.lease()` invalidates the handle on exit
  (`secrets.py:503`).
- Config stores secret *names*, never values (`config.py:97`).

### 13. Sandbox output capture cap

`max_capture_bytes` (default 8 MiB) bounds stdout/stderr captured from a
sandbox to prevent RAM exhaustion through the pipe; output beyond the cap is
dropped but the pipe is still drained (`sandbox/base.py:274`, `subprocess_
backend.py` `_read_capped`).

### 14. Human gates cannot be auto-cleared

`AWAITING_HUMAN_ATTESTATION` parks a task; recovery explicitly never clears it
(`replay.py:62`, `recovery.py:274`). Only an external caller
(`clear_human_gate`, `orchestrator.py:688`) can approve/reject.

## Trust boundaries

| Boundary | Enforcement | Verified? |
|---|---|---|
| Host ↔ sandbox | Workspace jail, env allowlist, AST scan, memory/timeout caps | Yes (sandbox/) |
| Agent ↔ model | `ModelLike` protocol; router mediates escalation | Yes (models/) |
| Runtime ↔ external network | Default-deny network (`allow_network=False`); LOCKDOWN severance | Partially (subprocess cannot enforce network; policy + AST scan cover it) |
| User ↔ API | Loopback bind enforced at startup | Yes (config.py:409) |
| Ledger ↔ tampering | Hash chain, verify_chain | Yes (ledger/) |
| Skill ↔ secrets | Bound SecretProxy; PERM_SECRET_READ check | Yes (skills/secrets.py) |
| Operator ↔ policy rules | Regex rules in DB; malformed rules skipped | Yes (policy.py:234) |

## Known limitations (not vulnerabilities — candid disclosure)

- **Subprocess sandbox is not real containment.** On the target (Windows) host,
  the default backend is `PROCESS`: same kernel, same user, same network stack.
  A determined adversary with arbitrary code execution defeats it. Use WSL2 or
  Docker (NAMESPACE) for untrusted code (`README.md:181`, `sandbox/base.py:25`).
- **Network egress on subprocess is not severed.** `allow_network=False` is
  logged as a gap; enforcement is via the `PERM_NET_EGRESS` permission and the
  AST scanner's socket/urllib/requests patterns (`subprocess_backend.py`,
  `policy.py:_NET_PATTERNS`).
- **No authentication or authorization beyond the single permission mode.** Any
  process on the loopback can call the API; there is no user model
  (`README.md:175`).
- **No TLS on the ingestion API.** Loopback only; no transport security
  configured (`AGENTS.md:132`).
- **Observability is untested.** Metrics/tracing code exists but is never
  validated (`AGENTS.md:97`); security-relevant events are logged via structlog
  but not verified to be emitted in all paths.
- **Provider API keys in `.env`** are read by `dotenv_value` without
  `os.environ`, but the process environment may still contain them if exported
  by the user (`config.py:460`).
- **`EvalProvider`/`EchoProvider` cannot drive a plan** — its minimal-schema
  output isn't a plan; this is a correctness property, not a security control
  (`AGENTS.md:111`).

## Unverified / inference areas

- The `hot_serving_policy_rules` table and operator rule editing path exist in
  schema but no UI/API for managing rules was found in `src/paa/api/` — rule
  management mechanism is unverified (may be direct DB or a planned feature).
- `marketplace_packages.signature_state` and signing exist (`marketplace/
  signing.py`), but end-to-end signed-package installation through the runtime
  was not traced beyond the installer module.

## Related documents

- [`backend/modules-and-services.md`](../backend/modules-and-services.md) — policy, sandbox, validation internals.
- [`modules/sandbox.md`](../modules/sandbox.md), [`modules/validation.md`](../modules/validation.md) — deep dives.
- [`operations/configuration-and-environment.md`](../operations/configuration-and-environment.md) — `PAA_POLICY__*`, `PAA_SANDBOX__*`, `PAA_API_HOST`.
- [`architecture/component-interactions.md`](../architecture/component-interactions.md) — trust-boundary diagram.
