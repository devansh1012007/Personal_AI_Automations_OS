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
# The compose file is the source of truth
# ---------------------------------------
# The set of images this script fetches is *read out of* `docker-compose.yml`.
# It is not written down here. It used to be a hand-copied table, and the copy
# drifted: two of its four entries were older versions than the ones compose
# actually ran, so the script fetched an image the deployment never used and
# left the one it did use for `docker compose up` to fetch — and fail on.
# One list, one file, and a test that holds the two together.
#
# A service with a `build:` section is BUILT here: its `image:` is the tag the
# build produces, and the script reports it as built rather than pulling it.
# A service with only an `image:` is PULLED.
#
# A pulled image must be digest-pinned, and a tag-only reference is an ERROR
# rather than a warning. A tag is a promise about a moving target, and this
# repository has already been bitten by it: the MinIO community image is
# archived upstream and its registry path now answers 401, so a deployment
# that named `minio/minio:latest` could not be reproduced at all. A digest
# names exactly one image, forever. The pin is read from compose — this script
# carries no digest of its own.
#
# Three references are refused, and each gets its OWN message, because one
# message cannot describe three different mistakes: an image that is not
# digest-pinned, an image whose reference is a VARIABLE (the script does not
# interpolate compose variables — it cannot know what your shell and your
# `.env` hold), and an `image:` key with no value at all. The refusals happen
# before a byte is fetched.
#
# An empty plan is refused too. A compose file the reader finds no images in
# is a broken source of truth, and a script that reports "every image is
# present" about it has checked nothing.
#
# The digest check is inside the retry unit
# -----------------------------------------
# One attempt is ONE pull PLUS its verification, the rule the Dockerfiles
# already follow for a download and its SHA-256 check: a transfer that
# produced the wrong bytes is a failed *attempt*, not a failed fetch.
#
# The comparison is on the DIGEST, never on the repository path. The compose
# reference is `<repo>:<tag>@sha256:X` while the daemon records
# `<repo>@sha256:X` — no tag — and a mirror changes the path again. A digest
# names one image; a path names a shelf it happens to sit on.
#
# Requirements
# ------------
# GNU coreutils. The script bounds every network operation with `timeout`,
# which is a GNU coreutils program: it is not in a stock macOS or BSD system,
# and a busybox `timeout` is a different program with different flags. The
# script checks for it and says so plainly, rather than failing with
# "command not found" in the middle of a pull.
#
# Usage
# -----
#   deploy/fetch-images.sh                 # fetch what is missing
#   deploy/fetch-images.sh --list          # print what would be fetched
#   deploy/fetch-images.sh --verify        # check the digests of what is present
#   deploy/fetch-images.sh --load <dir>    # import images from a directory of tarballs
#   PAA_IMAGE_MIRROR=registry.example.com deploy/fetch-images.sh
#
# The mirror variable is a *prefix*, not a registry: `docker pull` is given
# `<mirror>/<repository>` so that a mirror which only caches Docker Hub can
# serve the official images from `library/`, and the other repositories keep
# their own path. The mirror changes the path; it does not change the digest,
# so the verification is unaffected by it.
#
# `PAA_COMPOSE_FILE` names the compose file to read (default
# `docker-compose.yml`, resolved against the repository root). It exists so
# the parity test can point the script at a compose file it controls, and it
# is the only way to change which file is the source of truth.
#
# The script touches the LOCAL IMAGE STORE only: `docker image inspect`,
# `docker pull`, `docker tag` and `docker load`. It never stops, removes or
# prunes anything, and it never touches a running container. The `docker tag`
# is issued ONLY when a mirror changed the image's repository path: without a
# mirror the pulled reference IS the reference compose uses, and tagging it
# would add a MUTABLE alias — a tag carrying no digest — that a later
# unpinned pull could move to different bytes, in a deployment whose whole
# point is that the pin is not a promise.
#
# Exit status is non-zero if any image could not be fetched, so it can be
# used in CI as a gate. `--list` is a gate as well: it exits non-zero when the
# plan it prints names an image this deployment cannot pin, and it refuses an
# empty plan rather than reporting success on nothing.

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT"

