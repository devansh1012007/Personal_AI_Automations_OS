# Docker deployment — the server topology

This runs PAA with the RFC's original stack: PostgreSQL, a Qdrant server, Redis,
MinIO, and (optionally) a gVisor-contained sandbox. It is the counterpart to the
laptop's embedded default. Both are the same codebase; the difference is
configuration. See `docs/adr/0019-docker-server-deployment.md` for the design.

## Laptop vs server: which to run

| | Laptop (embedded, default) | Server (Docker, this doc) |
|---|---|---|
| Relational | SQLite (WAL), in-process | PostgreSQL 16 |
| Cold lake | content-addressed files | MinIO (S3) |
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
# edit .env: set POSTGRES_PASSWORD, MINIO_ROOT_USER/PASSWORD, etc.

docker compose up -d --build
docker compose ps            # all services healthy except qdrant (started)
docker compose logs -f paa   # watch the runtime boot
```

The runtime applies `schema_postgres.sql` on first connect and creates the
MinIO bucket. The ingestion API is reachable at `http://127.0.0.1:8787` — and
**only** there.

Stop with `docker compose down`; add `-v` to also drop the named volumes (this
destroys the ledger — do it deliberately).

## What is exposed, and what is not

Security posture is loopback-first by construction:

- **Only** the runtime's ingestion API publishes a host port, bound to
  `127.0.0.1:8787`. `Settings._enforce_loopback` refuses a non-loopback
  `api_host` unless `PAA_ALLOW_NON_LOOPBACK=1` is set deliberately, so a typo
  cannot expose the runtime.
- PostgreSQL, Redis, Qdrant and MinIO publish **no host ports**. They are
  reachable only from the `paa` container over the internal `paa_net` bridge.
  To administer MinIO, uncomment the loopback-bound console mapping in
  `docker-compose.yml` (`127.0.0.1:9001:9001`) — never bind it to `0.0.0.0`.
- Secrets live in `.env` (git-ignored, `.dockerignore`-d). Config stores only
  the *names* of the env vars that hold MinIO credentials; the values never
  enter config or the ledger.

`paa_net` is not marked `internal` because the runtime needs outbound access for
model escalation. The backing services still publish no host ports, so they
remain unreachable from outside the compose project regardless.

## Enabling gVisor (`runsc`)

gVisor interposes a user-space kernel between the container and the host — the
syscall boundary the RFC §13 requires and that namespaces alone do not provide.
It is **opt-in** and **Linux-only**.

1. Install runsc: https://gvisor.dev/docs/user_guide/install/
2. Register it with the daemon in `/etc/docker/daemon.json`:
   ```json
   { "runtimes": { "runsc": { "path": "/usr/local/bin/runsc" } } }
   ```
   then `sudo systemctl restart docker`.
3. Verify the daemon sees it:
   ```bash
   docker info --format '{{json .Runtimes}}'   # must include "runsc"
   ```
4. Bring the stack up with the override:
   ```bash
   docker compose -f docker-compose.yml -f docker-compose.gvisor.yml up -d
   ```

The override sets `PAA_SANDBOX__CONTAINER_RUNTIME=runsc`, so the throwaway
worker containers the runtime launches request `--runtime=runsc`. The runtime
uses it **only** when the daemon confirms it is registered, and reports
`IsolationLevel.VM` only then; if runsc is requested but absent it falls back to
`runc` and honestly reports `NAMESPACE` rather than claiming containment it does
not have (`paa/sandbox/docker_backend.py`, verified by
`tests/sandbox/test_docker_backend.py`). The override also runs the `paa`
container itself under runsc as defence in depth — comment that line out to keep
only the worker containers contained, which is the minimal sufficient hardening.

On a non-Linux host the override is inert: the runtime reports `NAMESPACE` (or
degrades to a weaker backend) and says so in the logs.

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
| `PAA_STORAGE__BACKEND_COLDLAKE` | `minio` | `S3BlobStore` |
| `PAA_STORAGE__MINIO_ENDPOINT` | `http://minio:9000` | |
| `PAA_MINIO_ACCESS_KEY` / `PAA_MINIO_SECRET_KEY` | from `.env` | credential values |
| `PAA_SANDBOX__CONTAINER_RUNTIME` | `runc` / `runsc` | OCI runtime |

To point one substrate back at its embedded engine, change just that variable —
e.g. `PAA_STORAGE__BACKEND_COLDLAKE=filesystem` keeps Postgres and Redis but
uses the local content-addressed store for blobs.

## Troubleshooting

- **`paa` restarts / DSN errors** — check `.env` matches the DSN Compose builds;
  `docker compose logs postgres` for auth failures.
- **`backend_relational='postgres' requires storage.postgres_dsn`** — the DSN
  env var is unset; confirm `PAA_STORAGE__POSTGRES_DSN` is present in the `paa`
  service environment.
- **MinIO auth errors** — `PAA_MINIO_ACCESS_KEY` / `PAA_MINIO_SECRET_KEY` must
  equal `MINIO_ROOT_USER` / `MINIO_ROOT_PASSWORD`.
- **gVisor not used** — `docker info` must list `runsc`; the runtime logs
  `sandbox.docker.runtime_unavailable` when it asked for a runtime the daemon
  does not have.
