# ADR-0042: The embeddings image is derived, installs online with retries, and pins the runtime tag

## Status

Accepted

*Recorded 2026-11-07 by W1-IMPL-01 (wave 1) for plan item P-014. The derived
`Dockerfile.embed` and its CI job are the P-005 implementer's to write.
This ADR resolves the offline-vs-derived contradiction the investigation
found in the plan, pins the base tag, and records why the digest-pin rule
does not apply.*

## Context

The base runtime image is deliberately torch-free. Its Dockerfile header
says so in so many words: the heavyweight embeddings extra (~2 GB of torch)
is left out, the hash-embedder fallback keeps the runtime functional, and a
deployment that wants real embeddings should build a **derived image**. The
base image's install is offline by design — the wheelhouse stage downloads
every wheel, and the builder stage installs from it with `--no-index`, so
the install either finds every wheel or fails loudly. That offline property
is what makes the wheelhouse safe, and it is not to be "simplified" back to
a networked install.

Hash-only embeddings are the cause of the recall gap: paraphrases the
keyword tier cannot spell-match fail under the hash embedder's geometry.
The derived image is the pre-approved answer.

**The contradiction, stated plainly.** The plan's P-005 text says the
derived install runs `pip install --no-index` from the wheelhouse, offline,
per the Dockerfile convention. But a derived image `FROM paa/runtime:4.1`
starts from the *runtime* stage, which contains only the virtualenv and the
Docker client — **no wheelhouse**. An offline install from a wheelhouse the
image does not have is impossible. Copying the plan text blindly would file
a Dockerfile that cannot build. This ADR corrects the plan claim with the
evidence: the derived image cannot be offline without extending the base
image's wheelhouse groups, which would bloat the base build for torch.

## Decision

**The derived `Dockerfile.embed` installs online, with the full retry
convention.** It starts `FROM paa/runtime:4.1` — pinned to the tag the
compose file builds and runs (`docker-compose.yml:67`), never `latest` —
and installs the `embeddings` extra (`sentence-transformers` in
`pyproject.toml:42`) with `PIP_RETRIES=10`, `PIP_TIMEOUT=120`, an outer
`retry()` shell loop (5 attempts, growing backoff) and `pip check` last.
Every network operation retries; only a *persistent* failure stops the
build. The base Dockerfile is not modified: its offline wheelhouse install
(wheelhouse and builder stages, `--no-index` with `pip check`) is untouched,
and this ADR supersedes only the plan sentence that claimed the derived
image shares it.

**The digest-pin rule does not apply, and ADR-0030 is not contradicted.**
The deployment-image contract requires pulled images to be digest-pinned;
a tag-only pull is an error. The derived image is a **local build** on a
tag produced by the base build (`build:` + `image:`), not a compose pull —
same class as the services themselves. It is pinned to `paa/runtime:4.1`
because that is the tag, and re-pointing that tag re-points the derived
image by construction.

**CI asserts the separation.** Inside the embed image,
`python -c "import sentence_transformers"` succeeds. Inside the base image
the same import must fail and the hash-fallback degraded marker must be
present. The hash fallback path asserts its degraded marker in the base;
the real-embedder parity suite (the A5b criterion) runs only inside the
embed image, and its skip in a base-image run is logged, never counted as a
failure of the base run.

### Alternatives rejected

* **Online install with the retry convention (chosen).** Lean; keeps the
  base wheelhouse torch-free; the network-fragility convention already
  covers online operations.
* **Extend the base wheelhouse groups to include embeddings, then install
  offline in the derived image.** Rejected: the base build downloads ~2 GB
  of torch wheels on every cold wheelhouse even for deployments that never
  embed. The investigation recommended the online option for this reason,
  and the record agrees.
* **Base-image `--no-index` claim copied as written.** Rejected: the
  wheelhouse does not exist in the derived image. A claim that cannot build
  is not a plan.

## Consequences

**What it costs.** A derived image (~2 GB, isolated), a CI embed job, and
the discipline that the derived image's pin is edited in exactly one place.
A flaky mirror is absorbed by the retry loops; a persistent outage fails
the build loudly, never silently partial.

**What it does not change.** The base image, the wheelhouse offline property,
and embedder selection (the spec-found check picks the real embedder
transparently; absent, the hash fallback serves with its warning).

**How to roll it back.** Delete the derived image. A5a (fake-vector suite)
stays green; A5b skips, logged. Nothing in the base image needs changing.
