# The attested change bundle (`paa-attest-bundle/1`)

**Status: built (SPEC 2 §3.1 C7.2, H3.1 attested change bundle).** `paa attest
build` gathers the mandate, the diff, the commands run, the tests actually run,
the approvals and a hash chain over all of it, signs the header covering the
chain root, sections, blob index and key claims with the algorithm it
declares, and leaves a **self-contained directory** a reviewer can verify
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
    "claims": {
      "algorithm": "ed25519",
      "key_id": "sha256 of the public key's base64 text",
      "public_key_b64": "…"
    },
    "value_b64": "…",
    "signed_over": "sha256 of the exact signed bytes"
  },
  "notes": { "workspace": "/abs/path", "diff_since": null, "signing_secure": true }
}
```

The **chain** walks the six sections in the fixed order above. Step *i* is
`sha256(canonical_json({prev, section, sha256}))` folding from `"genesis"`; the
root is the last step. The **signature** is over the canonical JSON of the
signed header: `schema`, `bundle_id`, `created_at`, `tool_version`, `sections`,
`chain_root`, `blobs`, `notes` and `signature_claims`. `signed_over` is the
SHA-256 of those exact bytes, and the stateless helpers in
`paa.attest.format` dispatch strictly on the *signed* algorithm claim — an
HMAC tag relabelled `ed25519` is never sent to the symmetric branch. The
outer `algorithm`, `key_id` and `public_key_b64` copies are conveniences;
`signature.claims` is the signed authority and the two must agree.

## The sections

| Section | Contents | Honesty rules |
|---|---|---|
| `mandate` | Who asked (asserted, `-asserted` naming), basis, the content-addressed policy version, scope, and the goal. | Identity is *asserted*, never authority (the mandate module's own rule). An empty goal is refused at build. |
| `diff` | git's own view (head, status, diff, commits since `--diff-since`) plus the workspace's full content manifest (path/size/SHA-256 per file). | `git.available=false` with a reason is an honest answer; a failed `status`/`diff` after HEAD resolves **refuses the build** rather than signing an empty clean diff. `--diff-since` is resolved through `rev-parse --end-of-options` to an object id before `git log`, so an option-looking token cannot make git write a file. An empty diff is reported as a warning, and every signed git caveat also appears in the `paa verify` verdict. |
| `commands` | One row per command: argv, `source`, `exit_code`, `state`, `evidence_sha256`. | `observed` rows cite an embedded T1 bundle; `asserted` rows say so in the note. Never collapsed. |
| `tests` | The same row shape, for test commands. | `command_green` is `exit_code == 0` — "the test command exited zero", no stronger claim. A bundle whose tests are all asserted carries a warning in the verdict. |
| `approvals` | Sign-offs: approver (asserted), decision (`approved`/`rejected`/`changes_requested`), note, time. | Rejections are *recorded*, not refused — an attested rejection is more evidence, not less. |
| `evidence` | The embedded `paa-observe-bundle/1` bundles: run id, digest, tier, command, outcome. | Only `phase="final"` observe bundles with `evidence_tier="T1"` and `resume="none"` are accepted — a `started` bundle is the observer's crash marker, and a resumed or lower-tier run is not a fresh witness. |

## The two-phase write

The manifest is written twice: a `phase="started"` skeleton before any
evidence is gathered, the `phase="final"` record after the signature. A
manifest still saying `started` is the crash marker for a build that never came
back, and `paa verify` refuses it. Writes are atomic against a **process crash**
(`<name>.tmp` + rename: a reader sees the old file or the new one, never a
splice). There is no file/parent-directory `fsync`, so this is not a
power-loss durability guarantee.

## Verification — the five doors

`paa verify <dir>` (offline; reads the bundle and nothing else):

| Check | What it proves | What breaks it |
|---|---|---|
| `manifest` | Parses, `paa-attest-bundle/1`, `phase="final"`. | A started skeleton, a foreign schema. |
| `blobs` | The signed index is an exact set: every entry exists, hashes to its name and matches its declared size, and no signed section or evidence blob is omitted, added or relabelled. | **One flipped byte, anywhere**, or a changed evidence count. |
| `chain` | The re-walked sections reproduce `chain_root`. | A substituted, renamed or reordered section. |
| `signature` | The claim fields agree with the signed copies, `signed_over` reproduces the signed bytes, and the signature verifies under the declared algorithm with the embedded public key. | Any manifest edit after signing, including an algorithm downgrade. |
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
use when the `paa[attest]` extra supplies `cryptography`, mode `0600`. Mode
`0600` is enforced on **load** too: a group/world-readable existing key is
refused with `chmod 600` in the message, because a key anyone can read makes
every later signature forgeable. The private half never leaves the producer.
An existing key must match the host's algorithm: a symmetric dev key cannot
be reused on a host that can sign Ed25519, because that is how a verifier is
tricked into checking the wrong scheme. Building under the HMAC dev fallback
is **refused** unless `--insecure-fallback-signing` is passed; the fallback is
labelled `hmac-dev-fallback` in the signed claims and `paa verify` refuses it
by default — anyone who can verify that bundle can forge it.

## Secrets are refused, not redacted into history

A bundle is portable and publishable, so high-confidence secret shapes
(provider API keys, GitHub/Slack tokens, private-key blocks and named
`*_SECRET`/`*_TOKEN`/`*_PASSWORD` assignments) in the goal, argv, approval
notes, git status/diff or an embedded observe bundle **refuse the build**.
The refusals name the *kind* found and never echo the value — error text is
itself a place secrets get copied into tickets. The producer must remove or
redact the material at source; silently rewriting a section or an embedded T1
bundle would break the very hash chain the artifact exists to prove.

## `paa attest pr`

Verifies the bundle first (integrity doors only, with no `--trust` pin) and
refuses with exit 2 if any door fails: a paste-ready summary of a started,
tampered or fallback-signed bundle would be a quietly-false artifact. For a
bundle that verifies, it emits the PR comment body — the goal, chain root,
key id, observed/asserted test split and exact verification command — to the
console or `--out <file>`. Every untrusted value is rendered as an inert,
single-line markdown code span with a fence longer than any backtick run in
the value; newlines and C0/DEL controls are flattened so no scalar can become
a heading, link, reference definition or HTML block. (U+2028/U+2029 and bidi
controls stay literal but inert inside the code span.) It verifies again
after rendering, because the renderer reads the manifest a second time. A
local writer who rewrites the directory between the first check and that
second check — or before the `--out` write — can still show a body whose
bytes differ from both checks; every field stays inert-escaped, but the
reviewer's final act is still `paa verify` immediately before publishing.
It **posts
nothing**: this runtime has no network surface for posting, and a command
that pretended to publish would be exactly the artifact the attestation
exists to catch.

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
      - run: pip install -e ".[attest]"   # or: pip install "paa[attest]"
      - run: paa verify "${{ inputs.bundle }}" --trust "${{ secrets.ATTEST_SIGNER_PUBLIC_KEY_B64 }}"
      # Exit code is CI's verdict: 0 only when every door held.
```

## Consuming bundles programmatically

`paa.attest.verify.verify_bundle(dir, trust_key=..., workspace=...)` returns a
`VerificationResult` (named checks, summary facts) without touching a
database; `read_manifest` / `read_section` are the read-only accessors. The
builder (`paa.attest.bundle.build_bundle`) is producer-side and may touch the
runtime's settings — that asymmetry is the point.
