# syntax=docker/dockerfile:1
# =====================================================================
# PAA runtime image — the RFC's server topology (docs/adr/0019).
#
# Multi-stage, and the stages are arranged around *the network*, because the
# network is the only part of a build that fails for reasons nobody in this
# repository controls:
#
#   wheelhouse  Downloads every dependency wheel, once. Its layer depends
#               only on pyproject.toml, so a source change does not re-download
#               anything, and a rebuild after a code change does no network
#               I/O for dependencies at all.
#   builder     Installs from that wheelhouse with --no-index: **offline**.
#               The install either finds every wheel it needs or fails
#               loudly — it can never quietly install less than was asked
#               for, which is the property that makes the wheelhouse safe.
#   runtime     The lean image: the virtualenv, the Docker client, and a
#               non-root user. No build toolchain, no pip cache, no source.
#
# Every network operation retries. A flaky mirror or a dropped connection is
# not a reason for a build to fail, and a build that fails for a transient
# reason is indistinguishable from a build that fails for a real one — so
# the failure has to be unambiguous, which means the transient cases are
# absorbed first and only a *persistent* failure is allowed to stop the
# build. See docs/deployment/docker.md.
#
# The server extras (postgres, s3, redis, vector/qdrant-client, api, otel)
# are installed here. The heavyweight embeddings extra (torch, ~2 GB) is left
# out deliberately — the hash-embedder fallback keeps the runtime functional,
# and a deployment that wants real embeddings should build a derived image.
# =====================================================================

# ---- wheelhouse -----------------------------------------------------------
FROM python:3.12-slim@sha256:a6e34c598f2467ed0e9a8d349809fcd8b5c603269512df273a0bb1784edc11b1 AS wheelhouse

# pip's own retry budget: a slow index gets a long timeout and many
# attempts, so a marginal network is given every chance before the outer
# loop below decides the failure is real.
ENV PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_RETRIES=10 \
    PIP_TIMEOUT=120

# The requirement list is read out of the project's own metadata rather than
# maintained by hand. A hand-maintained list is a second place to forget to
# update, and the failure it produces — an image missing a dependency — is
# exactly the kind that shows up at runtime instead of at build time.
#
# The extras named here are the ones the *server* image needs. The
# heavyweight `embeddings` extra is deliberately absent (see the header).
#
# Two files are written, from one script and one list of extras: the
# requirements the wheelhouse downloads, and the extras the builder
# installs. Two hand-maintained lists of the same thing would eventually
# disagree — and the disagreement would be a build failure at best, because
# the offline install cannot find a wheel that was never downloaded.
COPY pyproject.toml ./
RUN python3 - <<'PY'
import pathlib
import tomllib

meta = tomllib.loads(pathlib.Path("pyproject.toml").read_text(encoding="utf-8"))
project = meta["project"]
extras = project["optional-dependencies"]
groups = ("vector", "api", "redis", "postgres", "s3", "otel")

wanted = list(project["dependencies"])
for group in groups:
    wanted.extend(extras[group])
# The build backend and the packaging tools have to be installable *offline*,
# so they are part of the wheelhouse rather than fetched when the project is
# built. Without them the builder stage would need the network, and the
# offline install would be a lie.
wanted.extend(("hatchling", "pip", "setuptools", "wheel"))

pathlib.Path("/requirements.txt").write_text("\n".join(dict.fromkeys(wanted)) + "\n")
pathlib.Path("/extras.txt").write_text(",".join(groups))
PY

# --only-binary=:all: is a deliberate constraint, not a shortcut. A
# dependency that ships no wheel would have to be *built*, and this image
# carries no compiler — so the build would fail with a compiler error that
# names the wrong cause. With the constraint it fails with "no matching
# distribution", which names the right one. If a dependency ever needs an
# sdist, the fix is a base image with a toolchain, not a silent build.
#
# The retry loop is the outer defence: pip's --retries covers a request that
# failed mid-flight, and the loop covers a download that failed in a way pip
# considered complete. Backoff grows, so a mirror that is down stays down
# for a while rather than being hammered.
RUN set -eux; \
    retry() { \
        attempt=0; \
        until "$@"; do \
            attempt=$((attempt + 1)); \
            if [ "$attempt" -ge 5 ]; then \
                echo "giving up after $attempt attempts: $*" >&2; \
                return 1; \
            fi; \
            echo "attempt $attempt failed: $* — retrying in $((attempt * 10))s" >&2; \
            sleep $((attempt * 10)); \
        done; \
    }; \
    retry pip download --dest /wheelhouse --only-binary=:all: -r /requirements.txt; \
    ls /wheelhouse | head -5; \
    echo "wheelhouse: $(ls /wheelhouse | wc -l) wheels"