MAX_ATTEMPTS="${PAA_FETCH_MAX_ATTEMPTS:-6}"
BASE_DELAY="${PAA_FETCH_BASE_DELAY:-5}"
PULL_TIMEOUT="${PAA_FETCH_TIMEOUT:-900}"
MIRROR="${PAA_IMAGE_MIRROR:-}"

COMPOSE_FILE="${PAA_COMPOSE_FILE:-docker-compose.yml}"
case "$COMPOSE_FILE" in
  /*) ;;
  *) COMPOSE_FILE="$REPO_ROOT/$COMPOSE_FILE" ;;
esac

usage() {
  sed -n '2,/^set -euo/p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//; $d'
}

log()  { printf '\033[1;34m==>\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33m warn\033[0m %s\n' "$*" >&2; }
err()  { printf '\033[1;31m fail\033[0m %s\n' "$*" >&2; }

# --- the compose file, read as text -----------------------------------------
# PyYAML is not a dependency of this project, and adding one for a shell
# script would be a worse trade than reading the two blocks directly — the
# same reasoning the deployment tests use. The rules this reader follows are
# the shape of the file: a service is a key at two-space indent under
# `services:`, and its `image:` and `build:` are keys at four-space indent.
#
# `tests/deployment/test_fetch_images.py` locks this reader to the compose
# file with an independent Python parse, so a drift in either one fails the
# build instead of shipping.
#
# The output is one tab-separated row per service: "action<TAB>image<TAB>service",
# where the action is "pull" or "build".
#
# Two details of the reader are load-bearing, and both are about not reading
# a value that is not there:
#
# * a carriage return is stripped from every line, because a compose file
#   written on Windows carries one at the end of every line, and a digest
#   with a carriage return on the end of it matches nothing — the reference
#   could never be satisfied;
# * an `image:` key with no value is reported as "-", the same as a service
#   that has no `image:` key at all, because an empty field in a
#   tab-separated row is not a field: the shell's field splitting collapses
#   consecutive tabs, so an empty image would shift the service name into
#   the image column and the row would be read as a service with no image.
parse_compose() {
  [ -f "$COMPOSE_FILE" ] || { err "no such compose file: ${COMPOSE_FILE}"; return 1; }
  awk '
    { sub(/\r$/, "") }
    function flush(  action) {
      if (service == "") return
      if (!have_image && !have_build) return
      action = (have_build ? "build" : "pull")
      printf "%s\t%s\t%s\n", action, (have_image && image != "" ? image : "-"), service
    }
    /^[^[:space:]#]/ {
      # A top-level key. Only "services:" opens the map the rest of the reader
      # cares about; "networks:" and "volumes:" sit at the same indent and are
      # not services.
      in_services = ($0 ~ /^services:[[:space:]]*$/)
      next
    }
    in_services && $0 ~ /^  [^[:space:]#][^:]*:[[:space:]]*(#.*)?$/ {
      # A service name: a key at two-space indent with no value of its own.
      flush()
      service = $0
      sub(/^  /, "", service)
      sub(/:.*$/, "", service)
      image = ""
      have_image = 0
      have_build = 0
      next
    }
    in_services && service != "" && $0 ~ /^    image:/ {
      line = $0
      sub(/^    image:[[:space:]]*/, "", line)
      sub(/[[:space:]]+#.*$/, "", line)
      if (substr(line, 1, 1) == "\"" || substr(line, 1, 1) == "\047")
        line = substr(line, 2)
      if (length(line) > 0) {
        last = substr(line, length(line), 1)
        if (last == "\"" || last == "\047") line = substr(line, 1, length(line) - 1)
      }
      image = line
      have_image = 1
      next
    }
    in_services && service != "" && $0 ~ /^    build:/ {
      have_build = 1
      next
    }
    END { flush() }
  ' "$COMPOSE_FILE"
}

