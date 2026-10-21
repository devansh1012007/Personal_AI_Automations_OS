# Docker deployment — the server topology

This runs PAA with the RFC's original stack: PostgreSQL, a Qdrant server, Redis,
an S3-compatible object store, and (optionally) a gVisor-contained sandbox. It
is the counterpart to the laptop's embedded default. Both are the same codebase;
the difference is configuration. See `docs/adr/0019-docker-server-deployment.md`
for the design, and `docs/adr/0026-s3-cold-lake-substrate.md` for why the object
store is the *S3 protocol* rather than one vendor's server.

## Laptop vs server: which to run

| | Laptop (embedded, default) | Server (Docker, this doc) |
|---|---|---|
| Relational | SQLite (WAL), in-process | PostgreSQL 16 |
| Cold lake | content-addressed files | S3 (SeaweedFS) |
| Queue | SQLite durable queue | Redis (AOF) |
| Vector | numpy / embedded Qdrant | Qdrant server |
| Sandbox | subprocess / WSL | Docker, optional gVisor `runsc` |
| Resident overhead | ~0 MB of servers | several GB across services |
| Needs Docker | no | yes |
| Best for | a single developer machine, ≤ ~3.5 GB free | a host with RAM to spare, real concurrency, stronger containment |

Run the laptop path with nothing installed but the core dependencies (`pip
install .`). Run the server path when you want PostgreSQL's concurrency, object
storage that scales past one disk, and — on Linux — a syscall-boundary sandbox.

## Prerequisites

- Docker Engine + Compose v2 (`docker compose`, not the old `docker-compose`).
- For gVisor only: a **Linux** host with `runsc` installed and registered (see
  below). gVisor does not exist on Windows or macOS.

## First run

```bash
cp .env.example .env
# edit .env: set POSTGRES_PASSWORD, S3_ROOT_USER/S3_ROOT_PASSWORD, etc.

docker compose up -d --build
docker compose ps            # all services healthy except qdrant (started)
docker compose logs -f paa   # watch the runtime boot
```

The runtime applies `schema_postgres.sql` on first connect and creates the
S3 bucket. The ingestion API is reachable at `http://127.0.0.1:8787` — and
**only** there.

Stop with `docker compose down`; add `-v` to also drop the named volumes (this
destroys the ledger — do it deliberately).

## What is exposed, and what is not

Security posture is loopback-first by construction:

- **Only** the runtime's ingestion API publishes a host port, bound to
  `127.0.0.1:8787`. `Settings._enforce_loopback` refuses a non-loopback
  `api_host` unless `PAA_ALLOW_NON_LOOPBACK=1` is set deliberately, so a typo
  cannot expose the runtime.
- PostgreSQL, Redis, Qdrant and the object store publish **no host ports**.
  They are reachable only from the `paa` container over the internal `paa_net`
  bridge. The object store's console is not published at all: the `seaweedfs`
  service in `docker-compose.yml` binds nothing, so there is no port mapping to
  uncomment — administer it from inside the network, or bind one deliberately
  and never to `0.0.0.0`.
- Secrets live in `.env` (git-ignored, `.dockerignore`-d). Config stores only
  the *names* of the env vars that hold the S3 credentials; the values never
  enter config or the ledger.

`paa_net` is not marked `internal` because the runtime needs outbound access for
model escalation. The backing services still publish no host ports, so they
remain unreachable from outside the compose project regardless.

### The Docker socket — read this before deploying

The `paa` container mounts `/var/run/docker.sock`. The runtime daemon needs it to
launch the throwaway containers the sandbox backend runs commands in, and — once
the tool layer lands — the MCP-server and plugin containers.

**This is host root.** Anyone who can talk to that socket can start a privileged
container on the host. The decision, and the alternatives that were considered
and deferred, are recorded in [ADR-0024](../adr/0024-docker-socket-and-gvisor-containment.md).

Two things make it work, and both are easy to get wrong:

1. **The socket's group GID must be added to the container's user.** The socket is
   `root:<docker-gid>` mode `0660`; the container's `paa` user is uid 10001 and is
   not in the host's `docker` group. `docker-compose.yml` passes it with
   `group_add`, sourced from `DOCKER_GID` in `.env`:

   ```bash
   echo "DOCKER_GID=$(stat -c %g /var/run/docker.sock)" >> .env
   ```

   Without it the client fails with `permission denied while trying to connect to
   the docker API at unix:///var/run/docker.sock` — which reads like a socket
   problem, and is one.

2. **The image carries the Docker *client*, pinned by SHA-256.** The daemon package is
   deliberately not installed — the container talks to the host's daemon, it does not run
   one. The client is Docker's official **static** build, fetched at image-build time and
   verified against a SHA-256 recorded in the `Dockerfile`. It is static rather than an
   apt package because a package version is spelled per distribution release and Docker's
   `stable` channel prunes old versions, so pinning a package would eventually break the
   build for reasons unrelated to this code.

### The three networks

| Network | `internal` | Who is on it | External routing |
|---|---|---|---|
| `paa_net` | no | `paa`, postgres, redis, qdrant, seaweedfs | yes — the runtime needs model-escalation egress |
| `paa_tools` | **yes** | `paa`, and the tool containers that need none | **no** — a container attached only to this network reaches the daemon and the other tools, and nothing else |
| `paa_tools_egress` | no | tool containers that genuinely need the internet | yes, but never `paa_net` — so still no Postgres/Redis/Qdrant/S3 |

A tool container joins **exactly one** of `paa_tools` / `paa_tools_egress`. A
container on both would route its egress through the wrong one, and that is
exactly the mistake the split exists to prevent. `paa` is on both `paa_net` and
`paa_tools`, with an explicit `priority` so that `paa_net` supplies the default
route and the daemon keeps its egress.

