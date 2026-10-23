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
declared per-backend, never aspirational (`sandbox/base.py:58`). gVisor (`runsc`, VM)
is unavailable on the target Windows machine, so containment is pluggable and
callers can branch on `isolation_level.is_real_containment` (`sandbox/base.py:97`).
The sandbox is "a guard against a confused agent, not a hostile one" for the
subprocess backend (`sandbox/base.py:26`).

## Internal implementation

### `sandbox/base.py` — contract
- `SandboxSpec`: command (tuple, never a shell string), workspace_path,
  read_only_mounts, env (explicit allowlist), memory_mb, cpu_cores,
  timeout_seconds, allow_network (default False), recursion_depth,
  parent_task_id. `from_profile()` builds limits from `MODALITY_PROFILES`
  (`:356`). `_reject_ambient_env_smell` trips on `env=dict(os.environ)`
  (`:337`). Two fields carry containment decisions the caller makes
  explicitly:
  - `network` — the Docker network to attach to. `None` means the backend's
    default (`none` when the network is denied, the daemon's *default bridge*
    when it is allowed — which reaches the host and the internet, so it is
    more than "networked" implies).
  - `exec_tmpfs` — extra exec-capable scratch paths. `/tmp` is `noexec` on
    purpose; npm's cache holds executable shims, so an `npx`-started server
    fails with exit 126 on a noexec mount and gets its own path instead.
- `SandboxResult`: total outcome — a killed run still produces a result, not an
  exception (`:383`). `ok`, `to_payload()` (ledger-safe, stdout/stderr capped
  at 2000 chars). `duration_ms` is the **total** wall clock and keeps that
  meaning deliberately: it is the sandbox's *own* measurement of the two
  phases together, and the skill-ranking signal is a **different** field of
  the same name — the run record's own wall clock, read by
  `improvement/optimizer.py` from `hot_serving_execution_runs` and never from
  here. `start_duration_ms` reports the backend's start phase separately
  (ADR-0031). `killed_reason` distinguishes `"timeout"` (the *work* overran)
  from `"start_timeout"` (the *start* overran): the first is a workload
  problem, the second a host problem.
- `resolve_workspace()` / `assert_inside_workspace()`: resolve paths (collapse
  `..`, follow symlinks) then jail (`:495`, `:518`).
- `build_child_env()`: builds from empty; never inherits `os.environ`;
  Windows bootstrap keys inherited by value only when present (`:550`).
- `Sandbox.diagnose()`: async, returns the facts for `paa doctor`. The base
  implementation returns `describe()` — the backend's identity and its
  isolation level — because a backend that reports *nothing* renders as
  "none (dry-run)", which is false for every backend except the dry-run one:
  a subprocess backend really does run the workload, on the host, at
  `PROCESS` isolation. It must never raise: a diagnostic that throws is a
  diagnostic that lies.
- `collect_sandbox_facts(backend)`: the one collector both surfaces use, so
  a failed diagnostic is normalised identically by the CLI and the TUI.
- `sandbox_report_rows(facts)`: the one renderer both surfaces use, so the
  two cannot drift into disagreeing about the same host.
- `Sandbox.describe()`: sync identity — backend, isolation level, rank,
  `real_containment`. `DockerSandbox` overrides it to add the container
  facts (`container_runtime`, `runtime_available`, `image`, and the start
  budget it spends). `SubprocessSandbox` and `WslSandbox` override it to say
  that they have **no** start phase and therefore ignore
  `container_start_grace_seconds` — a setting only one backend honours must
  not be silently applied to the others (ADR-0031).

### Backends
- `SubprocessSandbox` — PROCESS isolation. Windows Job Object memory caps +
  kill-on-job-close; POSIX `ResourceWatchdog` sampled RSS; `asyncio.wait_for`
  timeout; output capped at `max_capture_bytes`; network NOT enforced (policy
  + AST scanner cover it).
- `WslSandbox` — NAMESPACE via WSL2 (`ulimit -v/-t/-c 0`, `unshare` when
  available).
- `DockerSandbox` — NAMESPACE (VM if `runsc` registered); `--read-only`,
  tmpfs, cap-drop ALL, no-new-privileges, pids-limit, cgroups.
  `runtime_available` is a **tri-state**: `True` / `False` / `None`
  ("could not determine" — the probe failed, which is not the same as "the
  daemon does not have it" and must not be reported as though it were).
  `diagnose()` probes the daemon and reports whether the requested runtime is
  actually registered, whether the daemon is reachable, and whether the image
  the sandbox would run is present. `--user` is derived from the workspace's
  owner, and the workspace path is translated through
  `SandboxSettings.host_path_map` because the daemon resolves bind mounts
  against the *host*.
- The container backend's **two budgets** (ADR-0031). `timeout_seconds` is the
  budget on the step's **work**, and `container_start_grace_seconds` is the
  budget on the container's **start**; they are enforced independently, because
  `docker run` is one opaque call and a backend that cannot see where the start
  ends and the work begins cannot separate them. The lifecycle is driven
  explicitly — `create` → `start` → `inspect` → `wait` → `logs` → `inspect` →
  `rm -f` — so the boundary is observable: `docker start` returns once the
  container's process is running, and `State.StartedAt` (the **daemon's**
  clock) is where the work's deadline is measured from. `--rm` is gone: the
  logs have to outlive the container, so the removal is an explicit
  `docker rm -f` in a `finally`. The container's fixed cost — the runtime's
  setup and teardown, ~200 ms, measured — is inside the lifetime and is
  charged to the caller, which is why the SIMPLE modality's budget was raised
  from 0.25 s to 1.0 s.
