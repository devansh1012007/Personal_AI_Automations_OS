# ADR-0030: The deployment's images are named in one place, and the pin is checked

## Status

Accepted

*Amended 2026-10-22 by W1-IMPL-02 (T-003), and amended again the same day by
W1-IMPR-02 (the wave-1 improvement). The second amendment closes three holes the
wave-1 review found in the first: the reader refused some shapes by their
*spelling* rather than by their *construct*, a profile-gated service was dropped
from the plan **before** the pin rule ran, and this ADR's own account of how
Compose merges a `profiles:` list was wrong. The two amendments are read
together; the second one restates the first where the first was wrong.*

## Context

`deploy/fetch-images.sh` existed to fetch the server topology's images with retries and a
resume, because a container image is fetched over a network that fails in ways that are
indistinguishable from a real error. `docker compose up` does not retry; it fails, and the
operator is left guessing whether the image is corrupt or the network is bad.

The script did not do that. It carried its own hand-copied table of the images the
deployment needs, and the copy had drifted from `docker-compose.yml`: the script's
`qdrant/qdrant:v1.15.4` and `chrislusf/seaweedfs:3.97` were **older versions** than the
`v1.19.2` and `4.48` the compose file actually ran. The script therefore fetched two
images the deployment never used, and left the two it did use to `docker compose up` —
which is also where the network is not retried.

Three of the script's other claims were false as well.

**The digest pin was documented and implemented nowhere.** The header said "the images are
pinned by digest" and described a "third field" that was "the digest the image must have",
with a mismatch as an error. No row had the third field and no code read it. The compose
file pins all four third-party images by digest; the script pulled them by tag and called
it pinning.

**The presence check asked for the wrong name.** It inspected `postgres`, `redis`,
`qdrant` and `seaweedfs` — the local names — while compose runs
`postgres:16@sha256:…`. `docker image inspect postgres` resolves to `postgres:latest`,
which the daemon does not have, so the check reported every image absent *even after
pulling it successfully*. The script failed on every image, every time, and its exit status
was therefore useless as a CI gate.

**`--load` was unreachable.** The branch consumed two arguments and shifted one, so
`--load <dir>` exited 2 at the argument parser. Had it been reachable it would have loaded
the tarballs **and then** pulled every image, because the branch had no `exit`.

Two further problems were in the *deployment* rather than in the script, and the same
change fixes them.

**`paa-init` could not be started on its own.** It declared `image: paa/runtime:4.1` and
no `build:` section, so Compose's contract for it was "pull this" — from a registry that
has never heard of the image. It worked only by accident: the `paa` service's build
produced the same tag in the same invocation, so the image was in the local store by the
time `paa-init` started. Starting `paa-init` alone — which is exactly what an operator
does to fix the data directory's ownership — failed with a pull error.

**A credential in the operator's shell did not reach the container.** The compose file
delivered the model configuration through `env_file: .env` and nothing else, so the
documented `export OPENROUTER_API_KEY=…` was silently ignored and a containerised runtime
could not reach a provider whose credential lived only in the shell.

## Decision

**The compose file is the single source of truth, and the script reads it.**

The hand-copied table and the local-image list are deleted. The script parses
`docker-compose.yml` for each service's `image:` and `build:`, and classifies it: a
service with a **`build:`** section is *built here* — its `image:` is the tag the build
produces — and a service with only an `image:` is *pulled*.

The parse is **dependency-free**. PyYAML is not a dependency of this project, and the
deployment tests say so explicitly and read the compose files as text; the script does the
same, in `awk`. A hand-rolled reader is only acceptable if something holds it to the
file, so `tests/deployment/test_fetch_images.py` parses the compose file a second time, in
Python, independently, and asserts that the two agree — and separately asserts what the
list *contains*, so that two readers agreeing on nothing cannot pass. The reader's rules
are the shape of the file: a service is a key at two-space indent under `services:`, and
its `image:` and `build:` are keys at four-space indent. A top-level `x-` extension field
is not a service, and neither is a nested key.

