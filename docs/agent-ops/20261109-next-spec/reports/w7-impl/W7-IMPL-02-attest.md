<!-- ORCH-DIGEST:START -->
agent: W7-ORCH | task: C7.2 (H3.1 attest) | status: DONE
code_commits: (this commit) | report_commit: (this commit)
files_changed: 38 (src/paa/attest/{__init__,format,bundle,verify}.py, src/paa/cli/attest.py, src/paa/cli/main.py, tests/attest/{__init__,test_bundle,test_verify,test_cli}.py, tests/conventions/test_import_conventions.py, docs/interfaces/attested-bundle-spec.md, this report, README.md, docs/README.md, AGENTS.md, docs citations x71 across 24 files)
contracts_changed: none (a new standalone artifact format, paa-attest-bundle/1; no ledger event types, no pipeline protocol keys)
tests: added 46 (tests/attest 46); full suite green, exit 0
ownership_violations: none
deferred_dependencies: none
out_of_scope_findings: 2
temporary_mitigation: no
deep_issue_flag: no
least_confident_about: the CLI import-time budget (+20-40 ms for the attest Typer surface, measured against a ~960 ms baseline; the chat-TUI startup budget tests are untouched and green — cli.main is not on their import path — but the number is machine-noisy)
confidence: high (gates green: full pytest 4,475/244/1x/0 exit 0, mypy 191 files clean, ruff clean, conventions incl. the docs lock green after re-anchoring)
last_comms_line_read: n/a
<!-- ORCH-DIGEST:END -->
# Implementation report — W7-ORCH — C7.2 (H3.1) the attested change bundle + `paa verify`

## 1. Task and acceptance criteria (restated)

