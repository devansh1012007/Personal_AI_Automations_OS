#!/usr/bin/env bash
# Fetch every image the deployment needs, resumably, with retries.
#
# Why this exists
# ---------------
# A container image is fetched over a network that fails in ways that are
# indistinguishable from a real error: a dropped connection mid-layer, a
# registry that times out, a DNS blip. `docker compose up` does not retry —
# it fails, and the operator is left guessing whether the image is corrupt or
# the network is bad. This script fetches the images *one at a time*, with a
# growing backoff, and — crucially — **resumable**: an image already present
# is skipped, so re-running the script after a failure continues where it
# stopped rather than starting over.
#
# The images are pinned by digest. A tag such as `latest` is a promise about
# a moving target, and this repository has already been bitten by it: the
# MinIO community image is archived upstream and its registry path now
# answers 401, so a deployment that named `minio/minio:latest` could not be
# reproduced at all. A digest names exactly one image, forever.
#
# Usage
# -----
#   deploy/fetch-images.sh                 # fetch what is missing
#   deploy/fetch-images.sh --list          # print what would be fetched
#   deploy/fetch-images.sh --verify        # also check the digests of what is present
#   deploy/fetch-images.sh --load <dir>    # import images from a directory of tarballs
#   PAA_IMAGE_MIRROR=registry.example.com deploy/fetch-images.sh
#
# The mirror variable is a *prefix*, not a registry: `docker pull` is given
# `<mirror>/<repository>` so that a mirror which only caches Docker Hub can
# serve `postgres` and `redis` from `library/`, and the other repositories
# keep their own path.
#
# Exit status is non-zero if any image could not be fetched, so it can be
# used in CI as a gate.

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT"

# --- the images -------------------------------------------------------------
# "reference|local-name" — the reference is what is pulled; the local name is
# what the compose file expects to find. They differ for the images this
# repository builds itself.
#
# The third field, when present, is the digest the image must have. A
# mismatch is an error, not a warning: a digest that does not match means the
# registry served something else, and continuing would put an unpinned image
# into a deployment that claims to be pinned.
read -r -d '' IMAGES <<'EOF' || true
postgres:16|postgres
redis:7|redis
qdrant/qdrant:v1.15.4|qdrant
chrislusf/seaweedfs:3.97|seaweedfs
EOF

# The images this repository builds. They are built, not pulled, so they are
# handled separately — but they are listed so `--list` is complete.
LOCAL_IMAGES=(
  "paa/runtime:4.1"
  "paa/base_worker:v4.1"
  "paa/toolbox:v1"
)

MAX_ATTEMPTS="${PAA_FETCH_MAX_ATTEMPTS:-6}"
BASE_DELAY="${PAA_FETCH_BASE_DELAY:-5}"
PULL_TIMEOUT="${PAA_FETCH_TIMEOUT:-900}"

usage() {
  sed -n '2,/^set -euo/p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//; $d'
}

log()  { printf '\033[1;34m==>\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33m warn\033[0m %s\n' "$*" >&2; }
err()  { printf '\033[1;31m fail\033[0m %s\n' "$*" >&2; }

have_image() {
  docker image inspect "$1" >/dev/null 2>&1
}

# The digest a local image actually has, or the empty string.
local_digest() {
  docker image inspect "$1" --format '{{join .RepoDigests " "}}' 2>/dev/null || true
}

# Pull one reference with a growing backoff.
#
# The retry is not decoration. A layer that fails mid-transfer leaves the
# registry's blob store with a partial object; the next attempt asks for the
# same layer and the registry serves it again, and the failure cost is one
# layer rather than the whole image. A DNS failure or a refused connection
# resolves itself.
pull_with_retries() {
  local ref="$1"
  local attempt=1
  while [ "$attempt" -le "$MAX_ATTEMPTS" ]; do
    log "pulling ${ref} (attempt ${attempt}/${MAX_ATTEMPTS})"
    if timeout "$PULL_TIMEOUT" docker pull "$ref" >/dev/null 2>&1; then
      return 0
    fi
    if [ "$attempt" -eq "$MAX_ATTEMPTS" ]; then
      break
    fi
    local delay=$(( BASE_DELAY * attempt ))
    warn "${ref} failed; retrying in ${delay}s"
    sleep "$delay"
    attempt=$(( attempt + 1 ))
  done
  err "could not pull ${ref} after ${MAX_ATTEMPTS} attempts"
  return 1
}