**The parity test is a shape lock on the current file, and it says so.** Two readers can
agree about one file and disagree about the next, so the agreement is *also* asserted
over a corpus of shapes the repository's own compose file does not happen to contain: a
trailing comment on an `image:` line, a service key with a trailing comment, a service
with neither an `image:` nor a `build:`, an `image:` with no value, a quoted value, a
carriage return, and a reference that is a variable. The trailing comment is the shape
that matters: the script strips it and the test's reader once did not, so the day
somebody added one the parity test would have failed with the **test** as the wrong
party — and a maintainer "fixing" the script to match the test would have produced a
pull reference of `postgres:16@sha256:X  # comment`. The rule is therefore stated once
and followed twice: strip a trailing comment from a service key and from an `image:`
value, skip a service that declares neither an image nor a build, report an `image:` with
no value as `-`, and strip the carriage return a compose file written on Windows leaves
at the end of every line — a digest with a carriage return on the end of it matches
nothing, so the reference could never be satisfied.

**A pulled image must be digest-pinned, and a tag-only reference is an error.**

Not a warning: the deployment's own compose file already pins every one of its images, a
tag is a promise about a moving target, and this repository has already been bitten by
one (the MinIO community image is archived upstream and its registry path now answers
401, so a deployment naming `minio/minio:latest` could not be reproduced at all). The
refusal happens **before** anything is fetched, so a stack that cannot be pinned fails
without a byte leaving the host. The pin is read from compose; the script carries no
digest of its own, and a test asserts that it does not.

**Three references are refused, and each gets its own message.**

One message cannot describe three different mistakes, so the plan keeps them apart: an
image that is **not digest-pinned** (the rule above), an image whose reference is a
**variable**, and an `image:` key with **no value**.

The variable is refused because the script does not interpolate compose's variables. It
reads the compose file as text; it cannot know what the operator's shell and `.env`
hold, and a guess would pull something the compose file did not name. What it must not
do is read `${IMAGE:-postgres:16}` as an image reference and then advise pinning it as
`${IMAGE:-postgres:16}@sha256:<digest>` — advice nobody can follow, about a reference
that is not an image. The message says what the situation is and what to do about it:
pin the image literally, or point the script at a compose file whose `image:` values are
already resolved.

The empty value is refused because Compose would refuse the file, and a service that
declares an image and gives it none is not a service the script can fetch.

**An empty plan is refused as well.** A compose file the reader finds no images in is a
broken source of truth, not an empty deployment. The script used to report "every image
the compose file names is present" about such a file and exit 0 — a gate that had
checked nothing, reporting success. (A *missing* compose file was handled correctly all
along; it is an *empty* one that was not.)

**The digest check is inside the retry unit.**

One attempt is *one pull plus its verification*, the rule the Dockerfiles already follow
for a download and its SHA-256 check: a transfer that produced the wrong bytes is a
failed **attempt**, not a failed fetch, and the next attempt gets to try again. The
verification is therefore inside the loop, not after it.

The comparison is on the **digest**, never on the repository path. The compose reference
is `<repo>:<tag>@sha256:X` while the daemon records `<repo>@sha256:X` — the tag is
stripped — and a mirror records its own path again. A digest names exactly one image; a
path names a shelf it happens to sit on.

`RepoDigests` is the field the check reads, and `.Id` is not. `.Id` is the image's
*content* identifier, and what it names depends on the daemon's image store: on the
classic graph driver it is the configuration digest, and on the containerd store it is the
target descriptor. On the reference host — Docker 29.9, containerd snapshotter — the two
happen to coincide for a single-platform image, so a check written against `.Id` would
pass here and fail on a host with the other store. `RepoDigests` is by definition the list
of manifest digests the registry served, so it answers the same question on every host.