Compose only creates the networks a service actually attaches to, so
`paa_tools_egress` is *declared* here but materialises only once something
attaches to it. The tool containers are launched by the runtime through the
Docker API rather than by Compose, so the runtime creates the network it needs
(`docker network create`) — which is a Phase 4c concern, not this one.

## Enabling gVisor (`runsc`)

gVisor interposes a user-space kernel between the container and the host — the
syscall boundary the RFC §13 requires and that namespaces alone do not provide.
It is **opt-in** and **Linux-only**.

1. Install the official static release. The GitHub release tarball
   (`gvisor-x86_64.tar.bz2`) carries a `SHA256SUMS` file; verify against it:

   ```bash
   curl -fsSLO https://github.com/google/gvisor/releases/download/release-20261012.0/SHA256SUMS
   curl -fsSLO https://github.com/google/gvisor/releases/download/release-20261012.0/gvisor-x86_64.tar.bz2
   sha256sum -c --ignore-missing SHA256SUMS
   tar -xjf gvisor-x86_64.tar.bz2
   runsc --version          # runsc version release-20261012.0
   ```

   The `storage.googleapis.com/gvisor/releases/release/<date>/` mirror lags the
   GitHub releases, so the GitHub tarball is the authoritative source for a
   current version.

   **The whole tarball goes in, not just `runsc`.** The release ships `runsc`
   *and* a `gvisor-bin/` directory of sidecars — `gvisor_sentry`,
   `checkpointgofer`, `runsc-fd-parking`, `runsc-metric-server` — plus the
   tarball's own `containerd-shim-runsc-v1`. `runsc` looks for the sentry
   *next to itself*, at `<dir-of-runsc>/gvisor-bin/gvisor_sentry`, so a
   runtime installed on its own is a runtime that is registered and cannot
   start a container:

   ```bash
   sudo install -m 0755 runsc /usr/local/bin/runsc
   sudo install -d -m 0755 /usr/local/bin/gvisor-bin
   sudo install -m 0755 gvisor-bin/* /usr/local/bin/gvisor-bin/
   sudo install -m 0755 containerd-shim-runsc-v1 /usr/local/bin/containerd-shim-runsc-v1
   ```

   The symptom of getting this wrong is
   `sidecar "gvisor_sentry" not usable: stat /usr/local/bin/gvisor-bin/gvisor_sentry:
   no such file or directory`, and it arrives as a *failed task* rather than
   as anything that names the sidecar.

2. Register it with the daemon in `/etc/docker/daemon.json` — **including the
   `runtimeArgs`**, which are not optional (see below). A ready-to-copy file
   is in [`deploy/docker/daemon.json`](../../deploy/docker/daemon.json), and
   the reasoning behind it is in
   [`deploy/docker/README.md`](../../deploy/docker/README.md):

   ```json
   { "runtimes": { "runsc": {
       "path": "/usr/local/bin/runsc",
       "runtimeArgs": [
         "--host-uds=all" ] } } }
   ```

   The permission is a **category**, not a path: `--host-uds=all` lets the
   sandbox connect to host Unix-domain sockets. Keep this flag in step with
   `deploy/docker/daemon.json` and `docker-compose.gvisor.yml`.

   > **The spelling changed, and the old one is worse than useless.** The
   > pinned build (`release-20261012.0`) spells the flag `--host-uds` and
   > takes `none|open|create|all`. The older `--hostuds=<path>` — which
   > named one socket — is **gone**: a runtime handed it exits 2 with
   > "flag provided but not defined", and the daemon reports
   > `failed to create shim task … did not terminate successfully: exit
   > status 2`. The daemon does **not** validate a runtime's flags when it
   > registers one, so `docker info` listing `runsc` proves nothing about
   > the runtime working. That is why step 3 runs a container and not just
   > a query.

   > **The file must be plain JSON.** The Docker daemon has no comment
   > syntax and *refuses to start* on a top-level key it does not recognise
   > — so the explanation lives in `deploy/docker/README.md`, not in the
   > file. Copying the file **overwrites** the host's daemon configuration;
   > merge it instead if the host already has one.

   then `sudo systemctl restart docker`.

3. Verify the daemon sees it:

   ```bash
   docker info --format '{{json .Runtimes}}'   # must include "runsc"
   docker run --rm --runtime=runsc hello-world
   ```

4. Bring the stack up with the override:

   ```bash
   docker compose -f docker-compose.yml -f docker-compose.gvisor.yml up -d
   ```

### `--host-uds` — the one thing that makes the socket work under gVisor

gVisor does **not** let a container open a host Unix socket merely because the
socket was bind-mounted into it. The permission has to be given to the
*runtime*, or the Docker client inside the `paa` container fails with
`permission denied while trying to connect to the docker API` — an error that
reads like a socket-permission problem and not like a gVisor problem, and that
makes the whole gVisor path look half-working.

The flag is `--host-uds=all`: it permits the sandbox to connect to host
Unix-domain sockets. The three places that carry it must agree:
`deploy/docker/daemon.json`, `docker-compose.gvisor.yml`, and this document —
and `tests/deployment/test_daemon_config.py` is what keeps them in step.

If the `paa` container also fails to *start* under runsc, Docker's default
seccomp profile can conflict with gVisor's own syscall policy. Add
`security_opt: [seccomp=unconfined]` to the `paa` service in
`docker-compose.gvisor.yml`. That is safe **because** gVisor is the syscall
boundary — Docker's seccomp filter is redundant once Sentry is interposing.

### What gVisor does and does not change

