# ADR-0024: The daemon holds the Docker socket, and tool containers run under gVisor

## Status

Accepted

## Context

RFC §13 specifies gVisor (`runsc`) as the containment substrate for anything the runtime
executes on the runtime's behalf, and ADR-0006 made the sandbox backend pluggable because
the machine the runtime was first built on could not run gVisor at all. ADR-0019 then
introduced the Docker server topology, whose `DockerSandbox` requests `--runtime=runsc`
*when the daemon reports it registered* and reports `IsolationLevel.VM` only when that is
true.

Two things were left unbuilt, and both are prerequisites for the tool surface the user
asked for (MCP servers and plugins whose tool executions stay inside a container):

**1. The daemon could not launch a container at all.** The `paa` runtime image is
`python:3.12-slim` with no Docker client, and `docker-compose.yml` mounted only the `paa`
data volume — no `/var/run/docker.sock`. The `DockerSandbox` backend was therefore
unreachable in the server topology: the code was correct, and the deployment could not
exercise it. Separately, `DockerSandbox.DEFAULT_IMAGE` named `paa/base_worker:v4.1`, and
**no Dockerfile in the repository built that image** — the recorded default and the
executed default were the same string pointing at nothing.

**2. There was no "the tool is contained" story to point at.** The plan in
`docs/MCP_AND_PLUGINS.md` requires every third-party tool execution — an MCP server, a
plugin command — to happen inside a gVisor container, with the host only ever speaking a
protocol to it. That needs three things the deployment did not have: a container image
that can host an MCP server, a container runtime the daemon can ask for, and a daemon
that can reach the Docker daemon at all.

The user chose, explicitly: the socket in the `paa` container (simple, effectively host
root), the daemon itself under `runsc`, the official gVisor release, and HTTP-transport
MCP alongside stdio.

## Decision

**The `paa` container mounts `/var/run/docker.sock` and carries the Docker client.**

- The client is Docker's **official static build**, pinned by SHA-256 and verified at
  image-build time — never a hand-dropped binary of unknown provenance, and never an apt
  package. The static build is used rather than `docker-ce-cli` for two reasons that are
  about the future, not the present: a package version is spelled per distribution release
  (`5:29.9.1-1~debian.13~trixie`), so pinning one couples the image to a codename, and
  Docker's `stable` channel prunes old package versions, so a pinned package version
  eventually stops existing and the build breaks for a reason that has nothing to do with
  this code. The static tarball is architecture-independent of the distro and Docker keeps
  it. Docker publishes no checksum for the static tarballs, so the digest is recorded in
  the Dockerfile — the same trust model as every other pinned download, and a strictly
  stronger one than "apt installed whatever was newest this morning". The daemon package is
  deliberately not installed: this container talks to the host's daemon, it does not run
  one.
- The socket's group GID is added to the container's `paa` user as a supplementary group
  (`group_add`), because the socket is `root:<docker-gid>` mode `0660` and the container's
  uid 10001 is not in the host's `docker` group. The GID is **configuration** (`DOCKER_GID`),
  not something baked into the image, because it is a property of the host.
- **The socket is host root and that is accepted deliberately.** Anyone who can talk to it
  can start a privileged container on the host. The mitigations are that the daemon is the
  only holder, the daemon runs as a non-root user with no capabilities of its own, and
  the containers it launches are hardened (`--read-only`, `--cap-drop ALL`,
  `--no-new-privileges`, and `--network none` whenever the network is denied). The
  alternative — a socket proxy exposing only the container verbs — was considered and
  deferred; the executor-sidecar design (the daemon never holds the socket at all) is the
  real fix and is more work.

**gVisor is registered with the Docker daemon, with the host Unix socket named
explicitly.**

```json
{ "runtimes": { "runsc": {
    "path": "/usr/local/bin/runsc",
    "runtimeArgs": [
      "--hostuds=/var/run/docker.sock",
      "--hostuds=/run/docker.sock" ] } } }
```

The `runtimeArgs` are not decoration. gVisor does not let a container open a host Unix
socket merely because it was bind-mounted in; the socket must be named. Without
`--hostuds`, the Docker client inside the `paa` container fails with a permission error
that reads like a socket problem and not like a gVisor problem, and the whole gVisor path
looks half-working. This is the single most likely way to get the deployment wrong, which
is why `deploy/docker/daemon.json` carries it and
`docker-compose.gvisor.yml` documents it.

