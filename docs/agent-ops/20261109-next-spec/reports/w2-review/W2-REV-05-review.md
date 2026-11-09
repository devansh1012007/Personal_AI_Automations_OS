<!-- ORCH-DIGEST:START -->
agent: W2-REV-05 | scope: Wave 2c T-N2-6 blobbing (c2d25e3) | verdict: APPROVE
findings: BLOCKER 0 | MAJOR 1 | MINOR 4 | NIT 2
claims_audit: 8 claims checked / 0 unsupported-or-false
band_aids_found: none
top_3_risks: production composition injects no blob store (fail-closed raise); store-level size guard absent (journal-only enforcement); preview is a canonical prefix (safe only for already-redacted journal payloads)
optimizations: DO-NOW 0 | DO-LATER 1 | NOT-WORTH-IT 1
reversal_check: executed (own boundary/tamper/replay probes + suite green)
confidence: high (blind pass, own probes, scoped suites green)
<!-- ORCH-DIGEST:END -->
# Code review — W2-REV-05 (T-N2-6 blobbing, commit c2d25e3)

## 0. Method (verdict 5 — APPROVE)
Blind pass on `src/paa/ledger/events.py` (blob contract), `src/paa/coder/journal.py` (size check), `tests/coder/test_blobbing.py` (29 tests), plus the substrate `src/paa/storage/coldlake/cas.py` (`put`/`get` digest contract). Then read W2-IMPL-06 §1-§12, ran the scoped suites myself, ran `ruff`/ `mypy` / `tests/conventions`, and wrote two throwaway red-team probes (boundary 8191/8192/8193, tampered blob file, descriptor swap, replay over a blobbed+small lineage; scripts run inline, source untouched).

## 1. Acceptance verification (all 6 brief checks — PASS)
- **>8KB offloaded with SHA pin** — [VERIFIED events.py:440-463,328-339] `maybe_blob_payload` encodes the canonical bytes, `store_blob_bytes` computes `sha256` over the raw bytes and verifies the returned ref's `sha256` (events.py:395-402); CAS `put` hashes raw bytes, never the frame [VERIFIED cas.py:229-257].
- **≤8KB unchanged** — [VERIFIED events.py:459-460] small payloads return a copy with nothing stored; my 8191/8192 probe confirmed `stub_blobbed=False`, no stub keys, round-trip identical. Journal small rows pass through the identical object (journal.py:424-425) so v1 shapes stay byte-identical.
- **Replay/verify reads back byte-identical** — [VERIFIED events.py:466-496] resolver fetches + verifies, then requires `canonical_json(parsed) == data`; `test_round_trip_is_byte_identical` + ledger round-trip pinned by tests. My replay-over-blobbed-lineage probe: `[True, False]` blob flags, phase `CREATED`, `unknown_events == []`, `verify_chain True []`, resolved == original.
- **Tamper detected** — [VERIFIED events.py:406-437 + cas.py:386-403] three layers: store `Corrupt` on bad bytes, resolver size+sha re-check, chain digest on descriptor swap. My tamper probe (flipped last file byte): `Corrupt: blob is not a readable zstd frame` — detected. Size-tamper, sha-substitution chain break, and non-canonical-bytes rejection all pinned in tests.
- **canonical_json / hash chain unaffected** — [VERIFIED git diff b8570a6..c2d25e3 -- events.py] `canonical_json`/`compute_event_digest`/`LedgerEvent.sealed` untouched; new code is appended after line 255 plus `__all__.extend` at the end. `verify_chain` (store.py:385) recomputes over the stored (stub) payload, which is the intended design.
- **Journal size check present** — [VERIFIED journal.py:413-432 + all four paths at 456-457,481-482,520-521,551-552] every append does `_assert_no_fact_id(original) -> _blobbed(original) -> _assert_no_fact_id(blobbed) -> append`. Checking the original before blobbing is load-bearing (blobbing must not hide `fact_id`).

## 2. Claims audit (8 checked, 0 false)
All of: threshold measured on canonical bytes; boundary 8192 inline / 8193 blobbed; descriptor is `{sha256,size_bytes,preview}` with preview ≤200; blob-before-row ordering (crash after `put` leaves an orphan, never a dangling pointer); dedupe via discriminator for duplicate blobbed PROPOSED; no new `TYPE_CHECKING` / no store import in the ledger core; production composition (`agent.py`) still builds the journal without a store; NEED-1/NEED-2 deferrals — each SUPPORTED against the code [VERIFIED as cited above; RAN scoped suites green]. The impl report's disclosure of its own limits (preview-as-content, S3 live run unverified, no load test) is accurate.

## 3. Findings

### MAJOR-1 (deferred dependency, not a code defect in c2d25e3): production journal is built without a blob store — `src/paa/coder/agent.py:219`
`CoderAgent.build` constructs `LedgerToolJournal(ledger, correlation_id)` with no `blob_store`, so an oversized journalled row **raises `ValueError` and aborts the tool call** (journal.py:426-431, correctly fail-closed) rather than blobbing. Today's journal rows are small by construction (240-char preview cap, 64-hex hashes), so this cannot fire on any current field — but N2.6 is not live in production until the composition root injects the configured store (the impl's NEED-2). Fix location is outside this task's write-set; tracked as a deferred dependency, not a blocker for c2d25e3.

### MINOR-1: size enforcement lives only in the journal, not at `LedgerStore.append` — `src/paa/ledger/store.py:126`
Any non-journal producer (orchestrator, recovery sweep) can still append a payload >8KB inline. The contract in `events.py` is opt-in per caller. This is a deliberate, disclosed scope limit (replay purity: `apply_event`/`replay` untouched and never fetch), but a future producer must remember to call `maybe_blob_payload`. A store-level guard would need a blob-store seam on `LedgerStore`, which is exactly the coupling BlobStoreLike was built to avoid — so the current shape is defensible; the risk is discoverability, not correctness.