The override sets `PAA_SANDBOX__CONTAINER_RUNTIME=runsc`, so the throwaway worker
containers the runtime launches request `--runtime=runsc`. The runtime uses it
**only** when the daemon confirms it is registered, and reports
`IsolationLevel.VM` only then; if runsc is requested but absent it falls back to
`runc` and honestly reports `NAMESPACE` rather than claiming containment it does
not have (`paa/sandbox/docker_backend.py`, verified by
`tests/sandbox/test_docker_backend.py`). The override also runs the `paa`
container itself under runsc as defence in depth — comment that line out to keep
only the worker containers contained, which is the minimal sufficient hardening.

**The syscall boundary gets stronger; the resource ceiling does not get weaker.**
Docker's `--memory` and `--cpus` are *daemon*-level limits — the daemon caps the
container's cgroup and refuses to start a container whose limit it cannot apply —
and the OCI runtime in charge does not change that. gVisor strengthens the syscall
boundary; it does not get an exception to the ceiling.

This was **measured**, on this host, with `runsc` registered. With `--memory 256m`
and a process that touches 512 MiB:

==================  ==========  ========  =========================
runtime            exit code   OOMKilled  cgroup ``memory.events``
==================  ==========  ========  =========================
``runc``           137         true      ``oom 1 oom_kill 1``
``runsc`` (gVisor) 137         true      ``oom 1 oom_kill 1``
==================  ==========  ========  =========================

and in both cases ``memory.peak`` reached ``memory.max`` exactly — the kernel's own
``oom_kill`` counter is what fired, under gVisor as much as under runc.
`SandboxResult.memory_enforcement` therefore reports `"cgroup"` for both: a claim
the host's own accounting supports rather than contradicts. Which *runtime* ran the
container is a separate fact, and the ledger records it separately — the isolation
level says `VM` under gVisor.

`paa doctor` reports all of it: the backend, the isolation level, whether the Docker
daemon is reachable, the OCI runtime and whether the daemon has it, and the image the
container runs — with the image's presence, because the image name is a string in four
places and until Phase 0 it named an image nobody had built.

The rows are ordered so they cannot contradict each other: the isolation level is
read *after* the daemon has been asked, because the level is derived from the
answer. This is the real output on a host where `runsc` is **not** registered —
the most informative case, because the runtime says so rather than claiming
containment it does not have:

```
$ paa doctor
┏━━━━━━━━━━━━━━━━━┳━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━┓
┃ component       ┃ status                                                     ┃
┡━━━━━━━━━━━━━━━━━╇━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━┩
│ version         │ 4.1.0                                                      │
│ home            │ /data                                                      │
│ permission mode │ ASK                                                        │
│ relational      │ postgres                                                   │
│ vector          │ QdrantVectorStore                                          │
│ graph           │ SqliteGraphStore                                           │
│ queue           │ RedisMessageQueue                                          │
│ model router    │ EscalatingModelRouter                                      │
│ sandbox         │ docker (NAMESPACE)                                         │
│ docker daemon   │ reachable, but 'runsc' is not registered                   │
│ oci runtime     │ runsc (NOT registered — falling back to runc, isolation is │
│                 │ NAMESPACE)                                                 │
│ sandbox image   │ paa/base_worker:v4.1 (present)                             │
│ open tasks      │ 0                                                          │
└─────────────────┴────────────────────────────────────────────────────────────┘
```

On a host where `runsc` **is** registered, the same command says
`docker (VM)`, `reachable`, and `runsc (registered)` — the isolation level
and the runtime row agreeing, which is the whole point of probing before
rendering.

The same renderer produces both this table and the TUI dashboard's, so the two
surfaces of one runtime cannot disagree about the same host.

On a non-Docker backend the doctor names the backend and its level instead —
`subprocess (PROCESS)`, `dryrun (NONE)` — because "what containment will my run
actually get" is the question the row exists to answer, and answering
"none (dry-run)" about a subprocess backend would be false.

On a non-Linux host the override is inert: the runtime reports `NAMESPACE` (or
degrades to a weaker backend) and says so in the logs.

## Fetching the images: `deploy/fetch-images.sh`

`docker compose up --build` fetches the third-party images itself, and it does not
retry: a dropped connection mid-layer and a registry that times out both look the same
to it, and both stop the deploy. `deploy/fetch-images.sh` exists for that case — it
fetches the images one at a time, with a growing backoff, and it is **resumable**: an
image already present is skipped, so re-running it after a failure continues where it
stopped.

```bash
deploy/fetch-images.sh                 # fetch what is missing
deploy/fetch-images.sh --list          # print what would be fetched
deploy/fetch-images.sh --verify        # check the digests of what is present
deploy/fetch-images.sh --load <dir>    # import images from a directory of tarballs
```

**The compose file is the source of truth.** The script reads `docker-compose.yml` and
acts on what it finds: a service with a `build:` section is *built here* (its `image:` is
the tag the build produces) and a service with only an `image:` is *pulled*. There is no
second list of images to keep in step — a copy of one is exactly how the script and
Compose came to disagree about which Qdrant and which SeaweedFS the deployment runs.

**Several compose files, merged — the gVisor stack is a merged stack.** The gVisor
override is a *layer* on the base file, not a file of its own:

```bash
docker compose -f docker-compose.yml -f docker-compose.gvisor.yml up -d
```

Two files, one stack — so the script has to read the merge, not one of the files:

```bash
deploy/fetch-images.sh --compose docker-compose.yml --compose docker-compose.gvisor.yml
```

`--compose` is repeatable, and the files are merged in the order they are given, later
wins — the same order and the same precedence `docker compose -f a -f b` uses. A
service's map is merged key by key, so the gVisor override's `paa` service — which
declares a runtime and an environment block and no image of its own — leaves the base
file's `build:` section and `image:` alone, and the merged `paa` is the built image
with the override's runtime on top.

The merge is a **subset** of Compose's own, and the shapes it does not implement are
**refused** rather than guessed: Compose's `extends:`, a YAML anchor or an alias, a
multi-line `image:`, a custom YAML tag, and the top-level `include:`. A wrong plan
fetches an image the deployment does not run and misses one it does, and nothing in the
output would say so. A service behind a `profiles:` key is **excluded** instead: a
profile that is not enabled is not a service `docker compose up` runs, and the script
says so on stderr.