Both spellings are listed because `/var/run` is a symlink to `/run` on the host but not
necessarily inside the container, and gVisor matches the `hostuds` path against the mount
namespace it is given. Four files carry the list — `deploy/docker/daemon.json`,
`docker-compose.gvisor.yml`, `docs/deployment/docker.md` and `docs/MCP_AND_PLUGINS.md` — and
they must agree; a test asserts that the first three carry both spellings.

**`deploy/docker/daemon.json` is plain JSON, and the explanation lives beside it.** The
Docker daemon has no comment syntax and *refuses to start* on a top-level key it does not
recognise — so a `_comment` key in that file would stop the host's Docker the moment an
operator followed the documented install procedure. The prose is in
`deploy/docker/README.md`, and `tests/deployment/test_daemon_config.py` asserts that every
top-level key is one the daemon knows.

**Two container images, with different jobs.**

- `paa/base_worker:v4.1` (`deploy/worker/Dockerfile`) — the image the sandbox backend
  launches for a *plan step*. It runs untrusted work, so it is deliberately minimal.
- `paa/toolbox:v1` (`deploy/toolbox/Dockerfile`) — the image MCP servers and plugin
  commands run in. It carries a Node runtime, because the overwhelming majority of
  published MCP servers are npm packages started with `npx`. The `paa` package is
  **not** installed in it: a plugin that imported it would expect a database connection
  and a ledger handle, and the container has neither.

Both run as a non-root uid, and the sandbox backend passes `--user` derived from
the workspace's owner, so the container can write the workspace it is given
without running as container-root. (Before Phase 0, the container ran as the
image's default — root — and the `--user` flag did not exist; both the
Dockerfiles and this ADR claimed otherwise, and the review caught it.)

**The container's uid is never 0, and the flag is never omitted.** `--user` is always
passed. Omitting it means "whatever the image says", and an image that says nothing runs
as root — so the flag is an invariant rather than a convenience. The uid is the
workspace's owner when that owner is known and not root; otherwise the image's own
declared user, when it declares one that is not root; otherwise the uid every image in
this repository ships. A **root-owned workspace** is refused at the first step:
`--user 0:0` would put the workload back in container-root, and the exact harm `--user`
exists to prevent — root-owned files in a directory the operator owns — is what would
happen. The first draft of this ADR said "not as root" without saying what happens when
the workspace *is* root-owned, and the review caught that too.

**The data directory is a host bind mount, not a named volume.** The Docker
daemon resolves a bind mount's source against the *host* filesystem, so the
workspace path the runtime knows is not the path the daemon must be given. With
a named volume that path cannot be translated at all, and the failure is silent:
the daemon creates an empty, root-owned directory in its place and the workload
runs against nothing. The fix is two halves — a host bind mount at a known path
(`PAA_DATA_DIR`), and `PAA_SANDBOX__HOST_PATH_MAP` telling the backend how to
translate the runtime's view into the host's.

The map is a **string**, not a tuple of pairs, and that is load-bearing.
`pydantic-settings` decodes a complex-typed field from an environment variable as JSON,
so a tuple field would demand `[["/data","/srv/paa/data"]]` in the environment. The
compose file said `/data=/srv/paa/data` — the obvious spelling — and the result was a
settings error at import: the container died before it could serve a request, and before
it could run the `paa doctor` that would have explained why. The field is therefore a
plain string with `NoDecode`, parsed by the settings layer, and a malformed entry is
reported by the doctor rather than raised at import. It is still **strict** at the point
of use: a broken entry refuses the run rather than being skipped, because a skipped entry
means the daemon is handed the runtime's own path and answers by creating an empty,
root-owned directory in its place.

**Three networks, declared before any tool container exists.** `paa_net` (the runtime and
its substrate, with external routing for model escalation), `paa_tools` (`internal: true`
— a container attached only to it has *no* external routing at all), and
`paa_tools_egress` (egress, but never `paa_net`). A tool container joins **exactly one**
of the last two; a container on both would route its egress through the wrong one.

Compose creates only the networks a service attaches to, so `paa_tools_egress` is
*declared* but materialises only once something joins it — and the tool containers are
launched by the runtime through the Docker API, not by Compose, so the runtime creates
the network it needs. The declaration is the reservation; the creation is the tool
layer's job.

**A networked container names its network.** `SandboxSpec.network` and
`SandboxSettings.default_container_network` exist so that "has network access"
is not the same thing as "is on the Docker default bridge" — the default bridge
reaches the host's gateway address and the internet, which is more than
"networked" implies. The backend names the network explicitly and refuses to
start a container on a network that does not exist, rather than letting Docker
fail with a message that reads like a workload error.