**The choice is covered by a test, because the stub can make it visible.** A stub that
answers `image inspect` identically whatever `--format` it is given makes the choice
invisible — the script could read `.Id` and pass every test. The stub therefore answers
the two fields **differently**: `.RepoDigests` with the manifest digest the reference
carries, and `.Id` with a digest that is not it. A script that read `.Id` fails every
check it makes, and the suite fails with it.

**The presence check uses the full reference compose uses.**

`docker image inspect <repo>:<tag>@sha256:X` asks the daemon for that image by its
digest; `docker image inspect <repo>` asks for `<repo>:latest`, which is a different
image. The check asks for the whole reference, and a daemon that holds it says so.

**`--load <dir>` works, and it stops.**

It consumes two arguments, loads every `*.tar` in the directory with the same retry
policy, and **exits**. It does not fall through into the fetch.

A loaded image is **not** digest-verified, and the script says so rather than pretending
otherwise. A `docker save` / `docker load` round trip restores an image by its
configuration, and the manifest digest the registry served is not what the load path can
be asked about. The pin is enforced on the pull path; a tarball is trusted the way an
operator's own disk is trusted, and the operator is told that is what happened.

**`--list` needs neither the docker CLI nor a running daemon.**

The list comes from the compose file, so the daemon-reachability gate moves *after* the
`--list` branch. The report is available on a host that has no Docker at all, which is
also what makes it testable.

**`--list` is a gate as well as a report.** It used to only *warn* about an image it
could not pin, so the plan it printed and the plan it enforced were two different
things: `--verify` and the fetch refuse an unpinned image, and `--list` — the one that
needs no daemon, and is therefore the one a CI job can run anywhere — reported success.
It now exits non-zero when the plan names an image this deployment cannot pin. The list
still prints, because the operator needs to see *what* is wrong, and the exit status is
the verdict.

**The GNU requirement is stated and checked.**

The script bounds every network operation with `timeout`, which is GNU coreutils: it is
not in a stock macOS or BSD system, and a busybox `timeout` is a different program with
different flags. The header says so, and the script fails with a sentence about the
requirement — rather than "command not found" in the middle of a pull.

**`paa-init` gets a `build:` section**, the same `context:` and `dockerfile:` as the
`paa` service. Two services with the same build section build the same tag, and the layer
cache makes the second one nearly free.

**The model credential and the `PAA_MODELS__*` settings are forwarded from the shell.**

The `paa` service's `environment:` block gains:

```yaml
OPENROUTER_API_KEY: ${OPENROUTER_API_KEY:-}
ANTHROPIC_API_KEY: ${ANTHROPIC_API_KEY:-}
PAA_MODELS__LOCAL_PROVIDER: ${PAA_MODELS__LOCAL_PROVIDER:-ollama}
PAA_MODELS__LOCAL_MODEL: ${PAA_MODELS__LOCAL_MODEL:-qwen2.5:3b-instruct}
PAA_MODELS__ESCALATION_PROVIDER: ${PAA_MODELS__ESCALATION_PROVIDER:-anthropic}
```

Compose's interpolation source is the **shell environment first and the project `.env`
second**, so the interpolation gives the documented `export OPENROUTER_API_KEY=…`
precedence *and* keeps the file working when the shell has nothing. A variable in the
`environment:` block also takes precedence over `env_file:`, so the value that arrives is
the interpolated one. The key names are the registry rows' `api_key_env` values
(`src/paa/models/registry.py`) — the same indirection `StorageSettings` uses for the S3
store. No new exposure: `environment:` sets a runtime variable on a container, exactly as
`env_file:` already does, and `.dockerignore` keeps `.env` out of the build context.

**These variables are optional, and the three settings carry the code's own defaults.**

They are not `:?`-guarded: a stack with no model provider is a legitimate configuration
— the runtime runs on its embedded defaults — and a required key would stop the
deployment from starting for a reason the operator did not cause. That is the opposite of
the object store's credential, which *is* guarded, and the two are different failures
handled differently on purpose.