Relative paths are resolved against the **repository root**, and a `--compose` flag
takes precedence over `PAA_COMPOSE_FILE`.

The scope is therefore the compose file's services. The two images this repository builds
that Compose does *not* name — `paa/base_worker:v4.1` and `paa/toolbox:v1` — are not in
the list; the runtime launches the first and the tool layer the second. Both are built
by hand, and the commands are in [The container images](#the-container-images) below.

**Every pulled image is digest-pinned, and the pin is checked.** A pulled image with no
`@sha256:` digest is an **error**, not a warning: a tag is a promise about a moving
target, and a deployment that names one cannot be reproduced. The pin comes from the
compose file — the script carries no digest of its own. Three references are refused,
each with its own message: an image that is not pinned, an `image:` key with no value,
and an `image:` whose reference is a **variable** (`${IMAGE:-postgres:16}`) — the script
reads the compose file as text and does not interpolate Compose's variables, so it
refuses one rather than guessing what your shell and `.env` hold. A compose file the
reader finds no images in is refused too: a plan with nothing in it is a broken source
of truth, not an empty deployment, and reporting success about one would be reporting
that nothing was checked.

The check is *inside* the retry: one attempt is one pull **plus** its verification, the
way the Dockerfiles treat a download and its SHA-256 check. A transfer that produced
something other than the pinned image is a failed *attempt*, so the next attempt tries
again, and a persistent failure stops the script with a non-zero exit status — which
makes it usable as a CI gate.

**`--list` needs no Docker, and it is a gate.** The list is read from the compose file,
so the report is available on a host with no daemon, and it is what the deployment tests
check the script against. It also *enforces*: it exits non-zero when the plan names an
image this deployment cannot pin. The list still prints — you need to see *what* is
wrong — and the exit status is the verdict.

**`--load` imports and stops.** It loads every `*.tar` in the directory and exits; it
does not then pull the rest of the deployment. A **loaded** image is not digest-verified:
a `docker save` / `docker load` round trip restores an image by its configuration, and
the manifest digest the registry served is not what the load path can be asked about. The
pin is enforced on the pull path.

**Requirements: GNU coreutils.** The script bounds every pull with `timeout`, which is a
GNU coreutils program — it is not in a stock macOS or BSD system, and a busybox
`timeout` is a different program with different flags. The script says so plainly rather
than failing with "command not found" in the middle of a pull. On macOS:
`brew install coreutils`.

**Behind a mirror:** `PAA_IMAGE_MIRROR=registry.example.com deploy/fetch-images.sh`. The
variable is a *prefix*, not a registry: the pull is given `<mirror>/<repository>`, with
the official images served from `library/`. The mirror changes the path, not the digest,
so the verification is unaffected by it. The pulled image is then **tagged** under the
name Compose expects — and only then: without a mirror the pulled reference already *is*
the reference Compose uses, and tagging it would add a mutable alias (a tag carrying no
digest) that a later unpinned pull could move to different bytes.

**The knobs, and where they come from.** All five are read from your **shell** — the
script does not read `.env`, and neither does `docker compose` for them:

| Variable | Default | What it does |
|---|---|---|
| `PAA_COMPOSE_FILE` | `docker-compose.yml` | The compose file to read, when no `--compose` flag is given. One file: the several-file case is the flag. |
| `PAA_FETCH_MAX_ATTEMPTS` | `6` | How many times one image is pulled before the script gives up on it. One attempt is one pull **plus** its verification. |
| `PAA_FETCH_BASE_DELAY` | `5` | The base of the backoff, in seconds. The delay before attempt *n* is `PAA_FETCH_BASE_DELAY × n`, so the wait grows with the attempt. |
| `PAA_FETCH_TIMEOUT` | `900` | How long one pull may take, in seconds, before it is killed and retried. |
| `PAA_IMAGE_MIRROR` | *(none)* | A registry prefix to pull through. It changes the repository path, never the digest. |

```bash
PAA_FETCH_MAX_ATTEMPTS=10 PAA_FETCH_BASE_DELAY=2 deploy/fetch-images.sh
```

**The script touches the local image store and nothing else.** It inspects, pulls, tags
and loads. It never stops, removes, restarts or prunes anything, and it never touches a
running container.

The reasoning, and the three defects this replaced, are in
[ADR-0030](../adr/0030-deployment-image-contract.md).

## The model provider in the container stack

The `paa` service forwards the model provider's variables from your shell as well as
from the project `.env`:

```bash
export OPENROUTER_API_KEY=sk-or-v1-...
export PAA_MODELS__LOCAL_PROVIDER=openrouter
export PAA_MODELS__LOCAL_MODEL=nvidia/nemotron-3-ultra-550b-a55b:free
docker compose up -d --build
```

Compose interpolates from the **shell first and the project `.env` second**, so a shell
export wins over the value in `.env`, and the value in `.env` is used when your shell
has nothing. (Before this, the file won and the shell export was silently ignored — a
containerised runtime could not reach a provider whose credential lived only in your
shell.)

All five variables are **optional**. A stack with no model provider is a legitimate
configuration: the runtime runs on its embedded defaults. The three `PAA_MODELS__*`
settings fall back to the code's own defaults (`ollama`, `qwen2.5:3b-instruct`,
`anthropic`) rather than to an empty string — an empty provider name is not an absent
one, and the container would die at boot resolving it.

The credential *names* are the registry rows' `api_key_env` values
(`src/paa/models/registry.py`), so the same key works for `paa agent` on a laptop and
for the container. The values are never written into config, the ledger, or an image
layer — `.env` is git-ignored and `.dockerignore`-d.

