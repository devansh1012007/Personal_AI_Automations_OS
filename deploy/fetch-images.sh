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
# The set of images this script fetches is *read out of* the compose file(s).
# It is not written down here. It used to be a hand-copied table, and the copy
# drifted: two of its four entries were older versions than the ones compose
# actually ran, so the script fetched an image the deployment never used and
# left the one it did use for `docker compose up` to fetch — and fail on.
# One list, one file, and a test that holds the two together.
#
# SEVERAL FILES, MERGED
# ---------------------
# The gVisor stack the documentation tells operators to run is a MERGED
# stack:
#
#     docker compose -f docker-compose.yml -f docker-compose.gvisor.yml up -d
#
# A script that read only the first file would answer a question about half
# the deployment, and the two halves happened to agree only because the
# override's `paa` service declares no image of its own. So the script takes
# SEVERAL compose files — `--compose <path>`, repeatable, merged in the order
# they are given — and reads the merged stack, which is what `docker compose
# up` will actually run. The merge is a documented SUBSET of Compose's own
# merge, and the shapes it does not implement are REFUSED rather than
# guessed: a wrong plan fetches an image the deployment does not run, and
# misses one it does. See the reader's own comments, and ADR-0030.
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
# Five more shapes are refused, each with its own message, and they are the
# shapes the MERGE does not implement: Compose's `extends:`, a YAML anchor
# or an alias, a multi-line `image:`, a custom YAML tag, and the top-level
# `include:`. A wrong plan fetches an image the deployment does not run and
# misses one it does, so the reader says "I do not understand this file"
# rather than guessing.
#
# A service behind a `profiles:` key is EXCLUDED rather than refused: a
# profile that is not enabled is not a service `docker compose up` runs, and
# the script says so on stderr instead of dropping it silently.
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
#   deploy/fetch-images.sh --compose docker-compose.yml \
#       --compose docker-compose.gvisor.yml    # the MERGED gVisor stack
#   PAA_IMAGE_MIRROR=registry.example.com deploy/fetch-images.sh
#
# `--compose` is repeatable and the files are merged in the order they are
# given, later wins — the same order and the same precedence `docker compose
# -f a -f b` uses, for the subset of the merge this script implements. The
# gVisor stack is the case that needs it: the override changes the `paa`
# service, and a script that read only the base file would be answering
# about a stack nobody runs.
#
# The mirror variable is a *prefix*, not a registry: `docker pull` is given
# `<mirror>/<repository>` so that a mirror which only caches Docker Hub can
# serve the official images from `library/`, and the other repositories keep
# their own path. The mirror changes the path; it does not change the digest,
# so the verification is unaffected by it.
#
# `PAA_COMPOSE_FILE` names ONE compose file to read (default
# `docker-compose.yml`, resolved against the repository root). It is the
# single-file spelling of `--compose`, kept because it is the way the
# deployment tests point the script at a compose file they control. A
# `--compose` flag on the command line takes precedence over it.
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

# The compose files the plan is read from, in the order they are merged. It
# is filled in after the arguments are parsed, because `--compose` and
# `PAA_COMPOSE_FILE` both feed it and the flag wins.
COMPOSE_FILES=()

usage() {
  sed -n '2,/^set -euo/p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//; $d'
}

log()  { printf '\033[1;34m==>\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33m warn\033[0m %s\n' "$*" >&2; }
err()  { printf '\033[1;31m fail\033[0m %s\n' "$*" >&2; }

