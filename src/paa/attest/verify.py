"""The offline verifier — ``paa verify`` (C7.2, H3.1).

This module is the consumer side of the attested change bundle, and its
one discipline is **what it never does**: no settings are loaded, no
store is opened, no ledger is queried, no cold-lake *service* is built,
no database of any kind is touched. A reviewer copies a bundle to a
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
    Every blob the *signed* index declares exists, hashes to its own name,
    matches its declared size, and is exactly the digest set referenced
    by the signed sections and evidence rows. This is where "flip one
    byte" lands, and where deleting an evidence row from the index can no
    longer quietly change the verdict.
``chain``
    The chain re-walked over the declared sections, in the declared
    order, reproduces ``chain_root`` — a substituted section, a renamed
    section or a reordered walk all break it.
``signature``
    The signature verifies over the canonical signed header using the
    algorithm the manifest *declares*. An HMAC tag relabelled ``ed25519``
    never reaches the HMAC branch, and the symmetric fallback is refused
    unless the caller explicitly opts into insecure mode.
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

Every hostile input is a failed check, never an exception for the CLI
to turn into a traceback. The verifier treats the entire bundle as
untrusted bytes: shapes are checked before they are used, blob reads
are bounded, and details carry field/type names rather than raw
attacker-controlled text where a value would add no meaning.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import zstandard

from paa.attest.format import (
    ATTEST_SCHEMA,
    SECTION_ORDER,
    chain_steps,
    verify_attestation_signature,
)
from paa.ledger.events import canonical_json

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
#: compresses remains readable without a format bump.
_RAW_SUFFIX = ".bin"
_COMPRESSED_SUFFIX = ".zst"

#: A failed verification must never be able to exhaust the reviewer's
#: memory with a hand-built zstd bomb. Attestation payloads are small;
#: 64 MiB leaves generous room for a source tree's manifest while
#: making an unbounded expansion impossible.
MAX_BLOB_BYTES = 64 * 1024 * 1024

#: JSON permits far deeper nesting than the canonicaliser can walk, and a
#: hostile manifest can use that to turn a verdict into a ``RecursionError``.
#: A legitimate bundle nests a handful of levels; 64 is generous and keeps
#: every later recursive walk (encoding, canonical JSON) inside the limit.
MAX_MANIFEST_DEPTH = 64

_HEX64_RE = re.compile(r"^[0-9a-f]{64}$")
_B64_KEY_RE = re.compile(r"[A-Za-z0-9+/]{32,128}={0,2}")


class _BlobProblem(Exception):
    """A blob could not be read safely; the detail names the failure type."""


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


def _encodable(value: object, encoding: str = "utf-8") -> bool:
    """A manifest is not usable if its text cannot be re-encoded or its
    nesting is too deep to walk safely.

    JSON accepts escaped lone surrogates that Python holds as ``str`` but
    cannot encode to UTF-8, and accepts nesting far past Python's recursion
    limit. Both would turn a verdict into an exception, so the manifest
    door refuses them before any later door uses the structure. The walk is
    iterative on purpose: using recursion here would be the very crash it
    is meant to prevent.
    """
    stack: list[tuple[object, int]] = [(value, 0)]
    while stack:
        current, depth = stack.pop()
        if depth > MAX_MANIFEST_DEPTH:
            return False
        if isinstance(current, str):
            try:
                current.encode(encoding)
            except UnicodeEncodeError:
                return False
        elif isinstance(current, dict):
            stack.extend((key, depth + 1) for key in current)
            stack.extend((item, depth + 1) for item in current.values())
        elif isinstance(current, list):
            stack.extend((item, depth + 1) for item in current)
    return True


def _mapping(value: object) -> dict[str, Any]:
    """Only a JSON object is a mapping; every other shape is empty."""
    return value if isinstance(value, dict) else {}


def _rows(value: object) -> list[dict[str, Any]]:
    """A JSON array of objects; non-members and non-objects are omitted."""
    if not isinstance(value, list):
        return []
    return [_mapping(item) for item in value]


def _summary(problems: list[str], limit: int = 5) -> str:
    return "; ".join(problems[:limit]) + (
        f" (+{len(problems) - limit} more)" if len(problems) > limit else ""
    )


def _read_blob(root: Path, digest: str) -> bytes:
    """Read one blob read-only and within a hard size bound.

    Mirrors the CAS's two-candidate lookup (raw first, then compressed)
    without constructing the store — the store's constructor creates its
    ``staging/`` directory, and a verifier that mutates the bundle it is
    verifying, or fails on a read-only bundle, is not a verifier.
    """
    if not isinstance(digest, str) or not _HEX64_RE.fullmatch(digest):
        raise _BlobProblem("malformed content digest")
    for suffix in (_RAW_SUFFIX, _COMPRESSED_SUFFIX):
        candidate = root / "blobs" / digest[:2] / digest[2:4] / f"{digest}{suffix}"
        if not candidate.is_file():
            continue
        try:
            size = candidate.stat().st_size
        except OSError as exc:
            raise _BlobProblem(f"unreadable blob metadata ({type(exc).__name__})") from exc
        if size > MAX_BLOB_BYTES:
            raise _BlobProblem("blob exceeds the verifier's size bound")
        try:
            data = candidate.read_bytes()
        except OSError as exc:
            raise _BlobProblem(f"unreadable blob ({type(exc).__name__})") from exc
        if len(data) > MAX_BLOB_BYTES:
            raise _BlobProblem("blob exceeds the verifier's size bound")
        if suffix == _COMPRESSED_SUFFIX:
            try:
                with zstandard.ZstdDecompressor().stream_reader(data) as reader:
                    data = reader.read(MAX_BLOB_BYTES + 1)
            except (zstandard.ZstdError, OSError, EOFError, ValueError) as exc:
                raise _BlobProblem(
                    f"undecodable compressed blob ({type(exc).__name__})"
                ) from exc
            if len(data) > MAX_BLOB_BYTES:
                raise _BlobProblem("decompressed blob exceeds the verifier's size bound")
        return data
    raise _BlobProblem("missing blob")


def _load_section(
    root: Path, sha256: str
) -> tuple[bytes | None, dict[str, Any] | None, str | None]:
    """Read and parse one section blob; the bytes come back for hashing."""
    try:
        data = _read_blob(root, sha256)
    except _BlobProblem as exc:
        return None, None, str(exc)
    try:
        value = json.loads(data.decode("utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError, ValueError, RecursionError) as exc:
        return data, None, f"section is not UTF-8 JSON ({type(exc).__name__})"
    if not isinstance(value, dict):
        return data, None, "section is not a JSON object"
    return data, value, None


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
    try:
        content = path.read_text(encoding="utf-8").strip()
    except (OSError, UnicodeDecodeError):
        return None
    if _B64_KEY_RE.fullmatch(content):
        return content
    try:
        raw = json.loads(content)
    except json.JSONDecodeError:
        return None
    if not isinstance(raw, dict):
        return None
    candidate = raw.get("public_key_b64") or raw.get("public_b64")
    return str(candidate) if candidate else None


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
        loaded: Any = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, ValueError, RecursionError) as exc:
        return VerificationResult(
            ok=False,
            checks=(
                CheckResult(
                    "manifest",
                    False,
                    f"manifest.json unreadable ({type(exc).__name__})",
                ),
            ),
        )
    if not isinstance(loaded, dict):
        return VerificationResult(
            ok=False,
            checks=(CheckResult("manifest", False, "manifest.json is not an object"),),
        )
    raw: dict[str, Any] = loaded
    if not _encodable(raw):
        return VerificationResult(
            ok=False,
            checks=(
                CheckResult(
                    "manifest",
                    False,
                    "manifest contains text that cannot be encoded as UTF-8 "
                    "or is too deeply nested",
                ),
            ),
        )
    if raw.get("schema") != ATTEST_SCHEMA:
        return VerificationResult(
            ok=False,
            checks=(
                CheckResult(
                    "manifest",
                    False,
                    f"not a {ATTEST_SCHEMA} manifest (schema tag refused)",
                ),
            ),
        )
    if raw.get("phase") != "final":
        return VerificationResult(
            ok=False,
            checks=(
                CheckResult(
                    "manifest",
                    False,
                    "phase is not final: a started manifest is the builder's "
                    "crash marker, not a bundle — nothing verifies",
                ),
            ),
        )
    checks.append(
        CheckResult(
            "manifest",
            True,
            "paa-attest-bundle/1, final, id "
            f"{str(raw.get('bundle_id') or '')[:24]}",
        )
    )

    # -- parse the signed structure, refusing every hostile shape -----------
    sections_raw = raw.get("sections")
    sections_shape_ok = isinstance(sections_raw, list)
    sections_declared: list[tuple[str, str]] = []
    if sections_shape_ok:
        for item in sections_raw or []:
            if not isinstance(item, dict):
                sections_shape_ok = False
                continue
            sections_declared.append(
                (str(item.get("name") or ""), str(item.get("sha256") or ""))
            )

    blobs_raw = raw.get("blobs")
    blobs_shape_ok = isinstance(blobs_raw, list)
    blob_problems: list[str] = []
    declared_pairs: set[tuple[str, str]] = set()
    if not blobs_shape_ok:
        blob_problems.append("blob index is not an array")
    else:
        for item in blobs_raw or []:
            if not isinstance(item, dict):
                blob_problems.append("blob entry is not an object")
                continue
            digest = str(item.get("sha256") or "")
            role = str(item.get("role") or "?")
            try:
                data = _read_blob(bundle_dir, digest)
            except _BlobProblem as exc:
                blob_problems.append(f"{role}: {exc} {digest[:12]}")
                continue
            if hashlib.sha256(data).hexdigest() != digest:
                blob_problems.append(f"{role}: content does not hash to its name")
                continue
            declared_bytes = item.get("bytes")
            if declared_bytes != len(data):
                blob_problems.append(f"{role}: declared size does not match the blob")
            if (digest, role) in declared_pairs:
                blob_problems.append(f"{role}: duplicate blob index entry")
            declared_pairs.add((digest, role))

    parsed_sections: dict[str, dict[str, Any]] = {}
    for name, digest in sections_declared:
        _, parsed, problem = _load_section(bundle_dir, digest)
        if parsed is not None:
            parsed_sections[name] = parsed
        elif problem:
            blob_problems.append(f"section:{name}: {problem}")

    # The signed section digests and evidence rows are the authority for
    # which blobs may be indexed. An index that omits one, adds one, or
    # relabels one is a different artifact even when its own entries hash.
    # A digest can legitimately appear under two section roles (two empty
    # sections, for instance), so the authority is the (digest, role) pair,
    # not a digest-keyed dict that would silently overwrite the first role.
    expected_pairs = {
        (digest, f"section:{name}") for name, digest in sections_declared
    }
    evidence_digests: set[str] = set()
    evidence_rows = _rows((parsed_sections.get("evidence") or {}).get("bundles"))
    for row in evidence_rows:
        digest = str(row.get("sha256") or "")
        run_id = str(row.get("run_id") or "")
        if _HEX64_RE.fullmatch(digest) and run_id:
            expected_pairs.add((digest, f"evidence:observe:{run_id}"))
            evidence_digests.add(digest)
    for section_name in ("commands", "tests"):
        for row in _rows((parsed_sections.get(section_name) or {}).get("entries")):
            ref = str(row.get("evidence_sha256") or "")
            if ref and ref not in evidence_digests:
                blob_problems.append(
                    f"{section_name}: cites undeclared evidence {ref[:12]}"
                )
    for digest, role in sorted(expected_pairs - declared_pairs):
        blob_problems.append(f"{role}: signed blob missing from index {digest[:12]}")
    for digest, role in sorted(declared_pairs - expected_pairs):
        blob_problems.append(f"{role}: unreferenced blob in index {digest[:12]}")

    if blob_problems:
        checks.append(CheckResult("blobs", False, _summary(blob_problems)))
    else:
        checks.append(
            CheckResult(
                "blobs",
                True,
                f"{len(declared_pairs)} signed index entries match their bytes",
            )
        )

    # -- the chain door ------------------------------------------------------
    names = [name for name, _ in sections_declared]
    if not sections_shape_ok or names != list(SECTION_ORDER):
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
                    "re-walked root does not match the declared chain_root",
                )
            )
        elif recomputed != (raw.get("chain") or []):
            checks.append(CheckResult("chain", False, "chain steps do not match the walk"))
        else:
            checks.append(CheckResult("chain", True, "root reproduced from sections"))

    # -- the signature door --------------------------------------------------
    signature = _mapping(raw.get("signature"))
    claims = _mapping(signature.get("claims"))
    claim_algorithm = str(claims.get("algorithm") or "")
    claim_key_id = str(claims.get("key_id") or "")
    claim_public = str(claims.get("public_key_b64") or "")
    try:
        recomputed_key_id = (
            hashlib.sha256(claim_public.encode("ascii")).hexdigest()
            if claim_public
            else ""
        )
    except UnicodeEncodeError:
        recomputed_key_id = ""
    claims_consistent = (
        claim_algorithm in ("ed25519", "hmac-dev-fallback")
        and signature.get("algorithm") == claim_algorithm
        and signature.get("key_id") == claim_key_id
        and signature.get("public_key_b64") == claim_public
        and claim_key_id == recomputed_key_id
    )
    signed_header: dict[str, Any] = {
        "schema": raw.get("schema"),
        "bundle_id": raw.get("bundle_id"),
        "created_at": raw.get("created_at"),
        "tool_version": raw.get("tool_version"),
        "sections": raw.get("sections") if sections_shape_ok else [],
        "chain_root": raw.get("chain_root"),
        # Both of these were unsigned once; a reviewer could edit them
        # without breaking the signature. They are part of the signed
        # bytes now, so a claim cannot be swapped after the fact.
        "blobs": raw.get("blobs") if blobs_shape_ok else [],
        "notes": _mapping(raw.get("notes")),
        "signature_claims": claims,
    }
    signed_bytes = canonical_json(signed_header).encode("utf-8")
    signed_over = hashlib.sha256(signed_bytes).hexdigest()
    signature_problems: list[str] = []
    if not claims_consistent:
        signature_problems.append("signature claims disagree with the signed claims")
    if str(signature.get("signed_over") or "") != signed_over:
        signature_problems.append("signed_over does not match the signed header")
    if not verify_attestation_signature(
        signed_bytes,
        str(signature.get("value_b64") or ""),
        claim_public,
        claim_algorithm,
    ):
        signature_problems.append(
            "signature does not verify under the declared algorithm "
            f"({claim_algorithm or 'unlabelled'})"
        )
    if signature_problems:
        checks.append(CheckResult("signature", False, _summary(signature_problems)))
    else:
        checks.append(
            CheckResult(
                "signature",
                True,
                f"{claim_algorithm} over the signed header holds",
            )
        )

    # -- the key door --------------------------------------------------------
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
                CheckResult("key", False, "could not read a public key from the pin")
            )
        elif pinned != claim_public:
            checks.append(
                CheckResult(
                    "key",
                    False,
                    "embedded key is not the pinned key: intact bundle, wrong signer",
                )
            )
        else:
            checks.append(CheckResult("key", True, "embedded key is the pinned key"))

    # -- the workspace door (optional) ---------------------------------------
    if workspace is not None:
        checks.append(_workspace_check(bundle_dir, sections_declared, workspace))

    # -- the summary, so the verdict quotes what it verified ----------------
    test_entries = _rows((parsed_sections.get("tests") or {}).get("entries"))
    approval_entries = _rows((parsed_sections.get("approvals") or {}).get("entries"))
    observed = sum(1 for e in test_entries if e.get("source") == "observed")
    warnings: list[str] = []
    if claim_algorithm == "hmac-dev-fallback":
        warnings.append(
            "signed with the symmetric dev fallback — forgeable by anyone "
            "who can verify"
        )
    if test_entries and observed == 0:
        warnings.append(
            "every test row is asserted: no observe bundle witnessed a test run"
        )
    # The builder's honesty caveats are signed in the diff section. A green
    # integrity verdict must still tell the reviewer that git was absent or
    # the diff was empty — integrity says the caveat is genuine, not that it
    # can be dropped from the summary.
    git_block = _mapping((parsed_sections.get("diff") or {}).get("git"))
    git_notes = [
        note for note in (git_block.get("warnings") or []) if isinstance(note, str)
    ]
    if git_block.get("available") is False and not git_notes:
        git_notes.append(
            "git was unavailable when this bundle was built; empty status and "
            "diff are not evidence of a clean tree"
        )
    warnings.extend(git_notes)

    return VerificationResult(
        ok=all(c.ok for c in checks),
        checks=tuple(checks),
        bundle_id=str(raw.get("bundle_id") or ""),
        chain_root=str(raw.get("chain_root") or ""),
        key_id=claim_key_id,
        algorithm=claim_algorithm,
        created_at=str(raw.get("created_at") or ""),
        evidence_count=len(evidence_rows),
        observed_tests=observed,
        asserted_tests=len(test_entries) - observed,
        approvals=len(approval_entries),
        warnings=tuple(warnings),
    )


def read_section(bundle_dir: Path, sha256: str) -> dict[str, Any] | None:
    """One section's parsed payload, read-only — for consumers (the PR
    body, reports) that want the content without re-verifying first.

    ``None`` when the blob is absent or not JSON: a consumer that needs
    certainty calls :func:`verify_bundle` and reads only when it held.
    """
    _, parsed, _ = _load_section(bundle_dir, sha256)
    return parsed


def _workspace_check(
    bundle_dir: Path, sections_declared: list[tuple[str, str]], workspace: Path
) -> CheckResult:
    """Recompute the workspace manifest and compare it to the attested one."""
    diff_sha = next((d for n, d in sections_declared if n == "diff"), "")
    _, diff_section, problem = _load_section(bundle_dir, diff_sha)
    if diff_section is None:
        return CheckResult(
            "workspace", False, f"the diff section is unreadable: {problem}"
        )
    manifest = _mapping(diff_section.get("manifest"))
    attested = {
        str(f.get("path")): str(f.get("sha256"))
        for f in _rows(manifest.get("files"))
        if isinstance(f.get("path"), str) and isinstance(f.get("sha256"), str)
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
    try:
        from paa.observe.bundle import snapshot_manifest

        current = snapshot_manifest(workspace.resolve()).by_path()
    except (OSError, ValueError, TypeError) as exc:
        return CheckResult(
            "workspace", False, f"workspace could not be walked ({type(exc).__name__})"
        )
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