## What needs root, and what it is done

Four things in this deployment need `sudo`. They are collected here because each
one is a *host* fact, and each one fails differently if it is skipped: a
runtime that is registered but cannot start a container, a measurement nobody
has taken, a stray directory nobody can remove, and a test suite that fails for
a reason that has nothing to do with the code.

**All four are done on the reference host.** What follows is the reasoning, so
that the next host can be set up the same way — and so that the two that
changed the *code* are not undone by someone who assumes they were mistakes.

### 1. Registering `runsc` with the daemon

Before: the binary was installed and the daemon did not know about it.

```bash
$ ls -l ~/.local/bin/runsc
-rwxr-xr-x 1 devansh devansh 56950133 … runsc
$ ~/.local/bin/runsc --version
runsc version release-20261012.0
$ docker info --format '{{json .Runtimes}}' | python3 -c 'import json,sys; print(list(json.load(sys.stdin)))'
['io.containerd.runc.v2', 'runc']
```

The daemon offered `runc` and `io.containerd.runc.v2` and **not** `runsc`.
Four commands close that, and the fourth is the one that is easy to miss:

```bash
sudo install -m 0755 ~/.local/bin/runsc /usr/local/bin/runsc
sudo install -d -m 0755 /usr/local/bin/gvisor-bin
sudo install -m 0755 /tmp/gvisor/gvisor-bin/* /usr/local/bin/gvisor-bin/
sudo cp deploy/docker/daemon.json /etc/docker/daemon.json    # MERGE, do not overwrite
sudo systemctl restart docker
```

**The sidecars are not optional.** The tarball ships `runsc` *and* a
`gvisor-bin/` directory, and `runsc` looks for the sentry next to itself at
`<dir-of-runsc>/gvisor-bin/gvisor_sentry`. Installing only `runsc` — which is
what the procedure used to say — gives a runtime that is registered and cannot
start a container:

```
sidecar "gvisor_sentry" not usable: stat /usr/local/bin/gvisor-bin/gvisor_sentry:
no such file or directory
```

**Why the copy.** `deploy/docker/daemon.json` points at
`/usr/local/bin/runsc`, and the daemon runs as root with a minimal `PATH`
(`/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin`). The copy in
`~/.local/bin` is on the *operator's* `PATH` and on nobody else's, so
without the copy the daemon cannot exec the runtime at all. The installed
copy is deliberately not on the operator's `PATH` either: `runsc` is the
daemon's runtime, and the operator drives it through `docker run
--runtime=runsc`.

**Why "merge, do not overwrite".** On this host `/etc/docker/daemon.json`
does not exist, so today the copy would *create* it. The rule still stands:
the daemon refuses to start on a top-level key it does not recognise — which
is why the file carries no comments and why the explanation lives in
`deploy/docker/README.md` — and a host that already has a daemon.json
usually has settings worth keeping (a registry mirror, a log driver, a
different storage driver). `cp` over it would drop them silently, and the
daemon would come back up with different behaviour than the operator had.

**Why the restart.** The daemon reads `daemon.json` **once, at startup**.
There is no reload and no SIGHUP for the runtime registry: without the
restart the file is a dead file and `docker info` will still show only
`runc`. The restart is the step that makes the file take effect — and it is
also the step that interrupts the running containers, so it is the one
command here with a visible cost.

**How to know it worked.** Two commands, and they answer different questions:

```bash
docker info --format '{{json .Runtimes}}'    # must list "runsc"
docker run --rm --runtime=runsc hello-world
```

The first reads the daemon's own view of the runtimes it will accept. This
is not a formality: the runtime's logic keys on *exactly* this fact — the
backend uses `runsc` only when the daemon reports it registered, and reports
`NAMESPACE` rather than `VM` when it does not — so `docker info` is the
same fact `paa doctor` reports, read from the daemon instead of through the
runtime.

The second is the end-to-end proof, and it is not redundant: **registration
is necessary but not sufficient.** gVisor's `runsc` needs the host Unix socket
passed into the sandbox explicitly (the `--host-uds` runtimeArgs), and it
needs the host's namespaces and `/proc` to be what it expects. A
`hello-world` under `--runtime=runsc` is the cheapest way to find out which
of those is missing, and none of it is fixable from the application code.

### 2. The cgroup measurement under `runsc`

The memory ceiling is the daemon's `--memory`, which is a *cgroup* limit: the
daemon caps the container's cgroup and refuses to start a container whose limit
it cannot apply. The question was whether gVisor changes that.

It does not, and the answer was taken rather than assumed. With `--memory 256m`
and a process that touches 512 MiB:

==================  ==========  ========  =========================
runtime            exit code   OOMKilled  cgroup ``memory.events``
==================  ==========  ========  =========================
``runc``           137         true      ``oom 1 oom_kill 1``
``runsc`` (gVisor) 137         true      ``oom 1 oom_kill 1``
==================  ==========  ========  =========================

and in both cases ``memory.peak`` reached ``memory.max`` exactly. The kernel's
own ``oom_kill`` counter is what fired, under gVisor as much as under runc.

The measurement is a few commands, and the *timing* matters: the cgroup is
removed when the container is, so the accounting has to be read while it is
still there.

```bash
cid=$(docker run -d --runtime=runsc --memory 256m --memory-swap 256m \
      python:3.12-slim python -c 'b = bytearray(512*1024*1024); [b.__setitem__(i,1) for i in range(0,len(b),4096)]')
