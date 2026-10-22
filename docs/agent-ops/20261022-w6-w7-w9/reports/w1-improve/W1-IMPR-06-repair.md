<!-- ORCH-DIGEST:START -->
agent: W1-IMPR-06 | scope: R-W1-301 (deploy/fetch-images.sh drops a service written at a level the reader does not read) | status: DONE
findings: FIXED 1 | REJECTED 0 | DEFERRED 0
blockers_major_open: none
code_commits: see §7 | report_commit: see §7
tests_added_or_strengthened: 3 (new) + 6 (corpus spellings added to an existing shape)
ownership_violations: none
confidence: high (the defect was reproduced on 90eda75, the fix is measured, and the reversal fails 9 cases)
<!-- ORCH-DIGEST:END -->

# Improvement report — W1-IMPR-06 (repair of R-W1-301)

**Task:** T-003 follow-up. One finding, one file pair, one proof.
**Write-set:** `deploy/fetch-images.sh`, `tests/deployment/test_fetch_images.py`,
`docs/adr/0030-deployment-image-contract.md`.
**Branch:** `ops/20261022-w6-w7-w9`. **Tree measured before the fix:** `90eda75`.

## 0. The finding, reproduced (BEFORE)

*Read at start:* `docs/agent-ops/20261022-w6-w7-w9/templates/STANDING-ORDERS.md` (binding),
`reports/w1-review/W1-DREV-04-delta.md` §4 R-W1-301 (in full),
`deploy/fetch-images.sh`, `tests/deployment/test_fetch_images.py`,
`docs/adr/0030-deployment-image-contract.md`, `templates/report-improvement.md`,
`COMMS.md` (last line read: the wave-1 `WAVE-START`, 2026-10-21T20:17:14Z, plus the
T-003 lines of 2026-10-21T21:55:43Z / 22:10:27Z / 22:15:50Z and the W1-IMPR-04
`DONE` of 2026-10-22T02:52:28Z — the line that claims R-W1-301 FIXED).

The claim under test: the previous agent reported the "wrong-indent service" shape as
**FIXED**. The code says otherwise. Three measurements, on the tree at `90eda75`.

### 0.1 The mixed case — the one in the brief

```
$ cat /tmp/opencode/impl06/MIXED.yml
services:
  postgres:
    image: postgres:16@sha256:0000000000000000000000000000000000000000000000000000000000000000
   qdrant:
     image: qdrant/qdrant:v1.19.2@sha256:1111111111111111111111111111111111111111111111111111111111111111

$ PAA_COMPOSE_FILE=/tmp/opencode/impl06/MIXED.yml bash deploy/fetch-images.sh --list
pull  postgres:16@sha256:0000000000000000000000000000000000000000000000000000000000000000  (postgres)
exit=0
```

`qdrant` is at **three** spaces. It is dropped from the plan, the plan is not refused,
and the exit status is **0**.

### 0.2 The single-service case — refused for the wrong reason

```
$ PAA_COMPOSE_FILE=/tmp/opencode/impl06/SINGLE3.yml bash deploy/fetch-images.sh --list
 fail no images found in /tmp/opencode/impl06/SINGLE3.yml — the compose file(s) name no
 fail service with an image: or a build: section. There is nothing to fetch,
 fail and reporting success would be reporting that nothing was checked.
exit=1
```

Exit 1, but the message is the **empty-plan** message: the service was not *refused*,
it was *not seen*, and the refusal came from the plan being empty. That is the
accident the previous agent's test exercised.

### 0.3 The merged case — the same hole with a **zero** exit status

The one the previous agent never ran: a services map at three spaces **beside** a file
the reader can read.

```
$ bash deploy/fetch-images.sh --compose /tmp/opencode/impl06/SINGLE3.yml \
    --compose /tmp/opencode/impl06/NORMAL.yml --list
pull  redis:7@sha256:0000000000000000000000000000000000000000000000000000000000000000  (redis)
exit=0
```

The plan is non-empty (the second file), so the empty-plan refusal does not fire, and
the three-space file's service is **silently dropped** — exit **0**, a green gate, and
a service the deployment runs that was never fetched.

### 0.4 What Compose thinks of these files

`docker compose -f <copy> config`, v5.6.0, read-only, on copies under `/tmp`:

