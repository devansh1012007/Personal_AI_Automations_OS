"""Ledger-driven rollback: compensation as new, separately authorized work.

``paa rollback <scope>`` walks a scope's effect rows **newest-first** and
compensates each one — but the word "rollback" is the command's name, not
its nature. README's Temporal composability rule is explicit: a
compensation is *"a new, separately authorized action"*, never an undo,
never history editing. What that means mechanically here:

* every compensation executes on a **fresh lineage** (a new uuid per
  run), journalled through the same
  :class:`~paa.pipelines.journal.CallJournal` vocabulary every pipeline
  call uses — ``run_started(run_kind="rollback")``, then per effect
  ``call_proposed`` → the file work → ``call_completed`` whose payload
  carries ``compensates: <effect_id>``. Riding the existing vocabulary is
  what buys crash-sweep behaviour for free: a rollback lineage is a
  pipeline-run lineage, the boot sweep already skips it
  (``SKIPPED_PIPELINE_LINEAGE``) and already closes a ``PROPOSED`` row a
  kill left dangling as ``interrupted``;
* the compensated set is **derived from the ledger at every run** (every
  ``PIPELINE_CALL_COMPLETED`` row carrying ``compensates``), never held
  in process state — so a re-run after a crash skips what completed,
  resumes what did not, and cannot double-compensate;
* each executed compensation that mutates a file registers **its own
  effect** (the restore is itself a file write, with its own captured
  prior state), so the ledger's account stays complete: rolling back a
  rollback lineage reverses the rollback, and no file mutation by this
  machinery goes unrecorded;
* **irreversible effects are reported, not "fixed"** — the report names
  the effect, its owner call, and states plainly that no compensation
  exists. A report that overstates recovery is worse than none.

Determinism is total: no model, no heuristic, no ordering beyond the
seq-descending walk. Failures are per-effect — one compensation failing
does not stop the walk; the failure is recorded (``call_failed``) and
reported, and a re-run retries exactly the effects whose compensation is
not recorded as completed.

Exit contract (the CLI maps it): ``0`` at least one compensation executed
this run; ``1`` nothing to roll back (scope absent, no effects, all
already compensated, or every attempt failed); ``2`` usage (the refusal
this module raises as :class:`RollbackRefused`).
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from paa.core.errors import PaaError
from paa.effects.files import apply_compensation, capture_prior
from paa.effects.registry import EffectRegistry
from paa.effects.spec import EffectKind, EffectSpec
from paa.ledger.events import BlobStoreLike
from paa.ledger.store import LedgerStore
from paa.pipelines.journal import CallJournal

__all__ = [
    "EffectOutcome",
    "RollbackRefused",
    "RollbackReport",
    "rollback_scope",
]

#: The rollback run's journal identity. A rollback *is* a pipeline-shaped
#: run on the platform's own vocabulary — ``run_kind="rollback"`` is what
#: distinguishes it in the ledger, and the pipeline-lineage boot-sweep
#: exemption is what keeps a rollback lineage from ever being requeued as
#: task work.
_ROLLBACK_PIPELINE = "effects.rollback"
_ROLLBACK_VERSION = "1.0.0"

#: Status vocabulary of one walked effect, as the report carries it.
STATUS_COMPENSATED = "compensated"
STATUS_ALREADY = "already_compensated"
STATUS_IRREVERSIBLE = "irreversible"
STATUS_FAILED = "failed"


class RollbackRefused(PaaError):
    """The rollback cannot run honestly; the reason is named."""


@dataclass(frozen=True, slots=True)
class EffectOutcome:
    """One walked effect's fate, as data for the report."""

    effect_id: str
    kind: str
    status: str
    owner_call: str
    detail: str

    def to_json(self) -> dict[str, Any]:
        return {
            "effect_id": self.effect_id,
            "kind": self.kind,
            "status": self.status,
            "owner_call": self.owner_call,
            "detail": self.detail,
        }


@dataclass(frozen=True, slots=True)
class RollbackReport:
    """What one rollback run did — the doctor rule applied to recovery.

    The report never overstates: an effect recorded as irreversible says
    so; an already-compensated effect says so; a failed compensation says
    it failed. ``newly_compensated`` is the exit contract's basis, and
    ``exit_code`` is derived, not stored, so the two cannot disagree.
    """

    scope: str
    rollback_lineage: str
    entries: list[EffectOutcome] = field(default_factory=list)

    @property
    def newly_compensated(self) -> int:
        return sum(1 for e in self.entries if e.status == STATUS_COMPENSATED)

    @property
    def already_compensated(self) -> int:
        return sum(1 for e in self.entries if e.status == STATUS_ALREADY)

    @property
    def irreversible(self) -> int:
        return sum(1 for e in self.entries if e.status == STATUS_IRREVERSIBLE)

    @property
    def failed(self) -> int:
        return sum(1 for e in self.entries if e.status == STATUS_FAILED)

    @property
    def exit_code(self) -> int:
        """0 iff this run compensated at least one effect; else 1."""
        return 0 if self.newly_compensated > 0 else 1

    def to_json(self) -> dict[str, Any]:
        return {
            "scope": self.scope,
            "rollback_lineage": self.rollback_lineage,
            "summary": {
                "effects": len(self.entries),
                "compensated": self.newly_compensated,
                "already_compensated": self.already_compensated,
                "irreversible": self.irreversible,
                "failed": self.failed,
            },
            "entries": [e.to_json() for e in self.entries],
        }