# poll the cgroup until the kernel records the kill, then read it
cg=/sys/fs/cgroup/docker/$cid
while ! grep -q 'oom_kill [1-9]' $cg/memory.events 2>/dev/null; do sleep 0.05; done
cat $cg/memory.max $cg/memory.peak; grep -E '^(oom|oom_kill)' $cg/memory.events
docker wait $cid; docker rm -f $cid
```

**This changed the code.** The plan expected a *weaker* ceiling under gVisor —
gVisor's own documentation describes limited cgroup delegation on a cgroup v2
host — and the ledger's mechanism field was written to say `"gvisor"` for a
gVisor run. The measurement disagreed: the ceiling was the kernel's cgroup in
both cases, so `SandboxResult.memory_enforcement` now reports `"cgroup"` for
both. Which *runtime* ran the container is a separate fact and is recorded
separately — the isolation level says `VM` under gVisor.

### 3. The stray root-owned `/data` on the host

```bash
$ ls -ld /data /data/workspaces /data/workspaces/task-1
drwxr-xr-x 3 root root 4096 /data
drwxr-xr-x 3 root root 4096 /data/workspaces
drwxr-xr-x 2 root root 4096 /data/workspaces/task-1     # empty
```

**What it is.** The host-side residue of a sandbox workspace whose path was
never translated. The runtime's workspace root is `/data/workspaces/<task-id>`
*inside* the runtime container (`PAA_HOME=/data`); on the host it is
`${PAA_DATA_DIR}/workspaces/<task-id>`. When the `host_path_map` is missing
or wrong, the daemon is handed the container-side path as a bind-mount
source — and **the Docker daemon creates a missing bind-mount source as
`root:root 0755`**. So a container-side path leaks onto the host's root as
a root-owned directory. `/data/workspaces/task-1` has exactly that shape:
`task-1` is a task id, `workspaces` is the runtime's workspace root, and
`/data` is the runtime's `PAA_HOME`.

**What is deleted.** Three empty directories. The tree is 12 KB in total and
contains no files.

**What breaks if it is deleted.** Nothing in this repository. No container on
this host bind-mounts a host `/data` — every `/data` in the mount table is a
*container-side destination* (`/tmp/opencode/paa-stack-data -> /data`, and
three named volumes -> `/data` for seaweedfs and redis). The runtime's own
state is at `${PAA_DATA_DIR:-/srv/paa/data}`, and the stack's is at
`/tmp/opencode/paa-stack-data`.

**What is at risk.** A host-level `/data` is a common convention for a
machine's data partition, and *that* is the whole reason this needs the
operator rather than the agent: something else on this host may own a
`/data`, and nothing here can see it. `rm -rf` on it would destroy whatever
that is, irreversibly.

**So use `rmdir`, not `rm -rf`.** `rmdir` refuses to remove a non-empty
directory, so it cannot delete data even if something else owns the path:

```bash
sudo rmdir /data/workspaces/task-1 /data/workspaces /data
```

If the operator knows the host has no other `/data`, `rm -rf` is equivalent —
but `rmdir` is the form that cannot be wrong. On the reference host the
`rmdir` form was used, and the tree was empty, so it removed all three
directories and nothing else.

### 4. The six test failures that were the host's

The suite used to have six failures that were **not** defects in the code: the
gate was "the failure set must not change", not "the suite must be green".
They are fixed on this host now — the suite is green — but the mechanism is
worth keeping, because the next host that lacks a system `python` will have
them again.

The cause is one deliberate design decision meeting one fact about this
host. The sandbox builds its child environment **from scratch** —
`build_child_env` in `paa/sandbox/base.py` — with

```
PATH=/usr/local/bin:/usr/bin:/bin
```

and deliberately *not* the host's `PATH`, because the host's `PATH` leaks the
developer's toolchain and can place an attacker-writable directory ahead of
the system ones. On this host `python` is at `~/.local/bin/python` (a
symlink to `/usr/bin/python3`); there is **no** `/usr/bin/python` and no
`/usr/local/bin/python`. So a plan step that runs `python -c "…"` cannot
find an interpreter, the task never reaches `COMMITTED`, and the six
assertions that expect `COMMITTED` fail. The ledger, the CLI and the
recovery sweep are all behaving correctly.

On a stock Ubuntu with `python-is-python3` installed, `/usr/bin/python`
exists, the sandbox's minimal `PATH` finds it, and the same six tests pass.
That is a one-line host fix, and it has been applied here:

```bash
sudo apt install python-is-python3
```

The two alternatives are *not* fixes. Widening the sandbox's `PATH` to
include `~/.local/bin` would undo the security property the minimal `PATH`
exists for, and changing the tests to use `python3` would change what they
exercise — the point of the step is to run a command *by name* in a minimal
environment. Neither belongs in a change that is not about the sandbox's
environment.

## The data directory, and why it is a bind mount

`PAA_HOME` is `/data` inside the `paa` container, and `/data` is a **bind mount** of
`${PAA_DATA_DIR:-/srv/paa/data}` on the host — not a named volume.

The reason is the sandbox backend. The Docker daemon resolves a bind mount's
source path against the **host** filesystem, so the workspace path the runtime
knows (`/data/workspaces/task-1`) is not the path the daemon must be given.
Handing it the runtime's path makes the daemon create a *different*, empty,
root-owned directory in its place — silently, and with no error.

The fix is two halves, and both are needed:

* **The data directory is a host bind mount at a known path.** `PAA_DATA_DIR` names
  the host side; the runtime's own path is translated by
  `PAA_SANDBOX__HOST_PATH_MAP`, which `docker-compose.yml` sets to
  `/data=${PAA_DATA_DIR:-/srv/paa/data}`.

  The map is a **string** with comma-separated `inside=host` pairs, not a JSON
  blob. That is deliberate: `pydantic-settings` decodes a complex-typed field
  from an environment variable as JSON, so a tuple-typed field would have
  demanded `[["/data","/srv/paa/data"]]` and the obvious spelling would have
  killed the container at import — before it could serve a request, and before
  it could run the doctor that would explain why. A malformed entry is reported
  by `paa doctor` as a `host path map` row and refuses the run; it is never
  silently ignored, because a skipped entry means the daemon mounts the wrong
  directory and answers by creating an empty, root-owned one.

* **The host directory must exist and be writable by the container's uid.**

  ```bash
  sudo mkdir -p /srv/paa/data
  sudo chown 10001:10001 /srv/paa/data     # the image's `paa` user
  ```

  A named volume gets this right for free; a bind mount does not, and the
  failure mode — a workspace the daemon cannot write — is quiet.

The container also runs as `--user` derived from the workspace's owner, so a
workload cannot leave root-owned files in a directory you own. The uid is never
0: a root-owned workspace falls back to the image's own non-root user rather
than selecting `--user 0:0`, and the flag is never omitted at all — an image
that says nothing about its user runs as root.

## The container images

Two images, with different jobs. Both are built from this repository.

| Image | Dockerfile | Job |
|---|---|---|
| `paa/base_worker:v4.1` | [`deploy/worker/Dockerfile`](../../deploy/worker/Dockerfile) | The image the sandbox backend launches for a **plan step**. It runs untrusted work, so it is deliberately minimal: Python 3.12, `git`, `ca-certificates`. |
| `paa/toolbox:v1` | [`deploy/toolbox/Dockerfile`](../../deploy/toolbox/Dockerfile) | The image **MCP servers and plugin commands** run in. It carries Node 24 (most published MCP servers are npm packages started with `npx`), plus `curl`, `jq` and `git`. |

```bash
docker build -t paa/base_worker:v4.1 deploy/worker
docker build -t paa/toolbox:v1    deploy/toolbox
```

Both base images are pinned **by digest**, so a rebuild does not quietly change
what is underneath. To refresh one, pull it and read the new digest:

```bash
docker pull python:3.12-slim
docker image inspect python:3.12-slim --format '{{index .RepoDigests 0}}'
```

The runtime image (`Dockerfile`) is pinned the same way, and its Docker client
is pinned by SHA-256 with the digest recorded in the Dockerfile.

### Building the images: the network is the fragile part

A container build reaches the network for its dependencies, and the network
fails for reasons nobody in this repository controls. A build that dies on a
dropped connection looks exactly like a build that died on a real error, so
the two have to be told apart — and the only way to tell them apart is to
absorb the transient ones first and let only a *persistent* failure stop the
build.

**Every network operation retries.**

| Operation | Retry policy |
|---|---|
| `apt-get update` / `install` | `-o Acquire::Retries=10` on both |
| `pip download` | `PIP_RETRIES=10`, `PIP_TIMEOUT=120`, **plus** an outer `retry()` loop: 5 attempts with growing backoff |
| the Docker client tarball | the same `retry()` loop; the SHA-256 is verified *after* the download, so a corrupt transfer is a failed attempt rather than a failed build |

pip's own `--retries` covers a request that failed mid-flight. The outer loop
covers the case pip cannot see: a download that *completed* from pip's point
of view and turned out to be truncated or wrong. Backoff grows between
attempts, so a mirror that is genuinely down is not hammered.

### The wheelhouse, and why the build is fast

The runtime image is built in three stages, and the stages are arranged around
the network rather than around the source:

| Stage | What it does | When it re-runs |
|---|---|---|
| `wheelhouse` | downloads every dependency wheel into `/wheelhouse` | only when `pyproject.toml` changes |
| `builder` | installs from that wheelhouse with `--no-index` — **offline** | when the source changes |
| `runtime` | copies the venv, adds the Docker client and a non-root user | when the venv changes |

The requirement list is extracted from the project's own metadata with
`tomllib`, never maintained by hand. A hand-maintained list is a second place
to forget to update, and the failure it produces is an image missing a
dependency at *runtime* rather than at build time.

The **offline install is the property that makes the wheelhouse safe**.
`pip install --no-index --find-links=/wheelhouse` either finds every wheel it
needs or fails loudly — it can never quietly install less than the project
declares. So a stale or incomplete wheelhouse becomes a *build failure*
instead of a broken image, and `pip check` runs afterwards as the last line of
defence against a wheelhouse that resolved to a combination the project does
not work with.

In practice: the first build downloads everything (a few minutes), and a
rebuild after a code change does **no network I/O for dependencies at all** —
about half a minute, entirely offline.

```bash
docker build -t paa/runtime:4.1 .
```

Two constraints are deliberate. `--only-binary=:all:` on the download means a
dependency that ships no wheel fails with "no matching distribution" — which
names the real problem — rather than trying to *build* it in an image that
carries no compiler. And the build backend (`hatchling`) is part of the
wheelhouse, not fetched at build time, so the builder stage genuinely needs
no network.

> Do not "simplify" the builder stage back to a networked `pip install`. The
> offline install is what turns a stale or incomplete wheelhouse into a build
> failure instead of a broken image.

### The toolbox needs an exec-capable scratch path

The sandbox backend mounts `/tmp` **`noexec`** — a binary a workload drops there
should not be executable. But npm's cache holds the *executable shims* that
`npx` installs, so an `npx`-started MCP server fails with:

```
sh: 1: <tool>: Permission denied        (exit 126)
```

The fix is not to drop `noexec` from `/tmp`; it is to give these workloads their
own exec-capable path. The backend does that through `SandboxSpec.exec_tmpfs`,
so a toolbox spec carries:

```python
exec_tmpfs=("/run",)
```

which mounts `/run` as a writable, exec-capable tmpfs (`mode=1777`, because the
mount is created by the daemon and is therefore root-owned). The toolbox image
points npm's cache there.

The `paa` package is deliberately **not** installed in the toolbox: a plugin that
imported it would expect a database connection and a ledger handle, and the
container has neither.

`paa/base_worker:v4.1` is the same string as the ledger's `allocated_worker_image`
default, so the recorded image and the executed image cannot drift apart — and
`tests/storage/test_schema_parity.py` now *proves* that, by asserting the
backend's default, the ledger event's default and the two schema defaults are
the same string.

## Configuration reference

Backend selection is driven entirely by `PAA_*` environment variables, set in
`docker-compose.yml` and overridable in `.env`:

| Variable | Server value | Meaning |
|---|---|---|
| `PAA_STORAGE__BACKEND_RELATIONAL` | `postgres` | use `PostgresDatabase` |
| `PAA_STORAGE__POSTGRES_DSN` | `postgresql://…@postgres:5432/paa` | psycopg DSN |
| `PAA_STORAGE__BACKEND_QUEUE` | `redis` | Redis dispatch fabric |
| `PAA_STORAGE__REDIS_URL` | `redis://redis:6379/0` | |
| `PAA_STORAGE__BACKEND_VECTOR` | `qdrant_server` | Qdrant server recall |
| `PAA_STORAGE__QDRANT_URL` | `http://qdrant:6333` | |
| `PAA_STORAGE__BACKEND_COLDLAKE` | `s3` | `S3BlobStore` |
| `PAA_STORAGE__S3_ENDPOINT` | `http://seaweedfs:8333` | any S3 server; the deployment runs SeaweedFS |
| `PAA_S3_ACCESS_KEY` / `PAA_S3_SECRET_KEY` | from `.env` | credential values |
| `PAA_SANDBOX__CONTAINER_RUNTIME` | `runc` / `runsc` | OCI runtime |
| `PAA_SANDBOX__BACKEND` | `docker` | the containment backend; `auto` would silently degrade to the subprocess backend if the Docker probe failed |
| `PAA_SANDBOX__CONTAINER_START_GRACE_SECONDS` | `5.0` | the budget on the container's **start**; the step's own `timeout_seconds` is the budget on the **work**, and the two are enforced separately (ADR-0031) |
| `PAA_SANDBOX__HOST_PATH_MAP` | `/data=/srv/paa/data` | how the runtime's path becomes the host's |
| `DOCKER_GID` | from `.env` | the host's docker-socket GID, added to the container user as a supplementary group |
| `PAA_DATA_DIR` | from `.env` | the host side of the `PAA_HOME` bind mount |

