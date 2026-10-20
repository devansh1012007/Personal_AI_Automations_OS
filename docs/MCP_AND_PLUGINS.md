# MCP & Plugins — containment-first design plan

**Status:** proposal. Decisions 1–7 are **taken** (see §7); the remaining questions are
listed there. **Phase 0 is implemented and verified**, except for the three host steps
that need `sudo` — registering `runsc` with the Docker daemon, restarting it, and
measuring the cgroup ceiling under it. See the status note in §4.
**Scope:** give the runtime an open-ended tool surface (MCP servers + user plugins) where
**every third-party tool execution happens inside a gVisor (runsc) container**, and the
host is never the thing that runs it.

**Decisions taken (2026-10-20):**

| # | Decision |
|---|---|
| 1 | A new first-class `src/paa/tools/` layer (not the skill registry). |
| 2 | The `paa` daemon container gets `/var/run/docker.sock` + the docker CLI, and the daemon itself runs under `runtime: runsc`. Accepted cost: the socket is host root. See §3.14. |
| 3 | Tool invocations go on the **ledger** (`TOOL_INVOKED` / `TOOL_COMPLETED` / `TOOL_FAILED`), including a recovery rule for an unresolved invocation. |
| 4 | In-process Python plugins are **allowed**, with a **rollback model** designed up front (§3.12). |
| 5 | Network egress is a **per-plugin opt-in**, confirmed at install time. |
| 6 | gVisor is the **official `release-20261012.0` static binary**, not Ubuntu's older apt package. |
| 7 | **HTTP-transport MCP is in scope**, with its own containment model (§3.13). |

---

## 0. The one-paragraph answer

Today PAA has a working MCP client (`src/paa/skills/adapters/mcp.py`) that **spawns the MCP
server as a host process** — same user, same filesystem, full environment — and a `Sandbox`
contract (`src/paa/sandbox/base.py`) that is **one-shot** (`run(spec) -> SandboxResult`), so
it structurally cannot express a long-lived stdio server. Both facts have to change before a
single third-party tool can be called safely.

The plan therefore has one load-bearing refactor and one load-bearing decision:

1. **Refactor:** add a *live session* primitive to the sandbox contract
   (`SandboxSession` + `Sandbox.start()`), implemented on `DockerSandbox` as
   `docker run -i --runtime=runsc ...` with stdin/stdout piped. That single change turns
   "run a command" into "run a contained process I can talk to", and it is what makes MCP
   containment possible at all.
2. **Decision:** the runtime grows a first-class **tool provider layer** (`src/paa/tools/`)
   that owns discovery, naming, mutability classification, capability declarations and
   dispatch — for MCP servers (stdio **and** HTTP transports), for plugin-provided exec
   tools, and for trusted in-process Python plugins.

Everything else in this document is plumbing around those two.

**Where each decision landed:**

| Topic | Section |
|---|---|
| The ten containment layers, and what is *not* contained | §2 |
| The tool provider layer, the registry, the manifest, the two-axis permission model | §3.1–§3.11 |
| In-process plugins and the rollback model | §3.12 |
| HTTP-transport MCP and its different containment model | §3.13 |
| The daemon under gVisor, holding the Docker socket | §3.14 |
| The phased delivery plan | §4 |
| Decisions taken and still open | §7 |

---

## 1. What exists today (honest inventory)