The three **settings** carry a non-empty fallback, and that is not a cosmetic choice. An
empty setting is not an absent setting: `pydantic-settings` reads
`PAA_MODELS__LOCAL_PROVIDER=""` as the provider named `""`, and
`build_local_provider("")` then fails to resolve a provider at all — the container dies
at boot, on a variable the operator never set. The fallback is `ModelSettings`' own
default, so "the operator set nothing" means exactly what it means on a laptop, and
`tests/deployment/test_compose_config.py` asserts that the compose file's copy and the
code's copy are the same string. The two **credentials** carry the empty fallback, which
is correct for them: the model layer reads a key by name and treats `""` as absent.

## Consequences

**The script and the compose file cannot drift apart**, because there is only one list
and it is read, not copied. The parity test is what makes the hand-rolled reader safe.

**The script's exit status is now meaningful as a CI gate.** It used to be non-zero on
every image; now it is non-zero when an image could not be fetched, and a failure inside
the retry unit is a failure of the *image*, not of the script's bookkeeping.

**The script touches the local image store and nothing else.** `docker image inspect`,
`docker pull`, `docker tag`, `docker load`. It never stops, removes, restarts or prunes
anything, and it never touches a running container — and the test that says so asserts
the `(verb, subcommand)` **pairs** it uses are exactly that set. A first-word check
cannot see `docker image prune -f` or `docker image rm`, because both of them start with
"image", and the first of those is the command that would delete somebody else's images.

**The mirror keeps working, and the digest check is unaffected by it.** The mirror
changes the repository path, not the digest. The script pulls
`<mirror>/<repository>:<tag>@sha256:X` — the digest travels with the pull, or the pull
is not pinned — and then tags the result under the name compose expects.

**The tag is issued only when the mirror changed the path.** Without a mirror the pulled
reference *is* the reference compose uses, and tagging it would add a **mutable** alias
— a tag carrying no digest — to the local store. A later unpinned pull could move that
alias to different bytes, in a deployment whose whole point is that the pin is not a
promise. The comparison is therefore on the **repository**, with the digest stripped from
both sides: `target` and the compose reference differ by the digest in the ordinary case,
so comparing them whole fires the tag on every pulled image.

(The obvious alternative — tagging the *pinned* name, `<repo>:<tag>@sha256:X` — is not
available: the daemon refuses to create a tag with a digest reference. In moby's
reference store, `AddTag` rejects a canonical reference outright — "refusing to create a
tag with a digest reference" — so `docker tag` to a digest target fails on every
daemon.)

**What the mirror does not fix is compose's own resolution.** The script tags the
mirrored image under the compose reference's tag, but compose's reference is
`<repo>:<tag>@sha256:X`, and whether a daemon can resolve a *cross-repository* digest
reference depends on its image store: the containerd store resolves it by digest
regardless of the repository name, and the classic graph driver keys its digest index by
the full `repository@digest` string. An operator behind a mirror on the classic driver
may therefore see Compose pull the digest itself, straight from the upstream registry.
This is a property of Compose and the daemon, not of this script, and it is recorded
rather than papered over.

**A local build is not fetched, and the script does not build it.** The plan is
`docker compose build`, which is where a compose service's build section is meant to be
built; a second implementation here would be a second place to keep the context and the
Dockerfile in step. The script reports a built image's presence and tells the operator
the command to run.

**The reader's rules are the compose file's rules.** A service is a key at two-space
indent under `services:`, and its `image:` and `build:` are at four-space indent. A
compose file that used a different shape — a service named with an inline value, a
top-level `x-` block *after* `services:` — would need the reader (and the Python side of
the parity test) to learn it. The parity test is what makes that a loud failure rather
than a silent omission.

**The script's scope is the compose file's services, and that is narrower than it was.**
The old script also listed `paa/base_worker:v4.1` and `paa/toolbox:v1` as "built here" —
two images this repository builds that no compose service names, because the runtime
launches them itself and the tool layer launches the toolbox. They are not in the plan
any more, so `--list` no longer mentions them. The build commands for both are
documented in `docs/deployment/docker.md`, and the loss is therefore mitigated rather
than suffered: the script's answer is now exactly the question it is asked, which is
"what does this compose file run?" — and `paa/base_worker:v4.1` is not something this
compose file runs.