`PAA_SANDBOX__BACKEND` and `PAA_SANDBOX__CONTAINER_RUNTIME` are **interpolated**
from the environment (`${PAA_SANDBOX__BACKEND:-docker}`) rather than written
literally, so an operator can override them in `.env`. A variable written
literally in the `environment:` block takes precedence over `env_file:` and
could not be changed at all — the safe default has to leave a way back.
`PAA_SANDBOX__HOST_PATH_MAP` is derived from `PAA_DATA_DIR` rather than being
overridable on its own, because the two have to agree: the map is what tells
the backend the *host* path for the directory the bind mount puts at `/data`.
The rest of the `environment:` block is literal, and those variables are the
deployment's decision rather than the operator's.

To point one substrate back at its embedded engine, change just that variable —
e.g. `PAA_STORAGE__BACKEND_COLDLAKE=filesystem` keeps Postgres and Redis but
uses the local content-addressed store for blobs.

## Troubleshooting

- **`paa` restarts / DSN errors** — check `.env` matches the DSN Compose builds;
  `docker compose logs postgres` for auth failures.
- **"the database's schema is incomplete"** — the schema was applied only part
  of the way, so the database is at no version at all and the runtime refuses
  to migrate it. The message names the tables that are missing. Restore the
  database from a backup, or point the runtime at a database it can create
  from scratch. Do **not** hand-edit `schema_migrations` to make the message
  go away: the version log is the only thing that says which shape the
  database has.