# ---- builder --------------------------------------------------------------
FROM python:3.12-slim@sha256:a6e34c598f2467ed0e9a8d349809fcd8b5c603269512df273a0bb1784edc11b1 AS builder

ENV PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PYTHONDONTWRITEBYTECODE=1

# The wheelhouse comes in first, so the layer that installs from it is
# invalidated by the *source*, not by the network. The extras list comes
# with it: the builder installs exactly the extras the wheelhouse holds.
COPY --from=wheelhouse /wheelhouse /wheelhouse
COPY --from=wheelhouse /extras.txt /extras.txt

WORKDIR /build
# Copy only what the build needs first, so the dependency layer caches across
# source-only changes.
COPY pyproject.toml README.md ./
COPY src ./src

# --no-index is the point of the whole arrangement: this step cannot reach
# the network, so it either has every wheel it needs or it fails. There is
# no third outcome in which the image silently ends up with fewer
# dependencies than the project declares.
#
# --no-build-isolation keeps pip from creating a fresh build environment and
# reaching for the network to populate it; hatchling came from the
# wheelhouse and is already here.
RUN set -eux; \
    python -m venv /opt/venv; \
    /opt/venv/bin/pip install --no-index --find-links=/wheelhouse --upgrade pip setuptools wheel hatchling; \
    # The extras come from the same list the wheelhouse downloaded, so the
    # two cannot drift.
    /opt/venv/bin/pip install --no-index --find-links=/wheelhouse --no-build-isolation ".[$(cat /extras.txt)]"; \
    # `pip check` verifies that the installed set is internally consistent.
    # It is the last line of defence against a wheelhouse that resolved to a
    # combination the project does not actually work with.
    /opt/venv/bin/pip check

# ---- runtime --------------------------------------------------------------
FROM python:3.12-slim@sha256:a6e34c598f2467ed0e9a8d349809fcd8b5c603269512df273a0bb1784edc11b1

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PATH="/opt/venv/bin:$PATH" \
    # PAA_HOME is the single mutable state root. In compose it is a *bind*
    # mount of ${PAA_DATA_DIR}, not a named volume: the Docker daemon has to
    # be told a real host path to mount the task workspace, and a named
    # volume's host path is not knowable from inside the runtime.
    PAA_HOME=/data \
    PAA_API_PORT=8787

