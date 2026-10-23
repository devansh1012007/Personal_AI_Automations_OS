# Module: Marketplace (`src/paa/marketplace/`)

## Module identity

- **Name:** Marketplace
- **Path:** `src/paa/marketplace/`
- **Main responsibility:** Signed `.paapkg` skill packages, a five-gate installer, registry clients (HTTP + local directory), and package signing/verification. Implements the RFC's signed skill economy with revenue-split readiness.
- **Architectural layer:** Integration / extensibility
- **Entrypoints:** `PackageInstaller.install()` (five gates), `SkillPackage.from_bytes()/load()`, `sign()`/`verify()`, `MarketplaceRegistry` clients
- **Public interfaces:** `InstallGate`, `InstallOutcome`, `InstallReport`, `PackageInstaller` (`installer.py`); `PackageError`, `PackageManifest`, `SkillPackage` (`package.py`); `KeyPair`, `SignatureState`, `generate_keypair`, `sign`, `verify`, `signing_is_secure` (`signing.py`); `HttpRegistry`, `LocalDirectoryRegistry`, `MarketplaceRegistry`, `RegistryEntry` (`registry_client.py`)
- **Other modules that depend on it:** None outside `src/paa/marketplace/` — skills is self-contained and not yet wired into the orchestrator's main task path. `skills/` does not currently import marketplace.

## Purpose and responsibilities

The marketplace is the trust layer for third-party skills: a downloaded package must pass five gates in order before a skill is registered; a failed gate leaves zero trace (no half-install, no dangling registry rows). Package identity is content-hash based (order-independent) and zip-slip protected on unpack. Signing uses Ed25519 (via `cryptography`) when available, otherwise an HMAC dev fallback that is loudly labeled insecure (`signing_is_secure()`).

## Internal implementation

### `package.py` — `.paapkg` format
A zip with `manifest.json` at root plus files. Content hash computed over sorted `name\0bytes\0` concatenation (order-independent, collision-resistant). `SkillPackage.from_bytes()` rejects zip-slip (names starting with `/` or containing `..`). `python_sources()` decodes `.py` files for the AST scan. Manifest declares everything needed for a trust decision before unpacking.

### `signing.py` — signatures
Two backends selected by availability: Ed25519 (real asymmetric) when `cryptography` is installed, or HMAC dev fallback (symmetric, forgeable — loudly labeled) otherwise. `signing_is_secure()` reports the backend so the installer can refuse to trust an unauthenticatable signature. `cryptography` imported lazily, never at module scope. `SignatureState`: `unverified`, `valid`, `invalid`, `untrusted_key` — mirrors `marketplace_packages.signature_state` CHECK values.

### `installer.py` — five-gate installer
Gates run in order (content hash → contract → signature → sandbox scan → validation → register); a package failing any gate leaves zero trace. `InstallGate`, `InstallOutcome`, `InstallReport`, `PackageInstaller`. (Gate specifics beyond ordering were not fully traced in source inspection; installer.py output was truncated during investigation — treat the per-gate checks as partially verified.)

### `registry_client.py` — registry clients
`HttpRegistry`, `LocalDirectoryRegistry`, `MarketplaceRegistry`, `RegistryEntry` for discovering packages from a registry.

### `contracts.py` (shared with skills)
`SkillContract` (frozen pydantic v2), `SkillInvocation`, `SkillResult`; `parse()` wraps Pydantic errors into `SkillContractError` without echoing input data.

## Inputs and outputs

| Operation | Input | Output | Side effects |
|---|---|---|---|
| `PackageInstaller.install(package)` | `.paapkg` bytes/path | `InstallReport` | writes `marketplace_packages` row only if all gates pass |
| `sign(payload, private_b64)` | bytes + key | base64 signature | none |
| `verify(payload, signature, public_b64)` | bytes + sig + key | bool | none |
| `RegistryClient.discover(...)` | query | list of packages | network (HTTP) or directory scan |

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
- Marketplace is not wired into `src/` outside its own package; no publishing UI, user model, or payment model (`README.md:174`).
- End-to-end signed-package installation through the runtime was not traced.

## Related documents

- [`skills.md`](skills.md) — Unified Skill Adapter that consumes registered skills.
- [`validation.md`](validation.md) — AST scan + schema validation used by installer gates.
- [`sandbox.md`](sandbox.md) — sandbox scan used by installer gates.
- [`../data/data-models-and-relationships.md`](../data/data-models-and-relationships.md) — `marketplace_packages`, `marketplace_trusted_keys` schema.
- [`README.md:174` marketplace gaps](../README.md)