# --- the plan ---------------------------------------------------------------
# The rows the script acts on, and the rows it refuses.
#
# A pulled image with no digest is refused, not warned about: the deployment's
# own compose file already pins every one of them, and a tag-only reference
# in a stack that claims to be pinned is the exact failure the pin exists to
# prevent.
#
# The three refusals are kept apart, because they are three different
# mistakes and one message cannot describe them: a missing pin, a reference
# that is a variable, and an `image:` key with no value.
PLAN=""
unpinned=""
interpolated=""
unnamed=""

build_plan() {
  PLAN="$(parse_compose)"
  unpinned=""
  interpolated=""
  unnamed=""
  while IFS=$'\t' read -r action image service; do
    [ -n "$service" ] || continue
    [ "$action" = "pull" ] || continue
    case "$image" in
      "-")
        # The reader reports an empty image: value as "-", the same as a
        # service with no image: key — and a pull row with "-" is a service
        # that declares an image and gives it no value.
        unnamed="${unnamed}${service}"$'\n'
        ;;
      *'${'*)
        # A compose variable. The script does not interpolate: it cannot know
        # what the operator's shell and .env hold, and guessing would mean
        # pulling something the compose file did not name.
        interpolated="${interpolated}${service}"$'\t'"${image}"$'\n'
        ;;
      *@sha256:*) ;;
      *)
        unpinned="${unpinned}${service}"$'\t'"${image}"$'\n'
        ;;
    esac
  done <<< "$PLAN"
}

refuse_unpinned() {
  local found=0 service image
  while IFS=$'\t' read -r service image; do
    [ -n "$service" ] || continue
    found=1
    err "${service}: ${image} is not digest-pinned — a tag is a promise about a"
    err "moving target. Pin it as ${image}@sha256:<digest> and re-run."
  done <<< "$unpinned"
  [ "$found" -eq 0 ]
}

refuse_interpolated() {
  local found=0 service image
  while IFS=$'\t' read -r service image; do
    [ -n "$service" ] || continue
    found=1
    err "${service}: ${image} is a VARIABLE, and this script does not"
    err "interpolate compose variables — it cannot know what your shell and"
    err "your .env hold, and a guess would pull something the compose file"
    err "did not name. Pin the image literally, or point the script at a"
    err "compose file whose image: values are already resolved."
  done <<< "$interpolated"
  [ "$found" -eq 0 ]
}

refuse_unnamed() {
  local found=0 service
  while IFS= read -r service; do
    [ -n "$service" ] || continue
    found=1
    err "${service}: declares an image: with no value."
    err "Compose would refuse the file, and so does this script."
  done <<< "$unnamed"
  [ "$found" -eq 0 ]
}

# Every refusal, and one answer: non-zero if any of them fired.
refuse_bad_images() {
  local ok=0
  refuse_unpinned || ok=1
  refuse_interpolated || ok=1
  refuse_unnamed || ok=1
  [ "$ok" -eq 0 ]
}

# --- the daemon -------------------------------------------------------------
# Every check below is against the LOCAL IMAGE STORE, and every one of them
# takes the FULL reference compose uses. A short local name is not the same
# thing: `docker image inspect <repo>` resolves to `<repo>:latest`, which is
# not the image compose runs, so a presence check on the short name reports
# "absent" for an image that is present — which is why the script used to
# fail on every image even when every pull had succeeded.
image_present() {
  docker image inspect "$1" >/dev/null 2>&1
}

# The digest the local image actually carries, or the empty string.
#
# `RepoDigests` is the canonical source: it is the list of manifest digests
# the registry served for this image. `.Id` is not — it is the image's
# content identifier, and what it names depends on the daemon's image store
# (the configuration digest on the classic graph driver, the target
# descriptor on the containerd store), so a check written against it would
# pass or fail according to the host rather than according to the image.
image_digests() {
  docker image inspect "$1" --format '{{join .RepoDigests " "}}' 2>/dev/null || true
}

