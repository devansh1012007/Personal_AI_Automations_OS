# =====================================================================
# paa/toolbox:v1 — the image MCP servers and plugin commands run in.
#
# This is the *tool* surface: the container a third-party MCP server, or a
# plugin's one-shot command, executes inside. It is the image the sandbox
# backend launches with `--runtime=runsc`, so everything in it is inside
# gVisor's syscall boundary.
#
# Why it exists separately from `paa/base_worker:v4.1` (deploy/worker):
#
#   * base_worker runs UNTRUSTED plan steps and is kept minimal on purpose.
#   * toolbox runs TOOL SERVERS, and the overwhelming majority of published
#     MCP servers are npm packages started with `npx`. A toolbox without a
#     Node runtime could not run the ecosystem this whole design exists to
#     expose.
#
# The `paa` package is deliberately NOT installed here. A plugin that
# imported it would expect a database connection and a ledger handle, and
# the container has neither — installing it would only make a broken plugin
# look like a working one. In-process Python plugins run in the daemon, not
# in this image, and get their capabilities over the host's facade.
#
# ---------------------------------------------------------------------
# The npm cache MUST be on an exec-capable path.
# ---------------------------------------------------------------------
# The sandbox backend mounts /tmp `noexec` — a binary dropped there by a
# workload should not be executable. But npm's cache holds the *executable
# shims* that `npx` installs, so an `npx`-started MCP server fails with:
#
#     sh: 1: <tool>: Permission denied      (exit 126)
#
# ...when the cache is on a noexec mount. The fix is not to drop `noexec`
# from /tmp; it is to give these workloads their own exec-capable scratch
# path. The backend does that via ``SandboxSpec.exec_tmpfs``, and a toolbox
# spec must therefore carry:
#
#     exec_tmpfs=("/run",)
#
# which mounts /run as a writable, exec-capable tmpfs. The image puts
# npm's cache there.
#
# Build:
#   docker build -t paa/toolbox:v1 deploy/toolbox
# =====================================================================

# ---- node -----------------------------------------------------------------
# Node 24 LTS, copied rather than installed from a tarball at build time so
# the image build needs no network beyond the base images and no moving
# upstream URL.
#
# The copy is only safe because both stages are the SAME Debian release:
# `python:3.12-slim` is trixie, so the node stage is `node:24-trixie-slim` —
# a bookworm node binary on a trixie base would be the wrong direction to
# mismatch (a newer base tolerates an older binary's glibc floor, not the
# reverse), and pinning both to trixie removes the question entirely.
#
# The `RUN node --version` below is the guard that makes this safe to leave
# alone: if the two base images ever drift onto different libc versions, the
# build fails here with a clear glibc error instead of shipping a toolbox
# whose `npx` mysteriously does not work.
#
# Both bases are pinned by digest. An image that will run third-party code
# should not quietly change underneath it, and a moving tag is exactly that.
# To refresh: `docker pull <image>` and read the digest from
# `docker image inspect <image> --format '{{index .RepoDigests 0}}'`.
FROM node:24-trixie-slim@sha256:7f486cabe892bff8fef04c0f7da8dba84a560be3e4e2740b7a86afc57ebec315 AS node

# ---- toolbox --------------------------------------------------------------
FROM python:3.12-slim@sha256:a6e34c598f2467ed0e9a8d349809fcd8b5c603269512df273a0bb1784edc11b1

# ca-certificates: an MCP server that talks to an HTTPS API needs the root
# bundle. curl and jq: the two tools a plugin script reaches for to talk to
# a REST API and shape its output. git: MCP servers are frequently git
# repositories.
#
# The apt retry flags are not decoration: a build that fails because a
# mirror dropped a connection is a build that failed for a reason nobody in
# this repository controls, and it is indistinguishable from a build that
# failed for a real reason. See docs/deployment/docker.md.
RUN apt-get -o Acquire::Retries=10 update \
    && apt-get -o Acquire::Retries=10 install -y --no-install-recommends ca-certificates curl jq git \
    && rm -rf /var/lib/apt/lists/*

# Node itself, plus the npm/npx entry points as symlinks into the copied
# module tree (the upstream image does the same).
COPY --from=node /usr/local/bin/node /usr/local/bin/node
COPY --from=node /usr/local/lib/node_modules /usr/local/lib/node_modules
RUN ln -s /usr/local/lib/node_modules/npm/bin/npm-cli.js /usr/local/bin/npm \
    && ln -s /usr/local/lib/node_modules/npm/bin/npx-cli.js /usr/local/bin/npx \
    && node --version && npm --version && npx --version

# The npm cache lives on /run, which the sandbox backend mounts as an
# exec-capable tmpfs (see the header). It cannot live on /tmp: that mount is
# noexec, and npx's shims are executables.
ENV NPM_CONFIG_CACHE=/run/npm-cache \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

# Non-root by construction, matching `paa/base_worker:v4.1` and the `paa`
# daemon's own uid/gid, so the container can write the workspace mount. The
# sandbox backend additionally passes `--user` derived from the workspace's
# owner, so the container runs as whoever owns the directory it was given
# rather than as root.
#
# /run is NOT created here: the sandbox backend mounts it as a tmpfs (see the
# header), and a tmpfs mount shadows whatever the image put at the path.
# npm recreates its cache directory inside the mount, because the mount is
# mode=1777.
RUN groupadd --system --gid 10001 paa \
    && useradd --system --uid 10001 --gid paa --home-dir /home/paa --create-home paa \
    && mkdir -p /workspace \
    && chown paa:paa /workspace

WORKDIR /workspace

USER paa

# No ENTRYPOINT: the sandbox backend supplies the server command as argv.
# (An ENTRYPOINT here would be prepended to it, and the backend could not
# guarantee what actually ran.)
CMD ["node", "--version"]
