# ADR-0019: Dual embedded/server topology for the Docker deployment

## Status

Accepted. Partially reverses ADR-0001 (relational), ADR-0004 (cold lake),
ADR-0005 (queue) and satisfies ADR-0006's deferred gVisor path — for the
server topology only. The embedded defaults those ADRs chose remain in force
for the laptop.

## Context

ADR-0001 through ADR-0006 replaced the RFC's server stack — PostgreSQL, a Qdrant
server, Redis, MinIO and gVisor — with embedded engines, because the target
machine was a Windows laptop with ~3.5 GB of free RAM and no Docker. Those were
**hardware** deviations, not disagreements with the RFC's design: the RFC's
stack is the right one when the hardware can carry it.

The user now wants a full Docker deployment that restores that stack. The
constraint is that this must **add** the server option without removing the
embedded one — the laptop path still has to boot on nothing but the core
dependencies. So the requirement is not "switch to Postgres" but "let the same
codebase run either substrate, chosen by configuration".

## Decision

Every substrate that was collapsed to an embedded engine keeps that engine as
its default and gains a server-backed sibling behind the *same interface*,
selected by a `StorageSettings` / `SandboxSettings` field:

| Substrate  | Embedded (laptop, default)         | Server (Docker)                    | Selector |
|------------|------------------------------------|------------------------------------|----------|
| Relational | `Database` (SQLite WAL)            | `PostgresDatabase` (psycopg 3)     | `backend_relational` |
| Cold lake  | `ContentAddressedStore` (files)   | `S3BlobStore` (MinIO/boto3)        | `backend_coldlake` |
| Queue      | `SqliteMessageQueue`              | `RedisMessageQueue`                | `backend_queue` |
| Vector     | numpy / embedded Qdrant           | Qdrant server                      | `backend_vector` |
| Sandbox    | subprocess / WSL                  | Docker, optionally `--runtime=runsc` | `sandbox.backend` + `sandbox.container_runtime` |

The rules that keep this honest:

1. **One interface per substrate.** `PostgresDatabase` presents the exact async
   surface of `Database` (connect/close/transaction/fetch_all/fetch_one/
   fetch_value/execute/execute_many); `S3BlobStore` presents the exact surface
   of `ContentAddressedStore` and mints the same `cas://` `BlobRef` URIs, so a
   `blob_uri` written by one backend resolves against the other. Callers above
   the substrate do not branch on which one they hold.

2. **The two SQL dialects stay in lockstep.** `schema_postgres.sql` mirrors
   `schema_sqlite.sql` table-for-table and column-for-column; only the *types*
   differ (native UUID / JSONB / TIMESTAMPTZ / SMALLINT and a `pg_trgm` GIN
   index in place of SQLite's FTS5 trigram virtual table). `tests/storage/
   test_schema_parity.py` parses both and fails the build on any table or column
   that exists in one and not the other. It runs without a database.

3. **Server clients import lazily.** psycopg, boto3 and redis are optional
   extras (`postgres`, `minio`, `redis`). Each is imported only inside the
   factory or method that needs it, so a laptop install without them stays
   importable and the full test suite runs with none of them present.

4. **Selection is configuration, never code.** `get_database(settings)`,
   `get_blob_store(settings)`, `get_queue(settings)` and `get_sandbox(settings)`
   read the backend fields. Docker Compose sets those fields via `PAA_*`
   environment variables (`docker-compose.yml`); the laptop leaves them at their
   embedded defaults.

5. **Containment is reported, not assumed.** `sandbox.container_runtime` names
   the OCI runtime (`runc` default, `runsc` for gVisor). `DockerSandbox` adds
   `--runtime=runsc` only when the daemon reports it is registered, and reports
   `IsolationLevel.VM` only then; a requested-but-absent runsc degrades to runc
   and reports `NAMESPACE`. Overstating containment would mislead the policy
   layer, so it is structurally impossible here.

Credentials (MinIO keys, the Postgres password, model API keys) are never stored
in config or the ledger. `StorageSettings.minio_access_key_env` /
`minio_secret_key_env` name the environment variables that hold the values — the
same indirection `ModelSettings` uses for API keys — and Compose sources them
from a git-ignored `.env`.

## Consequences

- **Both topologies are first-class.** The laptop keeps its zero-server,
  ~0 MB-overhead default; a server host gets the RFC's stack with real
  concurrency, a syscall-boundary sandbox, and object storage that scales past a
  single disk.
- **The parity test is now load-bearing.** Any change to one schema must be
  mirrored in the other or the build fails — this is the mechanism that stops
  the dialects drifting silently, which is the failure mode a dual-backend
  design invites.
- **The server path is correct-by-construction, not yet integration-tested
  here.** Docker is not installed on the dev box, so `PostgresDatabase`,
  `S3BlobStore` and the Compose stack are unit-tested against stubs/mocks and
  guarded by the parity test; end-to-end verification against a live stack is a
  deployment-time step (see `docs/deployment/docker.md`).
- **Moving back is a config change.** Flipping `backend_relational` to `sqlite`,
  `backend_coldlake` to `filesystem`, etc., returns any deployment to the
  embedded engines with no code change — the reversibility ADR-0001 valued is
  preserved in both directions.