# --- the compose files, read as text ---------------------------------------
# PyYAML is not a dependency of this project, and adding one for a shell
# script would be a worse trade than reading the two blocks directly — the
# same reasoning the deployment tests use. The rules this reader follows are
# the shape of the file: a service is a key at two-space indent under
# `services:`, and its `image:` and `build:` are keys at four-space indent.
#
# `tests/deployment/test_fetch_images.py` locks this reader to the compose
# files with an independent Python parse, so a drift in either one fails the
# build instead of shipping.
#
# THE MERGE
# ---------
# The files are read in the order they are given and merged, because the
# stack the deployment runs is the MERGED stack:
#
#     docker compose -f docker-compose.yml -f docker-compose.gvisor.yml up -d
#
# The merge implemented here is a SUBSET of Compose's own, and it is the
# subset this repository's files use:
#
#   * a service is a MAP, and the maps are merged key by key: a later file's
#     `image:` replaces an earlier file's, and a later file that declares no
#     `build:` leaves the earlier file's `build:` in place. That last rule is
#     what makes the gVisor override work at all — its `paa` service declares
#     neither an image nor a build, and the merged `paa` is the base file's
#     built image with the override's runtime on top;
#   * a service only one of the files declares is taken from that file;
#   * the plan lists the services in the order they first appear.
#
# What is NOT in the subset is REFUSED, not guessed: Compose's `extends:`, a
# YAML anchor or alias, a multi-line `image:`, a custom YAML tag, and the
# top-level `include:`. A script that silently mis-merges is worse than one
# that says "I do not understand this file" — a wrong plan fetches an image
# the deployment does not run, and misses one it does.
#
# A service gated behind a `profiles:` key is EXCLUDED, because a profile
# that is not enabled is not a service `docker compose up` runs. The script
# says so, on stderr, rather than dropping it silently.
#
# The output is one tab-separated row per service: "action<TAB>image<TAB>service",
# where the action is "pull" or "build". Two other rows exist: a service
# excluded by a profile comes out as "skip<TAB>profile<TAB>service", and a
# file the reader does not understand comes out as "refuse<TAB>message<TAB>-".
# The caller turns both into a warning and an error.
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
  local file
  for file in "${COMPOSE_FILES[@]}"; do
    if [ ! -f "$file" ]; then
      err "no such compose file: ${file}"
      return 1
    fi
  done
  awk -v root="$REPO_ROOT" '
    {
      sub(/\r$/, "")
    }
    FILENAME != current_file {
      # A new file. The service still open belongs to the previous one, and
      # its values have to land in the merged map before the new file starts.
      if (current_file != "") flush_service()
      reset_service()
      in_services = 0
      current_file = FILENAME
    }
    /^[^[:space:]#]/ {
      # A top-level key. Only "services:" opens the map the rest of the reader
      # cares about; "networks:" and "volumes:" sit at the same indent and are
      # not services.
      if ($0 ~ /^include:[[:space:]]*($|#)/) {
        refuse(display(FILENAME) ": the top-level include: is not supported.\n" \
               "This script does not follow Compose'"'"'s include:, and the services\n" \
               "it names would be missing from the plan.")
      }
      in_services = ($0 ~ /^services:[[:space:]]*$/)
      next
    }
    in_services && $0 ~ /^  [^[:space:]#][^:]*:/ {
      # A key at two-space indent: a service name, or a service with an
      # inline value — which Compose would refuse, because a service is a
      # mapping and not a string.
      key = $0
      sub(/^  /, "", key)
      inline = key
      sub(/^[^:]*:/, "", inline)
      sub(/[[:space:]]+#.*$/, "", inline)
      sub(/^[[:space:]]+/, "", inline)
      sub(/[[:space:]]+$/, "", inline)
      if (inline == "") {
        flush_service()
        service = key
        sub(/:.*$/, "", service)
        image = ""
        have_image = 0
        have_build = 0
        have_profile = 0
        next
      }
      # An anchor or an alias on the service key is refused: the reader would
      # then attribute the keys below it to the wrong service, and a wrong
      # plan fetches an image the deployment does not run.
      if (inline ~ /^[&*]/) {
        name = key
        sub(/:.*$/, "", name)
        refuse(display(FILENAME) ": the service " name " uses a YAML anchor or an alias.\n" \
               "This script does not resolve anchors, and an unresolved image\n" \
               "reference would be pulled as the literal text of the alias.")
      }
      next
    }
    in_services && service != "" && $0 ~ /^    image:/ {
      line = $0
      sub(/^    image:[[:space:]]*/, "", line)
      sub(/[[:space:]]+#.*$/, "", line)
      sub(/[[:space:]]+$/, "", line)
      if (line ~ /^[|>][-+0-9]*$/) {
        refuse(display(FILENAME) ": the service " service " declares a multi-line image:.\n" \
               "This script reads one line per image, and a folded or a literal\n" \
               "block would be read as the text of its indicator.")
      }
      if (line ~ /^!/) {
        refuse(display(FILENAME) ": the service " service " puts a custom YAML tag on its image:.\n" \
               "This script does not interpret YAML tags, so it cannot tell what\n" \
               "the tagged value is.")
      }
      if (line ~ /^\*/) {
        refuse(display(FILENAME) ": the service " service " declares an image: that is an alias.\n" \
               "This script does not resolve aliases, so it cannot tell which\n" \
               "image the alias names.")
      }
      if (line ~ /&/) {
        refuse(display(FILENAME) ": the service " service " declares an anchored image:.\n" \
               "This script does not resolve anchors, so it cannot tell which\n" \
               "image the anchor names.")
      }
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
    in_services && service != "" && $0 ~ /^    extends:/ {
      refuse(display(FILENAME) ": the service " service " uses extends:.\n" \
             "This script does not resolve Compose'"'"'s inheritance, and an image\n" \
             "inherited from another service would be fetched by guesswork.")
      next
    }
    in_services && service != "" && $0 ~ /^    build:/ {
      have_build = 1
      next
    }
    in_services && service != "" && $0 ~ /^    profiles:/ {
      have_profile = 1
      next
    }
    END {
      if (aborted) exit 1
      flush_service()
      for (i = 1; i <= g_count; i++) {
        name = g_order[i]
        if (!g_has_image[name] && !g_has_build[name]) continue
        if (g_gated[name]) {
          printf "skip\tprofile\t%s\n", name
          continue
        }
        action = (g_has_build[name] ? "build" : "pull")
        image = (g_has_image[name] && g_image[name] != "" ? g_image[name] : "-")
        printf "%s\t%s\t%s\n", action, image, name
      }
    }
    function display(name) {
      # The file as the operator wrote it, not as this script resolved it.
      if (root != "" && substr(name, 1, length(root) + 1) == root "/")
        return substr(name, length(root) + 2)
      return name
    }
    function refuse(message) {
      # A shape this reader does not implement. The message travels back as
      # rows, because the shell owns the wording of an error and the awk
      # program must not write to the terminal behind its back.
      aborted = 1
      count = split(message, lines, "\n")
      for (i = 1; i <= count; i++) printf "refuse\t%s\t-\n", lines[i]
      exit 1
    }
    function reset_service() {
      service = ""
      image = ""
      have_image = 0
      have_build = 0
      have_profile = 0
    }
    function flush_service() {
      # The service that is open is finished: its values land in the merged
      # map, and only the keys this file actually declared — a later file
      # that says nothing about `build:` leaves the earlier file'"'"'s alone.
      if (service == "") return
      if (!(service in g_seen)) {
        g_seen[service] = 1
        g_order[++g_count] = service
        g_image[service] = ""
        g_has_image[service] = 0
        g_has_build[service] = 0
        g_gated[service] = 0
      }
      if (have_image) {
        g_image[service] = image
        g_has_image[service] = 1
      }
      if (have_build) g_has_build[service] = 1
      if (have_profile) g_gated[service] = 1
      reset_service()
    }
  ' "${COMPOSE_FILES[@]}"
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
  local raw action image service rows="" refused=0
  # The reader's rows come back in three kinds: the plan itself, a service it
  # left out because a profile gates it, and a file it does not understand.
  # The last one is an error and stops the script here, before a mode's own
  # work, so a plan that was never built is never acted on.
  raw=""
  if ! raw="$(parse_compose)"; then
    refused=1
  fi
  while IFS=$'\t' read -r action image service; do
    [ -n "$service" ] || continue
    case "$action" in
      refuse)
        refused=1
        err "${image}"
        continue
        ;;
      skip)
        # A service behind a compose profile. `docker compose up` does not
        # start it unless the profile is enabled, so it is not in the plan —
        # but the operator is told, because an image that is not fetched is
        # an image the daemon has to pull on a day when nothing retries it.
        warn "${service} is gated behind a compose profile, so it is not"
        warn "fetched: \`docker compose up\` does not start a profile-gated"
        warn "service either. Fetch it by hand if you run with that profile."
        continue
        ;;
    esac
    rows="${rows}${action}"$'\t'"${image}"$'\t'"${service}"$'\n'
  done <<< "$raw"
  if [ "$refused" -ne 0 ]; then
    return 1
  fi
  PLAN="$rows"
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
# The compose files named on the command line, in the order they were given.
compose_flags=()
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
    --compose)
      [ $# -ge 2 ] || { err "--compose needs a path"; exit 2; }
      compose_flags+=("$2")
      shift
      ;;
    -h|--help) usage; exit 0 ;;
    *) err "unknown argument: $1"; usage; exit 2 ;;
  esac
  shift
done

# The files the plan is read from. The flag wins over the environment
# variable, because a flag on the command line is the more specific of the
# two — and because `PAA_COMPOSE_FILE` names ONE file, while the flag can
# name the several a merged stack is made of.
#
# The paths are resolved against the repository root, because the script
# changes directory there and an operator's relative path is relative to the
# repository they are standing in.
compose_paths=()
if [ ${#compose_flags[@]} -gt 0 ]; then
  compose_paths=("${compose_flags[@]}")
elif [ -n "${PAA_COMPOSE_FILE:-}" ]; then
  compose_paths=("$PAA_COMPOSE_FILE")
else
  compose_paths=("docker-compose.yml")
fi
for compose_path in "${compose_paths[@]}"; do
  case "$compose_path" in
    /*) ;;
    *) compose_path="$REPO_ROOT/$compose_path" ;;
  esac
  COMPOSE_FILES+=("$compose_path")
done

build_plan || exit 1

# An empty plan is a broken source of truth, not an empty deployment. The
# reader found no service with an `image:` or a `build:` section, so the
# script has nothing to fetch and nothing to check — and "every image the
# compose file names is present" would then be a report about nothing. It
# is refused, in every mode, before the mode's own work.
if [ -z "$PLAN" ]; then
  err "no images found in ${COMPOSE_FILES[*]} — the compose file(s) name no"
  err "service with an image: or a build: section, or every service they name"
  err "is behind a profile. There is nothing to fetch, and reporting success"
  err "would be reporting that nothing was checked."
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
  log "every image the compose file(s) name is present"
else
  err "one or more images could not be fetched — re-run this script to continue"
  exit 1
fi