- **Which migrations ran** — the runtime logs `postgres.schema_migrated` with
  the version it reached, once per version it carried the database forward
  through, and `postgres.schema_applied` when it created the schema outright.
  The embedded substrate logs the same two events under `database.`.
- **`backend_relational='postgres' requires storage.postgres_dsn`** — the DSN
  env var is unset; confirm `PAA_STORAGE__POSTGRES_DSN` is present in the `paa`
  service environment.
- **S3 auth errors** — `PAA_S3_ACCESS_KEY` / `PAA_S3_SECRET_KEY` must equal
  `S3_ROOT_USER` / `S3_ROOT_PASSWORD`.
- **gVisor not used** — `docker info` must list `runsc`; the runtime logs
  `sandbox.docker.runtime_unavailable` when it asked for a runtime the daemon
  does not have. `paa doctor` shows the same thing as an `oci runtime` row.
- **`permission denied while trying to connect to the docker API`** — the socket's
  GID is not a supplementary group of the container user. Set `DOCKER_GID` in
  `.env` to `stat -c %g /var/run/docker.sock` and recreate the container.
- **The same error, but only under gVisor** — the daemon's runtime registration
  is missing the host-Unix-socket permission. gVisor does not pass a host
  Unix socket through just because it was bind-mounted; the permission is
  given to the *runtime*:

  ```json
  "runtimeArgs": [ "--host-uds=all" ]
  ```

  A ready-to-copy file is `deploy/docker/daemon.json`.
- **`failed to create shim task … did not terminate successfully: exit status 2`**
  under `--runtime=runsc` — the runtime was given a flag it does not have.
  The pinned build spells the socket permission `--host-uds=all`; the older
  `--hostuds=<path>` was removed, and a runtime handed it exits 2 with "flag
  provided but not defined". The daemon does not validate a runtime's flags
  when it registers one, so `docker info` will still list `runsc`.
- **`paa` container will not start under runsc** — Docker's default seccomp
  profile can conflict with gVisor's. Add `security_opt: [seccomp=unconfined]`
  to the `paa` service in `docker-compose.gvisor.yml`.
