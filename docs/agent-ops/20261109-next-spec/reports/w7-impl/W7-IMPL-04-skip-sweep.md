# W7-IMPL-04 — the skip sweep: every gate opened, three real findings

**Date:** 2026-11-25
**Scope:** the host's `origin/main` merge (W6 + C7.1 + C7.2 hardened trees
landed as ccf0793/f5555b6) + C7.3 recorder + the full un-skip of the
environment-gated suites + the three real findings the un-skip surfaced.
**Status:** done; full suite green with every server-gated suite running.

## 1. The merge (105 conflicts, nine by hand)

`git pull` diverged: local 22 commits (SPEC 2 W4–W7 through C7.3) vs remote
2 squashed+hardened commits. Resolution policy: **theirs for the hardened
files** (the host's hardening is strictly additive review work over my
patches), **ours for the recorder**, and nine files merged by hand:

- `cli/main.py` — theirs (log-config callback) + the `record_app`
  registration.
- `cli/attest.py` — theirs + the C7.3 "Evidence artifacts" PR-body wording.
- `attest/bundle.py` — theirs (`_index_blob` dedup, `--end-of-options`,
  refusal gates) + the C7.3 `extra_evidence`/`observed_command_rows` doors
  adapted onto `_index_blob`.
- `observe/observer.py` — ours (module-level `git_query`/`git_before`/
  `git_after`, C7.3) + theirs (the `SandboxError` → "git unavailable"
  catch) grafted into `git_before`.
- `AGENTS.md`, `README.md`, `docs/README.md`, `docs/modules/cli.md`,
  `docs/interfaces/attested-bundle-spec.md` — theirs' hardened text + the
  C7.3 rows/sections; 24 drifted `path:line` citations re-anchored by
  difflib against both parents (`scripts/reanchor_citations.py`).

The remote's root-level demo scratch (`PR-BODY.md` + 7 `.bin` blobs,
committed by an accidental `git add -A` on the host, referenced by nothing)
was removed in the merge; their own board says those were never meant to be
adopted.

## 2. The index_role design (found: the recorder broke the hardened verifier)

The first merged run failed `tests/recorder/test_core.py` — the C7.2
hardening added a **signed blob index** exact-set check, and the verifier
derived every evidence row's expected index role as
`evidence:observe:{run_id}`. The recorder's flight-record rows use
`evidence:record:{run_id}` — a role the verifier could not know, so the
record blob landed as "unreferenced" and the observe pair as "missing".

The honest fix, both sides signed: **the evidence row names its own
`index_role`**. The builder stamps it (after the digest, from the door's
own role string); the verifier honours it when present and shape-checked
(`^evidence:<door>:<ref>$`), deriving `evidence:observe:{run_id}` only for
rows without one — i.e. every pre-C7.3 bundle verifies byte-identically.
A relabel needs a re-sign, which is the signature door's job to catch.

## 3. A1 — the repo-config gate (found: the host's three failing tests)

`tests/test_git_host_exec_gate.py` arrived failing 3/4: host-side git tools
(`git_status`/`git_stage`, isolation NONE) execute programs the repository's
own config names — `core.fsmonitor` on status, `filter.<name>.clean` on
stage — on the HOST, and the agent's `bash` can write `.git/config` (the
protected-path rule binds the fs tools, not bash).

Fix: `_repo_config_gate` in `git_tools.py` — every git tool call first
probes the repository's **local** config (`git config --local
--get-regexp`) for keys that name programs (`core.fsmonitor`,
`filter.*.clean/smudge/process`, `diff.external`) and raises `PaaError`
naming what it found, before any status/stage/commit runs. Local only: the
operator's global git-lfs filters are theirs, outside what the workspace
can write; refusing them would break every lfs checkout for no gain. The
agent loop catches the raise and shows the model the refusal, so the
errors-as-values contract survives at the loop while the tamper answer
stays a raised, named no. Probe reads config only — no index touch, no
driver run. File moved to `tests/coder/` per its own docstring; lint
fixed (RUF100 + 2× SIM105). 4/4 pass.

## 4. The environment sweep (found: one drifted contract)

`scripts/test-env.sh` brings up, without root: Redis (redislite's bundled
server, port 6379), PostgreSQL 16 (pgserver binaries + `pg_trgm` compiled
from the PG 16.2 source against the shipped headers + pgvector rebuilt),
and **two distinct S3 implementations** (SeaweedFS 3.90 + Garage 1.0.1 —
MinIO's OSS server is archived, 410 Gone, so the old recipe cannot be
reproduced; the suite's own distinguishability test would rightly refuse
two containers of one image). Prereqs: `qdrant-client`, `kuzu`, `psycopg`,
`psycopg_pool`, `boto3`, `pgserver`, `redislite`, `sentence-transformers`
(+CPU torch).

Un-skipped, all green: 44 qdrant-backed vector tests, ~40 postgres
(migrations + tier migration P009 + contract), 4 redis queue tests, 43 S3
live tests (both servers, including `test_the_two_servers_are_distinguishable`),
2 kuzu graph tests, 1 sentence-transformers embedder test.

**The finding:** `test_postgres_contract.py::
test_a_corrupt_row_resolves_to_no_match` failed once the server existed.
It corrupted the **raw** `aliases` column; since P003 the alias rung
expands `aliases_norm`, so the corruption was invisible and the test's
"no match" assertion failed against a healthy lookup. The sqlite twin
(`tests/memory/test_facts.py`) corrupts `aliases_norm` — the postgres twin
drifted at P003 and could not say so, because a postgres-gated test that
never runs is a contract nobody checks. Fixed to corrupt the same column
as its twin; docstring states the drift.

Remaining skips by design: the either-or driver gate
(`test_a_store_without_the_driver_is_not_supported` runs only when boto3
is absent), platform gates (Windows-only, symlink-less hosts), and the
model-key-gated live-model rows (no key in this sandbox).

## 5. Gates

- `ruff check src tests` — clean.
- `mypy src/paa` — 197 files, 0 issues (numpy upgraded to 2.5.5 to match
  the host's stubs, where bare `ndarray` carries default type arguments;
  the old `type-arg` ignores are unneeded and `warn_unused_ignores`
  would flag them).
- Full suite with every env gate open — green (see the board row).
- Conventions incl. the docs-reference lock — 34 passed after the
  re-anchor.

## 6. Known quirks (pre-existing, recorded)

- `session_root_died exit_code=128` warnings appear in normal sandbox CLI
  runs; results and classification stay correct. Root cause is the
  sandbox session monitor, not the code under test; noted here so the
  next run does not re-investigate it.
- pytest 9's double `-q` (addopts + manual) suppresses the summary line;
  count with a single `-q` or read the dots.