SPEC 2 §6 C7.2 / ROADMAP H3.1: the attested change bundle — mandate, diff,
commands run, tests actually run, approvals, hash chain, offline-verifiable
without the producer's database, reusing Ed25519 signing + the cold lake's
content addressing; `paa attest pr` emits the PR body; the GitHub Action
template is optional. Done when (the spec's own row): *flip one byte →
verification fails; verify on a clean checkout with no PAA database* — plus
the campaign gates (full pytest, mypy, ruff).

## 2-4. Problem, root cause, approach

The evidence ladder had a T1 artifact (C7.1) but nothing a *reviewer* could
hold: evidence about a run lived in the producer's ledger, and a diff on a
PR proves nothing about who asked for it, what commands ran, or whether the
bytes changed since. Approach: a self-contained directory whose payloads are
cold-lake CAS blobs (name = SHA-256 of raw bytes, `.bin`, written by the
real `ContentAddressedStore` at build), a six-section hash chain
(mandate/diff/commands/tests/approvals/evidence) folded over
`canonical_json`, and an Ed25519 signature over the signed header via the
marketplace's own signing. The verifier reads the directory and nothing
else: a small read-only blob reader mirrors the CAS layout (the store's
constructor mkdirs its staging; a verifier must not mutate), and the
build→verify round trip is pinned by test so the mirror cannot drift.

The load-bearing honesty decision: **integrity and origin are separate
checks and are never collapsed.** The signature proves the bundle is what
was signed; `--trust` proves *who* signed it. Without a pin the verdict
says `self-asserted key` — a forged re-signing verifies as intact and
fails the pin, which is exactly the split a reviewer needs, and it is
pinned by a test that performs the attack.

## 5. What changed

- `src/paa/attest/format.py` (new): the leaf both sides share — schema id,
  section order, approval vocabulary, `chain_steps`, `sha256_bytes`. No
  store, no settings; the one place the format's identity lives.
- `src/paa/attest/bundle.py` (new): the producer side — `KeyFile` +
  `load_or_generate_key` (Ed25519, mode 0600, generated once; HMAC fallback
  **refused** without `--insecure-fallback-signing` because a symmetric
  attestation lends proof's vocabulary to something forgeable);
  `Approval` (closed decision vocabulary, asserted identity, rejections
  recorded not refused); `_git` (absolute-path git, honest `None` for
  no-git/not-a-repo, `--diff-since` resolution refused loudly);
  `build_bundle` (two-phase started→final manifest, evidence blobs first so
  rows can cite digests, chain, signature, atomic writes) and
  `read_manifest` (schema-tag refusal).
- `src/paa/attest/verify.py` (new): the consumer side — `verify_bundle`
  with five named doors (manifest/blobs/chain/signature/key), the optional
  `--workspace` checkout-equivalence check that names drift (+created/
  ~modified/-deleted), `read_section` for consumers, and the b64-vs-path
  trust-key resolver (the b64 alphabet contains `/`, so shape — not the
  presence of a slash — is the discriminator). No settings load, no store
  opens; the clean-checkout test pins the behaviour (PAA_HOME at a
  nonexistent path stays nonexistent).
- `src/paa/cli/attest.py` (new) + `main.py` wiring: `paa attest build`
  (goal/requester/diff-since/command/test-bundle/test-command/evidence-
  bundle/approval/key/out/json), `paa attest pr` (paste-ready body, posts
  nothing — no network surface exists to post through, and pretending
  would be the quietly-false artifact this feature exists to catch), and
  top-level `paa verify` (per-door output, `--trust`, `--workspace`,
  `--json`; exit 1 on any failed door). Producer modules import inside the
  command bodies (the observe CLI's own pattern) so `paa --version` pays
  only for the Typer surface.
- `docs/interfaces/attested-bundle-spec.md` (new): the format for
  consumers — directory shape, manifest, the sections' honesty rules, the
  five doors, the key lifecycle, the PR body, and the GitHub Action
  template that runs verification in CI.
- Tests (`tests/attest/`, 46): the tamper matrix (one flipped byte in a
  section blob / an evidence blob / the manifest; swapped payloads; a
  missing blob; a started skeleton; a foreign schema; a reordered section
  list — every variant fails, naming the door), the clean-checkout pair
  (no PAA database; the verifier never creates the CAS staging dir),
  integrity-vs-origin (pin passes, wrong key fails, unreadable pin fails
  rather than skips, forged re-signing is intact-but-untrusted), workspace
  drift (named, not bare), determinism (same inputs → same chain root),
  the crash marker (a build that dies leaves `phase="started"`), the key
  lifecycle, and the refusal paths — plus the CLI front door driving the
  real flow: a scripted fake harness under `paa observe` → `paa attest
  build` → `paa verify` (green, pinned, flipped-byte red).
- Conventions: the new `TYPE_CHECKING` block in `cli/attest.py` classed E
  with a measured-intent note; 71 stale `path:line` citations re-anchored
  after the `cli/main.py` (+8 lines) and `README.md` (+4 lines) shifts
  (difflib-mapped, pure insertions, spot-checked semantically: `:126`
  submit, `:303` doctor, `:105` `_build_runtime`); the C7.1 interface doc
  `observe-bundle.md` was never indexed in `docs/README.md` — indexed now,
  alongside the new spec.

## 7. Tests and gates

- `tests/attest`: 46/46 green.
- Full suite: **4,475 passed / 244 env-gated skipped / 1 xfailed, 0
  failures, exit 0** (C7.1 close: 4,429 → +46, exactly the new tests;
  4,720 collected, 272 s).
- `mypy src/paa`: 0 issues in 191 files (+5: the attest package and its
  CLI).
- `ruff check src tests`: clean.
- Conventions incl. the docs-reference lock: green (after the re-anchoring
  above).
- A pytest-9 quirk worth recording: `-q` added to the ini `addopts`'s own
  `-q` suppresses the summary line entirely — count with a single `-q`.

## 13. Out-of-scope findings

- `paa pipeline list` still does not surface `declares_tier` in its table
  (only `inspect` does) — the C7.1 report left it "for C7.2's bundle work
  to avoid churning the list format twice"; this wave did not touch the
  list format either (the attested bundle is not a pipeline), so the gap
  stands for C7.3+, now with two witnesses.
- `paa attest build` does not yet extract command rows from a ledger
  lineage: v1 rows are observed (embedded T1 bundles) or asserted. The
  lineage extractor wants the recorder's framing (C7.3) so journal rows
  and bundle rows agree on what a "command" is; pulling it forward would
  guess that contract early.