- `DryRunSandbox` — NONE; records what would run, executes nothing (planning
  mode + tests).

### `sandbox/__init__.py` — factory
`get_sandbox(settings)`: explicit backend returned without healthcheck
(operator wants loud failure); `"auto"` walks order (docker, wsl, subprocess)
and takes first healthy; unreachable fallback raises `RuntimeError`
(subprocess always healthy) (`:98`).

### `_acquire_sandbox` (`runtime.py:420`)
A bounded probe (`asyncio.wait_for(..., timeout=_SANDBOX_PROBE_BOUND_SECONDS)`,
30 s) then fallback to subprocess. The bound is deliberately **longer** than the
Docker backend's own probe timeout (20 s), so the inner probe is what gives out
first and it can kill the daemon child it started — a cancelled wait would
leave that child running. The bound still exists so a wedged daemon cannot
stall the boot indefinitely.

The acquired backend is stored on the runtime as `Runtime.sandbox`, so the
diagnostic surface can ask the sandbox what containment it would deliver
instead of reaching into the worker's private `_sandbox`.

### `recursion.py` — RecursionGuard
Depth, node, cycle bounds for delegation (RFC §11). Cycle detection via
iterative DFS (`would_cycle` searches target→source). `register()` atomic
under RLock (`:212`).

### `watchdog.py` — ResourceWatchdog + HeartbeatTracker
RSS sampling (psutil → Windows native → /proc → unavailable) with peak-RSS
enforcement (ADR-0009); heartbeat liveness with miss tolerance
(`watchdog.py:374`).

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
| `Runtime._acquire_sandbox` → `get_sandbox` | select backend | Compose runtime | `runtime.py:420` |
| `ClawHubAdapter` → `Sandbox` | `run(SandboxSpec)` | Run skill entrypoint | `skills/adapters/claw_hub.py:458` |
| `RecursionGuard` → `MODALITY_PROFILES` | read ceilings | Depth/node bounds | `recursion.py:137` |

## Runtime behavior

- Backend selected at runtime build; worker reuses the same sandbox instance.
- `timeout_seconds=None` means "wait forever" (MAX modality, human-attested)
  (`sandbox/base.py:227`).
- `allow_network=False` on subprocess is not enforced (logged gap); enforced by
  policy + AST scanner.
- The **start** and the **work** are separate budgets, and only the container
  backend has a start phase (ADR-0031). The subprocess and WSL backends ignore
  `container_start_grace_seconds` entirely rather than adding it to something,
  and say so in their own report — the subprocess backend in both its
  `preflight` and its `describe()`, the WSL backend in its `describe()`.

## Failure behavior

- Job Object creation failure → degrades to watchdog, never takes down the run
  (`subprocess_backend.py`).
- Process start failure → `SandboxError`.
- Docker/WSL unavailable → `healthcheck()` returns `False` (cached), never
  raises; `run()` raises `SandboxError` if unavailable.
- Container start overran the grace → `killed_reason="start_timeout"` and
  `timed_out=False`: the container is killed and the run is **not** a work
  timeout (ADR-0031).
- The container is removed (`docker rm -f`) on every path out of the lifecycle,
  including the timeout and the start breach — a container left running holds
  the workspace bind mount.
- A `docker inspect` that does not answer inside its budget degrades the same
  way a daemon that cannot be reached does: the run continues and the deadline
  falls back to the client's own start-of-work clock. A *report* that cannot be
  made must not destroy the work it was reporting on — and the pre-work
  inspect's budget is the start grace, so tightening the grace raises the
  chance of hitting it (ADR-0031).
- A work budget that is already spent when the wait begins is logged with the
  **clock** named and the measured lag between the daemon's `StartedAt` and the
  client's own clock. A daemon whose clock runs behind the host's shortens the
  budget; that errs safe, but it must not read as a workload fault (ADR-0031).
- `sample_rss_mb` returns `(None, ...)` when no tier works — memory ceiling
  not enforced, run proceeds (`watchdog.py`).
- `_acquire_sandbox` probe timeout → subprocess fallback (`runtime.py:441`).

## Modification impact

- Adding a backend: implement `Sandbox` ABC, add to `build_backend`/`get_sandbox`
  (`__init__.py`).
- Changing `IsolationLevel` semantics affects callers branching on
  `is_real_containment`.
- Tests: `tests/sandbox/` (175 tests).

## Known gaps

- Subprocess sandbox cannot enforce `allow_network` or filesystem confinement
  beyond the cwd jail (`sandbox/base.py:70`, `sandbox/base.py:518`).
- Windows Job Object assignment races `CreateProcess` (sub-millisecond window,
  documented).
- Six pre-existing test failures: sandbox runs `python` where only `python3`
  exists (README build-status table).

## Related documents

- [`security/security-model-and-trust-boundaries.md`](../security/security-model-and-trust-boundaries.md) — containment trust boundary.
- [`modules/agents.md`](agents.md) — WorkerCell.
- [`modules/validation.md`](validation.md) — PytestRunner uses sandbox.
- [`architecture/component-interactions.md`](../architecture/component-interactions.md) — sandbox degradation diagram.
- [`docs/adr/0031` the container's start and the work's budget](../adr/0031-container-start-and-work-budgets.md) — the two budgets, measured.
- [`docs/adr/0006` gVisor note](../../README.md) (file absent; rationale in AGENTS.md/README)
- [`docs/adr/0009` peak RSS note](../../README.md)