**What it would take to move back.** A second list of images, kept in step by hand. The
drift that made the script useless is what that costs, and it is already paid once.

**What this ADR does not decide.** The container's start is not charged to the work
(ADR-0031), and the credential-forwarding rule here says nothing about where a
credential may be *stored* — only that the operator's shell is a legitimate place to
put one for the length of a deployment.

---

## Amendment — several compose files, and a merge that is not Compose's

*Amended 2026-10-22, by W1-IMPL-02 (T-003). Everything above still stands: the
compose file is the single source of truth, the script reads it rather than copying
it, a pulled image must be digest-pinned, the digest check is inside the retry unit,
and the script carries no image table of its own. This amendment changes one thing —
the source of truth is now a SET of compose files, merged — and adds nothing to the
rest.*

### What changed

The script takes **several** compose files: a repeatable `--compose <path>` flag,
merged in the order the files are given, later wins. `PAA_COMPOSE_FILE` stays as the
single-file spelling, because it is how the deployment tests point the script at a
compose file they control; a `--compose` flag on the command line takes precedence
over it, because a flag is the more specific of the two and because the variable
names ONE file while the flag can name the several a merged stack is made of.

### Why

The gVisor stack the documentation tells operators to run is a **merged** stack:

```
docker compose -f docker-compose.yml -f docker-compose.gvisor.yml up -d
```

Two files, one stack. The script read exactly one file, so it planned half of it —
and the two halves agreed only by **accident**, because the override's `paa` service
declares no `image:` of its own. Nothing tested the accident. The next person who
added an `image:` to the override got a deployment that fetched the base file's
image and ran the override's, with a green test suite saying the script and the
compose files were in step.

The fix is not to read the override and hope. It is to read the **merge**, because the
merge is what `docker compose up` will run.

### The subset that is implemented

Compose's merge is a deep, well-specified algorithm, and this script does not
re-implement it. It implements the subset the repository's compose files use:

* the files are read in the order they are given;
* a service is a **map**, and the maps are merged key by key: a later file's `image:`
  replaces an earlier file's, and a later file that declares no `build:` leaves the
  earlier file's `build:` in place;
* a service only one of the files declares is taken from that file;
* the plan lists the services in the order they first appear;
* a service behind a `profiles:` key is **kept in the plan**, because it is still
  an image the compose file names and the rules of this ADR — the digest pin above
  all — apply to it. The **fetch** decides: a profile that is not enabled is not a
  service `docker compose up` runs, so its image is not fetched, and the script says
  so on stderr *and* in the report rather than dropping it silently — an image that
  was not fetched is an image the daemon has to pull on a day when nothing retries
  it. `COMPOSE_PROFILES` — Compose's own variable — says which profiles the operator
  has enabled, and a service behind one of them IS fetched.

  Compose's rule, which this implementation now follows exactly, is that a
  **non-empty** profile list gates a service: `profiles: []` gates nothing, and
  `docker compose config --services` lists such a service. The reader therefore
  reads the *list* — in its block form and in its flow form — rather than the
  *presence* of the key. A `profiles:` that is not a list at all (a scalar, or a
  key with no value) is **refused**: Compose refuses such a file itself
  (`services.gpu.profiles must be a array`), so a reader that guessed would be
  planning a stack that cannot run.

  **How Compose merges a `profiles:` list across files: it CONCATENATES them.**
  Measured against `docker compose config`, v5.6.0: a service with `profiles: [p]`
  in the base file and `profiles: [q]` in the override is started by
  `--profile p` **and** by `--profile q`. The merged list is `[p, q]`. This ADR
  previously said that Compose *replaces* the key when a later file declares it,
  which is wrong; the script's rule (the merged list is the union of the files'
  lists) was right for the wrong reason, and is now right for the right one. The
  "Unknown" of the first amendment is closed: it was answerable with two commands.

  (The first amendment recorded the merge rule as *Unknown* and the replacement
  as the stated reason. Both are corrected here, with the measurement.)

  **A file whose every service is profile-gated is no longer refused as an empty
  plan.** It used to be, because the reader dropped those services and the plan
  came out empty. The plan no longer drops them, so the plan is not empty, and
  the reason for the refusal — *reporting success would be reporting that
  nothing was checked* — no longer holds: the images of those services **were**
  checked (the pin rule ran over them). The script reports them as not fetched
  and exits zero, which is what the stack does: with no profile enabled, it runs
  nothing.

