<!-- ORCH-DIGEST:START -->
agent: W2-IMPL-04 | domain: P-005 derived embed image (Dockerfile.embed + CI embed job, T06) | status: DONE
items: Dockerfile.embed + ci.yml embed-image job done (ADR-0042 online+retries, pinned paa/runtime:4.1); docker build NOT-RUN (docker absent, CI only); T06 pytest deferred (outside write-set, NEED posted) + A5b parity fixture FINDING
proposed_tasks: 0 (this is the implementation)
top_risks: embed-image build is CI-verified only (this host has no docker); CI T06 import assertion is the real gate
cross_domain_conflicts: none (Dockerfile.embed + ci.yml sole owner this wave)
out_of_plan_discoveries: 1 (.dockerignore checked-OK; A5b fixture decision left for ORCH)
confidence: high (pin + retry conventions grep-verified, ci.yml parses, compose_config 11 passed; build itself CI-only by design)
last_comms_line_read: 2026-11-07T08:48:33Z ORCH -> ALL | DECISION | W2 (COMMS.md:55)
<!-- ORCH-DIGEST:END -->

# Implementation report — W2-IMPL-04 — P-005 (T-005-P005)

## 0. Scope and method

Task: implement plan P-005 (T-005) per brief `docs/agent-ops/20261107-integration-fix/briefs/W2-IMPL-04.md`.
WRITE-SET only: `Dockerfile.embed` (new), `.github/workflows/ci.yml`.
Required reading done in brief order: brief, STANDING-ORDERS, AGENTS.md (project instructions),
00-mission.md, plan rev5 `C:\Users\devan\.opencode\plan\2026-11-07-integration-fix.md`
(P05 row line 67, T06 line 122, §K roll-out line 159), ADR-0042 (full, 95 lines),
W0-INV-05 §P-005/§P-013, W0-INV-01 §P-002/§P-004 (A5b criterion), W1-IMPL-01 report
(ORCH-DIGEST template), then code at HEAD: `Dockerfile` (all 266 lines), `docker-compose.yml:54-90`,
`pyproject.toml` (all 160 lines), `.github/workflows/ci.yml` (all 433 lines),
`src/paa/storage/vector/embeddings.py` (selection `get_embedder` 386-433, degraded marker 419-433),
`docs/adr/0030-deployment-image-contract.md:108-116,295-299`, `tests/deployment/test_compose_config.py`
(full), `tests/storage/test_vector.py` (`HAS_SENTENCE_TRANSFORMERS`:43, factory tests 908-929),
`.dockerignore` (full).

Branch verified: `ops/20261107-integration-fix` [RAN git branch --show-current].
HEAD at start: `8faa64e docs(ops): close wave 1, open wave 2 20261107-integration-fix` [RAN git log --oneline -5].
Working tree at start: `M docs/agent-ops/20261107-integration-fix/01-work-ledger.md` (sibling/orchestrator-owned,
never touched by me) [RAN git status --porcelain].
Docker absent on this host [RAN Get-Command docker -> CommandNotFoundException / DOCKER-ABSENT-CONFIRMED] —
the docker build is NOT-RUN (needs CI), per brief §3 and 00-mission RS2.

COMMS read at start; last line: 2026-11-07T08:48:33Z ORCH -> ALL | DECISION | W2 (wave 2 start).

## 1. Phased changes executed

### Phase 1 — `Dockerfile.embed` (new, repo root)

Written from ADR-0042's Decision verbatim (ADR wins over the plan's
offline claim, per brief §6):

- `FROM paa/runtime:4.1` at Dockerfile.embed:58 — exact pin from
  [VERIFIED docker-compose.yml:67] `image: paa/runtime:4.1` (also `:86`).
  No `latest`, no digest (ADR-0042: local-build class, ADR-0030's
  digest-pin rule for pulls does not apply; the tag is the contract).
