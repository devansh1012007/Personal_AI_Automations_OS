# =====================================================================
# PAA runtime image — the RFC's server topology (docs/adr/0019).
#
# Multi-stage: a builder resolves and compiles the wheel set into a virtualenv,
# and a lean runtime stage copies only that venv. The result carries no build
# toolchain, no pip cache and no source tree beyond the installed package, so
# the attack surface and the image size are both minimal.
#
# The server extras (postgres, minio, redis, vector/qdrant-client, api, otel)
# are installed here. The heavyweight embeddings extra (torch, ~2 GB) is left
# out deliberately — the hash-embedder fallback keeps the runtime functional,
# and a deployment that wants real embeddings should build a derived image.
# =====================================================================

# ---- builder --------------------------------------------------------------
FROM python:3.12-slim AS builder

ENV PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PYTHONDONTWRITEBYTECODE=1

# psycopg[binary] ships its own libpq and boto3/redis/qdrant-client are pure
# Python, so no system build dependencies are required for the server extras.
RUN python -m venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"

WORKDIR /build
# Copy only what the build needs first, so the dependency layer caches across
# source-only changes.
COPY pyproject.toml README.md ./
COPY src ./src

RUN pip install ".[postgres,minio,redis,vector,api,otel]"

# ---- runtime --------------------------------------------------------------
FROM python:3.12-slim AS runtime

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PATH="/opt/venv/bin:$PATH" \
    # PAA_HOME is the single mutable state root; it is a named volume in compose
    # so the ledger survives container replacement.
    PAA_HOME=/data \
    PAA_API_PORT=8787

# Non-root by construction. A fixed high UID avoids colliding with host users
# when a volume is bind-mounted, and owns only the state directory.
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