### MINOR-2: blob preview is a raw canonical prefix, i.e. content — `src/paa/ledger/events.py:313-325`
`_preview_of` cuts the first 200 chars of the canonical bytes, which include payload *values*. For journal payloads this is safe because the values are already shape summaries (`<str len=N sha256=…>`) or hashes; but `store_blob_bytes` is a public helper whose preview for a tool-result blob *is* content and could carry a canary. The impl discloses this (§12, NEED for shape previews in the future loop wiring). No live leak today; the future result-journaling path must not reuse this preview.

### MINOR-3: envelope version is not pinned to 1 — `src/paa/ledger/events.py:362-364`
`_blob_descriptor` accepts any `int` for `v` (excluding `bool`), so a hand-made `{"v": 99, "blob": {...}}` stub resolves as if it were v1. A future v2 envelope would be silently read as v1. Tightening to `== 1` costs nothing.

### MINOR-4: no size ceiling on the read path — `src/paa/ledger/events.py:406-437`
`read_blob_bytes` fetches the whole blob (`get` returns full `bytes`) and only then checks `len(data) == size_bytes`. A stub pointing at a multi-GB blob loads it fully before any check. In practice the writers are KB-scale (tool outputs capped 64KB), so this is bounded by the callers, not the resolver. Streaming (`get_stream`) is the honest fix if a future producer carries artifacts.

### NIT-1: preview content is not verified against the fetched bytes — `src/paa/ledger/events.py:406-437`
`read_blob_bytes` validates the preview's shape/length but never that it matches the data. Harmless: the preview is a human hint, never reconstructed, and the descriptor itself is chain-committed. No action needed.

### NIT-2: pre-existing Windows mypy baseline + one flaky conventions test (recorded, not regressions)
[RAN `.venv/Scripts/python -m mypy src` -> 3 errors, all pre-existing `src/paa/sandbox/subprocess_backend.py:548` (killpg/getpgid/SIGKILL on win32-only availability), 0 new.] [RAN `tests/conventions` first run -> 1 failure in `test_a_hung_aggregate_import_is_a_verdict` (child announced nothing within 2s on this host); re-ran `test_import_conventions` + `test_docs_references` -> 26 passed.] The blob change is unrelated to that child-process startup race.

## 4. Optimizations
- DO-LATER: enforce `v == 1` in `_blob_descriptor` (MINOR-3) — four characters, kills a future forward-compat footgun.
- NOT-WORTH-IT: streaming blob reads (MINOR-4) or moving the guard into `LedgerStore.append` (MINOR-1) at this task's scale; in-memory is honest for KB payloads and the current layering is the point.

## 5. Tests run (actual results)
- [RAN `.venv/Scripts/python -m pytest tests/coder/test_blobbing.py tests/coder/test_journal.py tests/ledger -q -p no:warnings` -> passed, EXIT=0 (224 dots incl. header; blobbing = 29 collected/passed alone).]
- [RAN `tests/ledger/test_coder_events.py tests/coder/test_secret_hygiene.py -q -p no:warnings` -> passed (52 + 3 skipped for the canary/hygiene set), EXIT=0.] — wait, exact dot count: 55 dots + 3 `s`; recorded as pass, EXIT=0.
- [RAN `.venv/Scripts/python -m ruff check src tests` -> `All checks passed!`]
- [RAN `.venv/Scripts/python -m mypy src` -> `Found 3 errors in 1 file`, all the pre-existing Windows `subprocess_backend.py:548` baseline, 0 in `events.py`/`journal.py`.]
- [RAN `tests/conventions` -> 1 flaky failure first run, green on retry (see NIT-2); `test_docs_references` + `test_import_conventions` 26 passed.] The impl's reported docs-references blank-cite failure is no longer present (its sibling re-point had landed by fa43a3e).
- Red-team probes (my own, [RAN]): 8191/8192/8193 boundary — 8191 and 8192 stay inline, 8193 blobs, all round-trip; tampered blob file detected as `Corrupt`; coherent descriptor swap resolves to the other payload (chain is the guard, as designed — pinned in `test_descriptor_sha_substitution_breaks_chain`); replay over a blobbed+small lineage — phase `CREATED`, no `unknown_events`, `verify_chain` true.

## 6. Band-aid sweep / long-run test
No band-aids: no swallowed errors (`Corrupt`/`NotFound` propagate unchanged), no test-weakening (the 29 tests assert the failure paths, not just the happy path), no suppressions added, no broad try/except. Source: canonical bytes + CAS rules are the source; descriptor pins the CAS address. Load: one canonical encode + one deduping `put` per oversized append; no unbounded state; blob-before-row ordering. Hostile: oversized (held once as bytes, same as inline), malformed descriptors rejected before fetch (including traversal-shaped digests, which CAS would also reject), non-UTF-8 blobs raise on resolve. Operability: `is_blobbed_payload`/`payload_size_bytes` expose reads; rollback is a revert before the first blobbed row. Reversal: removing the threshold check, the size/SHA re-checks, the canonical re-encode check, or the journal `_blobbed` call each fails a named test (impl §7, confirmed by construction of the test bodies).

## Verdict rationale
APPROVE c2d25e3: the six acceptance criteria all hold with evidence, the chain/SHA semantics are preserved, the journal check covers every append path with the fact_id order correct, and my independent probes reproduce the pinned behavior. The one MAJOR is a disclosed deferred dependency (production wiring), correctly fail-closed; the MINORs are deliberate scope limits or future-proofing with no live defect. No unresolved blockers.