- Installs the `embeddings` extra by installing the project itself
  from source (`COPY pyproject.toml README.md /build/` +
  `COPY src /build/src/`, then
  `retry /opt/venv/bin/pip install --no-build-isolation ".[embeddings]"`):
  the extra's requirements are read from `pyproject.toml:42`
  (`sentence-transformers>=3.3`) — single source of truth, no second
  hard-coded spec list (the same principle the base Dockerfile applies
  to its installer list, [VERIFIED Dockerfile:44-56]).
  `// DECISION:` online install, not `--no-index`: the runtime stage
  this builds FROM carries only the virtualenv
  [VERIFIED Dockerfile:246] `COPY --from=builder /opt/venv /opt/venv` —
  there is no wheelhouse to install from offline, and extending the
  base wheelhouse with torch would bloat every cold base build
  (ADR-0042's rejected alternative). ADR-0042 (W1-IMPL-01) already
  resolved this plan-vs-code contradiction; this file follows the ADR,
  and the file header records why. The plan's `--no-index` sentence is
  NOT copied as instruction.
- `// INVARIANT:` every network op retries. Exactly one network op
  (the pip install): `ENV PIP_DISABLE_PIP_VERSION_CHECK=1`,
  `PIP_NO_CACHE_DIR=1`, `PIP_RETRIES=10`, `PIP_TIMEOUT=120` (same
  values as [VERIFIED Dockerfile:39-42]) + outer `retry()` shell loop
  (5 attempts, growing backoff, same shape as the base). `pip check`
  is the last line of the build, as in the base (`Dockerfile:144`).
  `NO_CACHE_DIR` also guarantees no pip-cache layer leftovers.
  Version-check disable removes an unretried PyPI call from the
  install path. No apt, no tarball download — no other network ops.
- `// DECISION:` no `--only-binary=:all:` on the install line — the
  base builder's own local-project install omits it
  [VERIFIED Dockerfile:140]; the constraint the repo applies lives on
  the wheelhouse *download*, and this derived image has no wheelhouse
  stage. The install is `--no-build-isolation` because hatchling and
  setuptools are already in `/opt/venv` from the base builder
  [VERIFIED Dockerfile:137-140].
- Single stage; inherits the base runtime stage wholesale (user,
  `/data`, Docker client, healthcheck, `paa serve` CMD) — not
  re-staged, per brief. `COPY` leaves the source copy in the image
  history (a few MB of text next to ~2 GB of torch); the final `RUN`
  removes `/build` from the filesystem so no build leftovers remain
  visible. Trace comment `PLAN:integration-fix:P005` in the header.
  Header documents: derived-only, base stays torch-free, pin provenance.

### Phase 2 — CI `embed-image` job (`.github/workflows/ci.yml`, additive +121/-0)

A new top-level `jobs.embed-image` ("embed image separation"), added
after `jobs.gate` — no existing job restructured, no deletions
[VERIFIED git diff --numstat: 121 additions, 0 deletions]. Steps:

1. `checkout` (same pinned action `actions/checkout@3d3c42e5… # v7.0.1`,
   `fetch-depth: 1` as the gate job).
2. Pin assertion: `grep -q '^FROM paa/runtime:4.1$' Dockerfile.embed`
   — the ADR pin contract is checked in CI, so an unpinned FROM goes red
   before any image is built. (Locally verified: matches exactly.)
3. Build the base image (`docker build -t "$PAA_BASE_IMAGE" -f Dockerfile .`)
   and then the derived image
   (`docker build -t "$PAA_EMBED_IMAGE" -f Dockerfile.embed .`),
   each wrapped in the gate job's own `retry()` shell convention
   (5 attempts, growing backoff) so a flaky base-image pull does not
   read as a code failure. `// DECISION:` wrap the *build invocation*
   too — the Dockerfile's internal steps already retry their own
   network ops, but nothing can retry the initial `python:3.12-slim`
   digest pull from inside the build.
4. Base separation assertions: (a) `docker run --rm $PAA_BASE_IMAGE
   python -c "import sentence_transformers"` must FAIL — an import
   that succeeds fails the step loudly (ADR-0042: "the same import
   must fail" in the base); (b) degraded-marker assertion calling
   `get_embedder(Settings())` in the base image and asserting the
   result is a `HashEmbedder`. `// DECISION:` the marker
   (`embeddings.hash_fallback_active`) is emitted unconditionally on
   the exact branch returning `HashEmbedder`
   [VERIFIED src/paa/storage/vector/embeddings.py:419-433], so the
   returned type asserts the marker path ran without depending on
   structlog's unconfigured output plumbing. `// NOTE:` the
   import-fails assertion cannot pass vacuously for a *missing* image:
   the preceding build steps create the tag, and the hash-fallback
   step immediately after would fail on a broken image.
5. T06 assertions in the embed image: `docker run --rm $PAA_EMBED_IMAGE
   python -c "import sentence_transformers"` must exit 0 (plan §H T06),
   then `docker run --rm $PAA_EMBED_IMAGE pip check` exit 0
   (ADR-0042: `pip check` last — verified in the delivered image).

`// DECISION:` the embed job runs on the same workflow triggers as
`gate` (push / pull_request / workflow_dispatch) as a single job, not
a matrix row and not nightly. ADR-0042 scopes the job by content, not
by schedule (it is silent on nightly/all-only). The fallback rule in
the brief ("scope to the `all` shape") is satisfied structurally:
the `all` leg is the only gate leg that downloads torch
(`paa[all]` ⊃ `paa[embeddings]`, [VERIFIED pyproject.toml:77-79]), and
this job parallels it exactly once per push instead of once per matrix
leg — the "second torch download path" is bounded by one 45-minute
`timeout-minutes` (mirroring the gate), never ×3 shapes. No
`schedule:` trigger was added (that would change the existing jobs'
behavior — restructuring). Image names are defined once in the job's
`env:` (`PAA_BASE_IMAGE`, `PAA_EMBED_IMAGE`) — not in compose
(brief §3); the base tag textually equals the compose pin, and the
pin-grep step above keeps the two in step.

## 2. Deferred: T06 pytest test (outside write-set — NEED posted)

`tests/` is outside this write-set, so no test was written. Per the
standing orders a NEED was posted to COMMS and the exact test below is
delivered here for the orchestrator to schedule. The marker
`requires_embeddings` is registered (`pyproject.toml:97`,
"needs the sentence-transformers extra"), so `--strict-markers` is
satisfied, and the test needs no skip guard of its own — the existing
`HAS_SENTENCE_TRANSFORMERS` skipif pattern is left untouched.

Exact content, to be appended to `tests/storage/test_vector.py` beside
the existing `test_get_embedder_*` factory tests (:908-929) — it models
their shape and uses only imports that file already has:

```python
@pytest.mark.requires_embeddings
def test_get_embedder_selects_the_real_encoder_when_installed() -> None:
    """A5b (ADR-0042): with the extra installed, get_embedder returns the
    real SentenceTransformerEmbedder, not the hash fallback.

    Must fail on a base image (no extra): get_embedder returns
    HashEmbedder there. Add SentenceTransformerEmbedder to the
    paa.storage.vector import block at the top of this file.
    """
    embedder = get_embedder(ModelSettings())
    assert isinstance(embedder, SentenceTransformerEmbedder)
    assert embedder.dimensions == 384
```

What it does and does not prove: `SentenceTransformerEmbedder.__init__`
does not load the model ([VERIFIED embeddings.py:119-125], lazy `_load`),
so this is deterministic and network-free. It proves the selection flip
the embed image exists for. It does NOT run the A5b paraphrase-recall
parity (real vectors on the 10-paraphrase set): that needs engine weights
(`all-MiniLM-L6-v2`, ~90 MB, downloaded from the model hub on first
`embed()`), i.e. a model-fixture decision — weights vendored nowhere in
the repo, no existing test loads a real model, and a network-downloading
test would be flaky and slow by design. That fixture choice (and whether
the CI embed-image job then runs `pytest -m requires_embeddings` inside
the container, which additionally needs pytest + sources mounted — the
runtime stage carries neither) is left to the orchestrator as a named
FINDING (COMMS). The CI job in Phase 2 asserts T06's exact §H commands
(build + in-image import), which do not need a model.

## 3. Review audit (self-review, then final review)

Self-review after each phase (re-read diff from scratch):

- **Correctness/root cause:** Dockerfile follows ADR-0042 line-for-line
  (pin, online, retry(), pip-check-last). No `--no-index` directive in
  any RUN (only the explanatory comment). Base Dockerfile, compose,
  pyproject untouched (diff empty). No scope expansion.
  *No findings.*
- **Security/edge:** retry loop bounded (5 attempts, backoff) —
  persistent failure terminates the build loudly, never partial, with
  `set -eux` around the install. CI `if docker run ...` makes the
  expected-non-zero explicit. `PIP_NO_CACHE_DIR=1` prevents cache layers.
  *No findings.*
- **Maintainability/conventions:** comments explain *why* (ADR pin,
  online-vs-offline resolution, retry provenance) per house style;
  exported env names defined once; pinned actions mirror the gate job;
  no new `hasattr`/stringly tricks. The one expositional deviation
  (no `--only-binary=:all:`) matches the base builder precedent and is
  recorded. *No findings.*
- **Test rigor:** local verifications in §4 are static (they cannot
  catch a broken install — nothing executable without docker exists to
  test locally, and the brief explicitly accepts STATIC verification
  with NOT-RUN for the build). The CI assertions are each load-bearing:
  revert the pin → grep step red; revert embed install → T06 import
  step red; leak torch into base → torch-free step red; break hash
  fallback → marker step red; break consistency → pip-check red.
  *No findings.*

Final self-review (full diff, cross-phase): the two files are the only
files I wrote (plus this report); `git diff --numstat` shows ci.yml
+121/-0 (purely additive); compose_config tests green (compose
untouched). Line-ending note: edits preserve the files' existing
endings (the edit tool matches bytes).

Residual risks (not blockers): the docker build itself is unverifiable
until CI runs it (recorded as NOT-RUN below); the A5b parity suite
needs the fixture decision in §2; image names (`paa/runtime:4.1`,
`paa/runtime:embed`) are hard-coded strings — the pin-grep keeps the
base tag in step with compose, the embed tag exists only here by design.

## 4. Verification evidence (verbatim outcomes)

- [RAN Get-Command docker -> CommandNotFoundException]
  → **docker absent on this host. The docker build is NOT-RUN here by
  design (needs CI)** — RS2, brief §3. Never claimed otherwise.
- [RAN python yaml.safe_load .github/workflows/ci.yml -> jobs: gate,
  embed-image; env {PAA_BASE_IMAGE: paa/runtime:4.1, PAA_EMBED_IMAGE:
  paa/runtime:embed}; 8 steps as written; YAML-PARSE-OK]
- [RAN Select-String Dockerfile.embed '^FROM ' -> line 58:
  `FROM paa/runtime:4.1` (exact, matches the CI regex `^FROM
  paa/runtime:4.1$`)]
- [RAN Select-String Dockerfile.embed retry conventions ->
  `PLAN:integration-fix:P005` (:5), `FROM paa/runtime:4.1` (:58),
  `PIP_RETRIES=10` (:67), `PIP_TIMEOUT=120` (:68), `retry() {` (:91),
  `/opt/venv/bin/pip check` (:104); no `--no-index` in any RUN line
  (only the explanatory comment, :26)]
- [RAN .venv\Scripts\python.exe -m pytest
  tests/deployment/test_compose_config.py -q -> 11 passed]
  (compose untouched; my change cannot break it, and it is green)
- [RAN Select-String Dockerfile.embed in src/tests/compose/pyproject ->
  no references other than ADR/investigation/report records (file is
  net-new, nothing to regress)]
- [RAN Select-String Dockerfile\.embed (repo-wide) -> ADR-0042,
  W0-INV-05, briefs, ownership/W2.tsv — no test asserts on its
  content, so no existing test can break from the new file]
- [RAN git cat-file bytes of committed blobs -> Dockerfile.embed
  CR=LF=105 (105/105 lines CRLF); base Dockerfile blob CR=LF=266;
  ci.yml blob CR=LF=554] The repo norm is CRLF blobs, including the
  base Dockerfile that CI builds today — and the CI embed job builds
  that same file, so the derived file's matching endings cannot be a
  new failure mode.
- NOT RUN (needs CI): `docker build -f Dockerfile.embed` (either tag),
  in-image `import sentence_transformers`, in-image `pip check`,
  in-image `get_embedder` fallback assertion. NOT RUN (forbidden):
  full suite, containers, any docker invocation.
- ruff/mypy: no `src/` or `tests/` file was edited, so there is
  nothing new for ruff or mypy to check — stated instead of run
  (AGENTS.md gate unaffected).

## 5. Long-run test answers (per brief §8)

- **Source:** Dockerfile.embed exists with `FROM paa/runtime:4.1` exact
  (pin-grep asserted locally AND in CI); derived-only, base stays
  torch-free; online install + retries per ADR-0042; `pip check` last.
- **Load:** flaky mirror → 5-attempt retry with growing backoff and pip's
  own 10×120s budget absorb transient failure; persistent failure fails
  the build loudly. Torch ~2GB isolated to the derived image only.
- **Hostile/degraded:** base import assertion — torch leaking into the
  base image fails CI loudly; base serves hash fallback + degraded
  marker by construction. A corrupt/partial download is a failed
  attempt, never a silent partial image.
- **Operability:** not deployed by default (§K): no compose service
  references the embed image; base build and runtime are unchanged,
  rollback is "do not build/use the derived image".
- **Reversal:** delete Dockerfile.embed and the embed-image job — the
  base image, the gate, and the suite are exactly as before (A5a fake-vector
  suite green; A5b skips logged). Removing the T06 test (if written)
  restores the prior state with nothing else to revert.

## 6. Out-of-scope findings

1. `.dockerignore:46` excludes `Dockerfile` (container artefacts).
   `Dockerfile.embed` is not matched by that pattern and is not needed
   in the build context (it is the `-f` file, read client-side — same
   mechanism the compose `dockerfile: Dockerfile` build already relies
   on). No action needed; recorded as checked-OK. (`.dockerignore` is
   outside my write-set regardless.)
2. A5b parity-suite fixture decision (see §2) — FINDING posted to COMMS;
   left for the orchestrator.

## 7. Unverified assumptions

- [ASSUMED] Hatchling/setuptools are present in the runtime stage's
  `/opt/venv` (the builder installs them there,
  [VERIFIED Dockerfile:137]; the runtime copies the venv wholesale,
  [VERIFIED Dockerfile:246]) so `--no-build-isolation` resolves the
  build backend locally. Only provable by the CI build.
- [ASSUMED] Fresh `ubuntu-latest` runners pull `python:3.12-slim`
  (digest-pinned, pulled under the CI retry loop) without needing a
  host-side cache or daemon — the same assumption the gate job makes for
  pip/apt. Only provable by the CI build.
- [UNKNOWN] The embed-image registry name `paa/runtime:embed` — chosen
  per brief ("define once in CI yaml"); it has no external consumer
  today, so the choice cannot collide with anything on disk.