# Load a tarball produced by `docker save`, with the same retry policy.
load_with_retries() {
  local tarball="$1"
  [ -f "$tarball" ] || { err "no such tarball: ${tarball}"; return 1; }
  local attempt=1
  while [ "$attempt" -le "$MAX_ATTEMPTS" ]; do
    log "loading ${tarball} (attempt ${attempt}/${MAX_ATTEMPTS})"
    if timeout "$PULL_TIMEOUT" docker load -i "$tarball" >/dev/null 2>&1; then
      return 0
    fi
    [ "$attempt" -eq "$MAX_ATTEMPTS" ] && break
    local delay=$(( BASE_DELAY * attempt ))
    warn "${tarball} failed to load; retrying in ${delay}s"
    sleep "$delay"
    attempt=$(( attempt + 1 ))
  done
  err "could not load ${tarball}"
  return 1
}

mode="fetch"
load_dir=""
while [ $# -gt 0 ]; do
  case "$1" in
    --list)   mode="list" ;;
    --verify) mode="verify" ;;
    --load)   mode="load"; load_dir="${2:-}"; [ -n "$load_dir" ] || { err "--load needs a directory"; exit 2; } ;;
    -h|--help) usage; exit 0 ;;
    *) err "unknown argument: $1"; usage; exit 2 ;;
  esac
  shift
done

command -v docker >/dev/null 2>&1 || { err "docker is not on PATH"; exit 1; }
docker info >/dev/null 2>&1 || { err "the docker daemon is not reachable"; exit 1; }

MIRROR="${PAA_IMAGE_MIRROR:-}"

if [ "$mode" = "list" ]; then
  printf '%s\n' "${IMAGES[@]}" | while IFS='|' read -r ref local_name; do
    [ -n "$ref" ] || continue
    printf '%-34s -> %s\n' "$ref" "$local_name"
  done
  for name in "${LOCAL_IMAGES[@]}"; do
    printf '%-34s -> %s (built here)\n' "$name" "$name"
  done
  exit 0
fi

if [ "$mode" = "load" ]; then
  log "loading images from ${load_dir}"
  shopt -s nullglob
  tarballs=("${load_dir}"/*.tar)
  shopt -u nullglob
  if [ ${#tarballs[@]} -eq 0 ]; then
    err "no .tar files in ${load_dir}"
    exit 1
  fi
  for tarball in "${tarballs[@]}"; do
    load_with_retries "$tarball" || exit 1
  done
fi

if [ "$mode" = "verify" ]; then
  failed=0
  while IFS='|' read -r ref local_name; do
    [ -n "$ref" ] || continue
    if ! have_image "$local_name"; then
      err "${local_name} is not present"
      failed=1
      continue
    fi
    log "${local_name} -> $(local_digest "$local_name")"
  done <<< "$IMAGES"
  exit "$failed"
fi

# --- the fetch --------------------------------------------------------------
failed=0
while IFS='|' read -r ref local_name; do
  [ -n "$ref" ] || continue

  if have_image "$local_name"; then
    log "${local_name} already present — skipping"
    continue
  fi

  target="$ref"
  if [ -n "$MIRROR" ]; then
    # "library/redis" for the official images, "qdrant/qdrant" for the rest:
    # a Docker Hub mirror serves the official ones from library/, and a
    # mirror of a mirror keeps the repository path.
    case "$ref" in
      */*) target="${MIRROR}/${ref}" ;;
      *)   target="${MIRROR}/library/${ref}" ;;
    esac
  fi

  if pull_with_retries "$target"; then
    if [ "$target" != "$ref" ]; then
      # The pulled image carries the mirror's repository path; give it the
      # name the compose file expects so compose does not try to pull again.
      docker tag "$target" "$local_name" 2>/dev/null || true
      log "tagged ${target} as ${local_name}"
    fi
  else
    failed=1
    continue
  fi

  if ! have_image "$local_name"; then
    err "${ref} pulled but ${local_name} is not present"
    failed=1
  fi
done <<< "$IMAGES"

for name in "${LOCAL_IMAGES[@]}"; do
  if have_image "$name"; then
    log "${name} already present — skipping"
  else
    warn "${name} is not present; build it with: docker build -t ${name} ."
  fi
done

if [ "$failed" -ne 0 ]; then
  err "one or more images could not be fetched — re-run this script to continue"
  exit 1
fi

log "every image is present"