| file | shape | Compose |
|---|---|---|
| S1 | services map at **one** space | **ACCEPT** — `postgres: image: a@sha256:…` |
| S2 | services map at two spaces (normal) | ACCEPT |
| S3 | services map at **three** spaces | **ACCEPT** — `postgres: image: a@sha256:…` |
| S4 | services map at **four** spaces | **ACCEPT** — `postgres: image: a@sha256:…` |
| S5 | two services at two and three spaces (the brief's mixed file) | REJECT — `go-yaml load error … did not find expected key` |
| S6 | a service at five spaces after a normal one | REJECT — `mapping values are not allowed in this context` |
| S7 | a service at two, its keys at three | **ACCEPT** |
| S8 | a service at two, its keys at five | **ACCEPT** |
| S9 | a service at two, its keys at six | **ACCEPT** |
| S10 | a service at three, its keys at four | **ACCEPT** |
| S11 | a nested `build:` block at six | ACCEPT |
| S12 | services map at **five** spaces | **ACCEPT** |
| S13 | a service at three, its keys at four | **ACCEPT** |
| S14 | services map at one space, keys at two | **ACCEPT** |

So the hazard is exactly ADR-0030's line: a shape **Compose accepts** and the reader
does not implement. S3, S1, S4, S12 are services the deployment runs and the reader
cannot see at all; S7/S8/S9 are services the reader sees but whose keys it cannot see.
S5 is refused by Compose itself, but the reader still must not drop a service from it
silently — and the *merged* form of S3 (0.3) is the case that ends in a zero exit.

## 1. Finding-by-finding disposition

| Finding | Severity | Disposition | What I did / why rejected or deferred (with evidence) | Commit |
|---|---|---|---|---|
| R-W1-301 | MAJOR | **FIXED** | The reader's "can I see this service?" test now covers the level of a **service header**, not only the level of a service's **keys**; both readers carry the same rule; the corpus carries the missing spellings; the mixed case and the merged case are refused loudly, and the refusal names the file and the shape. Evidence: §0 (before) and §5.2 (after) — the mixed case goes from `exit=0` to `exit=1` with the file and the level named; the reversal fails 9 cases. | see §7 |

No other finding was in scope. R-W1-302…R-W1-309 belong to other files and other
owners; the two I read (R-W1-306's count, R-W1-307's row format) are unaffected by
this change and are not touched (see §6).

## 2. Code changes (per file, with rationale; root-cause level, no band-aids)

### 2.1 `deploy/fetch-images.sh` — the reader's rule

**The root cause.** The reader reads a services map as: *a service is a key at
two-space indent, and a service's keys are keys at four-space indent*. The previous
amendment made the rule about the level of a service's **keys** ("no key at four
spaces, but a key at some other indent" → refuse the service at its end). It left the
other half open: a service whose **header** is at a level the reader does not read as
a service. The reader never opens that service, so there is no service to refuse at
the end of it, and the service is dropped from the plan — silently, and with a zero
exit status whenever a file beside it keeps the plan non-empty.

**The fix, in the reader's own words** (`deploy/fetch-images.sh`, the level rule):

* a key at **four** spaces is a key of the open service — unless **no** service is
  open, in which case the services map itself is written at the wrong level and the
  key belongs to a service the reader never opened (refused at the line);
* a key at **two** spaces is a service header (the existing rule);
* a key at **another** level is a level the reader does not read: it is either a
  service the reader cannot see, or a key of a service it cannot see, and one of
  them may be the `image:` — so the file is **refused at the line** (this is the half
  that was open);
* a level **deeper than four** is content nested under a key the reader has read (a
  `build:` section, an `environment:` map) and cannot hide the service's own `image:`,
  because Compose reads that as a key of the service — so it is allowed, and the
  end-of-service rule decides whether the service's own keys were readable.

The reviewer's recipe is kept and made explicit: the end-of-service rule is now
`!saw_key && saw_key_any_indent` — *no key at four spaces, but at least one key at
some other indent* — with `saw_key_any_indent` (the old `other_level`) renamed to the
reviewer's vocabulary so the two names cannot drift apart. The flag is set for content
at a level the reader does not read as a service key (a level of two that is not a
service header, and anything deeper than four).

**The refusals are loud and specific.** Two new messages, both naming the file, the
level and the key:

```
<file>: the services map has a key at 3 spaces (qdrant), a level this reader does not understand.
This script reads a service at two spaces and its keys at four spaces. A key at
another level is a service this reader cannot see, or a key of a service it
cannot see — and one of them may be the image, so the file is refused
rather than read in part.
```

and, for the level-four-with-no-service shape:

```
<file>: the services map has a key at 4 spaces (postgres), and no service for it: a level this reader does not understand.
```

Both carry the phrase **"does not understand"** (the word the corpus pins), and both
name the file — `display(FILENAME)`, the path as the operator wrote it.

**Refusals kept apart.** The two new refusals are the *level* refusals; the
end-of-service refusal (a service whose keys are at another level) keeps its own
message and its own wording, so the two shapes stay distinguishable in the output.

**No band-aids.** No flag disables the path; no construct is special-cased per
spelling; the rule is one level rule plus the reviewer's end-of-service rule. The
script's own header comment (the file's contract with its reader) was updated to name
the shape it now refuses.