# Does the image this reference names carry the expected manifest digest?
#
# The comparison is on the DIGEST and not on the path: the daemon records
# `<repo>@sha256:X` where compose wrote `<repo>:<tag>@sha256:X`, and a mirror
# records its own path. A digest names one image; a path names a shelf it
# happens to sit on.
digest_matches() {
  local ref="$1" want="$2" digests entry
  digests="$(image_digests "$ref")"
  [ -n "$digests" ] || return 1
  for entry in $digests; do
    case "$entry" in
      *"@${want}") return 0 ;;
    esac
  done
  return 1
}

# Split a reference into its repository and its tag.
#
# The last path segment decides: a colon in a registry's *host* is a port,
# not a tag separator, so the segment after the final slash is the only
# place a tag can be.
split_ref() {
  local name="$1" last="${1##*/}"
  case "$last" in
    *:*) REPO="${name%:*}"; TAG="${name##*:}" ;;
    *)   REPO="$name"; TAG="" ;;
  esac
}

# The reference the mirror serves, with the digest kept. The mirror changes
# the repository path and nothing else — the digest travels with the pull,
# or the pull is not pinned.
mirror_target() {
  local repo="$1" tag="$2" digest="$3" target
  case "$repo" in
    */*) target="${MIRROR}/${repo}" ;;
    *)   target="${MIRROR}/library/${repo}" ;;
  esac
  [ -n "$tag" ] && target="${target}:${tag}"
  printf '%s@%s\n' "$target" "$digest"
}

# Pull one reference with a growing backoff, and verify the digest inside the
# same attempt.
#
# The retry is not decoration. A layer that fails mid-transfer leaves the
# registry's blob store with a partial object; the next attempt asks for the
# same layer and the registry serves it again, and the failure cost is one
# layer rather than the whole image. A DNS failure or a refused connection
# resolves itself.
#
# And the verification is *inside* the unit, the way the Dockerfiles treat a
# download and its SHA-256 check: a transfer that produced something other
# than the pinned image is a failed ATTEMPT, not a failed fetch, so the next
# attempt gets to try again.
pull_with_verification() {
  local ref="$1" want="$2" attempt=1 delay
  while [ "$attempt" -le "$MAX_ATTEMPTS" ]; do
    log "pulling ${ref} (attempt ${attempt}/${MAX_ATTEMPTS})"
    if timeout "$PULL_TIMEOUT" docker pull "$ref" >/dev/null; then
      if digest_matches "$ref" "$want"; then
        return 0
      fi
      warn "${ref} was pulled but does not carry the digest ${want}"
    else
      warn "${ref} could not be pulled"
    fi
    if [ "$attempt" -eq "$MAX_ATTEMPTS" ]; then
      break
    fi
    delay=$(( BASE_DELAY * attempt ))
    warn "retrying in ${delay}s"
    sleep "$delay"
    attempt=$(( attempt + 1 ))
  done
  err "could not fetch ${ref} with digest ${want} after ${MAX_ATTEMPTS} attempts"
  return 1
}

# Load a tarball produced by `docker save`, with the same retry policy.
load_with_retries() {
  local tarball="$1" attempt=1 delay
  [ -f "$tarball" ] || { err "no such tarball: ${tarball}"; return 1; }
  while [ "$attempt" -le "$MAX_ATTEMPTS" ]; do
    log "loading ${tarball} (attempt ${attempt}/${MAX_ATTEMPTS})"
    if timeout "$PULL_TIMEOUT" docker load -i "$tarball" >/dev/null; then
      return 0
    fi
    [ "$attempt" -eq "$MAX_ATTEMPTS" ] && break
    delay=$(( BASE_DELAY * attempt ))
    warn "${tarball} failed to load; retrying in ${delay}s"
    sleep "$delay"
    attempt=$(( attempt + 1 ))
  done
  err "could not load ${tarball}"
  return 1
}

# --- the modes --------------------------------------------------------------
# An image this repository builds. It is not pulled, and the script does not
# build it either — `docker compose build` is how a compose service's build
# section is meant to be built, and duplicating that here would be a second
# place to keep the context and the Dockerfile in step.
report_built() {
  local image="$1" service="$2"
  if [ -z "$image" ] || [ "$image" = "-" ]; then
    warn "${service} has a build: section and no image: — compose names it for you"
    return 0
  fi
  if image_present "$image"; then
    log "${image} is present (built here)"
  else
    warn "${image} is not present; build it with: docker compose build ${service}"
  fi
}

list_images() {
  local action image service
  while IFS=$'\t' read -r action image service; do
    [ -n "$service" ] || continue
    printf '%-6s%s  (%s)\n' "$action" "$image" "$service"
  done <<< "$PLAN"
}

verify_images() {
  local failed=0 action image service digest
  while IFS=$'\t' read -r action image service; do
    [ -n "$service" ] || continue
    if [ "$action" = "build" ]; then
      report_built "$image" "$service"
      continue
    fi
    digest="${image##*@}"
    if image_present "$image" && digest_matches "$image" "$digest"; then
      log "${image} is present and carries ${digest}"
    else
      err "${image} is not present with the digest ${digest}"
      failed=1
    fi
  done <<< "$PLAN"
  return "$failed"
}

load_images() {
  local dir="$1" tarball
  # `local -a`, so the array does not leak into the global scope: a shell
  # function's globals outlive the call, and a stale `tarballs` from an
  # earlier `--load` is exactly the kind of thing that makes a later run
  # believe it has tarballs it does not.
  local -a tarballs=()
  log "loading images from ${dir}"
  if [ ! -d "$dir" ]; then
    err "no such directory: ${dir}"
    return 1
  fi
  shopt -s nullglob
  tarballs=("${dir}"/*.tar)
  shopt -u nullglob
  if [ ${#tarballs[@]} -eq 0 ]; then
    err "no .tar files in ${dir}"
    return 1
  fi
  for tarball in "${tarballs[@]}"; do
    load_with_retries "$tarball" || return 1
  done
  log "loaded ${#tarballs[@]} image(s) from ${dir}"
  # A loaded image is NOT digest-verified, and the script says so rather than
  # pretending otherwise. A `docker save` / `docker load` round trip restores
  # an image by its configuration, and the manifest digest the registry
  # served is not what the load path can be asked about. The pin is enforced
  # on the pull path; a tarball is trusted the way an operator's own disk is
  # trusted, and the operator is told that is what happened.
  warn "a loaded image is not digest-verified — the save/load round trip"
  warn "restores the image by its configuration, and the manifest digest the"
  warn "registry served is not what the load path can check. The pin is"
  warn "enforced on the pull path."
  return 0
}

fetch_images() {
  local failed=0 action image service digest name target
  while IFS=$'\t' read -r action image service; do
    [ -n "$service" ] || continue
    if [ "$action" = "build" ]; then
      report_built "$image" "$service"
      continue
    fi
    digest="${image##*@}"
    name="${image%@*}"
    if image_present "$image" && digest_matches "$image" "$digest"; then
      log "${image} is already present — skipping"
      continue
    fi
    split_ref "$name"
    target="$image"
    if [ -n "$MIRROR" ]; then
      target="$(mirror_target "$REPO" "$TAG" "$digest")"
      if image_present "$target" && digest_matches "$target" "$digest"; then
        log "${image} is already present (via ${MIRROR}) — skipping"
        continue
      fi
    fi
    if pull_with_verification "$target" "$digest"; then
      # The tag is issued ONLY when the mirror changed the image's repository
      # path. Without a mirror the pulled reference IS the reference compose
      # uses, and tagging it would add a MUTABLE alias — a tag carrying no
      # digest — to the local store: a later unpinned `docker pull` could
      # move that alias to different bytes, in a deployment whose whole point
      # is that the pin is not a promise.
      #
      # The comparison is on the REPOSITORY, with the digest stripped from
      # both sides: `target` and the compose reference differ by the digest
      # in the ordinary case, so comparing them whole would fire the tag on
      # every pulled image.
      if [ -n "$MIRROR" ] && [ "${target%@*}" != "$name" ]; then
        # The pulled image carries the mirror's repository path; give it the
        # name the compose file expects so compose does not try to pull it
        # again.
        if docker tag "$target" "$name" >/dev/null 2>&1; then
          log "tagged ${target} as ${name}"
        else
          warn "pulled ${target} but could not tag it as ${name}"
        fi
      fi
    else
      failed=1
    fi
  done <<< "$PLAN"
  return "$failed"
}

# --- the gates --------------------------------------------------------------
# GNU coreutils' `timeout`, checked before anything is fetched so the failure
# is a sentence about the requirement rather than "command not found" in the
# middle of a pull.
require_gnu_timeout() {
  if ! command -v timeout >/dev/null 2>&1; then
    err "GNU coreutils 'timeout' is required and was not found on PATH."
    err "The script bounds every pull with it. Install GNU coreutils"
    err "(macOS: 'brew install coreutils' and put 'gtimeout' on PATH)."
    exit 1
  fi
  if ! timeout --version 2>/dev/null | grep -q "GNU coreutils"; then
    err "'timeout' on PATH is not GNU coreutils'."
    err "The script requires GNU coreutils' timeout (macOS: 'brew install"
    err "coreutils')."
    exit 1
  fi
}

# --- the entry point --------------------------------------------------------
mode="fetch"
load_dir=""
while [ $# -gt 0 ]; do
  case "$1" in
    --list)   mode="list" ;;
    --verify) mode="verify" ;;
    --load)
      mode="load"
      [ $# -ge 2 ] || { err "--load needs a directory"; exit 2; }
      load_dir="$2"
      shift
      ;;
    -h|--help) usage; exit 0 ;;
    *) err "unknown argument: $1"; usage; exit 2 ;;
  esac
  shift
done

build_plan

# An empty plan is a broken source of truth, not an empty deployment. The
# reader found no service with an `image:` or a `build:` section, so the
# script has nothing to fetch and nothing to check — and "every image the
# compose file names is present" would then be a report about nothing. It
# is refused, in every mode, before the mode's own work.
if [ -z "$PLAN" ]; then
  err "no images found in ${COMPOSE_FILE} — the compose file names no"
  err "service with an image: or a build: section. There is nothing to"
  err "fetch, and reporting success would be reporting that nothing was"
  err "checked."
  exit 1
fi

# `--list` is a report about a FILE, so it needs neither the docker CLI nor a
# running daemon. The gate for those comes after this branch.
#
# It is a *gate* as well as a report: the plan it prints is checked, and a
# plan this deployment cannot pin is a failure with a non-zero exit status,
# not a warning in a log line. The list still prints — the operator needs to
# see WHAT is wrong — and the verdict follows it.
if [ "$mode" = "list" ]; then
  list_images
  if ! refuse_bad_images; then
    err "--list exits non-zero: the plan above names images this deployment"
    err "cannot pin. It is a report and a gate, and the gate says no."
    exit 1
  fi
  exit 0
fi

require_gnu_timeout

command -v docker >/dev/null 2>&1 || { err "docker is not on PATH"; exit 1; }
docker info >/dev/null 2>&1 || { err "the docker daemon is not reachable"; exit 1; }

case "$mode" in
  load)
    load_images "$load_dir" || exit 1
    # `--load` STOPS. It imports what it was given and it does not then pull
    # the whole deployment on top of it.
    exit 0
    ;;
  verify)
    refuse_bad_images || exit 1
    verify_images || exit 1
    exit 0
    ;;
esac

refuse_bad_images || exit 1

if fetch_images; then
  log "every image the compose file names is present"
else
  err "one or more images could not be fetched — re-run this script to continue"
  exit 1
fi
