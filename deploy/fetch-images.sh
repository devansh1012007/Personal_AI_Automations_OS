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
# More shapes are refused, each with its own message, and they are the
# shapes the MERGE does not implement: Compose's `extends:`, a YAML anchor
# or an alias, the YAML MERGE KEY (`<<:`), a multi-line `image:`, a custom
# YAML tag, the top-level `include:`, a `profiles:` that is not a list, a
# mapping written as a FLOW MAPPING, and a service whose keys are not at
# the indent this reader reads. The refusal is on the CONSTRUCT and not on
# the spelling of a line: `include: [other.yml]` is refused exactly as
# `include:` on a line of its own is, an alias is refused wherever it
# appears under `services:`, and a key spelled `image :` is read as the key
# `image` — the way Compose reads it. A wrong plan fetches an image the
# deployment does not run and misses one it does, so the reader says "I do
# not understand this file" rather than guessing.
#
# The line the refusal set is drawn along: a shape COMPOSE refuses itself
# is not a hazard — no deployment can run such a file — and a shape Compose
# ACCEPTS and this reader does not implement is a hazard, and is refused.
#
# A service behind a `profiles:` key is KEPT in the plan, and the FETCH
# decides what to do with it: the rules above — the digest pin above all —
# apply to every image the compose files name, gated or not, because an
# image that is not checked is an image that can be unpinned. A service
# whose profile is not enabled is reported as not fetched, in the report and
# in the final message, rather than erased from the plan. `COMPOSE_PROFILES`
# — Compose's own variable — is honoured, so a profile the operator enabled
# is a profile whose images are fetched.
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
# `COMPOSE_PROFILES` is COMPOSE's own variable, and the script reads it:
# it is the list of profiles the operator has enabled, so the images of a
# profile-gated service are fetched when that profile is enabled and are
# reported as not fetched when it is not. The script reads the same
# variable `docker compose up` reads, so the plan and the stack agree.
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
# empty plan rather than reporting success on nothing. The exit status says
# WHICH kind of failure it is: 1 is a refusal (a shape the reader does not
# understand, an image that cannot be pinned) and 2 is a usage error (an
# argument the script was given wrong).

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT"

MAX_ATTEMPTS="${PAA_FETCH_MAX_ATTEMPTS:-6}"
BASE_DELAY="${PAA_FETCH_BASE_DELAY:-5}"
PULL_TIMEOUT="${PAA_FETCH_TIMEOUT:-900}"
MIRROR="${PAA_IMAGE_MIRROR:-}"

# The compose profiles the operator has enabled.
#
# This is COMPOSE's own variable, not one of ours: `docker compose up` reads
# the same one, and a service behind a profile the operator enabled IS a
# service the deployment runs. The script honours it for the same reason it
# reads the digest from the compose file rather than carrying one of its
# own — a second source of truth about the stack is a second thing to keep
# in step, and this one is already in step by construction.
#
# Compose's own rule: a service is started when its profile list is EMPTY
# (it is gated behind nothing) or when the list intersects the enabled
# profiles. The list is comma-separated, and so is this variable.
ENABLED_PROFILES=""
if [ -n "${COMPOSE_PROFILES:-}" ]; then
  IFS=',' read -r -a _enabled <<< "${COMPOSE_PROFILES}"
  for _name in "${_enabled[@]}"; do
    _name="${_name//[[:space:]]/}"
    [ -n "$_name" ] || continue
    ENABLED_PROFILES="${ENABLED_PROFILES}${ENABLED_PROFILES:+,}${_name}"
  done
  unset _enabled _name