The `build:` rule is what makes the gVisor override work at all. The override's `paa`
service declares a runtime and an environment block and no image and no build, so the
merged `paa` is the base file's built image with the override's runtime on top. A
merge that dropped the earlier `build:` would plan `paa` as a **pull** of an image
that is not on any registry.

This subset was checked against the real thing — `docker compose config`, v5.6.0 —
on every shape the repository's files use: the base file's `build:` survives an
override that says nothing about it, a later file's `image:` replaces an earlier
file's, a service only one file declares is in the stack, and a profile-gated
service is not in the model Compose runs (it is, with the profile enabled). That
cross-check is a verification of the subset, not a test: the deployment suite is
dependency-free and does not require the Docker CLI, and the parity test still
compares two hand-rolled readers rather than Compose.

### What is refused, and why a refusal is the right answer

Ten shapes are outside the subset, and each is an **error** with its own message,
before a byte is fetched:

* Compose's `extends:` — the image would be inherited from another service, and the
  script does not resolve inheritance;
* a YAML anchor or an alias — an unresolved reference would be pulled as the literal
  text of the alias, and an anchored service key would make the reader attribute the
  keys below it to the *wrong service*;
* the YAML **merge key** (`<<:`) — it imports a whole map into another one, and the
  reader does not resolve it: a service whose `image:` comes from a merged map has
  no image here, and it would be **dropped from the plan, silently**;
* a multi-line `image:` — the reader reads one line per image, and a folded or a
  literal block would be read as the text of its indicator;
* a custom YAML tag — the script does not interpret YAML tags, so it cannot tell what
  the tagged value is;
* the top-level `include:` — the script does not follow it, and the services it names
  would be missing from the plan;
* a `profiles:` that is not a list — Compose refuses such a file itself, so the
  reader refuses it rather than guessing which services it gates;
* **a mapping written as a flow mapping** — a service whose map is on one line
  (`postgres: {image: …}`) and a `services:` map on one line (`services: {…}`). Compose
  accepts both, and the reader cannot see a single key of either, so a reader that
  ignored them would plan a stack that is **missing a service, silently**;
* **a service whose keys are not at the reader's indent** — the reader reads a
  service's keys at four spaces, and a service written any other way could hide an
  `image:` from the plan. Compose accepts such a file, so the reader refuses it
  rather than dropping the service from the plan.

**The refusal is on the construct, not on the spelling of a line.** This is the
distinction the wave-1 review turned up, and it is now the rule the reader
follows:

* the `include:` refusal is bound to the **key**, so the block form
  (`include:` on a line of its own), the flow form (`include: [other.yml]`) and the
  scalar form (`include: other.yml`) are all refused — the flow form is the one the
  compose specification wants, and it is the one an operator is most likely to
  write;
* an **alias** is refused wherever it appears under `services:` — the merge key is
  an alias in a third place, and an alias on a key the reader does not read is still
  a reference it cannot resolve;
* a **key** is read as a key: `image : x` and `"image": x` are the key `image`, the
  way Compose reads them. A reader bound to the spelling `^    image:` misses both,
  and a missed `image:` is a service dropped from the plan, silently — the same
  failure as the merge key, with the same fix.