## Consequences

**The daemon can now launch containers, and `paa doctor` can prove it.** The runtime owns
the sandbox as a first-class attribute (`Runtime.sandbox`) rather than being reached for
through the worker's private `_sandbox`, and `Sandbox.diagnose()` reports the container
runtime, whether the daemon actually has it, and the image — the three facts that decide
what containment a run gets. A backend that reports `NAMESPACE` because `runsc` was asked
for and is not registered now *says so*, instead of looking like a weaker configuration
choice.

**The socket is the weakest link in the chain.** It is a deliberate, recorded, and
reversible decision, but it is real: a compromise of the `paa` daemon is a compromise of
the host. The path to removing it is the executor sidecar, in which the daemon talks to a
service that owns the socket over the loopback API and never holds it itself. Until then,
the daemon should run under `runsc` as well (the `docker-compose.gvisor.yml` override
does), so the daemon's own syscall surface is bounded.

**gVisor changes what "the memory limit" means.** Docker's `--memory` and `--cpus` are
cgroup limits, and gVisor on a cgroup v2 host has limited cgroup delegation, so those
limits are not enforced the way `runc` enforces them. The syscall boundary is *stronger*
under gVisor and the resource ceiling is *weaker*; both are stated rather than traded
away silently. `SandboxResult.memory_enforcement` reports the **mechanism** — `"gvisor"`
or `"cgroup"` — not the aspiration, so the ledger never records a ceiling that was not
applied. (The first draft of this ADR claimed the field already did that; it did not,
and the review caught it.)

The mechanism follows the **effective** runtime, not the requested one. A host that asks
for gVisor but has not registered it runs the container under runc, with a real cgroup
cap, and that run records `"cgroup"`. Keying it on the request would put a ceiling in
the ledger on every host where gVisor is not installed — which is every host, by default.

**The resource ceiling under gVisor is documented, not measured.** That gVisor's cgroup
delegation is limited enough on a cgroup v2 host that `--memory` is not a hard ceiling is
stated in this ADR and in the deployment guide on the authority of gVisor's own
documentation. It has **not** been measured on this host, because `runsc` is not
registered here — that needs root, and it is the one Phase 0 item still outstanding.
Until it is measured, the claim is a documented expectation rather than a verified fact,
and the ledger's `"gvisor"` string should be read as "gVisor did the accounting", not as
"the cap was enforced to the byte".

**The tool image needs an exec-capable scratch path, and `/tmp` must stay
`noexec`.** npm's cache holds the executable shims `npx` installs, so an
`npx`-started MCP server fails with `Permission denied` (exit 126) when the cache
is on a `noexec` mount. The answer is not to weaken `/tmp`: `SandboxSpec.exec_tmpfs`
gives those workloads their own writable, exec-capable path (`/run`), and the
toolbox image points npm's cache there. The paths are validated, because a tmpfs mount
*replaces* the path it is mounted on: mounting over `/usr` would not give the workload a
scratch directory, it would give it an empty filesystem where the image's own binaries
were.

**The recorded worker image and the executed worker image can no longer drift.** They were
the same string pointing at an image nobody had built. Now the string names a Dockerfile
in this repository, and `tests/storage/test_schema_parity.py` asserts that the
backend's default, the ledger event's default and both SQL schema defaults are the
same string — the link the parity test did not previously make.

**The doctor renders one report, and both surfaces render it.** The facts are produced by
the sandbox package, so their vocabulary belongs there too: `sandbox_report_rows` in
`paa/sandbox/base.py` is the single renderer, used by `paa doctor` and by the TUI
dashboard alike. Two renderers with two copies of the vocabulary drift, and a runtime
that tells the operator two different things about the same host — depending on which
surface they opened — is worse than either of them being wrong alone, because the
operator has no way to tell which to believe.

The same principle decides what a backend with nothing extra to report says. The base
`Sandbox.diagnose()` returns the backend's *identity* — its name and its isolation
level — rather than an empty dict. An empty dict would have rendered as "none
(dry-run)" for a subprocess backend, which is not merely unhelpful but **false**: the
workload does run, on the host, at `PROCESS` isolation. "What containment will my run
actually get" is the question that row exists to answer, and it has to answer it
honestly on every backend, not only on the one this ADR is about.

**Moving back is a configuration change, which was the point.** The socket mount, the
`group_add`, and the `runtimeArgs` are all in the compose files and one JSON file. A
deployment that decides the socket is too much can remove the mount and fall back to the
subprocess backend, and the runtime will report the weaker containment truthfully rather
than pretending.
