# The attested change bundle (`paa-attest-bundle/1`)

**Status: built (SPEC 2 §3.1 C7.2, H3.1 attested change bundle).** `paa attest
build` gathers the mandate, the diff, the commands run, the tests actually run,
the approvals and a hash chain over all of it, signs the chain root with
Ed25519, and leaves a **self-contained directory** a reviewer can verify
offline on a clean machine: `paa verify <dir>`. The builder is
`src/paa/attest/bundle.py`, the verifier is `src/paa/attest/verify.py`, and
the format facts both sides share live in `src/paa/attest/format.py`. The
command entry points are `src/paa/cli/attest.py`.

The bundle is the spec's answer to "agents mostly fail by succeeding wrongly":
one artifact that says *who asked for this work*, *what the change is*, *what
actually ran* (and whether anybody *witnessed* it, or it is merely asserted),
*who signed off*, and *that these bytes are the ones that were signed* —
checkable without the producer's database and without trusting the producer.

## Where bundles live

`$PAA_HOME/attest/<id>/` (or `--out`). The directory is the artifact; copy it,
attach it, `tar` it — verification reads only what it contains.

## The directory shape

```text
<bundle-dir>/
  manifest.json                     the signed index
  blobs/<aa>/<bb>/<sha256>.bin      content-addressed payloads, raw
  staging/                          the CAS writer's scratch (absent after a copy;
                                    the verifier never needs or creates it)
```

Every payload — the six sections and any embedded observe bundles — is stored
by the cold lake's content-addressing contract
(`paa.storage.coldlake.cas.ContentAddressedStore`, ADR-0004): a blob's name
**is** the SHA-256 of its raw bytes, so every read can verify what it got.
Blobs are written raw (`.bin`), not zstd-compressed: a transparency artifact
should be inspectable with `sha256sum` and a text editor.

## The manifest

```json
{
  "schema": "paa-attest-bundle/1",
  "phase": "final",
  "bundle_id": "9f3c…",
  "created_at": "2026-11-25T02:51:07.412+00:00",
  "tool_version": "4.1.0",
  "sections": [
    { "name": "mandate", "sha256": "…" },
    { "name": "diff", "sha256": "…" },
    { "name": "commands", "sha256": "…" },
    { "name": "tests", "sha256": "…" },
    { "name": "approvals", "sha256": "…" },
    { "name": "evidence", "sha256": "…" }
  ],
  "blobs": [
    { "sha256": "…", "bytes": 598, "role": "evidence:observe:0f1e…" },
    { "sha256": "…", "bytes": 505, "role": "section:mandate" }
  ],
  "chain": ["<step-0>", "<step-1>", "…"],
  "chain_root": "<last step>",
  "signature": {
    "algorithm": "ed25519",
    "key_id": "sha256 of the public key's base64 text",
    "public_key_b64": "…",
    "value_b64": "…",
    "signed_over": "sha256 of the exact signed bytes"
  },
  "notes": { "workspace": "/abs/path", "diff_since": null, "signing_secure": true }
}
```

The **chain** walks the six sections in the fixed order above. Step *i* is
`sha256(canonical_json({prev, section, sha256}))` folding from `"genesis"`; the
root is the last step. The **signature** is Ed25519 over the canonical JSON of
the signed header (schema, bundle_id, created_at, tool_version, sections,
chain_root), via `paa.marketplace.signing` — the same signing the marketplace
uses, reuses by design.

## The sections

