"""The offline verifier — ``paa verify`` (C7.2, H3.1).

This module is the consumer side of the attested change bundle, and its
one discipline is **what it never does**: no settings are loaded, no
store is opened, no ledger is queried, no cold-lake *service* is built,
no database of any kind is touched. (Some producer-side modules are
imported for their definitions along the way — Python runs a package's
``__init__`` for any submodule — but importing a class is not opening a
database, and the clean-checkout test pins the behaviour rather than
the metaphor: a verifier run with ``PAA_HOME`` pointed at a path that
does not exist leaves it nonexistent.) A reviewer copies a bundle to a
clean checkout, runs ``paa verify <dir>``, and the answer comes from
the bundle's own bytes — hashes recomputed, chain re-walked, signature
checked against the public key the manifest carries. The producer's
machine could explode and the verdict would not change, which is the
whole point of an offline-verifiable artifact: the spec's acceptance
line is *"a reviewer can verify one on a machine without PAA's
database"*, and this module is where that is true.

Five checks, each named so a failure says which door was breached:

``manifest``
    The manifest parses, carries the ``paa-attest-bundle/1`` schema tag,
    and is ``phase="final"`` — a ``started`` manifest is the build's own
    crash marker and is refused, never silently upgraded.
``blobs``
    Every blob the manifest declares exists and hashes to its own name.
    This is where "flip one byte" lands: a changed payload no longer
    matches its content address, whichever payload it was.
``chain``
    The chain re-walked over the declared sections, in the declared
    order, reproduces ``chain_root`` — a substituted section, a renamed
    section or a reordered walk all break it.
``signature``
    The Ed25519 (or, honestly labelled, HMAC dev-fallback) signature
    verifies over the canonical signed header with the embedded public
    key. Integrity — the bundle is what was signed.
``key``
    Origin, kept separate from integrity on purpose. With ``--trust``
    the embedded key must be the pinned one, else the check fails. With
    no pin the result is reported as ``self-asserted key`` — intact,
    unsigned-by-anyone-you-named — and never as ``trusted``, because a
    bundle that re-signs itself with a fresh key is *intact and
    untrusted at the same time*, and collapsing the two would let a
    forger buy trust with a keygen.

An optional sixth check, ``workspace``, is what makes the bundle a
reviewer's tool rather than a curiosity: given ``--workspace``, the
verifier recomputes the workspace's content manifest and compares it to
the diff section's, reporting the drift (files created, modified,
deleted since the attestation) instead of a bare mismatch — so "verify
on a clean checkout" means *this checkout, shown to match or shown how
it doesn't*.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from paa.attest.format import ATTEST_SCHEMA, SECTION_ORDER, chain_steps
from paa.ledger.events import canonical_json
from paa.marketplace.signing import verify as verify_signature

__all__ = [
    "CheckResult",
    "VerificationResult",
    "read_section",
    "verify_bundle",
]

#: The CAS's on-disk layout, mirrored read-only: a blob named by digest
#: lives at ``blobs/<aa>/<bb>/<digest><suffix>``, written raw (``.bin``)
#: or compressed (``.zst``). The builder writes raw — a transparency
#: artifact should be inspectable with ``sha256sum`` and a text editor —
#: and the compressed spelling is read too so a future writer that
#: compresses remains readable without a format bump. The compatibility
#: is pinned by the build-then-verify round-trip test, not claimed here.
_RAW_SUFFIX = ".bin"
_COMPRESSED_SUFFIX = ".zst"


@dataclass(frozen=True, slots=True)
class CheckResult:
    """One named check: what door, whether it held, what was seen."""

    name: str
    ok: bool
    detail: str


@dataclass(frozen=True, slots=True)
class VerificationResult:
    """The verdict, as facts rather than a boolean.

    ``ok`` is the conjunction of the checks that ran (the workspace
    check runs only when a workspace was given). ``checks`` carries each
    named result so a failure says which door was breached, and the
    summary fields (bundle id, chain root, key id, counts) let a
    reviewer quote the artifact they verified in the very line that says
    they verified it.
    """

    ok: bool
    checks: tuple[CheckResult, ...]
    bundle_id: str = ""
    chain_root: str = ""
    key_id: str = ""
    algorithm: str = ""
    created_at: str = ""
    evidence_count: int = 0
    observed_tests: int = 0
    asserted_tests: int = 0
    approvals: int = 0
    warnings: tuple[str, ...] = field(default_factory=tuple)


def _read_blob(root: Path, digest: str) -> bytes | None:
    """Read one blob read-only, returning ``None`` when absent.

    Mirrors the CAS's two-candidate lookup (raw first, then compressed)
    without constructing the store — the store's constructor creates its
    ``staging/`` directory, and a verifier that mutates the bundle it is
    verifying, or fails on a read-only bundle, is not a verifier.
    """
    for suffix in (_RAW_SUFFIX, _COMPRESSED_SUFFIX):
        candidate = root / "blobs" / digest[:2] / digest[2:4] / f"{digest}{suffix}"
        if not candidate.is_file():
            continue
        data = candidate.read_bytes()
        if suffix == _COMPRESSED_SUFFIX:
            import zstandard

            data = zstandard.ZstdDecompressor().decompress(data)
        return data
    return None


def _load_section(
    root: Path, sha256: str
) -> tuple[bytes | None, dict[str, Any] | None]:
    """Read and parse one section blob; the bytes come back for hashing."""
    data = _read_blob(root, sha256)
    if data is None:
        return None, None
    try:
        return data, dict(json.loads(data.decode("utf-8")))
    except (json.JSONDecodeError, UnicodeDecodeError):
        return data, None


def verify_bundle(
    bundle_dir: Path,
    *,
    trust_key: str | None = None,
    workspace: Path | None = None,
) -> VerificationResult:
    """Verify one attested change bundle, offline, against its own bytes.

    ``trust_key`` is a public key in base64 (or the path of a key file
    / public-key file carrying one); when given, the embedded key must
    match it or the ``key`` check fails. ``workspace`` adds the
    checkout-equivalence check. Neither argument reaches for a database.
    """
    checks: list[CheckResult] = []
    manifest_path = bundle_dir / "manifest.json"

    # -- the manifest door ---------------------------------------------------
    try:
        raw: dict[str, Any] = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return VerificationResult(
            ok=False,
            checks=(
                CheckResult(
                    "manifest", False, f"manifest.json unreadable: {exc}"
                ),
            ),
        )
    if raw.get("schema") != ATTEST_SCHEMA:
        checks.append(
            CheckResult(
                "manifest",
                False,
                f"not a {ATTEST_SCHEMA} manifest (schema={raw.get('schema')!r})",
            )
        )
        return VerificationResult(ok=False, checks=tuple(checks))
    if raw.get("phase") != "final":
        checks.append(
            CheckResult(
                "manifest",
                False,
                f"phase={raw.get('phase')!r}: a started manifest is the "
                "builder's crash marker, not a bundle — nothing verifies",
            )
        )
        return VerificationResult(ok=False, checks=tuple(checks))
    checks.append(
        CheckResult("manifest", True, f"paa-attest-bundle/1, final, id {raw.get('bundle_id')}")
    )

    sections_declared = [
        (str(s.get("name") or ""), str(s.get("sha256") or ""))
        for s in raw.get("sections") or []
    ]

    # -- the blobs door ------------------------------------------------------
    blob_entries = raw.get("blobs") or []
    tampered: list[str] = []
    for entry in blob_entries:
        digest = str(entry.get("sha256") or "")
        role = str(entry.get("role") or "?")
        data = _read_blob(bundle_dir, digest)
        if data is None:
            tampered.append(f"{role}: missing blob {digest[:12]}…")
        elif hashlib.sha256(data).hexdigest() != digest:
            tampered.append(f"{role}: content does not hash to its name {digest[:12]}…")
    if tampered:
        checks.append(
            CheckResult(
                "blobs",
                False,
                "; ".join(tampered[:5])
                + (f" (+{len(tampered) - 5} more)" if len(tampered) > 5 else ""),
            )
        )
    else:
        checks.append(
            CheckResult("blobs", True, f"{len(blob_entries)} blobs match their names")
        )

    # -- the chain door ------------------------------------------------------
    names = [name for name, _ in sections_declared]
    if names != list(SECTION_ORDER):
        checks.append(
            CheckResult(
                "chain",
                False,
                f"sections are {names}, not the fixed order {list(SECTION_ORDER)}",
            )
        )
    else:
        recomputed = chain_steps(sections_declared)
        if recomputed[-1] != raw.get("chain_root"):
            checks.append(
                CheckResult(
                    "chain",
                    False,
                    f"re-walked root {recomputed[-1][:12]}… != declared "
                    f"{str(raw.get('chain_root'))[:12]}…",
                )
            )
        elif recomputed != (raw.get("chain") or []):
            checks.append(
                CheckResult("chain", False, "chain steps do not match the walk")
            )
        else:
            checks.append(CheckResult("chain", True, "root reproduced from sections"))

    # -- the signature door --------------------------------------------------
    signature = raw.get("signature") or {}
    signed_header = {
        "schema": raw.get("schema"),
        "bundle_id": raw.get("bundle_id"),
        "created_at": raw.get("created_at"),
        "tool_version": raw.get("tool_version"),
        "sections": raw.get("sections") or [],
        "chain_root": raw.get("chain_root"),
    }
    signed_bytes = canonical_json(signed_header).encode("utf-8")
    signed_over = hashlib.sha256(signed_bytes).hexdigest()
    sig_ok = verify_signature(
        signed_bytes,
        str(signature.get("value_b64") or ""),
        str(signature.get("public_key_b64") or ""),
    )
    if not sig_ok:
        checks.append(
            CheckResult(
                "signature",
                False,
                f"signature does not verify (algorithm={signature.get('algorithm')!r}; "
                f"signed_over {str(signature.get('signed_over'))[:12]}… "
                f"vs recomputed {signed_over[:12]}…)",
            )
        )
    else:
        checks.append(
            CheckResult(
                "signature",
                True,
                f"{signature.get('algorithm')} over the signed header holds",
            )
        )

    # -- the key door --------------------------------------------------------
    embedded_key = str(signature.get("public_key_b64") or "")
    if trust_key is None:
        checks.append(
            CheckResult(
                "key",
                True,
                "self-asserted key (no --trust pin supplied): integrity is "
                "checked, origin is not claimed",
            )
        )
    else:
        pinned = _resolve_trust_key(trust_key)
        if pinned is None:
            checks.append(
                CheckResult("key", False, f"could not read a public key from {trust_key}")
            )
        elif pinned != embedded_key:
            checks.append(
                CheckResult(
                    "key",
                    False,
                    f"embedded key {str(signature.get('key_id'))[:12]}… is not the "
                    "pinned key: intact bundle, wrong signer",
                )
            )
        else:
            checks.append(
                CheckResult("key", True, "embedded key is the pinned key")
            )

    # -- the workspace door (optional) ---------------------------------------
    if workspace is not None:
        checks.append(_workspace_check(bundle_dir, sections_declared, workspace))

    # -- the summary, so the verdict quotes what it verified ----------------
    def _section(name: str) -> dict[str, Any] | None:
        sha = next((d for n, d in sections_declared if n == name), "")
        if not sha:
            return None
        _, parsed = _load_section(bundle_dir, sha)
        return parsed

    tests_section = _section("tests") or {}
    approvals_section = _section("approvals") or {}
    test_entries: list[dict[str, Any]] = [
        dict(e) for e in tests_section.get("entries") or []
    ]
    observed = sum(1 for e in test_entries if e.get("source") == "observed")
    warnings: list[str] = []
    if str(signature.get("algorithm")) == "hmac-dev-fallback":
        warnings.append(
            "signed with the symmetric dev fallback — forgeable by anyone "
            "who can verify"
        )
    if test_entries and observed == 0:
        warnings.append(
            "every test row is asserted: no observe bundle witnessed a test run"
        )

    return VerificationResult(
        ok=all(c.ok for c in checks),
        checks=tuple(checks),
        bundle_id=str(raw.get("bundle_id") or ""),
        chain_root=str(raw.get("chain_root") or ""),
        key_id=str(signature.get("key_id") or ""),
        algorithm=str(signature.get("algorithm") or ""),
        created_at=str(raw.get("created_at") or ""),
        evidence_count=len(raw.get("blobs") or [])
        - sum(1 for b in raw.get("blobs") or []
               if str(b.get("role", "")).startswith("section:")),
        observed_tests=observed,
        asserted_tests=len(test_entries) - observed,
        approvals=len(approvals_section.get("entries") or []),
        warnings=tuple(warnings),
    )


def read_section(bundle_dir: Path, sha256: str) -> dict[str, Any] | None:
    """One section's parsed payload, read-only — for consumers (the PR
    body, reports) that want the content without re-verifying first.

    ``None`` when the blob is absent or not JSON: a consumer that needs
    certainty calls :func:`verify_bundle` and reads only when it held.
    """
    _, parsed = _load_section(bundle_dir, sha256)
    return parsed


#: A base64 public key's shape: short, single-line, exactly the b64
#: alphabet. The alphabet includes ``/``, so a path-shaped test would
#: misread every raw key as a file — the shape is the discriminator.
_B64_KEY_RE = re.compile(r"[A-Za-z0-9+/]{32,128}={0,2}")


def _resolve_trust_key(trust_key: str) -> str | None:
    """A public key from a base64 string or a file path holding one.

    Accepts a raw base64 public key, a producer's key file (the JSON
    ``KeyFile`` shape, whose *public* half is the pin — the private half
    stays home), or a file holding one bare base64 key. ``None`` when
    nothing readable is there: an unreadable pin is a failed pin, never
    a skipped one.
    """
    text = trust_key.strip()
    if not text:
        return None
    if _B64_KEY_RE.fullmatch(text):
        return text
    path = Path(text)
    if not path.is_file():
        return None
    content = path.read_text(encoding="utf-8").strip()
    if _B64_KEY_RE.fullmatch(content):
        return content
    try:
        raw: dict[str, Any] = json.loads(content)
    except json.JSONDecodeError:
        return None
    candidate = raw.get("public_key_b64") or raw.get("public_b64")
    return str(candidate) if candidate else None


def _workspace_check(
    bundle_dir: Path, sections_declared: list[tuple[str, str]], workspace: Path
) -> CheckResult:
    """Recompute the workspace manifest and compare it to the attested one."""
    diff_sha = next((d for n, d in sections_declared if n == "diff"), "")
    _, diff_section = _load_section(bundle_dir, diff_sha)
    if diff_section is None:
        return CheckResult(
            "workspace", False, "the diff section is unreadable; nothing to compare"
        )
    attested = {
        str(f.get("path")): str(f.get("sha256"))
        for f in (diff_section.get("manifest") or {}).get("files") or []
    }
    if not attested:
        return CheckResult(
            "workspace",
            False,
            "the attested manifest is empty; workspace equivalence cannot be "
            "checked",
        )

    # Local import on purpose: the manifest walk is the observe bundle's
    # own instrument, and the verifier stays importable without dragging
    # the walk's dependencies into every caller that never asks for it.
    from paa.observe.bundle import snapshot_manifest

    current = snapshot_manifest(workspace.resolve()).by_path()
    created = sorted(current.keys() - attested.keys())
    deleted = sorted(attested.keys() - current.keys())
    modified = sorted(
        path
        for path in attested.keys() & current.keys()
        if attested[path] != current[path].sha256
    )
    if created or deleted or modified:
        parts = []
        if created:
            parts.append(f"+{len(created)} created ({created[0]}, …)")
        if deleted:
            parts.append(f"-{len(deleted)} deleted ({deleted[0]}, …)")
        if modified:
            parts.append(f"~{len(modified)} modified ({modified[0]}, …)")
        return CheckResult(
            "workspace",
            False,
            f"checkout drifts from the attestation: {'; '.join(parts)}",
        )
    return CheckResult(
        "workspace",
        True,
        f"checkout matches the attested manifest ({len(attested)} files)",
    )