# The Docker *client*, and only the client. The runtime daemon launches the
# throwaway worker containers (sandbox/docker_backend.py) and, once the tool
# layer lands, the MCP-server and plugin containers — so it needs a way to
# talk to the Docker daemon. The socket is mounted in docker-compose.yml.
#
# The client is a STATIC binary, not a package: Docker publishes one static
# build per architecture, it has no libc dependency at all, and it therefore
# does not care which Debian release the base image is on. That last point is
# the reason this is not an apt install — a `docker-ce-cli` package version
# is spelled per distribution release (`5:29.9.1-1~debian.13~trixie`), so
# pinning one couples the image to a codename, and Docker's `stable` channel
# prunes old package versions, so a pinned package version eventually stops
# existing and the build breaks for a reason that has nothing to do with this
# code.
#
# The tarball is pinned by SHA-256 and the build verifies it. Docker does not
# publish a per-file checksum for the static tarballs, so the digest is
# recorded here — which is the same trust model as every other pinned
# download, and a strictly stronger one than "apt installed whatever was
# newest this morning".
#
# The client will be pointed at a socket that is effectively host root, so
# "where did this binary come from" is a question worth being able to answer
# with a digest. `docker-ce` (the daemon) is deliberately NOT installed:
# this container talks to the host's daemon, it does not run one.
ARG DOCKER_CLI_VERSION=29.9.1
ARG DOCKER_CLI_SHA256_AMD64=c158ecc55adb3afb01bbd3539ed3b997d20c26dbdc418080506cc1bf5243ff57
ARG DOCKER_CLI_SHA256_ARM64=3e93cc6d1a686456a3b18eefdde26467a837a7a2b257af22e27cba1dbc1b2018
RUN set -eux; \
    retry() { \
        attempt=0; \
        until "$@"; do \
            attempt=$((attempt + 1)); \
            if [ "$attempt" -ge 5 ]; then \
                echo "giving up after $attempt attempts: $*" >&2; \
                return 1; \
            fi; \
            echo "attempt $attempt failed: $* — retrying in $((attempt * 10))s" >&2; \
            sleep $((attempt * 10)); \
        done; \
    }; \
    retry apt-get -o Acquire::Retries=10 update; \
    retry apt-get -o Acquire::Retries=10 install -y --no-install-recommends ca-certificates; \
    rm -rf /var/lib/apt/lists/*; \
    # Docker names the architecture directories the way the kernel does
    # (x86_64, aarch64); Debian names them the way the ABI does (amd64,
    # arm64). The two spellings are mapped here, once, rather than being
    # assumed to be the same — an unmapped name is a 404 at build time, and
    # a 404 on the *download* is the one failure a digest check cannot
    # catch, because there is nothing to check.
    case "$(dpkg --print-architecture)" in \
        amd64) goarch=x86_64; expected="${DOCKER_CLI_SHA256_AMD64}" ;; \
        arm64) goarch=aarch64; expected="${DOCKER_CLI_SHA256_ARM64}" ;; \
        *) echo "unsupported architecture: $(dpkg --print-architecture)" >&2; exit 1 ;; \
    esac; \
    # The download AND its verification are one attempt, so a corrupt
    # transfer is a failed *attempt* rather than a failed build. The
    # checksum is verified after the bytes arrive, because a truncated
    # download is a corruption the length check would not catch.
    fetch_docker_cli() { \
        python3 -c "import shutil, sys, urllib.request; \
response = urllib.request.urlopen(sys.argv[1], timeout=120); \
out = open(sys.argv[2], 'wb'); \
shutil.copyfileobj(response, out); \
out.close(); response.close()" \
            "https://download.docker.com/linux/static/stable/${goarch}/docker-${DOCKER_CLI_VERSION}.tgz" /tmp/docker.tgz \
            || return 1; \
        echo "${expected}  /tmp/docker.tgz" | sha256sum -c - || { rm -f /tmp/docker.tgz; return 1; }; \
    }; \
    retry fetch_docker_cli; \
    tar -xzf /tmp/docker.tgz -C /tmp docker/docker; \
    install -m 0755 /tmp/docker/docker /usr/local/bin/docker; \
    rm -rf /tmp/docker /tmp/docker.tgz; \
    docker --version

# Non-root by construction. A fixed high UID avoids colliding with host users
# when a volume is bind-mounted, and owns only the state directory.
#
# The Docker socket is owned by root:<docker-gid> with mode 0660, so the daemon
# user also needs that GID as a supplementary group. The compose file adds it
# with ``group_add``; the GID is a host property, so it is configuration
# (DOCKER_GID) rather than something baked into the image.
RUN groupadd --system --gid 10001 paa \
    && useradd --system --uid 10001 --gid paa --home-dir /home/paa --create-home paa \
    && mkdir -p /data \
    && chown -R paa:paa /data

COPY --from=builder /opt/venv /opt/venv

WORKDIR /home/paa
USER paa

VOLUME ["/data"]
EXPOSE 8787

# Liveness is a loopback TCP connect to the ingestion API the runtime binds
# (Settings.api_host is loopback-only by validator). No curl dependency: a
# three-line socket connect is enough to tell "process up and listening" from
# "crashed or wedged", which is what an orchestrator restart policy needs.
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD python -c "import os,socket,sys; \
s=socket.socket(); s.settimeout(3); \
sys.exit(0 if not s.connect_ex(('127.0.0.1', int(os.environ.get('PAA_API_PORT','8787')))) else 1)"

# The long-running daemon. `paa serve` is the console entry point declared in
# pyproject ([project.scripts] paa = "paa.cli.main:app"); it boots the runtime
# (Runtime.build) and serves the loopback ingestion API.
CMD ["paa", "serve"]
