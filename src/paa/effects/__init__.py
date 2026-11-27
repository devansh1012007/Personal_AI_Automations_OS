"""The effect registry: scope-owned effects, compensation as data (C9.2).

Wave 9's core row. README's Temporal composability section states the rule
this package implements: *"Not every effect is reversible. ... PAA must
preserve those facts in the ledger and treat a correction or compensation
as a new, separately authorized action."* The appendix in
``docs/VISION.md`` proposes the object model (effect records with an
owner, an operation identity, recovery information, authority provenance
and a lifecycle state); this package is the **smallest honest slice** of
it, and every boundary is stated rather than implied:

* an effect is a *record*, not a promise — a compensation path is declared
  as data (``remove`` or ``restore`` with the prior content's digest and
  blob address), never as a callable, never as a model judgment;
* the registry is a **reader** over the append-only ledger, not a second
  store — effect rows ARE the ledger's rows (a versioned ``effect`` field
  riding existing ``PIPELINE_CALL_COMPLETED`` payloads, ADR-0054), so the
  one invariant holds by construction: no state exists that a replay of
  the ledger cannot rebuild;
* rollback walks a scope's effects newest-first and executes each
  compensation as a **new, separately authorized, journalled action on a
  fresh lineage** — never an "undo", never history editing; the rollback
  lineage's rows reference the effect ids they compensate, and the
  compensated set is derived from the ledger at every run, so a re-run
  after a crash resumes with no double compensation;
* irreversible effects are **reported, not "fixed"** — the report names
  them, their owner call, and states plainly that no compensation exists.

The package's own map (the ``paa.thread`` convention: this init is the
map, the submodules are the territory — importing it pulls in nothing
beyond the stdlib, which is what lets ``pipelines/journal.py`` import the
spec at module scope without a cycle):

* :mod:`paa.effects.spec` — :class:`~paa.effects.spec.EffectSpec`,
  :class:`~paa.effects.spec.CompensationSpec`, the deterministic
  :func:`~paa.effects.spec.effect_id_for`, and the payload
  serialization both readers and the journal door share;
* :mod:`paa.effects.files` — :func:`~paa.effects.files.capture_prior`
  (call-site half: prior bytes to the blob store, compensation as data)
  and :func:`~paa.effects.files.apply_compensation` (executor half:
  jailed, idempotent-in-outcome restore/remove);
* :mod:`paa.effects.registry` — :class:`~paa.effects.registry.EffectRegistry`,
  the ledger reader: a scope's effects newest-first, and the compensated
  set derived from ``compensates`` rows;
* :mod:`paa.effects.rollback` — :func:`~paa.effects.rollback.rollback_scope`
  and :class:`~paa.effects.rollback.RollbackReport`, the service behind
  ``paa rollback``.

The v1 compensable surface is workspace file effects (``file_write`` /
``file_edit``: restore prior bytes or remove a creation), jailed by the
same workspace rules any file op passes. External effects default to the
irreversible marker unless the call site declared a compensation.
"""