| Section | Contents | Honesty rules |
|---|---|---|
| `mandate` | Who asked (asserted, `-asserted` naming), basis, the content-addressed policy version, scope, and the goal. | Identity is *asserted*, never authority (the mandate module's own rule). An empty goal is refused at build. |
| `diff` | git's own view (head, status, diff, commits since `--diff-since`) plus the workspace's full content manifest (path/size/SHA-256 per file). | `git.available=false` with a reason is an honest answer. The manifest is what `paa verify --workspace` checks a checkout against. |
| `commands` | One row per command: argv, `source`, `exit_code`, `state`, `evidence_sha256`. | `observed` rows cite an embedded T1 bundle; `asserted` rows say so in the note. Never collapsed. |
| `tests` | The same row shape, for test commands. | `command_green` is `exit_code == 0` — "the test command exited zero", no stronger claim. A bundle whose tests are all asserted carries a warning in the verdict. |
| `approvals` | Sign-offs: approver (asserted), decision (`approved`/`rejected`/`changes_requested`), note, time. | Rejections are *recorded*, not refused — an attested rejection is more evidence, not less. |
| `evidence` | The embedded `paa-observe-bundle/1` bundles: run id, digest, tier, command, outcome. | Only `phase="final"` observe bundles are accepted — a `started` bundle is the observer's crash marker, not evidence of a completed run. |
| `evidence` (recorder rows) | Since C7.3, the evidence section also carries `paa-record/1` flight records: run id, digest, event count, chain root. | Embedded by the recorder at close; the blob is the record as of the attestation point, and the live record's `record_finalised` event cites the blob's digest, so the extension is visible. |

## The recorder's rows (C7.3, additive)

`paa record run` builds its bundle through two additive inputs to the
builder, both held to the observed/asserted rule:

- **`extra_evidence`** — arbitrary artifact files stored as
  content-addressed blobs with caller-supplied row fields (which must name
  their own `kind`). The recorder embeds its hash-chained flight record
  this way, as a `paa-record/1` evidence row carrying the event count and
  chain root.
- **`observed_command_rows`** — commands a flight record *witnessed
  executing* through the policy proxy. Each row cites the record blob's
  digest, and the builder **refuses any row whose `evidence_sha256` names
  no blob in the bundle** — an "observed" row must be backed by bytes the
  bundle actually carries, or the citation is exactly the quietly-false
  receipt this format exists to catch.

A bundle from the recorder therefore distinguishes three kinds of command
row: the harness command line itself (observed, T1, from the embedded
observe bundle), the mediated tool calls (observed, backed by the flight
record blob), and anything the operator asserted — same section, different
words, counted separately.

## The two-phase write

The manifest is written twice: a `phase="started"` skeleton before any
evidence is gathered, the `phase="final"` record after the signature. A
manifest still saying `started` is the crash marker for a build that never came
back, and `paa verify` refuses it. Writes are atomic (`<name>.tmp` + rename).

## Verification — the five doors

`paa verify <dir>` (offline; reads the bundle and nothing else):

| Check | What it proves | What breaks it |
|---|---|---|
| `manifest` | Parses, `paa-attest-bundle/1`, `phase="final"`. | A started skeleton, a foreign schema. |
| `blobs` | Every declared blob exists and hashes to its name. | **One flipped byte, anywhere.** |
| `chain` | The re-walked sections reproduce `chain_root`. | A substituted, renamed or reordered section. |
| `signature` | The Ed25519 signature verifies over the signed header with the embedded public key. | Any manifest edit after signing. |
| `key` | With `--trust <key>`: the embedded key **is** the pinned one. Without: reported `self-asserted`, never `trusted`. | A re-signed bundle from a different key. |

**Integrity and origin are separate facts, on purpose.** A bundle re-signed by
an attacker's key is *intact* (the signature covers exactly what was signed)
and *untrusted* (it fails the pin) at the same time. Without a pin the verdict
says `self-asserted key` — integrity checked, origin not claimed — because a
verifier that conflated the two would let anyone buy trust with a keygen.

The optional `--workspace <dir>` check recomputes that directory's content
manifest and compares it to the diff section's, naming the drift
(created/modified/deleted) instead of a bare mismatch — "verify on a clean
checkout" means *this checkout, shown to match or shown how it doesn't*.

## The signing key

`$PAA_HOME/attest/attest-key.json` (or `--key`): Ed25519, generated on first
use, mode `0600`. The private half never leaves the producer. Building under
the HMAC dev fallback (no `cryptography` package) is **refused** unless
`--insecure-fallback-signing` is passed — a symmetric "signature" anyone can
forge is worse than none, and the manifest labels the algorithm for every
verifier either way.

## `paa attest pr`

Emits the PR comment body — a markdown summary with the goal, the chain root,
the key id, the observed/asserted test split and the exact verification
command — to the console or `--out <file>`. It **posts nothing**: this runtime
has no network surface for posting, and a command that pretended to publish
would be exactly the quietly-false artifact the attestation exists to catch.

## A GitHub Action that verifies for real

```yaml
# .github/workflows/verify-attestation.yml — verify an attested bundle in CI.
# Pin the signer's public key (the reviewer's decision, not the producer's):
# store it as a repository secret ATTEST_SIGNER_PUBLIC_KEY_B64.
name: verify-attestation
on:
  workflow_dispatch:
    inputs:
      bundle:
        description: "Path or URL of the attested bundle directory/artifact"
        required: true
jobs:
  verify:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with:
          python-version: "3.12"
      - run: pip install -e .   # or: pip install paa
      - run: paa verify "${{ inputs.bundle }}" --trust "${{ secrets.ATTEST_SIGNER_PUBLIC_KEY_B64 }}"
      # Exit code is CI's verdict: 0 only when every door held.
```

## Consuming bundles programmatically

`paa.attest.verify.verify_bundle(dir, trust_key=..., workspace=...)` returns a
`VerificationResult` (named checks, summary facts) without touching a
database; `read_manifest` / `read_section` are the read-only accessors. The
builder (`paa.attest.bundle.build_bundle`) is producer-side and may touch the
runtime's settings — that asymmetry is the point.