### 2.2 `tests/deployment/test_fetch_images.py` — the independent oracle

`_services_of` — the Python reader written from the same words and not from the same
code — now carries **the same rule**: the same level classification, the same
"nested content is allowed, a level the reader does not read is refused" reasoning,
and the same `saw_key` / `saw_key_any_indent` pair. The two readers are compared by
the corpus lock, and a differential run over 49 shapes (below) reports **0
disagreements**.

The oracle is *not* a copy of the script's code: it is a different language, a
different control flow, and it was written from the rule, not from the diff. The
corpus is what keeps them honest.

### 2.3 `docs/adr/0030-deployment-image-contract.md` — the durable record

* The refusal-list bullet now names the whole shape ("a service written at a level the
  reader does not read"), and records that the previous rule was about the keys only.
* A new section, **"The level of a service is a level too (third amendment)"**, states
  that the previous paragraph was half true, gives the before/after transcripts, states
  the rule, and lists what Compose accepts (measured, v5.6.0).
* The section that claimed the fix no longer stands alone: it is corrected in place
  and cross-referenced from the new one.

## 3. Test changes

* **The corpus carries the missing spellings.** The shape
  *"a service whose keys are not at the reader's indent"* was renamed **"a service at
  a level the reader does not read"** — the shape is the same one, and the name now
  covers both halves — and it carries **nine** spellings instead of three: the keys at
  three, five and six spaces (the pre-existing three), the services map at one, three,
  four and five spaces, a service at three spaces whose keys are at four, and the
  **mixed file**. Each spelling is a separate test case (`[shape][index]`), so a
  missing one is a missing test rather than a missing assertion inside a passing one.
* **Three new tests**, each for a consequence rather than for the rule:
  * `test_a_service_at_another_level_is_refused_with_its_shape_named` — the
    single-service three-space case: exit 1, the shape's message, the file named, and
    **not** the empty-plan message.
  * `test_a_mixed_file_does_not_drop_the_service_at_another_level` — the brief's
    mixed file: exit 1, the shape's message, the key it could not read named, and the
    image not reported as planned.
  * `test_a_file_at_another_level_beside_a_readable_one_is_refused` — the **merged**
    case, which is the one that ended in a zero exit status: two files, one of them at
    three spaces, and the plan would otherwise be non-empty.
* Every new test asserts **no pull** happened (`stub.pulls() == []`): a refusal that
  has already fetched something is not a refusal.
* The existing `test_a_service_at_another_level_cannot_hide_an_unpinned_image` is
  untouched — the shape it pins (the keys at three spaces) still refuses, with the
  same message.

## 4. Optimizations applied and evidence of benefit (and ones declined, with reason)

**Applied.**

* The rule is **one comparison per line** on an integer the reader already counts;
  the cost is the same as the flag it replaces, and the plan is unchanged for every
  file the reader could already read (the repository's own merged stack still plans
  six rows, exit 0 — §5.2).
* The refusal message names the key, so the operator finds the line without opening
  the file. The key is extracted by the same "key as YAML reads it" rule the reader
  already uses (`normalise_key`), truncated at 40 characters, with tabs replaced by
  spaces so the message cannot break the tab-separated row it travels on.
* The two new refusals were folded into **one** function with a flag for the second
  shape, rather than two near-identical functions: the "a line" versus "a key"
  wording is decided once.

**Declined, with reason.**

* **A real YAML parser.** PyYAML is not a dependency, and ADR-0030 rejects making
  `--list` depend on it. The reader stays a text reader, and the rule stays a rule
  about levels.
* **Opening a service at any level** (i.e. treating every key as a possible service
  header). Measured: the "build section" shape
  (`build:` at four, `context:` at six, `image:` at four) would then attribute the
  `image:` to a service named `context`, and the plan would say *build* for `paa` and
  *pull* for `context`. Rejected on the measurement, not on taste.
* **Refusing every key deeper than four.** A key at five or six spaces is content
  nested under a key the reader has read, and it cannot hide the service's own
  `image:`. Refusing it would refuse legitimate compose files (a `build:` block
  written at five spaces, a `profiles:` list at six). The end-of-service rule
  decides, and it is the reviewer's rule.
* **Refusing the level-five-and-deeper service header** (S6). It is indistinguishable
  from nested content by level alone, and Compose **rejects** such a file
  (measured), so by ADR-0030's own line it is not a hazard. It is recorded in §6 as
  the one shape of the class left open, with the reason.

## 5. Commands run and results (scoped)

### 5.1 The scoped checks

```
[RAN] .venv/bin/python -m pytest tests/deployment -q          -> 118 passed, exit 0
[RAN] .venv/bin/python -m ruff check tests/deployment         -> All checks passed!
[RAN] bash -n deploy/fetch-images.sh                          -> clean, exit 0
[RAN] .venv/bin/python -m mypy src                            -> Success: no issues found in 137 source files
```

No container was started, stopped or pruned; no `docker compose up`; no fixed port;
`docker compose config` was used **read-only on copies under `/tmp`** only.

### 5.2 The proof, AFTER

```
$ PAA_COMPOSE_FILE=/tmp/opencode/impl06/MIXED.yml bash deploy/fetch-images.sh --list
 fail /tmp/opencode/impl06/MIXED.yml: the services map has a key at 3 spaces (qdrant), a level this reader does not understand.
 fail This script reads a service at two spaces and its keys at four spaces. A key at
 fail another level is a service this reader cannot see, or a key of a service it
 fail cannot see — and one of them may be the image, so the file is refused
 fail rather than read in part.
exit=1

$ PAA_COMPOSE_FILE=/tmp/opencode/impl06/SINGLE3.yml bash deploy/fetch-images.sh --list
 fail /tmp/opencode/impl06/SINGLE3.yml: the services map has a key at 3 spaces (postgres), a level this reader does not understand.
 fail This script reads a service at two spaces and its keys at four spaces. A key at
 fail another level is a service this reader cannot see, or a key of a service it
 fail cannot see — and one of them may be the image, so the file is refused
 fail rather than read in part.
exit=1

$ bash deploy/fetch-images.sh --compose /tmp/opencode/impl06/SINGLE3.yml --compose /tmp/opencode/impl06/NORMAL.yml --list
 fail /tmp/opencode/impl06/SINGLE3.yml: the services map has a key at 3 spaces (postgres), a level this reader does not understand.
 …
exit=1
```

and the files the reader can read are unaffected:

```
$ PAA_COMPOSE_FILE=/tmp/opencode/impl06/NORMAL.yml bash deploy/fetch-images.sh --list
pull  redis:7@sha256:0000000000000000000000000000000000000000000000000000000000000000  (redis)
exit=0

$ bash deploy/fetch-images.sh --compose docker-compose.yml --compose docker-compose.gvisor.yml --list
build paa/runtime:4.1  (paa-init)
build paa/runtime:4.1  (paa)
pull  postgres:16@sha256:ca0bd484cb98bf4b24eb1010e73fb3fcbd6714d240fbc1a10eea5b7dbecb641d  (postgres)
pull  qdrant/qdrant:v1.19.2@sha256:b7b0444c4c351c970b98e90a6f89c2ee4287c65b44e52b4cb503fa5b2aa927ad  (qdrant)
pull  redis:7@sha256:4fa24486b8bcca8eec45ee0eb166edc674795e53a2b53d1a9ef263eecebaac85  (redis)
pull  chrislusf/seaweedfs:4.48@sha256:4e61d15fd35994cb1e43e1e553dff106794841fd9a99ade2fc8c8bfce4d7872d  (seaweedfs)
exit=0
```

### 5.3 The two readers agree (differential)

A scratch harness (under `/tmp`, not committed) ran the script's reader and the
test's oracle over **49 shapes** — every level from one to six, the mixed files, the
nested maps, the profile forms, the multi-line values, the comments, the CRLF, the
tabs, the flow mappings, the merge key, the anchors, the sequences, the empty files —
and compared the reader's contract (the rows, and the refusal):

```
[RAN] .venv/bin/python /tmp/opencode/impl06/diff_readers.py   -> 49 shapes, 0 disagreements
```

### 5.4 The reversal

With the **pre-fix** script (`git show HEAD:deploy/fetch-images.sh`) and the new test
file:

```
[RAN] .venv/bin/python -m pytest tests/deployment --tb=no
        -> 9 failed, 109 passed in 1.53s
        FAILED …[a service at a level the reader does not read[3]]
        FAILED …[a service at a level the reader does not read[4]]
        FAILED …[a service at a level the reader does not read[5]]
        FAILED …[a service at a level the reader does not read[6]]
        FAILED …[a service at a level the reader does not read[7]]
        FAILED …[a service at a level the reader does not read[8]]
        FAILED test_a_service_at_another_level_is_refused_with_its_shape_named
        FAILED test_a_mixed_file_does_not_drop_the_service_at_another_level
        FAILED test_a_file_at_another_level_beside_a_readable_one_is_refused
```

Six corpus spellings and three tests fail without the fix; with it, 118 pass. (The
pre-fix script was restored by copying the file back from `/tmp` and the restore was
verified byte-for-byte — no `git checkout` was used.)

## 6. Residual risks and anything the delta reviewer should check

1. **The recorded residual still stands: two hand-rolled readers can agree on a wrong
   rule.** This change narrows the hole (nine spellings of the shape, a 49-shape
   differential) but does not close it: the only independent authority is Compose
   itself, and `--list` must not require the Docker CLI. ADR-0030 records this.
2. **One shape of the class is still open, deliberately.** A service header at **five
   spaces or deeper** that follows a service the reader *has* opened is
   indistinguishable from nested content by level alone, so it is not refused.
   Compose **rejects** such a file (measured, §0.4 S6), so by ADR-0030's own line it
   is not a hazard — but if a future YAML parser accepted it, the reader would drop
   the service silently. The alternative (refusing every key deeper than four) would
   refuse legitimate nested maps; the trade is recorded here rather than hidden.
3. **`docs/deployment/docker.md` is one sentence behind the ADR** (read-only for me;
   it is in my brief's FORBIDDEN list). It still describes the refusal as "a service
   whose keys are written at some level other than the four-space indent the reader
   reads — three spaces as much as six", which is true but incomplete: a service
   *written* at another level is refused too. The sentence to add is in the COMMS
   `FINDING` line. Nothing in it is false.
4. **The corpus's shape was renamed** ("a service whose keys are not at the reader's
   indent" → "a service at a level the reader does not read"), so the test ids in
   `tests/deployment/test_fetch_images.py` changed with it. The three pre-existing
   spellings keep their positions (indices 0–2) and their messages.
5. **The refusal messages are new strings.** A caller that matched the old message
   text would break; nothing in this repository does (the corpus matches the phrase
   "does not understand", which both new messages carry). The **exit codes did not
   change**: 1 for a refusal, 2 for a usage error.
6. **Not verified here:** the full suite (out of scope by the brief), a real
   deployment, and any YAML parser other than Compose's. The Compose measurements
   are from v5.6.0 on this host.
7. **Pre-existing, checked and *not* a disagreement:** a level-two line with two
   colons (`weird:key: value`) is read as a service with an inline value by both
   readers — the differential reports agreement for it and for the variant with no
   other service. I looked for the disagreement I expected there and it is not there.

## 7. Commits

| commit | files | what |
|---|---|---|
| `7532c9c` | `deploy/fetch-images.sh`, `tests/deployment/test_fetch_images.py`, `docs/adr/0030-deployment-image-contract.md` | the reader's level rule in both readers, the corpus spellings, the three tests, the ADR's third amendment |
| (docs, below) | this report | the report |

Trailers on both: `Task: T-003`, `Agent: W1-IMPR-06`, `Wave: 1`,
`Report: docs/agent-ops/20261022-w6-w7-w9/reports/w1-improve/W1-IMPR-06-repair.md`.

## 8. Out-of-scope findings

* **`docs/deployment/docker.md` is one sentence behind the ADR** (item 6.3 above) —
  the operator-facing refusal list names only the level of a service's *keys*. The
  file is in my brief's FORBIDDEN list, so it was not edited; a `FINDING` is posted in
  COMMS with the exact sentence to add.
* **The COMMS line of 2026-10-22T02:52:28Z (W1-IMPR-04) states that R-W1-301 is
  FIXED.** It was not, at that commit: the mixed case exited zero. The line is
  append-only, so the correction is this report and a `CORRECTION` line in COMMS.