**The line between a refusal and an ignorance.** A shape that **Compose refuses
itself** — a service written as a plain scalar (`postgres: echo hi`), a `profiles:`
that is not a list, an `image:` that is not a string — is not a hazard: no
deployment can run such a file, so the reader's answer about it does not matter.
A shape that **Compose accepts** and the reader does not implement is a hazard, and
it is refused. That line is what the refusal set is drawn along.

A refusal is the right answer because the alternative is **silent**. A plan that is
wrong fetches an image the deployment does not run and misses one it does, and nothing
in the output says so: the list looks right, the build is green, and the deployment
discovers the difference when it pulls an image that was never fetched — on a day when
nothing retries it.

The exit status says which kind of failure it is: **1** is a refusal and **2** is a
usage error (an argument the script was given wrong). The tests assert the code, not
merely "non-zero", because a test that cannot tell the two apart cannot tell a
refusal from a typo.

### One small alignment, in the same direction

The reader now strips **trailing whitespace** from an `image:` value. It did not
before, and the consequence was a pull reference with a space on the end of it — a
reference no registry has ever heard of, and one the `--list` output cannot show,
because the list is parsed. The parity test's own reader already stripped it, so the
two readers disagreed on a shape neither of them had a test for. They agree now, and
the shape is in the corpus.

### A key is read as a key

The reader used to match the **spelling** of a key — `^    image:` — and a key
Compose reads as `image` but that is spelled `image :` or `"image":` was missed
entirely. A missed `image:` is a service with no image, and a service with no image
is **dropped from the plan**: the deployment pulls it, the script never fetched it,
and every line of the script's output said the deployment was covered. Compose
accepts all three spellings and reads them as the same key (measured against
`docker compose config`, v5.6.0), so the reader now reads a **key**: the text up to
the first colon, with the spaces around it and the quotes around it removed.

The same rule is what closed the two holes the wave-1 review found: the `include:`
refusal is now bound to the key rather than to a line, and the merge key is refused
wherever it can inject keys into a map the reader reads — the document root, the
`services:` map, and a service's own map. A merge key deeper than that (inside an
`environment:` block, say) cannot change the image set, so it is not refused; the
refusal set is a list of shapes that change the *plan*, not of every YAML
construct.

### What it costs

* **A second thing to keep in step with Compose.** The subset above is a *summary*
  of Compose's merge, and Compose's merge is not frozen. If Compose changes how it
  merges a service map, this subset does not follow automatically — it follows when
  somebody notices, and the parity test is what makes the noticing cheap.
* **A refusal set to maintain.** Ten shapes are refused today; an eleventh compose
  feature that changes the image set has to be added to that list, deliberately, or it
  becomes a silent wrong answer again. The list is written down here and in the
  reader's own comments for exactly that reason. The list is a list of **shapes**,
  and a shape has more than one spelling: the corpus in the deployment tests carries
  every spelling of every refused shape, because a refusal bound to one spelling is
  a refusal with a hole in it, and the hole was found twice in one wave.
* **A flag, and the precedence between it and the environment variable.** Two ways to
  name the same thing is one more rule, and the rule is that the command line wins.
* **A profile list to read, in two spellings.** The block form and the flow form.
  The cost is one more shape in the reader, and the price of not paying it was a
  service Compose starts and the script does not fetch.

### What was deliberately NOT done

* **The parity test still compares two hand-rolled readers, not Compose.** It is a
  shape lock on the current files, and it says so in its own docstring. The obvious
  way to close that gap — `docker compose config` — would make `--list` require the
  Docker CLI, and the report on a host with no Docker at all is one of the reasons
  `--list` is a gate in the first place. **Recorded, not fixed.**
* **The Dockerfile base images are still not in the plan.** They are fetched by
  `docker build`, not by this script, and the scope above did not change: the plan is
  the compose files' services. **Recorded, not fixed.**
* **`PAA_COMPOSE_FILE` was not turned into a list.** It stays the single-file
  spelling, and the several-file case is the flag. One spelling per shape, and the
  shape an operator reaches for is the one that reads like the command they are
  running.
