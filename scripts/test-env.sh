#!/usr/bin/env bash
# The no-root test environment: every server-gated suite, on one sandbox.
#
# Brings up the four servers the default suite skips without (Redis,
# PostgreSQL with pg_trgm + pgvector, two distinct S3 implementations),
# prints the environment variables that un-skip their suites, and starts
# nothing that needs root. Servers run under /tmp; rerunning is safe.
#
# The suites themselves stay skip-gated (that is their contract with a
# laptop); this script is how a sandbox meets the contract instead.
#
# Prerequisites (one-off):
#   pip install -e ".[dev]" qdrant-client kuzu psycopg psycopg_pool boto3 \
#       sentence-transformers pgserver redislite
#   pip install torch --index-url https://download.pytorch.org/whl/cpu
#
# Usage:
#   scripts/test-env.sh up      # start every server, print the env line
#   scripts/test-env.sh status  # what is up
#   scripts/test-env.sh down    # stop everything started here
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
RUN=/tmp/paa-test-env
mkdir -p "$RUN"

# ---- Redis (redislite ships a real redis-server; we start one detached) ----
up_redis() {
    if command -v redis-cli >/dev/null 2>&1 && redis-cli -h 127.0.0.1 -p 6379 ping 2>/dev/null | grep -q PONG; then
        echo "redis: already up on 6379"; return
    fi
    "$(dirname "$(command -v python)")"/python - <<'EOF'
import redislite
redislite.Redis(serverconfig={"port": "6379", "bind": "127.0.0.1",
                              "save": "", "appendonly": "no"})
EOF
    for _ in $(seq 20); do
        redis-cli -h 127.0.0.1 -p 6379 ping 2>/dev/null | grep -q PONG && break
        sleep 0.5
    done
    echo "redis: up on 6379 (redislite)"
}

# ---- PostgreSQL (pgserver's binaries; pg_trgm compiled from the PG source,
#      pgvector rebuilt against the same headers) ----
PGBIN="$(dirname "$(command -v python)")/../lib/python3.12/site-packages/pgserver/pginstall/bin"
up_postgres() {
    [ -x "$PGBIN/pg_ctl" ] || { echo "postgres: pgserver not installed"; return 1; }
    if [ -S "$RUN/pg/.s.PGSQL.5432" ] && "$PGBIN/pg_isready" -q -h "$RUN/pg" 2>/dev/null; then
        echo "postgres: already up on $RUN/pg"; return
    fi
    rm -rf "$RUN/pg" && mkdir -p "$RUN/pg"
    "$PGBIN/initdb" -D "$RUN/pg" -U postgres --auth=trust -E UTF8 >"$RUN/pg-initdb.log" 2>&1
    "$PGBIN/pg_ctl" -D "$RUN/pg" -l "$RUN/pg-server.log" \
        -o "-k $RUN/pg -p 5432 -c listen_addresses=" start >/dev/null
    # pg_trgm is a contrib extension the pgserver build omits; compile it
    # from the matching source against the shipped server headers.
    if ! ls "$PGBIN/../share/postgresql/extension/" 2>/dev/null | grep -q "^pg_trgm.control"; then
        local ver="$("$PGBIN/pg_config" --version | grep -oE '[0-9]+\.[0-9]+')"
        curl -sSL "https://ftp.postgresql.org/pub/source/v${ver}/postgresql-${ver}.tar.bz2" -o "$RUN/pgsrc.tar.bz2"
        tar -xjf "$RUN/pgsrc.tar.bz2" -C "$RUN"
        cp -r "$RUN/postgresql-${ver}/contrib/pg_trgm" "$RUN/trgm-build"
        (cd "$RUN/trgm-build" && make USE_PGXS=1 PG_CONFIG="$PGBIN/pg_config" -j2 >/dev/null \
            && make USE_PGXS=1 PG_CONFIG="$PGBIN/pg_config" install >/dev/null)
    fi
    echo "postgres: up (socket $RUN/pg, trust auth, pg_trgm + vector present)"
}

