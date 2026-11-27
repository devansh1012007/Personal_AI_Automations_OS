"""Workspace-file compensation: capture prior state, restore or remove.

The v1 compensable surface. Two halves, both deterministic:

* :func:`capture_prior` — called *before* a mutating call's effect lands:
  it reads the target's current bytes, stores them in the
  content-addressed blob store, and returns the
  :class:`~paa.effects.spec.CompensationSpec` the call site attaches to
  its ``PIPELINE_CALL_COMPLETED`` row. A file that does not yet exist
  yields ``remove`` (the write creates it; compensating removes it); a
  file that exists yields ``restore`` (compensating puts the prior bytes
  back, fetched by address and verified by digest).
* :func:`apply_compensation` — called by the rollback to execute one
  compensation. The workspace jail applies exactly as it does to any file
  op (:func:`paa.sandbox.base.assert_inside_workspace` — both sides fully
  resolved, so a ``..`` segment or a symlink in the recorded path cannot
  escape), and every execution is *idempotent in outcome*: a restore onto
  a file that already holds the prior bytes reports ``already_prior`` and
  writes nothing, a remove of an absent file reports ``absent`` and
  unlinks nothing. That is what makes crash-resume safe — a kill between
  the file work and the completion row re-runs into a no-op, not a
  double-write.

The v1 boundary, stated: only workspace files are compensable here.
External effects are the caller's to declare — an un-declared external
call site records :attr:`~paa.effects.spec.EffectKind.IRREVERSIBLE`, and
the rollback reports those rather than inventing an undo the provider
never promised.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from paa.core.errors import SandboxError
from paa.effects.spec import CompensationAction, CompensationSpec
from paa.ledger.events import BlobStoreLike, store_blob_bytes
from paa.sandbox.base import assert_inside_workspace, resolve_workspace

__all__ = [
    "CompensationOutcome",
    "apply_compensation",
    "capture_prior",
]

#: Outcome vocabulary of one executed compensation, as recorded in the
#: compensation row's result. ``restored`` / ``removed`` did work;
#: ``already_prior`` / ``absent`` found the world already in the
#: compensated state (the idempotent resume case); the rollback report
#: carries the same words so it never overstates what happened.
_OUTCOME_RESTORED = "restored"
_OUTCOME_ALREADY_PRIOR = "already_prior"
_OUTCOME_REMOVED = "removed"
_OUTCOME_ABSENT = "absent"


@dataclass(frozen=True, slots=True)
class CompensationOutcome:
    """What one executed compensation did, as plain data."""

    outcome: str
    action: CompensationAction
    path: str
    bytes_sha256: str | None
    """Digest of the bytes written (a restore), or of the file removed."""

    def to_result(self) -> dict[str, Any]:
        """The compensation call's recorded result (ledger-safe: no content)."""
        return {
            "outcome": self.outcome,
            "action": self.action.value,
            "path": self.path,
            "bytes_sha256": self.bytes_sha256,
        }


def _jailed_target(workspace_root: Path, rel_path: str) -> Path:
    """Resolve a recorded compensation path inside the workspace jail.

    The recorded path was validated as relative and normalized at
    construction; this is the *enforcement* half, on the operator-named
    root, at execution time — the same jail every file op passes, so a
    compensation can never write where the original call could not.
    """
    root = resolve_workspace(workspace_root)
    return assert_inside_workspace(root / Path(rel_path), root)


def _sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def capture_prior(
    workspace_root: Path, rel_path: str, blob_store: BlobStoreLike
) -> CompensationSpec:
    """Capture the target's prior state and declare how to put it back.

    Reads the file's bytes (if any), stores them in the blob store, and
    returns the compensation spec for a write about to land on
    ``rel_path``: ``remove`` when the file is being created (nothing
    existed to restore) or ``restore`` with the prior digest and blob
    address when it is being replaced. Call this *before* the mutation;
    the spec is only as truthful as the moment it was captured at.
    """
    target = _jailed_target(workspace_root, rel_path)
    if not target.exists():
        return CompensationSpec(
            action=CompensationAction.REMOVE,
            path=rel_path,
            prior_sha256=None,
            prior_bytes_ref=None,
        )
    if not target.is_file():
        # A directory (or a special file) at the target is not a state a
        # file effect can capture honestly: a "restore" of nothing would
        # lie about what was there. Refuse — the call site records the
        # effect as irreversible rather than promising an undo it cannot
        # keep.
        raise SandboxError(
            "cannot capture prior state of a non-file target",
            path=str(target),
        )
    data = target.read_bytes()
    digest = _sha256_bytes(data)
    # Store the prior bytes through the verified put (the digest the
    # descriptor pins is recomputed from the same bytes here, so a store
    # that addressed different bytes fails loudly at capture time, not at
    # restore time).
    descriptor = store_blob_bytes(data, blob_store)
    return CompensationSpec(
        action=CompensationAction.RESTORE,
        path=rel_path,
        prior_sha256=digest,
        prior_bytes_ref=str(descriptor["sha256"]),
    )


def apply_compensation(
    workspace_root: Path, spec: CompensationSpec, blob_store: BlobStoreLike
) -> CompensationOutcome:
    """Execute one file compensation and report what actually happened.

    Deterministic and idempotent in outcome. ``restore`` verifies the
    fetched bytes against the recorded digest before writing (a blob that
    holds the wrong bytes is a broken record, not a restore); ``remove``
    unlinks only what exists. The returned outcome names the no-op cases
    (``already_prior``, ``absent``) as no-ops, so a re-run after a crash
    between the file work and its completion row records the truth: the
    world was already in the compensated state.
    """
    target = _jailed_target(workspace_root, spec.path)
    if spec.action is CompensationAction.REMOVE:
        if not target.exists():
            return CompensationOutcome(
                outcome=_OUTCOME_ABSENT,
                action=spec.action,
                path=spec.path,
                bytes_sha256=None,
            )
        removed_sha = _sha256_bytes(target.read_bytes())
        target.unlink()
        return CompensationOutcome(
            outcome=_OUTCOME_REMOVED,
            action=spec.action,
            path=spec.path,
            bytes_sha256=removed_sha,
        )

    # restore
    prior_sha = spec.prior_sha256
    prior_ref = spec.prior_bytes_ref
    if prior_sha is None or prior_ref is None:
        # Unreachable for a spec that passed construction validation
        # (restore requires both fields); a hand-built record that skips
        # validation is refused here rather than guessed at.
        raise SandboxError(
            "restore compensation lacks prior content", path=spec.path
        )
    if target.is_file():
        current_sha = _sha256_bytes(target.read_bytes())
        if current_sha == prior_sha:
            return CompensationOutcome(
                outcome=_OUTCOME_ALREADY_PRIOR,
                action=spec.action,
                path=spec.path,
                bytes_sha256=current_sha,
            )
    data = blob_store.get(prior_ref)
    actual = _sha256_bytes(data)
    if actual != prior_sha:
        raise SandboxError(
            "compensation blob does not match the recorded prior digest",
            path=spec.path,
            expected=prior_sha,
            actual=actual,
        )
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(data)
    return CompensationOutcome(
        outcome=_OUTCOME_RESTORED,
        action=spec.action,
        path=spec.path,
        bytes_sha256=actual,
    )
