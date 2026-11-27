# Module: Marketplace (`src/paa/marketplace/`)

## Module identity

- **Name:** Marketplace
- **Path:** `src/paa/marketplace/`
- **Main responsibility:** Signed `.paapkg` skill packages, a five-gate installer, registry clients (HTTP + local directory), and package signing/verification. Implements the RFC's signed skill economy with revenue-split readiness — and, since C9.1, the primitives under the pipeline economy's hosted half: the restatement fields a hub index carries, the publish write the local registry performs, and the compatibility range a manifest signs.
- **Architectural layer:** Integration / extensibility
- **Entrypoints:** `PackageInstaller.install()` (five gates), `SkillPackage.from_bytes()/load()`, `sign()`/`verify()`, `MarketplaceRegistry` clients
- **Public interfaces:** `InstallGate`, `InstallOutcome`, `InstallReport`, `PackageInstaller` (`installer.py`); `PackageError`, `PackageManifest`, `SkillPackage` (`package.py`); `KeyPair`, `SignatureState`, `generate_keypair`, `sign`, `verify`, `signing_is_secure` (`signing.py`); `HttpRegistry`, `LocalDirectoryRegistry`, `MarketplaceRegistry`, `RegistryEntry` (`registry_client.py`)
- **Other modules that depend on it:** `pipelines/packages.py` and `pipelines/publish.py` (the C9.1 pipeline economy: the five gates, the package form, the registry clients and the signing kit), `chat/extensions.py` (the C8.4 extension surface's installs), and the CLI doors over both. `skills/` does not import marketplace.

## Purpose and responsibilities

The marketplace is the trust layer for third-party skills: a downloaded package must pass five gates in order before a skill is registered; a failed gate leaves zero trace (no half-install, no dangling registry rows). Package identity is content-hash based (order-independent) and zip-slip protected on unpack. Signing uses Ed25519 (via `cryptography`) when available, otherwise an HMAC dev fallback that is loudly labeled insecure (`signing_is_secure()`).

## Internal implementation

### `package.py` — `.paapkg` format
A zip with `manifest.json` at root plus files. Content hash computed over sorted `name\0bytes\0` concatenation (order-independent, collision-resistant). `SkillPackage.from_bytes()` rejects zip-slip (names starting with `/` or containing `..`). `python_sources()` decodes `.py` files for the AST scan. Manifest declares everything needed for a trust decision before unpacking — including, since C9.1, the compatibility pair `requires_paa` (the range of runtime versions the package declares itself installable under) and `protocol` (the wire version it speaks), both **inside `signing_bytes()`** (`package.py:173`): a hub's index may restate them, but the signed manifest is the copy an install refuses against, so a hub cannot widen a compatibility claim and have it survive verification.

### `signing.py` — signatures
Two backends selected by availability: Ed25519 (real asymmetric) when `cryptography` is installed, or HMAC dev fallback (symmetric, forgeable — loudly labeled) otherwise. `signing_is_secure()` reports the backend so the installer can refuse to trust an unauthenticatable signature. `cryptography` imported lazily, never at module scope. `SignatureState`: `unverified`, `valid`, `invalid`, `untrusted_key` — mirrors `marketplace_packages.signature_state` CHECK values.

### `installer.py` — five-gate installer
Gates run in order (content hash → contract → signature → sandbox scan → validation → register); a package failing any gate leaves zero trace. `InstallGate`, `InstallOutcome`, `InstallReport`, `PackageInstaller`. (Gate specifics beyond ordering were not fully traced in source inspection; installer.py output was truncated during investigation — treat the per-gate checks as partially verified.)

### `registry_client.py` — registry clients
`HttpRegistry`, `LocalDirectoryRegistry`, `MarketplaceRegistry`, `RegistryEntry` (`registry_client.py:286`) for discovering packages from a registry. Since C9.1 the pipeline economy rides this module at both ends: `LocalDirectoryRegistry.publish` (`:422`) writes the package file plus an index entry restating the manifest's trust and compatibility fields (publisher key, signature, `requires_paa`, `protocol`) — the round-trip property: what publish writes, `search` (`:388`) lists and `fetch` returns, so a client picks a version and checks a signature without opening the archive. `pipelines/publish.py` is the writer's caller; `pipelines/packages.py` reads a hub through `registry_for_hub` (a directory here, or an `HttpRegistry` URL — the transport is injectable so the HTTP form is testable without a socket) and treats everything that comes back as untrusted bytes for the installer's gates.

### `contracts.py` (shared with skills)
`SkillContract` (frozen pydantic v2), `SkillInvocation`, `SkillResult`; `parse()` wraps Pydantic errors into `SkillContractError` without echoing input data.

## The pipeline economy's use of these primitives (C9.1)

The hosted marketplace the ROADMAP parked was unblocked by operator order
(W9), and it landed on the pipeline platform — see
[`pipelines.md`](pipelines.md) for the flow. What lives *here* is the part
the economy shares with every other package kind:

- the **five gates**, unmodified — a hub-served `pipeline` package rides
  them exactly as a skill does;
- the **signed manifest**, now carrying `requires_paa`/`protocol` inside
  the signed bytes, and the **publisher-key trust store**
  (`PackageInstaller.trust_key`, `installer.py:283`) a consumer installs
  a signed package under;
- the **hub index format**: a directory of `.paapkg` files plus
  `index.json`, written by `LocalDirectoryRegistry.publish` and read back
  by `search`/`fetch` — and served over HTTP by an `HttpRegistry` peer.

Publisher identity for pipelines lives with the publish flow
(`PAA_HOME/keys`, mode 0600, private half never travels) — see
`pipelines/publish.py`, not this module.

## Inputs and outputs

| Operation | Input | Output | Side effects |
|---|---|---|---|
| `PackageInstaller.install(package)` | `.paapkg` bytes/path | `InstallReport` | writes `marketplace_packages` row only if all gates pass |
| `PackageInstaller.trust_key(key_id, public_key, owner)` | a publisher's public key | — | `marketplace_trusted_keys` row |
| `sign(payload, private_b64)` | bytes + key | base64 signature | none |
| `verify(payload, signature, public_b64)` | bytes + sig + key | bool | none |
| `LocalDirectoryRegistry.publish(package)` | a `SkillPackage` | — | package file + index entry (identity and trust/compat fields restated) |
| `MarketplaceRegistry.search(query)` / `fetch(name, version)` | a query / a name and version | `RegistryEntry` list / `SkillPackage` | network (HTTP) or directory scan |

## Dependencies and connections

| Component | Relationship | Why | Source |
|---|---|---|---|
| `installer` → `package.py` | `SkillPackage.load/verify_content_hash` | Gate 1: content integrity | `installer.py` |
| `installer` → `skills/contracts.py` | `SkillContract.parse` | Gate 2: contract validity | `installer.py` |
| `installer` → `signing.py` | `verify` / `signing_is_secure` | Gate 3: signature authentication | `installer.py` |
| `installer` → `validation/` | AST scan + schema | Gates 4–5: sandbox scan + validation | `installer.py` |
| `installer` → `skills/registry.py` | `SkillRegistry.register` | Register skill after all gates | `installer.py` |
| `cryptography` | lazy import in `signing.py` | Ed25519 vs HMAC fallback | `marketplace/signing.py:50` |

## Runtime behavior

- Constructing providers/packages performs no I/O and opens no socket (safe at startup wiring).
- Signing backend chosen at runtime by availability; `signing_is_secure()` lets the installer decide whether to trust.
- Zip-slip protection prevents path-traversal on unpack.

## Failure behavior

- `cryptography` absent → HMAC dev fallback, `signing_is_secure()` returns False; installer may refuse untrusted signatures.
- Any gate failure → zero trace (no partial install).
- Corrupt manifest/zip → `PackageError`.

## Modification impact

- Adding a gate: extend the installer pipeline; order matters (content hash first).
- Changing the signing backend affects `marketplace_packages.signature_state` values.
- Tests: `tests/marketplace/` (18 tests per README).

## Known gaps

- Per-gate installer checks not fully traced (installer.py investigation output was truncated); the exact gate sequence beyond "content hash → contract → signature → sandbox scan → validation → register" is inferred from module structure, not line-verified.
- No user model or payment model for skills (`README.md:266`); the pipeline economy (C9.1) is signed distribution on a local-first runtime, not commerce.
- The remote publish path (`HttpRegistry.publish`, the POST verb on the wire) has no server to talk to; the pipeline publish flow refuses a remote hub URL with a named reason rather than pretending.

## Related documents

- [`skills.md`](skills.md) — Unified Skill Adapter that consumes registered skills.
- [`validation.md`](validation.md) — AST scan + schema validation used by installer gates.
- [`sandbox.md`](sandbox.md) — sandbox scan used by installer gates.
- [`../data/data-models-and-relationships.md`](../data/data-models-and-relationships.md) — `marketplace_packages`, `marketplace_trusted_keys` schema.
- [`README.md` marketplace gaps](../README.md)
