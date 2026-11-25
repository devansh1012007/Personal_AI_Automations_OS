"""The attested change bundle's format and builder (C7.2, H3.1).

"Agents mostly fail by succeeding wrongly" — the tests pass, the diff
looks plausible, and nobody can say *who asked for this*, *what actually
ran*, or *whether the artifact in front of them is the one that was
produced*. The attested change bundle is the standard bet against that:
one directory a reviewer can carry to a clean machine and verify offline,
without the producer's database and without trusting the producer.

The format (``paa-attest-bundle/1``) is a directory::

    <bundle-dir>/
      manifest.json                     the signed index
      blobs/<aa>/<bb>/<sha256>.bin      content-addressed payloads

Six sections ride the chain, in a fixed order — mandate, diff, commands,
tests, approvals, evidence — and every payload (the six sections plus any
embedded observe bundles) is a blob named by the SHA-256 of its raw bytes,
which is the cold lake's own content-addressing contract
(:class:`paa.storage.coldlake.cas.ContentAddressedStore`, ADR-0004):
the name is the checksum, so every read can verify what it got, and
deduplication is free.

Three properties the format holds by construction:

* **It is final or it says it is not.** Like the observe bundle, the
  manifest is written twice — a ``phase="started"`` skeleton before any
  evidence is gathered, the ``phase="final"`` record after the signature.
  A manifest still saying ``started`` is the crash marker for a build
  that never came back, and the verifier refuses it.
* **Integrity and origin are never conflated.** The Ed25519 signature
  over the signed header proves the bundle was not altered after
  signing — *integrity*, checkable with the public key embedded in the
  manifest. Whether that key is one the reviewer *trusts* is a separate
  fact: ``paa verify --trust <key>`` pins it, and without a pin the
  result says ``self-asserted key``, never ``trusted``. A bundle
  re-signed by an attacker's key therefore *verifies as intact* and
  *fails the pin* — which is exactly the honest split, and the reason
  the two checks are reported separately.
* **Observed and asserted are never collapsed.** Commands and tests are
  ``observed`` when a T1 observe bundle (C7.1) witnessed them — the row
  carries the evidence blob's digest — and ``asserted`` when the operator
  typed them in. Same section, different words, counted separately on
  the console. A bundle whose tests are all ``asserted`` is still a
  valid bundle; it is just not a bundle that proves anything ran.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import structlog

from paa.attest.format import (
    APPROVAL_DECISIONS,
    ATTEST_SCHEMA,
    ATTEST_TOOL_VERSION,
    SECTION_ORDER,
    chain_steps,
)
from paa.attest.format import (
    sha256_bytes as _sha256_bytes,
)
from paa.core.errors import PaaError
from paa.ledger.events import canonical_json
from paa.marketplace.signing import KeyPair, generate_keypair, sign, signing_is_secure
from paa.observe.bundle import BUNDLE_SCHEMA as OBSERVE_SCHEMA
from paa.observe.bundle import read_bundle, snapshot_manifest

__all__ = [
    "APPROVAL_DECISIONS",
    "ATTEST_SCHEMA",
    "ATTEST_TOOL_VERSION",
    "SECTION_ORDER",
    "Approval",
    "BuildResult",
    "KeyFile",
    "build_bundle",
    "chain_steps",
    "load_or_generate_key",
    "read_manifest",
]

log = structlog.get_logger(__name__)


def _utc_now_iso() -> str:
    return datetime.now(UTC).isoformat(timespec="milliseconds")


# ---------------------------------------------------------------------------
# The signing key file — producer side only
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class KeyFile:
    """The attest signing key as it lives on the producer's disk.

    The *private* half never leaves the producer: not into the bundle,
    not into the ledger, nowhere. The manifest carries the public half
    and a ``key_id`` (the SHA-256 of the public key's base64 text), so a
    reviewer can pin the key once and recognise it thereafter.
    """

    algorithm: str
    private_b64: str
    public_b64: str
    created_at: str
    secure: bool

    @property
    def key_id(self) -> str:
        return _sha256_bytes(self.public_b64.encode("ascii"))

    def to_json(self) -> dict[str, Any]:
        return {
            "algorithm": self.algorithm,
            "private_b64": self.private_b64,
            "public_b64": self.public_b64,
            "created_at": self.created_at,
            "secure": self.secure,
        }


def load_or_generate_key(path: Path, *, allow_insecure: bool = False) -> KeyFile:
    """Load the key file at ``path``, generating it on first use.

    Refuses to *generate* under the HMAC dev fallback unless
    ``allow_insecure`` — an attestation is a claim a reviewer acts on, so
    a symmetric "signature" anyone can forge is worse than no signature:
    it lends the vocabulary of proof to something that has none. (Loading
    an existing insecure key file is allowed with the same flag; the
    manifest still labels the algorithm honestly for the verifier.)
    """
    if path.exists():
        raw: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))
        key = KeyFile(
            algorithm=str(raw["algorithm"]),
            private_b64=str(raw["private_b64"]),
            public_b64=str(raw["public_b64"]),
            created_at=str(raw.get("created_at") or ""),
            secure=bool(raw.get("secure") or False),
        )
        if not key.secure and not allow_insecure:
            raise PaaError(
                f"{path} holds a dev-fallback (symmetric, forgeable) signing key; "
                "attesting with it needs --insecure-fallback-signing, and the "
                "manifest will label the algorithm for every verifier"
            )
        return key

    if not signing_is_secure() and not allow_insecure:
        raise PaaError(
            "real asymmetric signing is unavailable (the cryptography package "
            "is not installed), and the HMAC fallback is symmetric — anyone "
            "who can verify can forge. Install paa[all] or pass "
            "--insecure-fallback-signing to attest anyway, honestly labelled"
        )
    pair: KeyPair = generate_keypair()
    key = KeyFile(
        algorithm="ed25519" if pair.secure else "hmac-dev-fallback",
        private_b64=pair.private_b64,
        public_b64=pair.public_b64,
        created_at=_utc_now_iso(),
        secure=pair.secure,
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    # 0o600 from the start: the file holds the private half, and a
    # world-readable key file would make every later attestation
    # forgeable by anyone who read it once.
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        json.dump(key.to_json(), handle, indent=2, sort_keys=True)
    if not key.secure:
        log.warning("attest.key.insecure_fallback", path=str(path))
    return key


# ---------------------------------------------------------------------------
# The sections' row types
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Approval:
    """One sign-off, as asserted by the producer.

    The approver's identity is *asserted*, exactly like a mandate's
    requester (``-asserted`` naming, no authentication in v0): the bundle
    records who the producer says approved, and a reviewer who needs more
    than that asks for it out of band. The decision vocabulary is closed
    so a reader never meets a free-text verdict it cannot interpret.
    """

    approver_kind: str
    approver_id: str
    decision: str
    note: str = ""
    recorded_at: str = field(default_factory=_utc_now_iso)

    def __post_init__(self) -> None:
        if self.decision not in APPROVAL_DECISIONS:
            raise PaaError(
                f"approval decision {self.decision!r} is not one of "
                f"{sorted(APPROVAL_DECISIONS)}; the vocabulary is closed so "
                "a reader never meets a verdict it cannot interpret"
            )
        if not self.approver_kind.strip() or not self.approver_id.strip():
            raise PaaError(
                "an approval needs a non-empty approver kind and id; an "
                "anonymous approval is an absent one, recorded as such"
            )

    def to_json(self) -> dict[str, Any]:
        return {
            "approver_asserted": {
                "kind": self.approver_kind,
                "id": self.approver_id,
            },
            "decision": self.decision,
            "note": self.note,
            "recorded_at": self.recorded_at,
        }


# ---------------------------------------------------------------------------
# Git facts — the producer's own workspace, asked directly
# ---------------------------------------------------------------------------


def _git(workspace: Path, *args: str, timeout_s: int = 60) -> str | None:
    """One git query against ``workspace``; ``None`` on any absence.

    git is resolved to an absolute path up front (``shutil.which``), so
    the answer never depends on the caller's ``PATH`` and the call is a
    fully named executable — and a host without git, or a workspace that
    is not a repository, returns ``None`` rather than raising: "no git
    evidence" is an honest answer the diff section records, not a failure
    to suppress.
    """
    git = shutil.which("git")
    if git is None:
        return None
    try:
        proc = subprocess.run(  # noqa: S603 — argv is a literal, resolved above
            [git, *args],
            cwd=workspace,
            check=False,
            capture_output=True,
            text=True,
            timeout=timeout_s,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if proc.returncode != 0:
        return None
    return proc.stdout


def _git_facts(workspace: Path, since: str | None) -> dict[str, Any]:
    """The diff section's git block: status, diff, head, commits since."""
    head = _git(workspace, "rev-parse", "HEAD")
    available = head is not None
    facts: dict[str, Any] = {
        "available": available,
        "reason": "" if available else "git unavailable, or not a repository",
        "head": head.strip() if head else None,
        "status": _git(workspace, "status", "--porcelain") or "",
        "diff": _git(workspace, "diff", "HEAD") or "",
        "since": since,
        "commits_since": [],
    }
    if available and since:
        # --since resolution is the one place a bad operator input can
        # fail: refuse loudly rather than silently attesting "no commits".
        log_out = _git(workspace, "log", "--oneline", f"{since}..HEAD")
        if log_out is None:
            raise PaaError(
                f"--diff-since {since!r} does not resolve against this "
                "workspace's history; the attestation would silently claim "
                "there are no commits since it"
            )
        facts["commits_since"] = [
            {"line": line} for line in log_out.splitlines() if line.strip()
        ]
    return facts


# ---------------------------------------------------------------------------
# The builder
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class BuildResult:
    """What the builder hands back: where, what identity, what content."""

    bundle_dir: Path
    bundle_id: str
    chain_root: str
    key_id: str
    algorithm: str
    section_sizes: dict[str, int]
    evidence_count: int
    observed_tests: int
    asserted_tests: int
    warnings: tuple[str, ...]


def _write_json_atomic(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
    tmp.replace(path)


def _observe_evidence(path: Path) -> tuple[dict[str, Any], bytes]:
    """Read one observe bundle as evidence: (summary, raw file bytes).

    Refuses a bundle that is not ``final`` — a manifest stuck at
    ``started`` is the observer's own crash marker (C7.1's rule), and
    embedding it as evidence of a *completed* run would borrow a crash's
    shape to imply an outcome nobody saw.
    """
    try:
        bundle = read_bundle(path)
    except (ValueError, OSError, json.JSONDecodeError) as exc:
        raise PaaError(
            f"{path} is not a readable {OBSERVE_SCHEMA} bundle: {exc}"
        ) from exc
    if bundle.phase != "final":
        raise PaaError(
            f"{path} is stuck at phase={bundle.phase!r}: the observer never "
            "finished watching this run, so it is not evidence of one — "
            "re-run paa observe and attest the final bundle"
        )
    raw = path.read_bytes()
    summary = {
        "kind": OBSERVE_SCHEMA,
        "run_id": bundle.run_id,
        "command": list(bundle.command),
        "evidence_tier": bundle.evidence_tier,
        "resume": bundle.resume,
        "exit_code": bundle.child.get("exit_code"),
        "state": bundle.classification.get("state"),
    }
    return summary, raw


def _observed_row(summary: dict[str, Any], blob_sha: str) -> dict[str, Any]:
    exit_code = summary.get("exit_code")
    return {
        "command": summary["command"],
        "source": "observed",
        "evidence_sha256": blob_sha,
        "exit_code": exit_code,
        "state": summary.get("state"),
        "command_green": exit_code == 0 if isinstance(exit_code, int) else None,
    }


def build_bundle(
    *,
    out_dir: Path,
    workspace: Path,
    goal: str,
    requester: str = "operator",
    requester_kind: str = "cli",
    diff_since: str | None = None,
    asserted_commands: Iterable[Sequence[str]] = (),
    test_bundles: Iterable[Path] = (),
    asserted_tests: Iterable[Sequence[str]] = (),
    evidence_bundles: Iterable[Path] = (),
    extra_evidence: Iterable[tuple[str, Path, dict[str, Any]]] = (),
    observed_command_rows: Iterable[Mapping[str, Any]] = (),
    approvals: Iterable[Approval] = (),
    key: KeyFile,
) -> BuildResult:
    """Build one attested change bundle at ``out_dir``.

    The sequence is the crash discipline: the ``started`` skeleton lands
    first, then evidence is gathered and blobs are written, then the
    signed final manifest replaces the skeleton atomically. A build that
    dies mid-way leaves a manifest that says so.

    Beyond the observe-bundle doors, two additive inputs serve the
    recorder (C7.3): ``extra_evidence`` stores arbitrary artifact files
    as evidence blobs — the recorder's hash-chained flight record rides
    the chain as ``paa-record/1`` evidence — and ``observed_command_rows``
    carries commands a flight record *witnessed executing*, each citing
    the record blob's digest. Both are held to the same rule the
    observed/asserted split states: a row may only claim ``observed``
    when a blob in *this* bundle backs its ``evidence_sha256``.
    """
    from paa.config import get_settings
    from paa.ledger.mandate import explicit_mandate
    from paa.storage.coldlake.cas import ContentAddressedStore

    if not goal.strip():
        raise PaaError(
            "an attested bundle needs a goal — the mandate's answer to "
            "'what was this work for' is the one field a reviewer cannot "
            "supply later"
        )
    workspace = workspace.resolve()
    created_at = _utc_now_iso()
    bundle_id = hashlib.sha256(
        canonical_json(
            {"goal": goal, "workspace": str(workspace), "at": created_at}
        ).encode()
    ).hexdigest()[:24]

    # -- phase 1: the started skeleton (the crash marker) ------------------
    _write_json_atomic(
        out_dir / "manifest.json",
        {
            "schema": ATTEST_SCHEMA,
            "phase": "started",
            "bundle_id": bundle_id,
            "created_at": created_at,
            "tool_version": ATTEST_TOOL_VERSION,
            "sections": [],
            "blobs": [],
            "chain": [],
            "chain_root": None,
            "signature": None,
            "notes": {"workspace": str(workspace)},
        },
    )

    # -- phase 2a: evidence blobs first, so section rows can cite them ----
    store = ContentAddressedStore(out_dir)
    blob_entries: list[dict[str, Any]] = []

    test_rows: list[dict[str, Any]] = []
    command_rows: list[dict[str, Any]] = []
    evidence_rows: list[dict[str, Any]] = []

    for path in test_bundles:
        summary, raw = _observe_evidence(path)
        ref = store.put(raw, compress=False)
        blob_entries.append(
            {"sha256": ref.sha256, "bytes": ref.size_bytes,
             "role": f"evidence:observe:{summary['run_id']}"}
        )
        evidence_rows.append(
            {
                "kind": summary["kind"],
                "run_id": summary["run_id"],
                "sha256": ref.sha256,
                "evidence_tier": summary["evidence_tier"],
                "resume": summary["resume"],
                "command": summary["command"],
                "exit_code": summary["exit_code"],
                "state": summary["state"],
            }
        )
        test_rows.append(_observed_row(summary, ref.sha256))

    for path in evidence_bundles:
        summary, raw = _observe_evidence(path)
        ref = store.put(raw, compress=False)
        blob_entries.append(
            {"sha256": ref.sha256, "bytes": ref.size_bytes,
             "role": f"evidence:observe:{summary['run_id']}"}
        )
        evidence_rows.append(
            {
                "kind": summary["kind"],
                "run_id": summary["run_id"],
                "sha256": ref.sha256,
                "evidence_tier": summary["evidence_tier"],
                "resume": summary["resume"],
                "command": summary["command"],
                "exit_code": summary["exit_code"],
                "state": summary["state"],
            }
        )
        command_rows.append(_observed_row(summary, ref.sha256))

    # The recorder's door (C7.3): arbitrary evidence artifacts — the
    # hash-chained flight record above all — stored as blobs with the
    # caller's row fields, which must name their own ``kind`` so a
    # reader never meets an evidence row it cannot interpret. The
    # digest is stamped here, from the stored bytes, because the blob's
    # name is the checksum: a caller cannot cite a digest this bundle
    # does not actually carry.
    for role, path, row_fields in extra_evidence:
        raw = path.read_bytes()
        ref = store.put(raw, compress=False)
        kind = str(row_fields.get("kind") or "")
        if not kind:
            raise PaaError(
                f"extra evidence {path} carries no 'kind' in its row — an "
                "evidence row a reader cannot interpret is not evidence"
            )
        run_ref = str(row_fields.get("run_id") or path.stem)
        blob_entries.append(
            {"sha256": ref.sha256, "bytes": ref.size_bytes,
             "role": f"evidence:{role}:{run_ref}"}
        )
        row = dict(row_fields)
        row["sha256"] = ref.sha256
        evidence_rows.append(row)

    # The recorder's command rows (C7.3): commands a flight record
    # witnessed executing through the policy proxy. Validated against
    # the blobs this bundle actually carries — an "observed" row whose
    # evidence digest names no blob in *this* bundle is exactly the
    # quietly-false citation the observed/asserted split exists to
    # prevent, so it is refused, loudly.
    backed_digests = {b["sha256"] for b in blob_entries}
    for raw_row in observed_command_rows:
        row = dict(raw_row)
        command = row.get("command")
        if (
            not isinstance(command, list)
            or not command
            or not all(isinstance(part, str) for part in command)
        ):
            raise PaaError(
                f"an observed command row needs a non-empty 'command' list "
                f"of strings; got {command!r}"
            )
        cited = row.get("evidence_sha256")
        if not isinstance(cited, str) or cited not in backed_digests:
            raise PaaError(
                "an observed command row cites evidence "
                f"{cited!r}, which is not a blob in this bundle — an "
                "observed row must be backed by bytes the bundle carries"
            )
        exit_code = row.get("exit_code")
        command_rows.append(
            {
                "command": command,
                "source": "observed",
                "evidence_sha256": cited,
                "exit_code": exit_code,
                "state": row.get("state"),
                "command_green": exit_code == 0 if isinstance(exit_code, int) else None,
            }
        )

    # -- phase 2b: the six section payloads ---------------------------------
    mandate = explicit_mandate(
        kind=requester_kind,
        requester_id=requester,
        policy=get_settings().policy,
        workspace=str(workspace),
    )

    # The workspace manifest: the content identity of every file, so a
    # reviewer can check a clean checkout against the attestation. .git
    # is skipped by the walker (the observe bundle's own rule) because
    # git's churn is the git block's business, not the content manifest's.
    snapshot = snapshot_manifest(workspace)

    sections: dict[str, dict[str, Any]] = {
        "mandate": {"v": 1, "goal": goal, **mandate.granted_payload()},
        "diff": {
            "v": 1,
            "workspace": str(workspace),
            "git": _git_facts(workspace, diff_since),
            "manifest": {
                "count": snapshot.count,
                "files": [f.to_json() for f in snapshot.files],
                "truncated": False,
            },
        },
        "commands": {
            "v": 1,
            "entries": command_rows
            + [
                {
                    "command": list(cmd),
                    "source": "asserted",
                    "evidence_sha256": None,
                    "exit_code": None,
                    "state": None,
                    "command_green": None,
                    "note": "asserted by the producer; no observe bundle "
                    "witnessed it",
                }
                for cmd in asserted_commands
            ],
        },
        "tests": {
            "v": 1,
            "entries": test_rows
            + [
                {
                    "command": list(cmd),
                    "source": "asserted",
                    "evidence_sha256": None,
                    "exit_code": None,
                    "state": None,
                    "command_green": None,
                    "note": "asserted by the producer; no observe bundle "
                    "witnessed it and no outcome is claimed",
                }
                for cmd in asserted_tests
            ],
        },
        "approvals": {"v": 1, "entries": [a.to_json() for a in approvals]},
        "evidence": {"v": 1, "bundles": evidence_rows},
    }

    # -- phase 2c: section blobs, then the chain over their digests --------
    section_digests: list[tuple[str, str]] = []
    section_sizes: dict[str, int] = {}
    for name in SECTION_ORDER:
        raw = canonical_json(sections[name]).encode("utf-8")
        ref = store.put(raw, compress=False)
        blob_entries.append(
            {"sha256": ref.sha256, "bytes": ref.size_bytes, "role": f"section:{name}"}
        )
        section_digests.append((name, ref.sha256))
        section_sizes[name] = ref.size_bytes

    steps = chain_steps(section_digests)
    chain_root = steps[-1]

    # -- phase 3: the signature and the final manifest ----------------------
    signed_header = {
        "schema": ATTEST_SCHEMA,
        "bundle_id": bundle_id,
        "created_at": created_at,
        "tool_version": ATTEST_TOOL_VERSION,
        "sections": [
            {"name": name, "sha256": digest} for name, digest in section_digests
        ],
        "chain_root": chain_root,
    }
    signed_bytes = canonical_json(signed_header).encode("utf-8")
    signature_value = sign(signed_bytes, key.private_b64)

    warnings: list[str] = []
    if not key.secure:
        warnings.append(
            "signed with the symmetric dev fallback — forgeable by anyone who "
            "can verify; not an attestation, a rehearsal of one"
        )
    if sections["tests"]["entries"] and all(
        entry["source"] == "asserted" for entry in sections["tests"]["entries"]
    ):
        warnings.append(
            "every test row is asserted: no observe bundle witnessed a test "
            "run, so the bundle records claims, not evidence"
        )

    blobs_index = sorted(blob_entries, key=lambda b: (b["role"], b["sha256"]))
    manifest = {
        **signed_header,
        "phase": "final",
        "blobs": blobs_index,
        "chain": steps,
        "signature": {
            "algorithm": key.algorithm,
            "key_id": key.key_id,
            "public_key_b64": key.public_b64,
            "value_b64": signature_value,
            "signed_over": _sha256_bytes(signed_bytes),
        },
        "notes": {
            "workspace": str(workspace),
            "diff_since": diff_since,
            "signing_secure": key.secure,
        },
    }
    _write_json_atomic(out_dir / "manifest.json", manifest)

    observed_tests = sum(
        1 for entry in sections["tests"]["entries"] if entry["source"] == "observed"
    )
    asserted_test_count = len(sections["tests"]["entries"]) - observed_tests
    return BuildResult(
        bundle_dir=out_dir,
        bundle_id=bundle_id,
        chain_root=chain_root,
        key_id=key.key_id,
        algorithm=key.algorithm,
        section_sizes=section_sizes,
        evidence_count=len(evidence_rows),
        observed_tests=observed_tests,
        asserted_tests=asserted_test_count,
        warnings=tuple(warnings),
    )


def read_manifest(path: Path) -> dict[str, Any]:
    """Read and schema-check a manifest. Raises ``ValueError`` for a
    foreign schema — a directory that is not a ``paa-attest-bundle/1``
    must not be silently reinterpreted."""
    raw: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))
    if raw.get("schema") != ATTEST_SCHEMA:
        raise ValueError(
            f"{path} is not a {ATTEST_SCHEMA} manifest "
            f"(schema={raw.get('schema')!r})"
        )
    return raw