async def rollback_scope(
    store: LedgerStore,
    scope: uuid.UUID,
    *,
    workspace_root: Path | None = None,
    blob_store: BlobStoreLike | None = None,
) -> RollbackReport:
    """Compensate one scope's uncompensated effects, newest-first.

    The scope is the effect-bearing lineage (in v1, a pipeline run's id —
    ADR-0054 states the task-to-runs resolution boundary). The walk is
    deterministic: effects in seq-descending order, each uncompensated
    one executed as a journalled call on a fresh rollback lineage, the
    compensated set derived from the ledger before the walk begins. A
    compensable effect with no workspace root or no blob store refuses
    up front — a rollback that silently skipped file compensations would
    be the exact overstatement this module refuses to make.
    """
    registry = EffectRegistry(store, blob_store=blob_store)
    effects = await registry.effects_for_scope(scope)
    compensated = await registry.compensated_effect_ids()

    pending = [
        e
        for e in effects
        if e.spec.effect_id not in compensated
        and e.spec.kind is not EffectKind.IRREVERSIBLE
        and e.spec.compensation is not None
    ]
    if pending and (workspace_root is None or blob_store is None):
        raise RollbackRefused(
            "compensable file effects need a workspace (--workspace, the jail's "
            "root) and a blob store (where a restore's prior bytes live)",
            scope=str(scope),
            pending=len(pending),
        )

    lineage = uuid.uuid4()
    journal = CallJournal(store, lineage, blob_store=blob_store)
    await journal.run_started(
        _ROLLBACK_PIPELINE, _ROLLBACK_VERSION, run_kind="rollback"
    )

    entries: list[EffectOutcome] = []
    seq = 0
    for item in effects:
        spec = item.spec
        owner = f"{spec.owner.lineage}/call:{spec.owner.seq}"
        if spec.effect_id in compensated:
            entries.append(
                EffectOutcome(
                    effect_id=spec.effect_id,
                    kind=spec.kind.value,
                    status=STATUS_ALREADY,
                    owner_call=owner,
                    detail="compensation recorded in the ledger; skipped",
                )
            )
            continue
        if spec.kind is EffectKind.IRREVERSIBLE or spec.compensation is None:
            entries.append(
                EffectOutcome(
                    effect_id=spec.effect_id,
                    kind=spec.kind.value,
                    status=STATUS_IRREVERSIBLE,
                    owner_call=owner,
                    detail=(
                        "no compensation exists for this effect; reported, not fixed"
                    ),
                )
            )
            continue

        seq += 1
        comp = spec.compensation
        await journal.call_proposed(
            seq,
            "effects.compensate",
            {
                "effect_id": spec.effect_id,
                "action": comp.action.value,
                "path": comp.path,
                "prior_sha256": comp.prior_sha256,
            },
        )
        if workspace_root is None or blob_store is None:
            # Unreachable — the pre-walk refusal names this case — but
            # restated here so the branch's precondition is local, not
            # three screens up. A dangling PROPOSED row is closed by the
            # boot sweep, like any interrupted call.
            raise RollbackRefused(
                "compensable file effects need a workspace and a blob store",
                scope=str(scope),
            )
        try:
            # The compensation's own effect: capture the file's current
            # state *before* compensating, so the restore this run is
            # about to perform is itself reversible — the ledger keeps a
            # complete account of every file this machinery touches.
            own_comp = capture_prior(workspace_root, comp.path, blob_store)
            outcome = apply_compensation(workspace_root, comp, blob_store)
        except (PaaError, OSError, ValueError) as exc:
            reason = exc.message if isinstance(exc, PaaError) else type(exc).__name__
            await journal.call_failed(seq, "effects.compensate", reason)
            entries.append(
                EffectOutcome(
                    effect_id=spec.effect_id,
                    kind=spec.kind.value,
                    status=STATUS_FAILED,
                    owner_call=owner,
                    detail=f"compensation failed: {reason}",
                )
            )
            continue
        own_effect = EffectSpec.for_call(
            lineage, seq, EffectKind.FILE_WRITE, own_comp
        )
        await journal.call_completed(
            seq,
            "effects.compensate",
            outcome.to_result(),
            effect=own_effect,
            compensates=spec.effect_id,
        )
        entries.append(
            EffectOutcome(
                effect_id=spec.effect_id,
                kind=spec.kind.value,
                status=STATUS_COMPENSATED,
                owner_call=owner,
                detail=f"{outcome.outcome} ({comp.action.value} {comp.path})",
            )
        )

    status = (
        "completed" if not any(e.status == STATUS_FAILED for e in entries) else "partial"
    )
    await journal.run_completed(
        status,
        metrics={
            "effects": len(entries),
            "compensated": sum(1 for e in entries if e.status == STATUS_COMPENSATED),
            "already": sum(1 for e in entries if e.status == STATUS_ALREADY),
            "irreversible": sum(1 for e in entries if e.status == STATUS_IRREVERSIBLE),
            "failed": sum(1 for e in entries if e.status == STATUS_FAILED),
        },
    )
    return RollbackReport(scope=str(scope), rollback_lineage=str(lineage), entries=entries)
