# Module: Sandbox (`src/paa/sandbox/`)

## Module identity

- **Name:** Sandbox
- **Path:** `src/paa/sandbox/`
- **Main responsibility:** Pluggable containment for agent-executed steps; honest declaration of what containment each backend actually delivers.
- **Architectural layer:** Control / security
- **Entrypoints:** `get_sandbox(settings)`, `Sandbox.run(spec)`, `Sandbox.healthcheck()`, `RecursionGuard.register()`
- **Public interfaces:** `IsolationLevel`, `SandboxSpec`, `SandboxResult`, `Sandbox` ABC, `resolve_workspace`, `assert_inside_workspace`, `build_child_env` (`base.py`); `SubprocessSandbox`, `WslSandbox`, `DockerSandbox`, `DryRunSandbox` (backends); `RecursionGuard` (`recursion.py`); `ResourceWatchdog`, `HeartbeatTracker` (`watchdog.py`)
- **Other modules that depend on it:** `agents/reasoning.py` (WorkerCell), `validation/engine.py` (test runner), `runtime.py` (`_acquire_sandbox`), `daemon.py` (indirectly), `skills/adapters/` (Claw-Hub runs entrypoints in sandbox)

## Purpose and responsibilities

Containment with honesty: `IsolationLevel` (NONE/PROCESS/NAMESPACE/VM) is
declared per-backend, never aspirational (`base.py:54`). gVisor (`runsc`, VM)
is unavailable on the target Windows machine, so containment is pluggable and
callers can branch on `isolation_level.is_real_containment` (`base.py:93`).
The sandbox is "a guard against a confused agent, not a hostile one" for the
subprocess backend (`base.py:26`).

## Internal implementation

### `base.py` — contract
- `SandboxSpec`: command (tuple, never a shell string), workspace_path,
  read_only_mounts, env (explicit allowlist), memory_mb, cpu_cores,
  timeout_seconds, allow_network (default False), recursion_depth,
  parent_task_id. `from_profile()` builds limits from `MODALITY_PROFILES`
  (`:190`). `_reject_ambient_env_smell` trips on `env=dict(os.environ)`
  (`:170`).
- `SandboxResult`: total outcome — a killed run still produces a result, not an
  exception (`:217`). `ok`, `to_payload()` (ledger-safe, stdout/stderr capped
  at 2000 chars).
- `resolve_workspace()` / `assert_inside_workspace()`: resolve paths (collapse
  `..`, follow symlinks) then jail (`:282`, `:305`).
- `build_child_env()`: builds from empty; never inherits `os.environ`;
  Windows bootstrap keys inherited by value only when present (`:337`).

### Backends
- `SubprocessSandbox` — PROCESS isolation. Windows Job Object memory caps +
  kill-on-job-close; POSIX `ResourceWatchdog` sampled RSS; `asyncio.wait_for`
  timeout; output capped at `max_capture_bytes`; network NOT enforced (policy
  + AST scanner cover it).
- `WslSandbox` — NAMESPACE via WSL2 (`ulimit -v/-t/-c 0`, `unshare` when
  available).
- `DockerSandbox` — NAMESPACE (VM if `runsc` registered); `--read-only`,
  tmpfs, cap-drop ALL, no-new-privileges, pids-limit, cgroups.
- `DryRunSandbox` — NONE; records what would run, executes nothing (planning
  mode + tests).

### `__init__.py` — factory
`get_sandbox(settings)`: explicit backend returned without healthcheck
(operator wants loud failure); `"auto"` walks order (docker, wsl, subprocess)
and takes first healthy; unreachable fallback raises `RuntimeError`
(subprocess always healthy) (`:98`).

### `_acquire_sandbox` (`runtime.py:382`)
5s probe timeout (`asyncio.wait_for(..., timeout=5.0)`) then fallback to
subprocess — a cold WSL VM can take 10–15s; boot must not wait.

### `recursion.py` — RecursionGuard
Depth, node, cycle bounds for delegation (RFC §11). Cycle detection via
iterative DFS (`would_cycle` searches target→source). `register()` atomic
under RLock (`:212`).

### `watchdog.py` — ResourceWatchdog + HeartbeatTracker
RSS sampling (psutil → Windows native → /proc → unavailable) with peak-RSS
enforcement (ADR-0009); heartbeat liveness with miss tolerance
(`:373`).

## Inputs and outputs

| Operation | Input | Output | Side effects |
|---|---|---|---|
| `Sandbox.run(spec)` | `SandboxSpec` | `SandboxResult` | spawns contained process |
| `Sandbox.healthcheck()` | — | bool | probes backend availability |
| `RecursionGuard.register(source, target, depth)` | edge | — (raises on breach) | records edge in graph |
| `build_child_env(spec)` | spec | `dict[str,str]` | none (pure) |

## Dependencies and connections

| Component | Relationship | Why | Source |
|---|---|---|---|
| `WorkerCell` → `Sandbox` | `run(spec)` | Execute step | `reasoning.py:456` |
| `PytestRunner` → `Sandbox` | `run(spec)` | Run tests | `validation/test_runner.py` |
| `Runtime._acquire_sandbox` → `get_sandbox` | select backend | Compose runtime | `runtime.py:394` |
| `ClawHubAdapter` → `Sandbox` | `run(SandboxSpec)` | Run skill entrypoint | `skills/adapters/claw_hub.py:435` |
| `RecursionGuard` → `MODALITY_PROFILES` | read ceilings | Depth/node bounds | `recursion.py:129` |

## Runtime behavior

- Backend selected at runtime build; worker reuses the same sandbox instance.
- `timeout_seconds=None` means "wait forever" (MAX modality, human-attested)
  (`base.py:141`).
- `allow_network=False` on subprocess is not enforced (logged gap); enforced by
  policy + AST scanner.

## Failure behavior

- Job Object creation failure → degrades to watchdog, never takes down the run
  (`subprocess_backend.py`).
- Process start failure → `SandboxError`.
- Docker/WSL unavailable → `healthcheck()` returns `False` (cached), never
  raises; `run()` raises `SandboxError` if unavailable.
- `sample_rss_mb` returns `(None, ...)` when no tier works — memory ceiling
  not enforced, run proceeds (`watchdog.py`).
- `_acquire_sandbox` probe timeout → subprocess fallback (`runtime.py:382`).

## Modification impact

- Adding a backend: implement `Sandbox` ABC, add to `build_backend`/`get_sandbox`
  (`__init__.py`).
- Changing `IsolationLevel` semantics affects callers branching on
  `is_real_containment`.
- Tests: `tests/sandbox/` (39 tests per README).

## Known gaps

- Subprocess sandbox cannot enforce `allow_network` or filesystem confinement
  beyond the cwd jail (`base.py:70`, `:130`).
- Windows Job Object assignment races `CreateProcess` (sub-millisecond window,
  documented).
- Six pre-existing test failures: sandbox runs `python` where only `python3`
  exists (README build-status table).

## Related documents

- [`security/security-model-and-trust-boundaries.md`](../security/security-model-and-trust-boundaries.md) — containment trust boundary.
- [`modules/agents.md`](agents.md) — WorkerCell.
- [`modules/validation.md`](validation.md) — PytestRunner uses sandbox.
- [`architecture/component-interactions.md`](../architecture/component-interactions.md) — sandbox degradation diagram.
- [`docs/adr/0006` gVisor note](../../README.md) (file absent; rationale in AGENTS.md/README)
- [`docs/adr/0009` peak RSS note](../../README.md)
