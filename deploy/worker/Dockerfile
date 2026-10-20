# =====================================================================
# paa/base_worker:v4.1 — the sandbox *worker* image (docs/adr/0019, RFC §13).
#
# This is the image `DockerSandbox` launches for a plan step. It runs
# UNTRUSTED work: a plan step's command is agent-authored, and on a
# multi-tenant deployment it is a stranger's code. Every package added here
# is attack surface, so the image is deliberately as small as it can be while
# still being able to run the commands a plan step realistically contains.
#
# It is NOT the image MCP servers and plugins run in — that is
# `paa/toolbox:v1` (deploy/toolbox/Dockerfile). The distinction matters:
# the worker image is the untrusted-workload surface and stays minimal; the
# toolbox is the *tool* surface and carries a Node runtime because most MCP
# servers are published as npm packages.
#
# The version string is load-bearing: `DockerSandbox.DEFAULT_IMAGE` and the
# ledger's `allocated_worker_image` default both name this exact tag, so the
# recorded image and the executed image cannot drift apart. Bump the tag in
# both places together, and in both SQL schemas (the parity test enforces
# that the two schemas agree).
#
# The base is pinned by digest. An image that runs untrusted work should not
# change underneath itself, and a moving tag is exactly that. To refresh:
#   docker pull python:3.12-slim
#   docker image inspect python:3.12-slim --format '{{index .RepoDigests 0}}'
#
# Build:
#   docker build -t paa/base_worker:v4.1 deploy/worker
# =====================================================================

FROM python:3.12-slim@sha256:a6e34c598f2467ed0e9a8d349809fcd8b5c603269512df273a0bb1784edc11b1

# git and ca-certificates are the two things a plan step reaches for
# constantly (clone a repo, fetch a dependency over TLS). Nothing else.
#
# The apt retry flags are not decoration: a build that fails because a
# mirror dropped a connection is a build that failed for a reason nobody in
# this repository controls, and it is indistinguishable from a build that
# failed for a real reason. See docs/deployment/docker.md.
RUN apt-get -o Acquire::Retries=10 update \
    && apt-get -o Acquire::Retries=10 install -y --no-install-recommends git ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# Non-root by construction, matching the `paa` daemon's own uid/gid so the
# container can write the workspace mount the daemon owns (/data/workspaces
# is owned by paa:paa in the server topology).
#
# The sandbox backend additionally passes ``--user`` derived from the
# workspace directory's owner, so the container runs as whoever owns the
# directory it was given. The uid here is the fallback for an image run by
# hand.
RUN groupadd --system --gid 10001 paa \
    && useradd --system --uid 10001 --gid paa --home-dir /home/paa --create-home paa \
    && mkdir -p /workspace \
    && chown paa:paa /workspace

# The runtime mounts the workspace here and sets it as the workdir, so the
# image's own /workspace is only a placeholder that makes a bare
# `docker run` of this image work.
WORKDIR /workspace

USER paa

# No ENTRYPOINT: the sandbox backend supplies the command as argv. An
# ENTRYPOINT here would be prepended to it and the backend could not
# guarantee what actually ran.
CMD ["python", "-c", "print('paa base worker')"]