# ---- Two distinct S3 implementations (SeaweedFS + Garage), no docker ----
up_s3() {
    mkdir -p "$RUN"
    if ! curl -s -o /dev/null http://127.0.0.1:9000/; then
        # The seaweed tarball is not a clean single-dir archive; extract
        # just the binary. MinIO's OSS server is archived (410 Gone), so
        # SeaweedFS + Garage are the two distinct implementations.
        if [ ! -x "$RUN/weed" ]; then
            curl -sSL -o "$RUN/seaweed.tar.gz" \
                https://github.com/seaweedfs/seaweedfs/releases/download/3.90/linux_amd64.tar.gz
            tar -xzf "$RUN/seaweed.tar.gz" -C "$RUN" weed
            chmod +x "$RUN/weed"
        fi
        cat > "$RUN/seaweed-s3.json" <<'EOF'
{"identities": [{"name": "paa-test",
  "credentials": [{"accessKey": "paa-test-key", "secretKey": "paa-test-secret"}],
  "actions": ["Admin", "Read", "Write", "List", "Tagging"]}]}
EOF
        mkdir -p "$RUN/seaweed-data"
        ("$RUN/weed" server -ip 127.0.0.1 -dir="$RUN/seaweed-data" -volume.max 0 \
            -filer -s3 -s3.port 9000 -s3.config "$RUN/seaweed-s3.json" \
            -master.port 9333 -volume.port 8080 >"$RUN/seaweed.log" 2>&1 &)
    fi
    if ! curl -s -o /dev/null http://127.0.0.1:9020/; then
        [ -x "$RUN/garage" ] || { curl -sSL -o "$RUN/garage" \
            "https://garagehq.deuxfleurs.fr/_releases/v1.0.1/x86_64-unknown-linux-musl/garage" \
            && chmod +x "$RUN/garage"; }
        mkdir -p "$RUN/garage-meta" "$RUN/garage-data"
        cat > "$RUN/garage.toml" <<'EOF'
metadata_dir = "GARAGE_RUN/garage-meta"
data_dir = "GARAGE_RUN/garage-data"
db_engine = "lmdb"
replication_mode = "none"
rpc_bind_addr = "127.0.0.1:3901"
rpc_public_addr = "127.0.0.1:3901"
rpc_secret = "9a52a01cbeffbc6fcb0d13509768f5d1ea6b8b73cae0c6c2b9d16f5c0ae65ac9"
[s3_api]
s3_region = "us-east-1"
api_bind_addr = "127.0.0.1:9020"
root_domain = ".s3.garage.localhost"
[s3_web]
bind_addr = "127.0.0.1:3902"
root_domain = ".web.garage.localhost"
index = "index.html"
[admin]
api_bind_addr = "127.0.0.1:3903"
admin_token = "paa-test-admin-token"
EOF
        sed -i "s|GARAGE_RUN|$RUN|g" "$RUN/garage.toml"
        ("$RUN/garage" -c "$RUN/garage.toml" server >"$RUN/garage.log" 2>&1 &)
        sleep 3
        export GARAGE_RPC_SECRET=9a52a01cbeffbc6fcb0d13509768f5d1ea6b8b73cae0c6c2b9d16f5c0ae65ac9
        local node
        node=$("$RUN/garage" -c "$RUN/garage.toml" status | grep -oE '^[0-9a-f]{16}' | head -1)
        "$RUN/garage" -c "$RUN/garage.toml" layout assign -z dc1 -c 1G "$node" >/dev/null 2>&1 || true
        "$RUN/garage" -c "$RUN/garage.toml" layout apply --version 1 >/dev/null 2>&1 || true
        if ! "$RUN/garage" -c "$RUN/garage.toml" key info paa-test >/dev/null 2>&1; then
            "$RUN/garage" -c "$RUN/garage.toml" key create paa-test >"$RUN/garage-key.txt" 2>&1
        fi
        "$RUN/garage" -c "$RUN/garage.toml" key allow --create-bucket paa-test >/dev/null 2>&1 || true
        GK=$(grep -oE 'GK[0-9a-f]{24}' "$RUN/garage-key.txt" | head -1 || true)
        GS=$(grep -oE 'Secret key: [0-9a-f]{64}' "$RUN/garage-key.txt" | cut -d' ' -f3 || true)
        echo "$GK $GS" > "$RUN/garage-creds"
    fi
    echo "s3: seaweedfs on :9000, garage on :9020"
}

case "${1:-up}" in
up)
    up_redis
    up_postgres
    up_s3
    sleep 8  # raft elections
    if [ -f "$RUN/garage-creds" ]; then
        read -r GK GS < "$RUN/garage-creds"
    else
        GK="GK-see-garage-key.txt"; GS="see-garage-key.txt"
    fi
    cat <<EOF

Run the suite with:

  PAA_TEST_POSTGRES_DSN='postgresql://postgres:@/postgres?host=$RUN/pg' \\
  PAA_TEST_S3_ENDPOINT=http://127.0.0.1:9000 \\
  PAA_TEST_S3_ACCESS_KEY=paa-test-key PAA_TEST_S3_SECRET_KEY=paa-test-secret \\
  PAA_TEST_S3_GARAGE_ENDPOINT=http://127.0.0.1:9020 \\
  PAA_TEST_S3_GARAGE_ACCESS_KEY=$GK \\
  PAA_TEST_S3_GARAGE_SECRET_KEY=$GS \\
  python -m pytest tests/ -o addopts=""
EOF
    ;;
status)
    redis-cli -h 127.0.0.1 -p 6379 ping 2>/dev/null || echo "redis: down"
    "$PGBIN/pg_isready" -h "$RUN/pg" 2>/dev/null || echo "postgres: down"
    curl -s -o /dev/null -w "seaweedfs(9000): %{http_code}\n" http://127.0.0.1:9000/ || echo "seaweedfs: down"
    curl -s -o /dev/null -w "garage(9020): %{http_code}\n" http://127.0.0.1:9020/ || echo "garage: down"
    ;;
down)
    pkill -f "weed server" 2>/dev/null || true
    pkill -f "garage.*garage.toml" 2>/dev/null || true
    [ -x "$PGBIN/pg_ctl" ] && "$PGBIN/pg_ctl" -D "$RUN/pg" stop -m fast >/dev/null 2>&1 || true
    redis-cli -h 127.0.0.1 -p 6379 shutdown nosave 2>/dev/null || true
    echo "down"
    ;;
*)
    echo "usage: $0 {up|status|down}" >&2; exit 2 ;;
esac