fi

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
# YAML anchor or alias, the YAML merge key (`<<:`), a multi-line `image:`, a
# custom YAML tag, the top-level `include:`, a `profiles:` that is not a
# list, a mapping written as a flow mapping, and a service written at a level
# the reader does not read — its header at three spaces, or its keys at a
# level that is not four. A script that silently mis-merges is worse than one
# that says "I do not understand this file" —
# a wrong plan fetches an image the deployment does not run, and misses one
# it does.
#
# The refusal is on the CONSTRUCT and not on the spelling of a line: the
# reader reads a KEY, so `include: [other.yml]` is refused exactly as
# `include:` on a line of its own is, an alias is refused wherever it
# appears under `services:`, and a key spelled `image :` is read as the key
# `image` — the way Compose reads it. A refusal bound to one spelling is a
# refusal with a hole in it.
#
# The line the refusal set is drawn along: a shape COMPOSE refuses itself
# (a service written as a plain scalar, a `profiles:` that is not a list)
# cannot be deployed, so the reader's answer about it does not matter; a
# shape COMPOSE ACCEPTS and the reader does not implement is a hazard, and
# is refused.
#
# A service gated behind a `profiles:` key is KEPT in the plan, because it
# is still an image the compose file names and the plan's rules apply to it.
# The FETCH decides: a profile that is not enabled is a service
# `docker compose up` does not run, so it is not fetched — and it is
# reported, on stderr and in the report, rather than dropped silently.
# `COMPOSE_PROFILES` says which profiles the operator has enabled.
#
# The output is one tab-separated row per service:
# "action<TAB>image<TAB>service<TAB>profiles", where the action is "pull",
# "build" or "skip" — "skip" for a service a compose profile gates and the
# operator has not enabled — and the profiles column carries the names of the
# compose profiles the service is gated behind ("-" when it is gated behind
# none). A file the reader does not understand comes out as
# "refuse<TAB>message<TAB>-", and the caller turns it into an error.
#
# `--list` prints one row per service: the action, the image, and the service
# in parentheses — and, for a "skip" row, a trailing bracket that names the
# profiles that gate it ("[behind a compose profile that is not enabled: …]"),
# so the report on stdout and the warning on stderr say the same thing.
#
# The profile names travel on the ROW rather than deciding the row's
# membership, because a service behind a profile is still an image the
# compose file names: the plan's invariants — the digest pin above all —
# apply to it, and the decision to fetch it or not is the FETCH's decision,
# made after the rules have run. A reader that dropped the service first
# would let an unpinned image through the pin rule with a zero exit status.
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
    # A blank line and a comment inside a `profiles:` block are not the end
    # of the block: Compose reads the list under the key, and a comment
    # between the key and the first item is an ordinary comment.
    in_profiles && ($0 ~ /^[[:space:]]*$/ || $0 ~ /^[[:space:]]*#/) { next }
    # The items of a `profiles:` list in its block form. They are the only
    # lines the reader reads that carry a value at this indent, so they are
    # collected before anything else looks at them.
    in_profiles && $0 ~ /^[[:space:]]*-[[:space:]]/ {
      item = $0
      sub(/^[[:space:]]*-[[:space:]]*/, "", item)
      sub(/[[:space:]]+#.*$/, "", item)
      sub(/[[:space:]]+$/, "", item)
      profiles = add_profile(profiles, unquote(item))
      profile_items++
      next
    }
    in_profiles {
      # The block ended. A `profiles:` key with no items at all is not a
      # list — Compose refuses such a file — so the reader refuses it too,
      # rather than reading it as "no profiles".
      if (block_form && profile_items == 0) {
        refuse(display(FILENAME) ": the service " service " declares a profiles: with no list.\n" \
               "Compose requires a list of profile names, and an empty one is not\n" \
               "a list.")
      }
      in_profiles = 0
      block_form = 0
    }
    /^[^[:space:]#]/ {
      # A top-level key. Only "services:" opens the map the rest of the reader
      # cares about; "networks:" and "volumes:" sit at the same indent and are
      # not services.
      #
      # What the reader acts on is the KEY, not the spelling of the line:
      # Compose reads `include: [other.yml]` and `include:` on its own line
      # as the same key, and it reads `include :` as the key `include` too.
      # A refusal bound to one spelling of a line is a refusal with a hole
      # in it, and the hole is a stack the script plans and Compose does not
      # run.
      line = $0
      top_key = line
      top_value = ""
      pos = index(line, ":")
      if (pos > 0) {
        top_key = substr(line, 1, pos - 1)
        top_value = substr(line, pos + 1)
      }
      top_key = normalise_key(top_key)
      sub(/^[[:space:]]+/, "", top_value)
      sub(/[[:space:]]+$/, "", top_value)
      if (top_key == "include") {
        refuse(display(FILENAME) ": the top-level include: is not supported.\n" \
               "This script does not follow Compose'"'"'s include:, and the services\n" \
               "it names would be missing from the plan.")
      }
      if (top_key == "<<") {
        refuse(display(FILENAME) ": the compose file uses a YAML merge key (<<:) at the top level.\n" \
               "This script does not resolve merge keys, and the keys a merged map\n" \
               "carries would be missing from the plan.")
      }
      if (top_key == "services" && top_value ~ /^\{/) {
        refuse(display(FILENAME) ": the services map is written as a flow mapping.\n" \
               "This script reads the services on their own lines, and it refuses\n" \
               "the rest rather than guessing which services the stack runs.")
      }
      if (top_value ~ /^\*/) {
        refuse(display(FILENAME) ": the top-level " top_key ": is a YAML alias.\n" \
               "This script does not resolve aliases, so it cannot tell what the\n" \
               "alias names.")
      }
      in_services = (top_key == "services")
      next
    }
    in_services && $0 ~ /^  [^[:space:]#][^:]*:/ {
      # A key at two-space indent: a service name, or a service with an
      # inline value — which Compose would refuse, because a service is a
      # mapping and not a string.
      line = $0
      sub(/^  /, "", line)
      key = line
      inline = ""
      pos = index(line, ":")
      if (pos > 0) {
        key = substr(line, 1, pos - 1)
        inline = substr(line, pos + 1)
      }
      key = normalise_key(key)
      sub(/[[:space:]]+#.*$/, "", inline)
      sub(/^[[:space:]]+/, "", inline)
      sub(/[[:space:]]+$/, "", inline)
      if (key == "<<") {
        refuse(display(FILENAME) ": the services map uses a YAML merge key (<<:).\n" \
               "This script does not resolve merge keys, and the services a merged\n" \
               "map carries would be missing from the plan.")
      }
      if (inline == "") {
        flush_service()
        service = key
        image = ""
        have_image = 0
        have_build = 0
        profiles = ""
        next
      }
      if (inline ~ /^\{/) {
        refuse(display(FILENAME) ": the service " key " is written as a flow mapping.\n" \
               "This script reads a service'"'"'s keys on their own lines, and it refuses\n" \
               "the rest rather than guessing which image the service names.")
      }
      # An anchor or an alias on the service key is refused: the reader would
      # then attribute the keys below it to the wrong service, and a wrong
      # plan fetches an image the deployment does not run.
      if (inline ~ /^[&*]/) {
        refuse(display(FILENAME) ": the service " key " uses a YAML anchor or an alias.\n" \
               "This script does not resolve anchors, and an unresolved image\n" \
               "reference would be pulled as the literal text of the alias.")
      }
      next
    }
    # The LEVEL of a line, COUNTED rather than matched.
    #
    # An interval expression ("five or more spaces") is not portable between
    # the awks a host may have — mawk does not backtrack into one — and the
    # reader must not depend on which awk it found. The count is exact and
    # the same count the Python reader in the tests makes.
    #
    # The reader reads a services map as: a service is a key at two-space
    # indent, and a service'"'"'s keys are keys at four-space indent. A key at
    # any OTHER level is a level the reader does not read, and there are two
    # ways such a key hides a service:
    #
    #   * the key IS a service, written at a level the reader does not read
    #     AS a service — a services map written at three spaces, say. The
    #     service is not in the plan at all, and one of its keys may be the
    #     `image:`. There is no service to refuse at the end of it, because
    #     the reader never opened one: this refusal is at the LINE.
    #   * the key is a key OF a service, written at a level the reader does
    #     not read as a service key — a service whose keys sit at three, or
    #     five, or six spaces. The service is in the plan, but empty, and
    #     the refusal is at the END of the service, where the reader knows
    #     it saw no key of it.
    #
    # The first is the half that was open: the reader'"'"'s rule was about the
    # level of a service'"'"'s KEYS, and a service whose HEADER is at a level
    # the reader does not read was not read and not refused — it was
    # dropped from the plan, silently, and a file beside it kept the plan
    # non-empty, so the script exited ZERO.
    #
    # A level DEEPER than four is not a hazard on its own: it is content
    # nested under a key the reader has read (an `environment:` map, a
    # `build:` section), and the reader cannot find an `image:` in there,
    # because Compose reads a service'"'"'s `image:` as a key OF the service.
    # So a deeper line is allowed, and the end-of-service rule decides
    # whether the service'"'"'s OWN keys were readable.
    in_services {
      probe = $0
      indent = 0
      while (substr(probe, indent + 1, 1) == " ") indent++
      # A blank line and a comment are not keys and cannot hide one.
      if (indent < length(probe) && substr(probe, indent + 1, 1) != "#") {
        if (indent == 4) {
          # A key at the level the reader reads a service'"'"'s keys at. It is
          # a key of the open service — unless NO service is open, in which
          # case the services map itself is written at the wrong level and
          # the key belongs to a service the reader never opened.
          if (service == "") refuse_level(indent, level_key(probe), 1)
        } else if (indent == 2) {
          # A line at the service level that is not a service header: a
          # sequence item, or a scalar where a service belongs. The reader
          # does not read it, and it is content it cannot see.
          saw_key_any_indent = 1
        } else if (indent > 4 && service != "") {
          # Content nested under a key the reader has read. It cannot hide
          # the service'"'"'s own `image:` — Compose reads that as a key of the
          # service — so it is allowed here, and the end-of-service rule
          # decides whether the service'"'"'s own keys were readable.
          saw_key_any_indent = 1
        } else {
          # A key at a level this reader does not read: one space, or
          # three, or five with no service open. It is either a service the
          # reader cannot see, or a key of a service it cannot see, and one
          # of them may be the image — so the file is refused rather than
          # read in part.
          refuse_level(indent, level_key(probe), 0)
        }
      }
    }
    in_services && service != "" && $0 ~ /^    [^[:space:]#]/ {
      # A key at four-space indent: one of the keys this reader reads, or
      # one it ignores. The KEY decides, not the spelling of the line:
      # Compose reads `image : x` and `"image": x` as the key `image`, and
      # so does this reader — a missed `image:` is a service that is
      # dropped from the plan, silently, which is the one failure this
      # reader cannot have.
      saw_key = 1
      line = $0
      sub(/^    /, "", line)
      key = line
      value = ""
      pos = index(line, ":")
      if (pos > 0) {
        key = substr(line, 1, pos - 1)
        value = substr(line, pos + 1)
      }
      key = normalise_key(key)
      sub(/[[:space:]]+#.*$/, "", value)
      sub(/^[[:space:]]+/, "", value)
      sub(/[[:space:]]+$/, "", value)
      if (key == "<<") {
        refuse(display(FILENAME) ": the service " service " uses a YAML merge key (<<:).\n" \
               "This script does not resolve merge keys, and the keys a merged map\n" \
               "carries would be missing from the plan.")
      }
      if (key == "image") {
        if (value ~ /^[|>][-+0-9]*$/) {
          refuse(display(FILENAME) ": the service " service " declares a multi-line image:.\n" \
                 "This script reads one line per image, and a folded or a literal\n" \
                 "block would be read as the text of its indicator.")
        }
        if (value ~ /^!/) {
          refuse(display(FILENAME) ": the service " service " puts a custom YAML tag on its image:.\n" \
                 "This script does not interpret YAML tags, so it cannot tell what\n" \
                 "the tagged value is.")
        }
        if (value ~ /^\*/) {
          refuse(display(FILENAME) ": the service " service " declares an image: that is an alias.\n" \
                 "This script does not resolve aliases, so it cannot tell which\n" \
                 "image the alias names.")
        }
        if (value ~ /&/) {
          refuse(display(FILENAME) ": the service " service " declares an anchored image:.\n" \
                 "This script does not resolve anchors, so it cannot tell which\n" \
                 "image the anchor names.")
        }
        if (substr(value, 1, 1) == "\"" || substr(value, 1, 1) == "\047")
          value = substr(value, 2)
        if (length(value) > 0) {
          last = substr(value, length(value), 1)
          if (last == "\"" || last == "\047") value = substr(value, 1, length(value) - 1)
        }
        image = value
        have_image = 1
        next
      }
      if (key == "extends") {
        refuse(display(FILENAME) ": the service " service " uses extends:.\n" \
               "This script does not resolve Compose'"'"'s inheritance, and an image\n" \
               "inherited from another service would be fetched by guesswork.")
        next
      }
      if (key == "build") {
        have_build = 1
        next
      }
      if (key == "profiles") {
        # A `profiles:` key is a LIST of profile names, and the list has two
        # spellings: a block (the items on the following lines) and a flow
        # (the items inside the brackets). Both are read, because the list
        # decides which services the deployment runs.
        if (value == "") {
          in_profiles = 1
          block_form = 1
          profile_items = 0
        } else if (value ~ /^\[/) {
          if (value !~ /\]$/) {
            refuse(display(FILENAME) ": the service " service " declares a profiles: list this reader does not understand.\n" \
                   "This script reads a list of profile names on one line or as a\n" \
                   "block under the key, and it refuses the rest rather than\n" \
                   "guessing which services it gates.")
          }
          inner = substr(value, 2, length(value) - 2)
          count = split(inner, parts, /,/)
          for (i = 1; i <= count; i++) {
            item = parts[i]
            sub(/^[[:space:]]+/, "", item)
            sub(/[[:space:]]+$/, "", item)
            if (item == "") continue
            profiles = add_profile(profiles, unquote(item))
          }
        } else {
          # A scalar. Compose requires a list of profile names and refuses
          # the file, so the reader refuses it too.
          refuse(display(FILENAME) ": the service " service " declares a profiles: that is not a list.\n" \
                 "Compose requires a list of profile names, and this script does not\n" \
                 "guess what a single name gates.")
        }
        next
      }
      # Any other key: the reader ignores its value. An ALIAS is the one
      # value it cannot ignore — it is a reference the reader cannot
      # resolve, and a shared or merged key is exactly how a compose file
      # hides an image from a reader that only looks at spellings.
      if (value ~ /^\*/) {
        refuse(display(FILENAME) ": the service " service " declares " key ": as a YAML alias.\n" \
               "This script does not resolve aliases, so it cannot tell what the\n" \
               "alias names.")
      }
      next
    }
    END {
      if (aborted) exit 1
      if (in_profiles && block_form && profile_items == 0) {
        refuse(display(FILENAME) ": the service " service " declares a profiles: with no list.\n" \
               "Compose requires a list of profile names, and an empty one is not\n" \
               "a list.")
      }
      flush_service()
      for (i = 1; i <= g_count; i++) {
        name = g_order[i]
        if (!g_has_image[name] && !g_has_build[name]) continue
        action = (g_has_build[name] ? "build" : "pull")
        image = (g_has_image[name] && g_image[name] != "" ? g_image[name] : "-")
        printf "%s\t%s\t%s\t%s\n", action, image, name, (g_profiles[name] == "" ? "-" : g_profiles[name])
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
    function level_key(probe, key) {
      # The key a line carries, for the message that names it: the text up to
      # the first colon, with the spaces and the quotes around it removed —
      # the key as YAML reads it. A line that carries no colon has no key,
      # and the message says "a line" instead.
      key = probe
      sub(/^[[:space:]]+/, "", key)
      sub(/[[:space:]]+$/, "", key)
      if (index(key, ":") == 0) return ""
      key = substr(key, 1, index(key, ":") - 1)
      key = normalise_key(key)
      # A tab would break the row the message travels on.
      gsub(/\t/, " ", key)
      if (length(key) > 40) key = substr(key, 1, 40)
      return key
    }
    function refuse_level(indent, key, orphan, head) {
      # A key at a level this reader does not read. The file and the shape
      # are named, because a refusal that does not say what it read is a
      # refusal the operator has to guess about.
      #
      # `orphan` is the second shape of the same refusal: a key at the
      # level the reader reads a service'"'"'s keys at, with NO service open.
      # The services map itself is written at the wrong level, so the key
      # belongs to a service the reader never opened.
      if (key == "") {
        head = "the services map has a line at " indent " spaces"
      } else {
        head = "the services map has a key at " indent " spaces (" key ")"
      }
      if (orphan) {
        refuse(display(FILENAME) ": " head ", and no service for it: a level this reader does not understand.\n" \
               "This script reads a service at two spaces and its keys at four spaces, so a key at\n" \
               "that level with no service open is a service written at a level this reader does\n" \
               "not understand — and one of its keys may be the image, so the file is refused\n" \
               "rather than read in part.")
      }
      refuse(display(FILENAME) ": " head ", a level this reader does not understand.\n" \
             "This script reads a service at two spaces and its keys at four spaces. A key at\n" \
             "another level is a service this reader cannot see, or a key of a service it\n" \
             "cannot see — and one of them may be the image, so the file is refused\n" \
             "rather than read in part.")
    }
    function normalise_key(key) {
      # The key as YAML reads it: the spaces around it and the quotes around
      # it are not part of the name. Compose reads `image :` and
      # `"image":` as the key `image`, and so does this reader.
      sub(/^[[:space:]]+/, "", key)
      sub(/[[:space:]]+$/, "", key)
      if (length(key) >= 2) {
        first = substr(key, 1, 1)
        last = substr(key, length(key), 1)
        if (first == last && (first == "\"" || first == "\047"))
          key = substr(key, 2, length(key) - 2)
      }
      return key
    }
    function unquote(value) {
      # A quoted scalar, without the quotes. Only a MATCHING pair is
      # stripped: a value that starts with a quote and does not end with one
      # is not a quoted scalar, and half-stripping it would invent a value
      # the file does not have.
      if (length(value) >= 2) {
        first = substr(value, 1, 1)
        last = substr(value, length(value), 1)
        if (first == last && (first == "\"" || first == "\047"))
          value = substr(value, 2, length(value) - 2)
      }
      return value
    }
    function add_profile(list, item) {
      # The profile names a service is gated behind, as a comma-separated
      # list, without duplicates. Compose CONCATENATES the lists of several
      # files, so the merged list is the union of them.
      if (item == "") return list
      if (index("," list ",", "," item ",") > 0) return list
      return (list == "" ? item : list "," item)
    }
    function reset_service() {
      service = ""
      image = ""
      have_image = 0
      have_build = 0
      profiles = ""
      in_profiles = 0
      block_form = 0
      profile_items = 0
      saw_key = 0
      saw_key_any_indent = 0
    }
    function flush_service() {
      # The service that is open is finished: its values land in the merged
      # map, and only the keys this file actually declared — a later file
      # that says nothing about `build:` leaves the earlier file'"'"'s alone.
      if (service == "") return
      if (!saw_key && saw_key_any_indent) {
        # The reader saw no key of this service at its own level, and there
        # was a key at some other level. The service'"'"'s keys are written
        # some other way, and one of them may be the image — so the service
        # is refused rather than dropped from the plan.
        refuse(display(FILENAME) ": the service " service " has keys this reader does not understand.\n" \
               "This script reads a service'"'"'s keys at four-space indent, and a\n" \
               "service written any other way could hide an image: from the plan.")
      }
      if (!(service in g_seen)) {
        g_seen[service] = 1
        g_order[++g_count] = service
        g_image[service] = ""
        g_has_image[service] = 0
        g_has_build[service] = 0
        g_profiles[service] = ""
      }
      if (have_image) {
        g_image[service] = image
        g_has_image[service] = 1
      }
      if (have_build) g_has_build[service] = 1
      count = split(profiles, parts, /,/)
      for (i = 1; i <= count; i++) {
        if (parts[i] == "") continue
        g_profiles[service] = add_profile(g_profiles[service], parts[i])
      }
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
# The services the plan did not fetch, because a compose profile that is not
# enabled gates them. They are named in the report and in the final
# message: an image that was not fetched is an image the daemon has to pull
# on a day when nothing retries it.
SKIPPED=""

# Is the service behind a profile the operator has enabled?
#
# Compose's own rule: a service is started when its profile list is empty —
# it is gated behind nothing — or when the list intersects the enabled
# profiles. The list is the row's fourth column, comma-separated.
profile_is_enabled() {
  local profiles="$1" name
  # "-" is the row's "no profiles" — an empty field is not a field in a
  # tab-separated row, so the reader writes a dash there.
  [ -n "$profiles" ] || return 0
  [ "$profiles" = "-" ] && return 0
  local IFS=','
  for name in $profiles; do
    case ",${ENABLED_PROFILES}," in
      *",${name},"*) return 0 ;;
    esac
  done
  return 1
}

build_plan() {
  local raw action image service profiles rows="" plan="" refused=0
  # The reader's rows come back in two kinds: the plan itself, and a file
  # it does not understand. The last one is an error and stops the script
  # here, before a mode's own work, so a plan that was never built is
  # never acted on.
  raw=""
  if ! raw="$(parse_compose)"; then
    refused=1
  fi
  # EVERY service the files name is in these rows — including the ones a
  # profile gates. The rules below run over these rows, and they must: an
  # image that is not checked is an image that can be unpinned. The
  # exclusion of a profile-gated service is the FETCH's decision, and it is
  # made after the rules have run, not before.
  while IFS=$'\t' read -r action image service profiles; do
    [ -n "$service" ] || continue
    case "$action" in
      refuse)
        refused=1
        err "${image}"
        continue
        ;;
    esac
    rows="${rows}${action}"$'\t'"${image}"$'\t'"${service}"$'\t'"${profiles}"$'\n'
  done <<< "$raw"
  if [ "$refused" -ne 0 ]; then
    return 1
  fi
  unpinned=""
  interpolated=""
  unnamed=""
  while IFS=$'\t' read -r action image service profiles; do
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
  done <<< "$rows"
  # Now the FETCH decides. A service behind a profile the operator has not
  # enabled is one the deployment does not run, so it is not fetched — but
  # it stays in the plan, marked, and the report says so. Dropping it here
  # instead would put the exclusion BEFORE the rules above, which is how an
  # unpinned image behind a profile used to pass the gate.
  SKIPPED=""
  while IFS=$'\t' read -r action image service profiles; do
    [ -n "$service" ] || continue
    if ! profile_is_enabled "$profiles"; then
      action="skip"
      SKIPPED="${SKIPPED}${service}"$'\n'
      warn "${service} is gated behind a compose profile that is not enabled, so"
      warn "it is not fetched: \`docker compose up\` does not start a profile-gated"
      warn "service either. Enable the profile with COMPOSE_PROFILES and the"
      warn "script fetches it."
    fi
    plan="${plan}${action}"$'\t'"${image}"$'\t'"${service}"$'\t'"${profiles}"$'\n'
  done <<< "$rows"
  PLAN="$plan"
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
  local action image service profiles
  while IFS=$'\t' read -r action image service profiles; do
    [ -n "$service" ] || continue
    if [ "$action" = "skip" ]; then
      # A service the deployment does not run, because a compose profile
      # that is not enabled gates it. It is in the report — the report is
      # what the operator reads — and it is marked, so the report and the
      # warning on stderr say the same thing.
      printf '%-6s%s  (%s)  [behind a compose profile that is not enabled: %s]\n' \
        "$action" "$image" "$service" "$profiles"
      continue
    fi
    printf '%-6s%s  (%s)\n' "$action" "$image" "$service"
  done <<< "$PLAN"
}

verify_images() {
  local failed=0 action image service profiles digest
  while IFS=$'\t' read -r action image service profiles; do
    [ -n "$service" ] || continue
    if [ "$action" = "skip" ]; then
      # Not part of the stack this run, so there is nothing to verify.
      continue
    fi
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
  local failed=0 action image service profiles digest name target
  while IFS=$'\t' read -r action image service profiles; do
    [ -n "$service" ] || continue
    if [ "$action" = "skip" ]; then
      # Not part of the stack this run: a compose profile that is not
      # enabled gates it, and the plan already said so.
      continue
    fi
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
  err "service with an image: or a build: section. There is nothing to fetch,"
  err "and reporting success would be reporting that nothing was checked."
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

# The final message says what it means. "Every image the compose file(s)
# name is present" is only true when the plan was the whole file: a
# service behind a compose profile that is not enabled is named by the file
# and is NOT in the local store, so the claim has to name the stack that
# runs rather than the file, and the report has to say what was left out.
if fetch_images; then
  skipped_count=0
  while IFS= read -r skipped_service; do
    [ -n "$skipped_service" ] || continue
    skipped_count=$(( skipped_count + 1 ))
  done <<< "$SKIPPED"
  if [ "$skipped_count" -gt 0 ]; then
    log "every image this stack runs is present"
    warn "${skipped_count} service(s) behind a compose profile that is not enabled were not fetched:"
    while IFS= read -r skipped_service; do
      [ -n "$skipped_service" ] || continue
      warn "  ${skipped_service}"
    done <<< "$SKIPPED"
  else
    log "every image the compose file(s) name is present"
  fi
else
  err "one or more images could not be fetched — re-run this script to continue"
  exit 1
fi