| Piece | State | Where |
|---|---|---|
| MCP protocol client (JSON-RPC 2.0 over stdio, both framings, robust to crash/timeout/bad JSON) | **works, tested** | `src/paa/skills/adapters/mcp.py` (501 lines) |
| MCP server → `SkillContract` mapping | **works** | `mcp.py:359-397` |
| MCP server process launch | **host process, unsandboxed, full env inherited** | `mcp.py:102-110` — `asyncio.create_subprocess_exec(*cmd, stdin=PIPE, stdout=PIPE, stderr=PIPE)`; no cwd, no env allowlist, no timeout, no memory cap, no network control |
| The claim that this is contained | **false** | `mcp.py:408-412` — *"an MCP server is its own subprocess and provides its own isolation boundary, so there is nothing to mount here."* A host subprocess provides no boundary at all. |
| Sandbox contract | **one-shot only** | `src/paa/sandbox/base.py:389-435` — `Sandbox.run(spec) -> SandboxResult` |
| Docker + gVisor backend | **implemented, honest** | `src/paa/sandbox/docker_backend.py` — adds `--runtime runsc` only when the daemon reports it; reports `IsolationLevel.VM` only then, else `NAMESPACE` |
| gVisor container hardening | **already good** | `--read-only`, `--tmpfs /tmp`, `--memory`+`--memory-swap`, `--cpus`, `--pids-limit`, `--cap-drop ALL`, `--security-opt no-new-privileges`, `--network none` |
| `runsc` on this host | **installed, NOT registered** | `~/.local/bin/runsc` → `runsc version release-20261012.0` (official release, checksum-verified). The daemon does not know about it: it is not at `/usr/local/bin/runsc` and `/etc/docker/daemon.json` does not name it, so the daemon reports only `runc`. Registering it needs `sudo` — one of the three root-gated steps at the end of §4. |
| Docker daemon | **works, `runc` only** | `docker info` → runtimes: `io.containerd.runc.v2`, `runc` |
| The worker image `paa/base_worker:v4.1` | **built in Phase 0** | `deploy/worker/Dockerfile` — the image `DockerSandbox` launches for a plan step. It never existed before: it was a *string* in `docker_backend.py:59`, `ledger/events.py:166` and the two SQL schemas, with no Dockerfile behind it. |
| The toolbox image `paa/toolbox:v1` | **built in Phase 0** | `deploy/toolbox/Dockerfile` — the image MCP servers and plugin commands run in. Carries Node 24, because most published MCP servers are npm packages started with `npx`. |
| The `paa` daemon image | **can launch containers as of Phase 0** | the Docker client is installed (Docker's official static build, pinned by SHA-256), `/var/run/docker.sock` is mounted, and the socket's GID is added to the container user via `group_add` + `DOCKER_GID`. |
| Skill registry (DB-backed, reliability, permissions) | **implemented but never constructed** | `src/paa/skills/{registry,usa}.py` — no call site outside `src/paa/skills/` |
| Coder tool protocol (`name`/`description`/`schema()`/`run(args) -> str`) | **works, live, tested** | `src/paa/coder/loop.py:85-97` |
| Tool permission policy (allow/deny/ask globs, PLAN refuses mutating) | **works** | `src/paa/coder/permissions.py` |
| `AgentLoop` gate order (hook → permission → confirm → execute → hook) | **works** | `src/paa/coder/loop.py:200-255` |
| Runtime tool catalogue (9 tools) | **works** | `src/paa/coder/runtime_tools.py:85-107` |
| Ledger / replay / recovery | **works** | `src/paa/ledger/` |

### The three findings that shape everything

1. **MCP support already exists and is completely unwired.** `McpServerAdapter`,
   `UnifiedSkillAdapter`, `SkillRegistry` are never constructed anywhere in `src/paa/` outside
   `src/paa/skills/`. There is no call site. So this is not "build MCP from scratch" — it is
   "build the *host boundary* MCP needs, then wire it".
2. **The `Sandbox` ABC cannot express a conversation.** `run()` returns a *total* outcome
   (`SandboxResult`) and closes stdin (`stdin=asyncio.subprocess.DEVNULL` in the Docker
   backend, `docker_backend.py:316`). An MCP stdio server is a duplex pipe held open for the
   life of a session. **This is the single hardest blocker and the reason the plan leads
   with a sandbox refactor rather than with a tool registry.**
3. **gVisor is not actually deployed here.** `runsc` is installed but not
   *registered* with the Docker daemon, so the daemon reports only `runc` and
   the runtime honestly says `NAMESPACE`. The daemon container could not
   spawn containers at all: the runtime image carried no Docker client and
   no socket was mounted. The "we use gVisor" property was *aspirational* in
   three separate places. Phase 0 fixes that before any code depends on it —
   except for the root-gated steps that need `sudo`, which are listed at the end of
   §4.

---

## 2. The containment model

This is the answer to "how do we make sure tool executions from MCP and plugins stay in our
container only". It is a stack of ten layers. Each is enforced on the **host** side, and each
is a place where a mistake is a hole.

### L0 — Placement: there is exactly one way to start a third-party process

> **Invariant:** no MCP server and no plugin process is ever started on the host. The only
> spawn path is `Sandbox.start()` → `docker run --runtime=runsc`.

Concretely this means the current `MCPClient.start()` host-spawn must **not** be the default
path. Two ways to achieve that, and they differ in blast radius:

| Option | Behaviour | Risk |
|---|---|---|
| **A. Remove the host spawn entirely** | `MCPClient` requires a transport; the only transport is a contained session. | None. Breaks `tests/skills/test_mcp.py`'s fake server (fixable — the fake is a local process, and it can be a *contained* process in tests, or the test can use a loopback transport). |
| **B. Keep it behind an explicit unsafe opt-in** | `PAA_PLUGINS__ALLOW_HOST_SPAWN=1` | A footgun that a `.env` line can turn on. |

**Recommendation: Option A**, with a *test-only* in-process transport that is not reachable
from configuration. The point of the whole design is that "the MCP server ran on the host"
must not be a state the system can be in.

**One carve-out, and it is deliberate:** a `trust = "trusted"` **in-process Python plugin**
(§3.12) does run in the daemon's address space. That is the point of it — it is the only way
to write a tool that reaches into the ledger, the memory graph, or the model router. Its
damage is bounded by the capability facade (R1) and undone by the rollback model (R3–R6),
and it is never a *third-party* MCP server. The invariant therefore reads:

> No **MCP server** and no **exec plugin** is ever started on the host. A **trusted
> in-process Python plugin** may run in the daemon, under the facade and the rollback model
> of §3.12, and is refused by default.

### L1 — Syscall boundary: gVisor (`runsc`)

The container's syscalls are serviced by Sentry, not by the host kernel. A malicious or
compromised MCP server cannot `open("/etc/shadow")` on the host, `ptrace` a host process,
`mount` anything, or `kexec`. This is the layer that nothing else provides.

Requires: `runsc` installed **and registered with the Docker daemon**
(`/etc/docker/daemon.json` → `{"runtimes": {"runsc": {"path": "/usr/local/bin/runsc"}}}` →
`systemctl restart docker`), and the daemon must *report* it
(`docker info --format '{{json .Runtimes}}'` must list `runsc`).

`DockerSandbox` already does exactly this and already reports honestly
(`docker_backend.py:99-109`, `:166-211`). **No code change needed for L1** — it needs a host
that is actually configured.

### L2 — Namespace + cgroup hardening (already in `DockerSandbox._build_args`)

`--read-only` (immutable rootfs), `--tmpfs /tmp:rw,noexec,nosuid,size=64m`,
`--cap-drop ALL`, `--security-opt no-new-privileges`, `--pids-limit 128`,
`--memory`/`--memory-swap` (pinned equal), `--cpus`.

**The caveat was measured, and it did not survive the measurement.** gVisor on cgroup v2
(Ubuntu 24.04's default) has *limited cgroup delegation* — so the plan expected
`--memory`/`--cpus` not to be enforced the way runc enforces them, and expected
`SandboxResult.memory_enforcement` to report `"gvisor"` under runsc. Measured on this
host with `runsc` registered, `--memory 256m` and a process touching 512 MiB gave, under
**both** runtimes: exit 137, `OOMKilled=true`, `memory.peak == memory.max` exactly, and
the kernel's own `memory.events` recording `oom 1 oom_kill 1`. The kernel's `oom_kill`
counter is what fired, under gVisor as much as under runc.

`--memory` is a *daemon*-level limit — the daemon caps the container's cgroup and
refuses to start a container whose limit it cannot apply — and the OCI runtime in
charge does not change that. So the ceiling was a hard ceiling in both cases, and
`memory_enforcement` reports `"cgroup"` for both. Which *runtime* ran the container is a
separate fact and is recorded separately: the isolation level says `VM` under gVisor.

### L3 — Network severance

`--network none` by default → the container has no interfaces but loopback. The MCP server
cannot reach the internet, the Postgres/Redis/Qdrant/SeaweedFS containers, or the host.

**This is the property that makes "the tool stays in our container" true in the way that
matters.** The stdio pipe is the *only* channel between the tool and the host.

If a plugin genuinely needs egress (a web-fetch MCP server, say), the manifest opts in with
`network = "egress"`, and the container is placed on a **dedicated egress network**, never
`paa_net` — so it still cannot reach the substrate services.

**HTTP-transport MCP cannot use `--network none`** — the daemon has to reach the server
over TCP. The network model for that case is a dedicated `paa_tools` network, and the
addressing, lifecycle and token story are in §3.13. The stdio path keeps the stronger
property, and the two are not interchangeable.

### L4 — Filesystem

The container sees: its image layers (read-only), `/workspace` (the bind mount, `rw` by
default, `ro` if the plugin declares it read-only), `/tmp` (tmpfs), and `/mnt/ro<N>` for
declared read-only mounts. The host filesystem is otherwise invisible.

**Not contained:** the workspace itself, which is by design — it is the shared state
between the runtime and the tool.

### L5 — Environment & secrets

`build_child_env` (`sandbox/base.py:337-386`) builds the child env **from empty**. The host
env is never inherited. The plugin manifest declares env vars **by name**
(`token_env = "ACME_API_KEY"`); the host resolves the *value* at spawn time from
`os.environ` / `config.dotenv_value` and passes only the named ones.

The host's own secrets (DB DSNs, `ANTHROPIC_API_KEY`, S3 store keys) are therefore never in a
plugin's environment. A plugin that needs a secret at *call* time (not launch time) gets it
through the existing `SecretBroker` (`src/paa/skills/secrets.py`), which checks
`Permission.SECRET_READ` and returns a `SecretValue` that redacts itself in `str()`/`repr()`.

### L6 — Capability gate (host, before the container is created)

The manifest declares, per plugin and per tool, the `Permission` set it needs
(`core/types.py:149-166`). Those must be a subset of the active mode's grants
(`core/types.PermissionMode.granted`). Under `LOCKDOWN` (which grants only
`PERM_SANDBOX_RUN`), a plugin declaring `PERM_NET_EGRESS` is refused **before** a container
is created — no side effect, no container, no ledger mutation.

This mirrors the existing USA step 3 (`skills/usa.py:178-190`): *authorization precedes
execution*.

### L7 — Interaction gate (host, per call)

The existing `ToolPermissionPolicy` (`coder/permissions.py`) decides allow / deny / ask per
call, and `AgentLoop._handle_call` runs it before `tool.run()` (`coder/loop.py:223-234`).
PLAN mode refuses mutating tools; an explicit deny beats everything including `bypass`.

**One change is needed here:** mutability is currently inferred from *name globs*
(`DEFAULT_MUTATING_TOOLS`: `write*`, `edit*`, `bash*`, `*write*`, …). A third-party tool
named `mcp__acme__create_ticket` matches none of those, so it would be *read-only by
accident* — exactly the failure mode `AGENTS.md` warns about for `submit_task`/`run_task`/
`remember_fact`.

The fix is a **union**, never a replacement:

> A tool is mutating if **either** a glob matches **or** the tool's descriptor declares
> `mutating = true`.

And the default for a third-party tool whose mutability was not declared is
**mutating = true** (fail closed).

### L8 — Ledger

`TOOL_INVOKED` is appended **before** the call, and records the plugin, the tool, the
container image + digest, the isolation level, and the network mode. `TOOL_COMPLETED` /
`TOOL_FAILED` follow. This is the repo's core rule — *ledger event before side effect,
always* — applied to tools for the first time.

Consequence: if the host dies mid-call, the ledger shows a `TOOL_INVOKED` with no
completion, and recovery knows the side effect may have happened. See §7.

### L9 — Resource ceilings

Per-call timeout, per-session idle timeout, per-session max lifetime, memory/CPU caps,
`--pids-limit`, output capture cap (`SandboxSettings.max_capture_bytes`), and a max number
of concurrently live MCP sessions.

### L10 — The honest list of what is *not* contained

- **The PAA daemon process itself** is not inside the container in the dev topology. If the
  daemon is compromised, the attacker has the ledger, the DB, and the model keys. Mitigation:
  also run the daemon under runsc (the `docker-compose.gvisor.yml` override already offers
  this), and never hand a plugin a live runtime object.
- **gVisor itself** is a large, complex attack surface. A gVisor escape is a full host
  compromise. Mitigations: pin the runsc version, minimal images, `--read-only`, no docker
  socket, all caps dropped, no egress.
- **Egress mode** weakens L3 substantially.
- **The model's own judgement** is not a security boundary. Tool *output* is untrusted input
  that can carry a prompt injection. The deterministic policy agent and the ledger are the
  backstop, not the model.

---

## 3. The architecture

### 3.1 Package layout

```
src/paa/sandbox/
├── base.py                (edit)  + SandboxSession ABC, Sandbox.start()
├── session.py             (new)   SandboxSession, SubprocessSession, SessionLimits
├── docker_session.py      (new)   DockerSession — `docker run -i`
├── docker_backend.py      (edit)  + DockerSandbox.start()
├── subprocess_backend.py  (edit)  + SubprocessSandbox.start()

src/paa/tools/             (new package)
├── __init__.py
├── base.py                ToolDescriptor, ToolResult, ToolProvider protocol
├── registry.py            ToolRegistry — host-owned catalogue
├── mcp_protocol.py        MCPTransport protocol + MCPClient (moved from skills)
├── mcp_provider.py        McpToolProvider — a contained MCP server (stdio or HTTP)
├── mcp_http.py            (new)   Streamable-HTTP transport: POST /mcp + SSE
├── exec_provider.py       ExecToolProvider — a contained one-shot command
├── native_provider.py     NativeToolProvider — wraps build_runtime_tools(rt)
├── plugin_provider.py     (new)   In-process Python plugin loader (trusted only)
├── context.py             (new)   PluginContext — the capability facade (R1)
├── rollback.py            (new)   Checkpoint / compensate / restore (R3–R6)
├── manifest.py            plugin.toml parsing + strict validation
└── loader.py              discover plugin dirs, build providers, degrade on failure

src/paa/skills/adapters/mcp.py   (edit)  re-export MCPClient from paa.tools.mcp_protocol
src/paa/config.py                (edit)  + PluginSettings
src/paa/runtime.py               (edit)  + Runtime.tools: ToolRegistry
src/paa/coder/agent.py           (edit)  merge provider tools into the loop's catalogue
src/paa/coder/permissions.py     (edit)  mutating = glob OR declared
src/paa/coder/loop.py            (edit)  pass the declared mutating flag into the policy
src/paa/core/types.py            (edit)  + TOOL_INVOKED / TOOL_COMPLETED / TOOL_FAILED
                                            + TOOL_COMPENSATION_REQUIRED / PLUGIN_DISABLED
                                            + PLUGIN_REGISTERED / PLUGIN_ROLLED_BACK
src/paa/ledger/replay.py         (edit)  + _NO_PHASE_CHANGE entries

deploy/toolbox/Dockerfile         (new)   → paa/toolbox:v1 (python + node; NOT paa)
deploy/docker/daemon.json         (new)   gVisor runtime registration, with --host-uds
docker-compose.yml                (edit)  + docker socket, + docker CLI image, + paa_tools net
docker-compose.gvisor.yml         (edit)  + --host-uds note, + seccomp=unconfined note
```

### 3.2 The sandbox session primitive

```python
# src/paa/sandbox/session.py

class SandboxSession(abc.ABC):
    """A live, bidirectional, contained process.

    ``Sandbox.run()`` is one-shot: it returns a total outcome and closes stdin.
    A stdio MCP server is a *conversation* — it must be able to read a request,
    think, and write a reply, over and over, for the life of a session. This
    is the primitive that can express that, and it is the reason the MCP
    adapter can be contained at all.
    """

    @property
    @abc.abstractmethod
    def isolation_level(self) -> IsolationLevel: ...

    @property
    @abc.abstractmethod
    def backend(self) -> str: ...

    @abstractmethod
    async def write(self, data: bytes) -> None: ...

    @abstractmethod
    async def read(self, n: int = -1) -> bytes: ...

    async def read_line(self) -> bytes:
        """One newline-terminated frame. Default implementation over ``read``."""
        ...

    @abstractmethod
    async def aclose(self) -> None: ...

    @property
    def exit_code(self) -> int | None:
        return None
```

and on the `Sandbox` ABC:

```python
    async def start(self, spec: SandboxSpec) -> SandboxSession:
        """Begin a live contained process.

        The default implementation refuses. A backend that cannot hold a duplex
        pipe must say so rather than silently degrade to a one-shot run — the
        difference between "the MCP server is contained" and "the MCP server ran
        on the host" is exactly the difference this design exists to preserve.
        """
        raise SandboxError(f"{self.name} backend does not support live sessions")
```

Non-abstract, so existing backends keep working unchanged until they opt in.

### 3.3 `DockerSandbox.start()` — the gVisor-backed session

The argv is `DockerSandbox._build_args` with three deltas:

| Delta | Why |
|---|---|
| add `-i` (keep stdin open) | the pipe is the transport |
| drop `--rm`? **No — keep `--rm`** | cleanup on exit. If the host is SIGKILLed, stdin closes → the MCP server sees EOF → it exits → `--rm` fires. No orphan. |
| add `--pull=never` | a `docker pull` mid-call would block the pipe and hit the network policy we just set |

Everything else is inherited verbatim: `--runtime runsc` (only when the daemon reports it),
`--read-only`, `--tmpfs /tmp`, `--memory`, `--cpus`, `--pids-limit`, `--cap-drop ALL`,
`--security-opt no-new-privileges`, `--network none` (unless the manifest opts into
egress), `--volume workspace:/workspace:rw`, `--env K=V` from the allowlist.

```python
# src/paa/sandbox/docker_session.py
class DockerSession(SandboxSession):
    """A contained process held open over a pipe.

    The container is the *process*: ``docker run -i`` proxies the container's
    stdin/stdout straight to ours, so the MCP framing crosses the container
    boundary untouched. No proxy, no bridge binary, no extra moving part.
    """
```

Timeouts are handled with `docker kill <name>` (not killing the CLI), exactly as
`DockerSandbox._force_kill` already does — because killing the client leaves the container
running and holding the workspace mount.

### 3.4 The MCP transport refactor

`MCPClient` currently owns the subprocess. Split that:

```python
class MCPTransport(Protocol):
    async def write(self, data: bytes) -> None: ...
    async def read_line(self) -> bytes: ...
    async def aclose(self) -> None: ...
```

* `SubprocessTransport` — the current behaviour (host process). **Not reachable from
  configuration**; used only by tests and by the explicit "trusted plugin" path.
* `SandboxSessionTransport` — wraps a `SandboxSession`. **The production path.**

`MCPClient` keeps all of its framing autodetection, pending-futures correlation, and
crash/timeout robustness unchanged — it is the best-tested code in this area and it should
not change. It just stops owning the process.

The client moves from `src/paa/skills/adapters/mcp.py` to `src/paa/tools/mcp_protocol.py`
(an MCP server is a *tool* protocol, not a skill concept), with a re-export shim at the old
path so `tests/skills/test_mcp.py` and any importer keeps working.

### 3.5 The tool provider layer

```python
# src/paa/tools/base.py

class ToolDescriptor(BaseModel):
    """What the model is told about a tool, and what the host needs to gate it."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    name: str
    description: str
    input_schema: dict[str, Any]
    output_schema: dict[str, Any] = Field(default_factory=lambda: {"type": "object"})

    mutating: bool = True
    """Fail closed. A third-party tool whose mutability was not declared is
    mutating, because the alternative — a tool that writes the world while
    claiming to be a read — is the failure that matters."""

    required_permissions: tuple[Permission, ...] = (Permission.SANDBOX_RUN,)
    risk_profile: float = Field(default=0.5, ge=0.0, le=1.0)

    provider: str
    """``"mcp"``, ``"exec"``, ``"native"``."""

    source: str
    """The plugin + server this came from, for the ledger and for `--list-tools`."""


class ToolProvider(abc.ABC):
    """A source of tools. One plugin, one MCP server, one native catalogue."""

    @property
    @abc.abstractmethod
    def provider(self) -> str: ...

    @abc.abstractmethod
    async def discover(self) -> list[ToolDescriptor]: ...

    @abc.abstractmethod
    async def invoke(self, descriptor: ToolDescriptor, arguments: dict[str, Any]) -> ToolResult: ...

    async def aclose(self) -> None:
        """Release the container/process. Idempotent."""
```

Providers:

| Provider | Backing | Containment |
|---|---|---|
| `McpToolProvider` | one MCP server, stdio transport | `docker run -i --runtime=runsc` |
| `McpToolProvider` | one MCP server, HTTP transport | `docker run -d --runtime=runsc` on `paa_tools` (§3.13) |
| `ExecToolProvider` | one command, JSON on stdin / JSON on stdout | `docker run --runtime=runsc` (one-shot) |
| `PluginProvider` | an in-process Python module (`trust = "trusted"`) | **none** — the facade + rollback model of §3.12 is the containment |
| `NativeToolProvider` | `build_runtime_tools(rt)` | in-process, already trusted |

`ExecToolProvider` is the "plugin" escape hatch for tools that are not MCP servers: a small
script, run contained, that reads a JSON request and writes a JSON response. It reuses the
existing one-shot `Sandbox.run()` — no new primitive needed.

### 3.6 The registry and the name mangling

```python
# src/paa/tools/registry.py
class ToolRegistry:
    """The host-owned catalogue of everything the model may call."""
```

Names are namespaced so two plugins cannot collide and so the operator can see where a tool
came from:

| Source | Exposed name | Example |
|---|---|---|
| MCP server `acme`, tool `search_tickets` | `mcp__acme__search_tickets` | the MCP convention; also the Claude Code convention |
| Exec plugin `acme`, tool `render` | `exec__acme__render` | |
| Native | unchanged | `read_ledger` |

Collision → the second registration is **refused** (and logged), never silently overwritten.

`ToolRegistry` also owns:

* the **mutating** flag per tool (union with the glob policy at the call site),
* the **capability** set per tool (union of the plugin's and the tool's declarations),
* `describe()` — a ledger-safe summary of the catalogue (plugin, image, digest, isolation
  level, network mode, mutating, permissions).

### 3.7 The plugin manifest

`PAA_HOME/plugins/<name>/plugin.toml` (user scope) or `./plugins/<name>/plugin.toml`
(project scope). TOML is stdlib `tomllib` in 3.11+ — **no new dependency**.

```toml
[plugin]
name         = "acme"
version      = "1.0.0"
description  = "Acme internal tools: tickets, search, deploy status."
trust        = "untrusted"        # "untrusted" | "trusted"  (trusted ⇒ in-process, §3.12)
network      = "none"             # "none" | "egress"  (egress ⇒ install-time confirmation)
permissions  = ["PERM_SANDBOX_RUN"]

# The tables this plugin may write. Used to scope the rollback checkpoint (R3).
scope = ["hot_serving_active_facts", "hot_serving_relationships"]

# How to undo the side effects that a transaction cannot undo (R4).
[compensations]
close_issue = "jira:transition:ISSUE-{issue_id}:cancelled"

# One or more providers.
[[provider]]
kind        = "mcp"                # "mcp" | "exec" | "python"
transport   = "stdio"             # "stdio" | "http"
name        = "acme-mcp"
command     = ["npx", "-y", "@acme/mcp-server"]
image       = "paa/toolbox:v1"
image_digest = "sha256:..."        # optional but recommended
timeout_seconds = 30
idle_timeout_seconds = 300
max_lifetime_seconds = 3600
# Env var NAMES, resolved to values at spawn time. Never values.
env         = { "ACME_TOKEN" = "ACME_API_KEY" }
# Per-tool globs. Anything not listed inherits `default_mutating`.
default_mutating = true
mutating   = ["create_*", "delete_*"]
readonly   = ["search_*", "get_status"]

[[provider]]
kind    = "exec"
name    = "acme-render"
command = ["python", "-m", "acme.render"]
image   = "paa/toolbox:v1"
```

For an HTTP-transport provider the transport block grows (§3.13 H7):

```toml
[[provider]]
kind       = "mcp"
transport  = "http"
name       = "acme-mcp"
image      = "paa/toolbox:v1"
port       = 8931
addressing = "network"            # "network" (server topology) | "localhost" (dev)
network    = "none"               # "none" ⇒ paa_tools (internal) | "egress" ⇒ paa_tools_egress
```

Validation is strict (pydantic, `extra="forbid"`), mirroring `SkillContract`. A malformed
manifest is **refused with a precise error**, and it does not prevent the runtime from
booting — the same degrade-don't-crash rule as `_try_vector`/`_try_graph`/`_try_queue`.

### 3.8 The two-axis permission model

This resolves a real confusion in the existing code: there are **two** `PermissionMode`
enums with different value sets.

| Axis | Enum | Set by | Question it answers |
|---|---|---|---|
| **Capability** | `core.types.PermissionMode` (`AUTO/ASK/SUPERVISED/LOCKDOWN/SAFE/HIGH_RISK`) | `PAA_POLICY__MODE` | *What is the plugin allowed to do?* (net egress, secret read, file delete) |
| **Interaction** | `coder.permissions.PermissionMode` (`ask/accept_edits/plan/bypass`) | `paa agent --mode` | *Should a human be prompted before this specific call?* |

Both gates apply, in this order, per call:

```
1. capability gate   (host) — plugin's declared permissions ⊆ mode.granted?  → refuse
2. interaction gate  (host) — ToolPermissionPolicy: allow / deny / ask        → refuse / prompt
3. ledger event      (host) — TOOL_INVOKED appended                          → the side effect may now happen
4. container start   (host) — docker run -i --runtime=runsc
5. the call          (container) — tools/call over the pipe
6. ledger event      (host) — TOOL_COMPLETED / TOOL_FAILED
```

The capability gate is checked **before** a container is created, so a refused plugin costs
nothing and leaves no trace.

### 3.9 Flow: `paa agent "search the acme tickets for X"`

```
CoderAgent.build()
  └─ Runtime.build()
       └─ ToolRegistry  ← native provider + plugin providers
            └─ McpToolProvider.discover()
                 └─ Sandbox.start(spec)  → docker run -i --runtime=runsc ...
                      └─ MCPClient.initialize() → tools/list → descriptors
  └─ tools = {**build_runtime_tools(rt), **registry.as_loop_tools()}

AgentLoop.run()
  └─ model proposes mcp__acme__search_tickets
       └─ _handle_call
            ├─ PRE hook
            ├─ capability gate  (descriptor.required_permissions ⊆ mode.granted)
            ├─ interaction gate (mutating? PLAN denies; ASK prompts)
            ├─ TOOL_INVOKED → ledger
            ├─ tool.run(args) → McpToolProvider.invoke → MCPClient.call_tool
            └─ TOOL_COMPLETED → ledger
```

The in-process plugin path replaces the middle of that flow, and the difference is worth
seeing once:

```
       └─ PluginProvider.invoke()
            ├─ TOOL_INVOKED → ledger
            ├─ checkpoint   → scope snapshot (+ VACUUM INTO on SQLite)   (R3)
            ├─ BEGIN        → one transaction for the whole call        (R2)
            ├─ plugin.run(ctx)  → ctx.ledger / ctx.memory / ctx.secrets / ctx.model
            │                     (no Database, no LedgerStore)          (R1)
            ├─ COMMIT (or ROLLBACK on exception)
            └─ TOOL_COMPLETED → ledger
```

Note what is **not** in either flow: the container is only ever created by
`Sandbox.start()`, and the ledger event is always written before the side effect.

### 3.10 Wiring into the Runtime and the coder agent

`Runtime` gains one field:

```python
@dataclass
class Runtime:
    ...
    tools: ToolRegistry = field(default_factory=ToolRegistry)
```

`Runtime.build()` composes it (native provider always; plugin providers best-effort, each
failing independently — one broken plugin must not stop the others or the boot).

`CoderAgent.build()` merges:

```python
tools = {**build_runtime_tools(runtime), **runtime.tools.as_loop_tools()}
```

and extends the mutating set:

```python
mutating_tools=(*DEFAULT_MUTATING_TOOLS, *MUTATING_RUNTIME_TOOLS,
                 *runtime.tools.mutating_names())
```

**This is the fix for the "read-only by accident" hazard**: an MCP tool called
`mcp__acme__delete_ticket` matches no glob, so without the declared flag it would be
PLAN-exempt. With the union rule, it is not.

`ToolPermissionPolicy.evaluate` gains an optional declared-mutating argument, and
`AgentLoop._handle_call` passes it. The existing glob behaviour is unchanged for native
tools, so every existing test keeps its meaning.

### 3.11 Ledger integration

New event types (`core/types.py`):

| Event | Phase effect | Emitted by |
|---|---|---|
| `TOOL_INVOKED` | none (`_NO_PHASE_CHANGE`) | the tool dispatch layer, **before** the call |
| `TOOL_COMPLETED` | none | after a successful call |
| `TOOL_FAILED` | none | after a failed call |
| `PLUGIN_REGISTERED` | none | the plugin loader, at boot |

All four are informational, so they go in `replay.py`'s `_NO_PHASE_CHANGE` set — otherwise
replay would record them as `unknown_events` and log a warning on every boot.

Payloads carry the containment facts, because that is what the audit trail is *for*:

```json
{
  "tool": "mcp__acme__search_tickets",
  "provider": "mcp",
  "plugin": "acme",
  "server": "acme-mcp",
  "image": "paa/toolbox:v1",
  "image_digest": "sha256:...",
  "isolation_level": "VM",
  "network": "none",
  "mutating": false,
  "duration_ms": 812.4
}
```

---

### 3.12 In-process plugins: the rollback model

**Decision 4: in-process Python plugins are allowed.** They are the only way to write a tool
that reaches into the ledger, the memory graph, or the model router — a *contained* MCP
server cannot do that by construction. The cost is that such a plugin shares the daemon's
address space, and a plugin that corrupts state cannot be undone by a transaction alone.

The answer is six mechanisms, applied in this order.

### R1 — The plugin never gets raw state

The plugin's tool body receives a `PluginContext`, never a `Runtime`:

| The plugin gets | The plugin does **not** get |
|---|---|
| `ctx.ledger` → `LedgerEmitter` — typed events only; cannot append an arbitrary event, cannot set `state_version`/`prev_hash` | the `LedgerStore` |
| `ctx.memory` → `MemoryWriter` — goes through the cold-lake signal path (ADR-0022) | a `Database` handle, a cursor, `execute()` |
| `ctx.secrets` → `SecretBroker` proxy, gated on `PERM_SECRET_READ` | the vault file |
| `ctx.model` → the router, so escalation stays ledgered | provider credentials |
| `ctx.workspace` → a path jail under the plugin's own subdirectory | the filesystem |
| `ctx.tools` → the `ToolRegistry` (so a plugin may call other tools) | the raw registry internals |

**This is the primary damage control.** The blast radius of a bug is the set of operations
the facade exposes — not the database. It is the "capability facade" the flexibility audit
asked for, and it is the same shape as the proposed `RuntimeContext`.

### R2 — Every plugin call is one transaction

A plugin tool's writes go through the facade, which opens **one** transaction for the whole
call:

* the plugin raises → the transaction rolls back, and the call returns an error;
* the plugin returns normally → the transaction commits;
* the daemon crashes mid-call → the database's own crash recovery discards the uncommitted
  transaction.

So **uncommitted damage is impossible by construction** — the same rule the ledger already
relies on, and the same property the existing chaos tests exercise.

### R3 — A pre-call checkpoint (the rollback that was asked for)

Before a plugin tool runs, the runtime writes a rollback checkpoint:

* **Logical scope (always).** The plugin's manifest declares the tables/rows it may touch
  (`scope = ["hot_serving_active_facts", "hot_serving_relationships"]`). The checkpoint
  snapshots exactly those rows into a new `hot_serving_plugin_rollback` table — **both**
  schemas, parity test — together with the plugin name, tool name, correlation id, and
  timestamp.
* **Physical (SQLite only).** A `VACUUM INTO` copy of the database into
  `PAA_HOME/rollback/<plugin>/<ts>.db`. Cheap enough for a local-first runtime, and it is
  the only thing that can undo a `DROP TABLE` or a schema corruption.
* **Not physical (Postgres).** A per-call `pg_dump` is far too heavy. Postgres gets the
  logical scope plus the compensations of R4. **This asymmetry is stated, not hidden** — the
  server topology has a weaker rollback guarantee than the laptop, and the docs must say so
  rather than implying parity.

### R4 — Compensating undo records

Some plugin actions have effects *outside* the database (a Jira call, a `git push`). A
transaction cannot undo those. So a manifest may declare compensations — named recipes the
runtime can execute to undo an action:

```toml
[compensations]
close_issue = "jira:transition:ISSUE-{issue_id}:cancelled"
```

The runtime appends a `TOOL_COMPENSATION_REQUIRED` ledger event carrying the recipe and its
arguments **before** the external call. `paa plugins rollback <plugin>` walks those events
in reverse and executes the recipes.

The ledger is the record of what needs undoing — which is exactly its job, and why the
event type is added to `core/types.py` in the same change.

### R5 — Automatic trip and kill switch

* A plugin tool that fails **N times in a row** (N = 3, configurable) is **auto-disabled**:
  the registry marks it inactive, emits `PLUGIN_DISABLED`, and refuses further calls. It
  stays disabled until `paa plugins enable <name>`.
* If the daemon dies and the last ledger event was a plugin's `TOOL_INVOKED`, the boot sweep
  marks the plugin **suspect** and does not auto-load it. The recovery report says so:
  "paa plugins enable acme".
* `paa plugins disable <name>` takes effect immediately — the registry consults the
  database on each call, so no daemon restart is needed.

### R6 — `paa plugins rollback <name>`

1. Disable the plugin.
2. Restore the physical checkpoint (SQLite) or replay the logical scope restore (both).
3. Execute the compensating recipes in reverse ledger order.
4. Emit `PLUGIN_ROLLED_BACK` recording what was restored.
5. Leave the plugin **disabled**. Rolling back and re-enabling are separate decisions.

### The honest limits of this model

* An in-process plugin can corrupt the **daemon's memory**, not just the database. A
  segfault or an OOM is not recoverable by a checkpoint: the daemon restarts and
  ledger-based recovery takes over, but the in-memory damage is gone with the process. This
  is the real cost of "in-process".
* A plugin that has already committed an external side effect is undoable **only if it
  declared a compensation**. `paa plugins install` therefore refuses (or loudly warns on)
  a trusted plugin whose `compensations` are missing for a declared-mutating tool.
* The physical checkpoint is SQLite-only (R3). The server topology gets the logical scope
  and the compensations, and the docs say so.

### The alternative worth one more discussion

**"In-process" implemented as a supervised child process.** The plugin runs in a Python
child of the daemon, with a capability-scoped RPC channel back to the daemon for facade
operations. It is nearly as capable — it can reach every facade operation — but a crash
does not take the daemon down, and the parent owns the transaction, so R2 and R3 become
stronger rather than weaker.

This is strictly safer than literal in-process, and I would recommend it. It is a different
thing from what "in-process" usually means, and it costs an RPC layer. The plan supports
both, because R1–R6 are identical either way and only the process boundary differs —
**this is the one remaining question in §7 worth deciding before Phase 4.**

---

### 3.13 HTTP-transport MCP: a different containment model

**Decision 7: HTTP-transport MCP is in scope.**

The stdio model has a property HTTP cannot have: **the pipe is the only channel**. A
container with `--network none` can still talk to the daemon, because the daemon's end of
the pipe is a Unix pipe on the host, not a network. An HTTP MCP server is a *network
service*, and that changes five things.

### H1 — The container needs a network

`--network none` is off the table. The container joins a dedicated **`paa_tools`** network,
in one of two variants:

| Variant | Network | Reach |
|---|---|---|
| default (no egress) | `paa_tools`, `internal: true` | other containers on `paa_tools` and **nothing else** — no external routing, no internet |
| egress (opt-in, per plugin) | `paa_tools_egress`, a normal bridge | the internet, but **not** `paa_net`, so still no Postgres/Redis/Qdrant/SeaweedFS |

**A tool container joins exactly one of the two.** A container on both would route egress
through the non-internal one. This is called out because it is the kind of thing that gets
misconfigured silently, and the manifest validation should refuse a contradictory
configuration rather than let Docker resolve it.

### H2 — Addressing differs by topology

* **Server topology** (the `paa` daemon in a container, per Decision 2): the daemon and the
  tool container are both on `paa_tools`, so the daemon reaches the server at
  `http://<container-name>:<port>/mcp`. **Nothing is published to the host.** The tool's
  port never touches the host's network stack.
* **Dev topology** (the daemon on the Ubuntu host): the host cannot resolve container
  names, so the port must be published — bound to `127.0.0.1` on a **random high port**,
  never `0.0.0.0`. Any local process can then reach it, so the server must authenticate:
  the daemon generates a **per-session bearer token**, passes it into the container as an
  env var, and sends it on every request. The token is in the container's environment, not
  in the host's command line, and never in the ledger.

### H3 — The lifecycle is different

An HTTP MCP server is long-lived, so it is not a `docker run -i` child:

* The daemon creates it as a **detached container** (`docker run -d`) with a healthcheck and
  keeps it for the session; on shutdown, `docker stop`.
* A crashed server is detected by the healthcheck and **recreated**, not resurrected. A
  stdio server that crashes is a failed call; an HTTP server that crashes must be brought
  back.
* The daemon tracks the container id, so it can `docker kill` it and reap it. A boot-time
  sweep removes leftover `paa-tool-*` containers.

### H4 — The client is new code

The current `MCPClient` is stdio-only. Streamable HTTP needs:

* `POST /mcp` with a JSON-RPC body; the response is either a JSON body or an **SSE
  stream**.
* A **session id** header (`Mcp-Session-Id`) for correlation.
* **Server-initiated requests** (`sampling/createMessage`, `roots/list`) must be serviceable.
  The current client explicitly ignores them (`mcp.py:196-200`). Under stdio that is
  survivable; under HTTP a server that asks for sampling and gets silence behaves badly.
  → *Open question in §7: implement the server-request side, or declare the runtime a
  "no server-initiated requests" client and refuse servers that need them?*
* The framing, correlation, and robustness machinery in `MCPClient` is reusable as-is —
  only the transport differs. **The transport abstraction earns its keep twice**, which is
  the strongest argument for doing it in Phase 2 rather than later.

### H5 — What does *not* change

All ten containment layers still apply: gVisor, `--read-only`, `--tmpfs /tmp`,
`--cap-drop ALL`, `--no-new-privileges`, `--pids-limit`, the env allowlist, the capability
gate, the interaction gate, the ledger events, and the resource ceilings. The only
differences are the network shape (H1), the addressing (H2), and the lifecycle (H3).

### H6 — The new risk, stated plainly

The tool server is now **network-reachable**. In the dev topology a published
`127.0.0.1` port is reachable by every local process. Mitigations: the bearer token
(per-session, rotated), the random port, and the container is stopped when the daemon
exits. The server topology has no such exposure, because nothing is published.

### H7 — The manifest grows a transport section

```toml
[[provider]]
kind        = "mcp"
transport   = "http"          # "stdio" | "http"
name        = "acme-mcp"
image       = "paa/toolbox:v1"
image_digest = "sha256:..."
command     = [...]           # stdio only
port        = 8931            # http only
addressing  = "network"       # "network" (server topology) | "localhost" (dev)
network     = "none"          # "none" → paa_tools (internal); "egress" → paa_tools_egress
timeout_seconds = 30
idle_timeout_seconds = 300
```

The network names come from `PAA_PLUGINS__TOOLS_NETWORK` and
`PAA_PLUGINS__EGRESS_NETWORK` so a deployment can rename them.

---

### 3.14 The daemon under gVisor, holding the Docker socket

**Decision 2: the `paa` container gets `/var/run/docker.sock` mounted and the docker CLI
installed, and the `paa` service itself runs under `runtime: runsc`** (the existing
`docker-compose.gvisor.yml` override).

Four consequences that must land in the compose file, the image, and the docs.

### 1 — gVisor needs explicit permission for the host Unix socket

By default a gVisor container **cannot open a host Unix socket**, even one bind-mounted
into it. The daemon's runtime registration must carry the passthrough:

```json
{
  "runtimes": {
    "runsc": {
      "path": "/usr/local/bin/runsc",
      "runtimeArgs": [
        "--host-uds=all"
      ]
    }
  }
}
```

Without it, `docker` inside the `paa` container fails with a permission error that reads
like a socket-permission problem and not like a gVisor problem. **This is the single most
likely first-hour failure of the whole design**, so it gets a Phase 0 verification step
and a `paa doctor` check.

### 2 — The docker CLI has to be in the image

The runtime image is `python:3.12-slim` and has no docker client. Add Docker's official
**static** client — **the CLI only, never the daemon** — pinned by SHA-256 and verified at
image-build time.

The static build is used rather than `docker-ce-cli` because a package version is spelled
per distribution release (`5:29.9.1-1~debian.13~trixie`), so pinning one couples the
image to a codename, and Docker's `stable` channel prunes old package versions — so a
pinned package version eventually stops existing and the build breaks for a reason that
has nothing to do with this code. The static tarball is distro-independent and Docker
keeps it.

### 3 — The socket is host root, and that is accepted

Whoever can talk to the docker socket can start a privileged container on the host. The
daemon is the only holder, the daemon is under gVisor, and the daemon's own exposure is a
loopback API. This is the price of the simple topology, and it is exactly what the
socket-proxy alternative would have bought. **The choice is recorded here so that a future
reader knows it was a decision, not an oversight.**

### 4 — There is no nesting

The `paa` daemon runs under runsc, but the **worker** containers are created by the *host's*
dockerd, which starts them directly under the *host's* runsc. The daemon never runs runsc
inside runsc; it only speaks to the host daemon over the socket. Nesting would occur only
if the docker daemon itself ran inside the container, which it does not.

### 5 — `--security-opt seccomp=unconfined` may be required for the `paa` service

gVisor implements its own syscall policy; Docker's default seccomp profile can conflict
with it. If the `paa` container fails to start under runsc with the default profile,
`seccomp=unconfined` is the fix — and it is **safe here precisely because gVisor is the
syscall boundary**.

### The resulting network topology

```
paa_net            (bridge, not internal)   paa + postgres + redis + qdrant + seaweedfs
                    └─ the runtime needs model-API egress; the substrate
                       services publish no host ports.

paa_tools          (bridge, internal: true) paa + every no-egress tool container
                    └─ no external routing at all. HTTP MCP servers live here and
                       publish nothing to the host.

paa_tools_egress   (bridge, normal)         egress-enabled tool containers only
                    └─ the internet, but never paa_net, so still no substrate access.

The `paa` service joins paa_net AND paa_tools.
A tool container joins EXACTLY ONE of paa_tools / paa_tools_egress, and never paa_net.
```

The manifest validation (§4, Phase 4) enforces the "exactly one" rule, and `paa doctor`
reports which network each tool container is on.

---

## 4. Phased delivery

Each phase is independently verifiable, and each ends with the mandated
`ruff` → `mypy` → `pytest` gate and an independent review.

### Phase 0 — Host preparation and measurement

**Status: implemented and verified, including the three root-gated host steps.**
`runsc` is registered with the daemon, the daemon restarts with the
registration in place, a container starts under it, the host Unix socket is
reachable from inside a gVisor container, and the cgroup ceiling has been
measured under both runtimes. Two of those measurements changed the *code* — the
socket flag's spelling and the ledger's memory-enforcement string — and
`docs/deployment/docker.md` has the full reasoning for each.

* Install the **official gVisor `release-20261012.0` static binary** (Decision 6): from
  the GitHub release tarball `gvisor-x86_64.tar.bz2`, checksum-verified against the
  release's own `SHA256SUMS`. (The `storage.googleapis.com/gvisor/releases/release/<date>/`
  mirror lags the GitHub releases — `20261012.0` is not on it — so the GitHub tarball is
  the authoritative source for a current version.) `runsc --version` reports
  `runsc version release-20261012.0`.
* Register with Docker in `/etc/docker/daemon.json`, **including the `--host-uds`
  passthrough the `paa` container needs** (§3.14.1). A ready-to-copy file is in
  `deploy/docker/daemon.json`:
  ```json
  { "runtimes": { "runsc": {
      "path": "/usr/local/bin/runsc",
      "runtimeArgs": ["--host-uds=all"] } } }
  ```
  `systemctl restart docker`.
* Verify: `docker info --format '{{json .Runtimes}}'` lists `runsc`; then
  `docker run --rm --runtime=runsc hello-world` succeeds.
* **Measure** cgroup enforcement under runsc (allocate 1 GB under `--memory 256m` and see
  whether it is OOM-killed). Record the honest answer; it goes into
  `SandboxResult.memory_enforcement` and into the docs. **Done.** Both runtimes gave
  exit 137, `OOMKilled=true`, `memory.peak == memory.max` and the kernel's
  `oom_kill 1` — so the ceiling is the kernel's cgroup under either runtime, and the
  ledger says `"cgroup"`. The plan's expectation (a weaker ceiling under gVisor) came
  from gVisor's own documentation and was not measured; the measurement disagreed with
  it.
* Author `deploy/toolbox/Dockerfile` → `paa/toolbox:v1` — `python:3.12-slim` + Node 24 +
  `git`/`curl`/`jq`. **The `paa` package is deliberately excluded**: a plugin that
  imported it would expect a database connection and a ledger handle, and the container
  has neither. Build it, record the digest.
* Also author `deploy/worker/Dockerfile` → `paa/base_worker:v4.1` — the image the sandbox
  backend launches for a *plan step*. Deliberately minimal (Python + `git` +
  `ca-certificates`), because it runs untrusted work. **This closes a real defect**:
  the recorded default and the executed default were the same string pointing at an image
  nobody had built.
* Add the **docker CLI** to the runtime image (Docker's official static client, pinned by
  SHA-256 and verified at build time — the client is pointed at a host-root socket, so
  "where did this binary come from" should have a checkable answer) and **mount the
  socket** in `docker-compose.yml` (Decision 2).
* **The socket's GID must be a supplementary group of the container user.** The socket is
  `root:<docker-gid>` mode `0660` and the container's `paa` user is uid 10001, so the
  mount alone is not enough — `group_add: ["${DOCKER_GID:-999}"]` and a `DOCKER_GID` in
  `.env` (`stat -c %g /var/run/docker.sock`). *The mechanism is verified in the
  repository's own terms — the compose file declares the group and the socket mount, and
  `tests/deployment/test_compose_config.py` asserts that the compose file's environment
  is one the settings layer can actually boot. Whether the *host's* docker GID is the
  value the operator put in `.env` is not something a test can assert; it is checked by
  the container's own error message if it is wrong.*
* Create the three networks (`paa_net`, `paa_tools` internal, `paa_tools_egress`) in
  `docker-compose.yml` and attach `paa` to `paa_net` + `paa_tools`, with an explicit
  `priority` so `paa_net` supplies the default route and the daemon keeps its egress.
  *Compose only creates the networks a service attaches to, and the tool containers are
  launched by the runtime through the Docker API rather than by Compose — so
  `paa_tools_egress` is declared here and materialises when the runtime creates it in
  Phase 4c.*
* `paa doctor` reports the container runtime, whether the daemon has it, and the image —
  via a new `Sandbox.diagnose()` and `DockerSandbox.describe()`, with the sandbox exposed
  on the runtime as `Runtime.sandbox` instead of reached for through the worker's private
  `_sandbox`.
* Extend `docs/deployment/docker.md` with the measured facts, and record the socket
  decision in `docs/adr/0024-docker-socket-and-gvisor-containment.md`.

**Exit criterion:** the toolbox image runs (`docker run --rm paa/toolbox:v1` prints the
Node version — the image's `CMD` is `node --version`; a bare `--version` after the
image name would *replace* the `CMD`, because the image has no `ENTRYPOINT`); an
`npx`-started server works under the sandbox's `exec_tmpfs` path; the sandbox backend's
`docker run` argv is accepted by a live daemon with the workspace mounted at the right
host path and the container running as a non-root uid; and `paa doctor` reports the
runtime and its isolation level truthfully.

**The `runsc` half of the criterion is outstanding.** `docker run --rm --runtime=runsc
paa/toolbox:v1` and `docker run --runtime=runsc` from inside the `paa` container both
need `runsc` registered with the daemon, which needs `sudo`. The daemon's `runc`-only
fallback is verified — the backend uses `runsc` **only** when the daemon reports it
registered, reports `NAMESPACE` rather than `VM` when it does not, and the isolation
level and the memory-enforcement mechanism both follow the *effective* runtime.

**Three findings from the Phase 0 review, folded back into the plan** (they
changed the design, so they are recorded here rather than only in the ADR):

* **The toolbox's npm cache cannot live on `/tmp`.** The backend mounts `/tmp`
  `noexec`, and npm's cache holds the executable shims `npx` installs — so an
  `npx`-started MCP server failed with `Permission denied` (exit 126). The
  answer is `SandboxSpec.exec_tmpfs`: those workloads get their own writable,
  exec-capable path (`/run`), and `/tmp` keeps the `noexec` property. This is
  why §3.2's session primitive has to carry it.
* **The workspace path the runtime knows is not the path the daemon must be
  given.** The daemon resolves bind mounts against the *host*, so a runtime
  inside a container was mounting an empty, root-owned directory instead of
  its workspace. The fix is a host bind mount for the data directory plus
  `SandboxSettings.host_path_map` to translate. Without it, "the daemon can
  launch a container" is true and "a plan step can do work in it" is not.
* **`--user` did not exist.** The container ran as the image's default — root —
  and both the Dockerfiles and the ADR claimed otherwise. It exists now,
  derived from the workspace's owner, and the container's `HOME`/`TMPDIR` were
  fixed to name `/workspace` (the path *inside* the container) rather than the
  host path, where they pointed at nothing.

**Three more findings from the final Phase 0 review, folded back into the plan:**

* **The compose file's environment could not boot the runtime.** The
  host-path map was a `tuple[tuple[str, str], ...]` field, and
  pydantic-settings decodes a complex-typed field from an environment variable
  as **JSON** — so the obvious spelling (`/data=/srv/paa/data`) was a settings
  error at import. The container died before it could serve a request, and
  before it could run the doctor that would have explained why. The field is
  now a plain string, parsed by the settings layer; a malformed entry is
  reported by `paa doctor` and refuses the run rather than being silently
  dropped. `tests/deployment/test_compose_config.py` is the regression guard:
  it reads the compose files and asserts the environment they declare is one
  the settings layer can consume.
* **A root-owned workspace selected `--user 0:0`.** The `--user` flag was
  derived from the workspace's owner, and a root-owned workspace is root. The
  flag is now an *invariant* rather than a preference: it is always passed,
  the uid is never 0, and a root-owned workspace falls back to the image's own
  non-root user instead of putting the workload back in container-root.
* **The doctor and the TUI rendered the same facts differently.** Two copies
  of the vocabulary drifted, which is how a runtime ends up telling the
  operator two different things about the same host. The rendering now lives
  with the facts it renders — `sandbox_report_rows` in `paa/sandbox/base.py` —
  and both surfaces call it.

**The three root-gated steps, and they need `sudo`.** The three *commands* below are
the first two steps; the third is the cgroup measurement, which needs the daemon
restarted with `runsc` registered. All three are done on the reference host;
`docs/deployment/docker.md` has what they changed.

**`docs/deployment/docker.md` has the full rationale for each of them** — why the
binary is copied rather than left on the operator's `PATH`, why the daemon
configuration is *merged* rather than overwritten, what each command actually
changes, how to tell that it worked, and what the cgroup measurement under
`runsc` is for. It also covers the two other root-gated things on this host:
the stray root-owned `/data` left by a sandbox workspace whose path was never
translated (and why `rmdir`, not `rm -rf`, is the safe form), and the six
pre-existing test failures, which are the *host* rather than the code — the
sandbox builds its child environment with a deliberate minimal `PATH`, and on a
host whose `python` is user-local that `PATH` contains no interpreter. The fix
is one line on the host: `sudo apt install python-is-python3`.

```bash
sudo install -m 0755 ~/.local/bin/runsc /usr/local/bin/runsc   # step 1
sudo cp deploy/docker/daemon.json /etc/docker/daemon.json   # step 2 — merge, do not overwrite blindly
sudo systemctl restart docker                            # step 2 (continued)
docker info --format '{{json .Runtimes}}'    # must list "runsc"
docker run --rm --runtime=runsc hello-world
```

The first step copies the binary to `/usr/local/bin` because that is where the
daemon's `daemon.json` points, and because the daemon runs as root with a minimal
`PATH` — the copy in the operator's own `~/.local/bin` is invisible to it. The
installed copy is not on the operator's `PATH` either, which is deliberate: `runsc`
is the *daemon's* runtime, and the operator drives it through `docker run
--runtime=runsc`.

The second step **overwrites** the host's daemon configuration. If the host already
has one — a registry mirror, a log driver — merge the file into it instead, or the host
loses those settings along with the daemon. `deploy/docker/README.md` has the details,
including why the file itself carries no comments: the daemon refuses to start on a
top-level key it does not recognise.

### The substrate track (W3–W8) — the hardening Phase 1 sits on

**This is not an MCP phase.** It is a separate track of work that has to land *before*
Phase 1, because every layer above it — the tool registry, the ledger events, the plugin
manifests — assumes a substrate and a deployment that behave the same way on every
supported engine. The MCP work would otherwise be built on a foundation whose
cross-engine behaviour has not been established, and the first tool that resolves an
entity or runs under the server topology would be the thing that found out.

The work is tracked as **W3–W8** and each item carries its own gate
(`ruff` → `mypy` → `pytest`, with the failure set unchanged) and its own independent
review. The ordering below is the ordering they must be done in: each one closes a
class of defect the previous one made visible.

**W1 and W2 — done.** The substrate contract, the neutral error vocabulary, the
versioned migration list and the identifier-coercion sweep landed in
`feat(storage): a substrate contract, a neutral error vocabulary, and a migration list`,
after two rounds of independent review. The container work landed beside it in
`feat(deploy): the daemon holds the Docker socket; tool containers run under gVisor`.
The gate is unchanged across both: ruff 69, mypy 108 (eight below the 116 at the
previous HEAD), and a pytest failure set byte-identical to the baseline.

#### W3 — Entity resolution is the same answer on both substrates

**Status: not started. The design decision is taken; the implementation is not.**

Entity resolution is the one place where the two relational substrates are *not*
interchangeable, and it is the place the tool layer will lean on hardest. SQLite
resolves a name through FTS5's trigram matcher; PostgreSQL through `pg_trgm`'s
similarity. The candidate sets the two produce are not the same set, so the same term
resolves to a different entity — or to no entity — depending on which substrate is
underneath. A 29-term sweep found three terms that disagreed and two more that raised
on one substrate and not the other, and none of it was visible to the suite, because the
suite only ever ran the SQLite half.

**The decision (Option B).** The database must return a candidate set that is a
**proven superset** of the set the Python acceptance gate would accept, and the gate
then decides in Python, identically on both engines. The alternative — two different
approximations of the same question — is what is being replaced.

* The FTS5 *phrase* and the bare search term become two separate, deliberate
  queries. Today the same parameter is an FTS5 expression on one engine and a literal
  term on the other, which is how a single-word term ends up with no broader second
  pass on SQLite and a much broader one on PostgreSQL.
* The candidate predicate per engine is written so that every entity the Python gate
  would accept is *in* the candidate set. The superset property is proved by a test
  and not asserted in a comment.
* The `LIMIT` cut is made safe by a **deterministic total order**. Neither engine's
  current ordering is one — `bm25` ties, `similarity` ties, and the gate's `>=`
  tie-break means the *last* maximum wins, so the winner is decided by whichever order
  the engine happened to produce. A unique secondary key is added, per AGENTS.md's rule
  that a `LIMIT` cut needs a total order.
* The `OR`-token leak and the alias scoring are fixed. The alias comparison scores the
  whole JSON blob on one engine and the raw JSON text on the other, while the gate
  scores each alias individually — so neither engine's candidate set matches what the
  gate actually looks at.

**Done when:** a cross-engine test drives the same terms through both substrates and
gets the same answers, and the candidate set is a superset of the gate's on both.

#### W4 — The deployment fetches what it claims to fetch

**Status: not started.**

`deploy/fetch-images.sh` does not do what its own header says it does. The images are
not digest-pinned; two of the four it fetches are *older versions* than the ones
`docker-compose.yml` actually runs; the `--load` branch is unreachable (it consumes
two arguments and shifts one, so the whole branch is dead code — reproduced, exit 2);
the images are checked under local names (`postgres`, `redis`, …) that resolve to
`:latest` and that the daemon does not have, so the script **fails on every image even
when every pull succeeds**; and there is no digest verification at all, so `--verify`
passes an image with a completely wrong digest.

* The compose file becomes the **single source of truth**: the script reads the
  `image:` values from it rather than carrying a hand-copied table that drifts.
* Every image is **digest-pinned**, and the digest is verified **inside the retry
  unit** — a download and its check are one attempt, the same rule the Dockerfiles
  already follow. A verification outside the retry loop turns a failed transfer into
  a reported success.
* `--load` is made to work for the runtime that is actually present (`docker`), and
  the script stops requiring a running daemon for `--list`.
* The `timeout` dependency is GNU-only and the script does not say so; the timeout
  mechanism is made portable or the Linux requirement is stated.
* `paa-init` gets a `build:` section. It has an `image:` with no `build:`, and it
  works today only because the `paa` service's build happens to produce the same tag
  in the same invocation. Starting `paa-init` alone fails with a pull error.
* The compose file **forwards the model provider's credential and the `PAA_MODELS__*`
  settings**. `env_file: .env` does deliver them today, but only from the file — the
  documented `export OPENROUTER_API_KEY=…` workflow is silently ignored, because
  `env_file` reads the file and not the shell.

**Done when:** the script and the compose file cannot disagree, and a containerised
runtime can reach a provider by either route.

#### W4b — The `s3` rename is finished

**Status: done.** The `minio` extra is now `s3` (the rename is deliberately
breaking: `pip install 'paa[minio]'` no longer works), the legacy `minio_*`
configuration names still resolve, and `ADR-0026` records it. The
documentation that still said MinIO has been brought in line with the code:
the deployment guide, the configuration reference, the architecture and
operations documents, and the two interfaces documents that name the
settings fields. The one that told the operator to uncomment a port mapping
that does not exist now says the object store's console is not published at
all.

#### W5 — The container's start is not charged to the work

**Status: not started.** A measured 0.25-second step took about four seconds and
reported `timed_out=False`: the container's *start* was being counted against the
work's `timeout_seconds`, so the budget the caller set was not the budget the caller
got. The start cost is real and has to be paid — it is just not the work. A separate
`container_start_grace_seconds` gives the start its own budget, and `timeout_seconds`
means what it says: the work.

#### W6 — The suites that would have caught the above

**Status: not started. This is the item that keeps the others from being regressions.**

Each defect in W3–W5 was invisible to the suite, and each was invisible for the same
reason: the suite ran one substrate, one deployment path, and one image set. The
suites that make them visible:

* a **cross-engine parity suite** — the same inputs through SQLite and PostgreSQL,
  compared row for row, on the paths where the two are supposed to agree.
* a **PostgreSQL contract suite** — `requires_postgres`-marked, gated on a DSN that
  names a *server*, with the tests making and dropping their own database so a run
  can never touch the database a deployment is using. This is now in place for the
  migration and error-vocabulary contracts; the parity half is W3's job.
* a **parametrised S3 lifecycle and failure-mode suite** — against SeaweedFS and
  RustFS, not against a mock.
* a **fetch-images ↔ compose parity test** — the script and the compose file cannot
  disagree, and the test says so.

#### W7 — The cleanup that the review found

**Status: not started.** The `BlobStore` protocol and the cold lake's own store
disagree; the S3 error path is not neutral, which breaks the contract W2 established
for the relational substrates; `TYPE_CHECKING` stubs are left over from a change that
made the imports runtime ones; and there are stale comments and stale documentation —
`docs/deployment/docker.md`, `ADR-0019`, the README, `.env.example`, and the
configuration reference.

#### W8 — The gate, the review, and the split

**Status: not started.** The full gate (`ruff` at 69 findings, `mypy` at 108 — eight
*below* HEAD's 116 — and a pytest failure set byte-identical to the baseline), one
final independent review of the whole diff, and then the commits. The split is by
concern, not by file, because several files carry hunks from more than one change set:
the substrate contract and its vocabulary; the translation at the boundary; the
migration machinery and the schema lexer; the live-server suites; the S3 rename; and
the container work. The container work and the S3 rename are *not* part of W1–W2 and
must not ride along in a substrate commit.

**After W8:** the ROADMAP's own Phase 0 (`docs/ROADMAP.md` 0.1–0.5 — validation,
agents, models, observability, CI), which is a separate list and is not started.

### Phase 1 — `SandboxSession` primitive

* `src/paa/sandbox/session.py` — `SandboxSession` ABC, `SubprocessSession`,
  `SessionLimits` (idle timeout, max lifetime).
* `Sandbox.start()` default → refuse.
* `DockerSandbox.start()` → `docker run -i`.
* `SubprocessSandbox.start()` → host process, `IsolationLevel.PROCESS`, honest.
* `DryRunSandbox.start()` → refuse.

**Tests** (`tests/sandbox/test_session.py`):
* a fake `docker` on `PATH` asserts the exact argv (`-i`, `--runtime runsc` only when the
  daemon reports it, `--network none`, `--read-only`, `--cap-drop ALL`, …);
* idle timeout → `docker kill` is called, not just the CLI killed;
* a session whose container exits → `read_line` returns EOF and pending MCP calls fail
  with `ConnectionError` (not a hang);
* `SubprocessSandbox.start()` reports `PROCESS`, never `NAMESPACE`.

### Phase 2 — MCP transport refactor

* Move `MCPClient` to `src/paa/tools/mcp_protocol.py`; add `MCPTransport`,
  `SubprocessTransport`, `SandboxSessionTransport`.
* Re-export from `src/paa/skills/adapters/mcp.py` for compatibility.
* `McpServerAdapter` gains a transport factory; the host-spawn path is no longer the
  default.

**Tests:** the existing `tests/skills/test_mcp.py` must stay green (retargeted at
`SubprocessTransport`); new tests for `SandboxSessionTransport` over a fake session.

### Phase 3 — Tool provider layer

* `src/paa/tools/base.py`, `registry.py`, `mcp_provider.py`, `exec_provider.py`,
  `native_provider.py`.
* Name mangling, collision refusal, mutating fail-closed, capability aggregation.

**Tests:** discovery against a fake MCP server; collision refusal; a tool that returns
`isError=true` becomes a failed result, not an exception (mirroring the existing
`_normalise_tool_result` semantics); output schema validation via the existing
`SchemaValidator`.

### Phase 4 — Manifests and loader

* `src/paa/tools/manifest.py`, `loader.py`.
* `PluginSettings` in `config.py` (`PAA_PLUGINS__*`).
* The manifest schema covers every field the design introduced: `trust`, `network`,
  `transport`, `addressing`, `scope` (the rollback scope of R3), `compensations` (R4),
  `env` (by name), `image_digest`, the mutating globs, and the timeouts.
* Loader scans the plugin directories, validates, builds providers, and **degrades**: a
  broken plugin is logged and skipped; the rest load.
* Validation refuses the contradictions: a container on both `paa_tools` and
  `paa_tools_egress` (§3.13 H1); `transport = "http"` without a `port`; a `trusted` plugin
  with a mutating tool and no `compensations` (R4); a plugin declaring a scope that is not
  a real table.

**Tests:** valid manifest; every invalid field rejected with a precise message; a broken
plugin does not stop the boot; secrets are referenced by name and never resolved into the
manifest; the "exactly one tools network" rule is enforced at validation time, not left to
Docker.

### Phase 4b — In-process plugins: the facade and the rollback machinery

* `src/paa/tools/context.py` — `PluginContext`, the capability facade of R1. It exposes
  `ledger` (typed events only), `memory` (cold-lake signal path), `secrets` (broker proxy),
  `model` (the router), `workspace` (a path jail), and `tools` (the registry). **No
  `Database`, no `LedgerStore`, no provider credentials.**
* `src/paa/tools/plugin_provider.py` — loads a `trust = "trusted"` Python module and
  registers its tools. Refuses `trust = "untrusted"` in-process plugins outright: an
  untrusted plugin is an MCP server or an exec plugin, and nothing else.
* `src/paa/tools/rollback.py` — R2 (one transaction per call), R3 (logical scope snapshot +
  SQLite `VACUUM INTO` physical checkpoint), R4 (compensation records), R5 (auto-disable
  trip), R6 (`paa plugins rollback`).
* New table `hot_serving_plugin_rollback` — **both** `schema_sqlite.sql` and
  `schema_postgres.sql`; the parity test will catch a divergence.
* New events: `TOOL_COMPENSATION_REQUIRED`, `PLUGIN_DISABLED`, `PLUGIN_ROLLED_BACK`.
* CLI: `paa plugins rollback <name>`, `paa plugins disable <name>`, `paa plugins enable
  <name>`.

**Tests:** a plugin that raises mid-call leaves no rows behind (the transaction rolled
back); a plugin that corrupts its scoped rows is restored by `paa plugins rollback`; a
plugin that fails three times is auto-disabled and the ledger says so; a plugin with no
compensations for a mutating tool is refused at validation; the plugin cannot reach the
`Database` (assert the facade has no such attribute).

### Phase 4c — HTTP-transport MCP

* `src/paa/tools/mcp_http.py` — the streamable-HTTP transport: `POST /mcp` with a JSON-RPC
  body, SSE responses, the `Mcp-Session-Id` header, and the bearer token.
* `McpToolProvider` gains `transport = "http"`: the container is created **detached**
  (`docker run -d`) with a healthcheck, on `paa_tools` (internal) or `paa_tools_egress`,
  addressed by container name (server topology) or by a published `127.0.0.1:<random>` port
  with a bearer token (dev topology).
* A boot-time sweep reaps leftover `paa-tool-*` containers.
* The server-initiated-request question (§3.13 H4) is settled here.

**Tests:** the HTTP path against a fake streamable-HTTP MCP server; a server that crashes
is recreated, not resurrected; the bearer token is required and rotated per session; the
container is on exactly one tools network; nothing is published in the server topology.

### Phase 5 — Wiring

* `Runtime.tools`; `Runtime.build()` composes the registry.
* `CoderAgent.build()` merges the catalogues and extends the mutating set.
* `coder/permissions.py` + `coder/loop.py`: mutating = glob OR declared.
* CLI: `paa plugins list`, `paa plugins validate <dir>`, `paa agent --list-tools` (already
  exists — it will now show the plugin tools), `paa doctor` shows plugin/MCP status and
  the isolation level.

**Tests:** a third-party tool is not PLAN-exempt; a declared-mutating MCP tool is refused
in PLAN mode; an explicit deny still wins; `--list-tools` shows namespaced names.

### Phase 6 — Ledger integration

* `TOOL_INVOKED` / `TOOL_COMPLETED` / `TOOL_FAILED` / `PLUGIN_REGISTERED` in
  `core/types.py`; `_NO_PHASE_CHANGE` entries in `replay.py`.
* The dispatch layer emits them.
* **Recovery rule:** a lineage with a `TOOL_INVOKED` and no matching completion at boot →
  `PARKED_HUMAN`, not auto-resume. *A side effect may have happened and the host cannot know
  which.* This is a semantic change to `RecoveryEngine` and needs its own review.
* **Suspicion rule (R5):** if the daemon died with a plugin's `TOOL_INVOKED` outstanding,
  that plugin is marked suspect and is **not auto-loaded** at the next boot.

**Tests:** event ordering (the event precedes the side effect — assert with an
instrumented fake); replay over arbitrary orderings (the existing Hypothesis suite in
`tests/ledger/test_replay.py` must keep passing); the recovery rule; the suspicion rule.

### Phase 6b — Egress confirmation and the install surface

* `paa plugins install <dir>` — the operator-facing install: validates the manifest,
  **shows the egress request explicitly and requires a confirmation**, records the
  decision in the ledger, and writes the plugin into the user-scope plugin directory.
* `paa plugins list` — every plugin, its transport, its image + digest, its network, its
  tools, its mutating count, and its enabled/disabled state.
* `paa plugins validate <dir>` — manifest + schema check without installing.

**Tests:** an egress-requiring plugin cannot be installed without the confirmation; the
confirmation is recorded; a disabled plugin is not loaded; `paa plugins list` shows the
isolation level of the backend that would run it.

### Phase 7 — The confinement test suite

The acceptance criterion the user actually cares about. A deliberately hostile MCP server,
run inside gVisor, must fail at every escape:

| Attempt | Expected |
|---|---|
| read `/etc/shadow` | fails (no such file — the host's is not in the container) |
| read the host's `ANTHROPIC_API_KEY` from the env | absent (env allowlist) |
| write to `/etc/paa` | fails (read-only rootfs) |
| write outside `/workspace` | fails (not mounted) |
| `curl http://example.com` | fails (`--network none`) |
| `curl http://postgres:5432` | fails (not on the network) |
| connect to the host's Docker socket | fails (not mounted) |
| `fork()` 5000 times | fails (`--pids-limit`) |
| allocate 2 GB | killed (or the honest answer from Phase 0's measurement) |

Marked `requires_docker` / `requires_gvisor` so the default suite stays runnable.

### Phase 8 — Documentation and ADRs

* `docs/modules/tools.md`.
* Update `docs/modules/sandbox.md` (the session primitive), `docs/modules/skills.md` (the
  MCP client moved), `docs/deployment/docker.md` (the measured gVisor facts),
  `docs/modules/cli.md`, `AGENTS.md`, `README.md`.
* `docs/adr/0027-tool-containment-and-mcp.md` — the containment model, the "no host spawn"
  invariant, and the SPEC DEVIATION notes for the new event types and the new tool layer.
  (0024 is taken: the Docker socket and gVisor containment. The decisions about *how* a
  container is launched and contained are already recorded there — the socket, the
  `runtimeArgs`, the `--user` invariant, and the isolation level following the effective
  runtime — so 0027 is about the layer this document plans and nothing below it.
  The number is reserved, and the ADR is written when the layer is built: an ADR records
  a decision that has been made, and this one has not been taken yet.)
* `docs/adr/README.md` index update.

---

## 5. Test strategy

| Layer | What is proven | Where |
|---|---|---|
| Unit | argv construction, manifest validation, name mangling, mutating union, framing | `tests/sandbox/test_session.py`, `tests/tools/` |
| Hypothesis | the invariants that must hold for *all* inputs: name mangling is injective, mutating-union is monotonic in the declared flag, capability sets are supersets | `tests/tools/test_properties.py` |
| Integration (no Docker) | a fake MCP server over a loopback/in-process transport; provider dispatch; ledger ordering | `tests/tools/test_mcp_provider.py` |
| Integration (HTTP) | the streamable-HTTP transport against a fake server: session id, SSE, bearer token, server-request refusal | `tests/tools/test_mcp_http.py` |
| Integration (Docker) | the real `docker run -i --runtime=runsc` path, end to end | `tests/tools/test_contained_mcp.py`, marker `requires_docker` |
| Confinement | the escape-attempt table in §4, Phase 7 | `tests/tools/test_confinement.py`, marker `requires_gvisor` |
| Rollback | a plugin that raises mid-call leaves no rows; a plugin that corrupts its scope is restored; three failures auto-disable it; compensations replay in reverse | `tests/tools/test_rollback.py` |
| Chaos | the host is killed mid-call → the ledger shows `TOOL_INVOKED` with no completion → recovery parks it; the plugin is marked suspect and not auto-loaded | `tests/tools/test_chaos.py`, marker `chaos` |

The AGENTS.md rules apply throughout: `ruff` (line-length 100, `S` on), `mypy --strict`, and
the baseline is red — *do not expand the count* is the gate, not "make it zero".

---

## 6. Risks and how the design handles them

| Risk | Handling |
|---|---|
| gVisor is not installed / not registered | Phase 0 makes it a hard prerequisite; `DockerSandbox` already reports `NAMESPACE` honestly when `runsc` is absent, and the plugin loader **refuses to start a container** whose isolation level is below what the plugin's risk profile demands. |
| The `paa` container cannot reach the Docker socket (gVisor blocks host Unix sockets by default) | §3.14.1 — `--host-uds=all` in the daemon's runtime registration, verified in Phase 0 from *inside* a runsc-hosted container. This is the most likely first-hour failure, so it is verified first. |
| The docker socket is host root | Accepted deliberately (Decision 2). Recorded in §3.14.3 so it reads as a decision, not an oversight. The daemon is the only holder; it runs under gVisor; its own exposure is a loopback API. |
| A plugin escapes the container | gVisor's syscall boundary is the answer. Residual risk: a gVisor escape. Mitigated by minimal images, `--read-only`, no socket *inside* the tool container, all caps dropped, no egress by default. |
| A plugin silently becomes read-only because its name matches no glob | The declared-mutating union, fail-closed default, and `mutating_tools=(*DEFAULT_MUTATING_TOOLS, *MUTATING_RUNTIME_TOOLS, *runtime.tools.mutating_names())`. |
| A plugin prompts for a secret and gets the host's DB DSN | `SecretBroker` gates on `PERM_SECRET_READ` and returns a redacting `SecretValue`; the launch env is an explicit allowlist built from empty. |
| An in-process plugin corrupts the daemon or the database | R1 (facade, no raw state) → R2 (one transaction per call) → R3 (checkpoint) → R4 (compensations) → R5 (auto-disable). The residual limit — daemon *memory* corruption is not rollback-able — is stated in §3.12, not glossed. |
| An HTTP MCP server is reachable by other local processes (dev topology) | §3.13 H2/H6 — random `127.0.0.1` port, per-session bearer token, container stopped with the daemon. The server topology publishes nothing. |
| A tool container is accidentally on both tools networks, restoring egress | The manifest validation refuses the combination (§4, Phase 4); `paa doctor` reports each container's networks. |
| Tool output carries a prompt injection | Tool output is untrusted input. The deterministic policy agent and the ledger are the backstop. A tool-output-specific policy rule is worth considering in a later phase. |
| The skills subsystem and the new tools layer duplicate each other | Decision 1: the new layer is authoritative for tools; the skill registry is left alone, and the debt is written down. A follow-up decides whether the skill registry is retired, absorbed, or projected onto the tool registry. |
| The tool catalogue grows past the model's context budget | The registry can rank/filter by intent (the skill registry already has `search()`). Deferred; the model-side budget is the constraint that forces it. |
| gVisor + cgroup v2 means memory limits are not enforced | **Measured, and false**: the kernel's cgroup enforced the cap under both runtimes (`oom_kill 1`, `memory.peak == memory.max`). `memory_enforcement` reports `"cgroup"`. |

---

## 7. Decisions

### Taken (2026-10-20)

| # | Decision | Where it is specified |
|---|---|---|
| 1 | A new first-class `src/paa/tools/` layer, reusing the MCP protocol client. The skill registry is left alone for now and the duplication is written down as debt. | §3, §3.1 |
| 2 | The `paa` daemon container gets `/var/run/docker.sock` + the docker CLI, and the daemon itself runs under `runtime: runsc`. The socket is host root; that is accepted deliberately. | §3.14 |
| 3 | Tool invocations go on the **ledger**: `TOOL_INVOKED` (before the side effect), `TOOL_COMPLETED`, `TOOL_FAILED`, plus `TOOL_COMPENSATION_REQUIRED`, `PLUGIN_DISABLED`, `PLUGIN_ROLLED_BACK`, `PLUGIN_REGISTERED`. | §3.11, §3.12 R4, Phase 6 |
| 4 | In-process Python plugins are **allowed** (`trust = "trusted"` only), with the six-mechanism rollback model. | §3.12 |
| 5 | Network egress is a **per-plugin opt-in**, confirmed at install time. | Phase 6b, §3.13 H1 |
| 6 | gVisor is the **official `release-20261012.0` static binary**, pinned by version and checksum. | Phase 0 |
| 7 | **HTTP-transport MCP is in scope**, with its own containment model. | §3.13 |

### Still open

**A. Literal in-process, or in-process *capability* over a supervised child process?**
This is the one question worth settling before Phase 4b, because it changes the process
boundary and the RPC layer, though not the rollback mechanisms (R1–R6 are the same either
way). My recommendation is the **supervised child process**: nearly the same capability, a
crash does not take the daemon down, and the parent owns the transaction. The cost is an
RPC surface that must itself be a capability boundary.

**B. Server-initiated MCP requests.** Under HTTP, a server may ask the client to sample
from the model or list roots. The current client ignores them. Either implement the
server-request side (so an MCP server can call back into PAA's model router), or declare
the runtime a "no server-initiated requests" client and refuse servers that need them.
Recommendation: **refuse them for now** and log the refusal clearly — the callback path
would need its own capability model, and it is not needed for the tool surface this plan
is about.

**C. Does the orchestrator's worker get plugin tools too?**
The planner's steps already run in the sandbox (`WorkerCell` → `Sandbox.run`).
Recommendation: no, initially — plugin tools are for the agent surface, and wiring them
into the planner means the planner's *output* would name tools it cannot safely call.

**D. Where do plugin manifests live, and who writes them?**
`PAA_HOME/plugins/` (user scope) and `./plugins/` (project scope). Should there be a
`paa plugins install <url>` that fetches a manifest over the network? That crosses into
the marketplace territory (`src/paa/marketplace/`), whose installer currently does
metadata registration only and whose smoke-test gate is a no-op. Recommendation: no remote
install in this phase — local directories only, and the marketplace work stays separate.

**E. Naming.** `mcp__<server>__<tool>` for MCP, `exec__<plugin>__<tool>` for exec
plugins, and (if in-process plugins are allowed) `plugin__<plugin>__<tool>`. Alternative:
one flat namespace for everything. Recommendation: the prefix split, because an MCP
server, an exec plugin, and an in-process plugin are three different trust objects.

**F. What happens to the skill registry?** Decision 1 left it alone. A follow-up should
decide whether it is retired, absorbed into the tool registry, or projected onto it. Not
blocking — but it is the debt this plan creates, and it should be tracked rather than
forgotten.

---

## 8. Deliverable sequence at a glance

```
Phase 0   host: runsc (official) + daemon.json w/ --host-uds + toolbox image
          + docker CLI + socket + the three networks + measure cgroups   (no code)
W3–W8    substrate track: entity-resolution parity, the deployment's images,
          the container's start budget, the suites that catch all of it,
          the cleanup, the gate and the split                        (see §4)
Phase 1   SandboxSession primitive                                        (sandbox/)
Phase 2   MCP transport refactor                                          (tools/mcp_protocol.py)
Phase 3   ToolProvider layer                                              (tools/)
Phase 4   Manifests + loader + PluginSettings                             (tools/, config.py)
Phase 4b  In-process facade + rollback machinery                          (tools/context.py,
                                                                             tools/rollback.py)
Phase 4c  HTTP-transport MCP                                              (tools/mcp_http.py)
Phase 5   Wire into Runtime + CoderAgent + CLI                            (runtime.py, coder/, cli/)
Phase 6   Ledger events + recovery rule + suspicion rule                 (core/types.py, ledger/)
Phase 6b  Egress confirmation + install surface                           (cli/, tools/)
Phase 7   Confinement test suite                                          (tests/tools/)
Phase 8   Docs + ADRs                                                     (docs/)
```

Phases 1–3 are the technical core and can be reviewed as one unit. Phase 4–5 are the
user-facing part. Phase 4b/4c are the two extensions the decisions asked for. Phase 6 is
the auditability upgrade. Phase 7 is the proof.

The **W3–W8 track** is not one of these phases and does not compete with them for
review attention: it is the substrate the phases above stand on, and it is the work
that makes the two relational engines interchangeable — which is the assumption every
layer above it makes without checking.
