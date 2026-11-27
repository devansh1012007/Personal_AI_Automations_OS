# Polish roadmap — themes, onboarding, docs site (Wave 9, C9.6)

Status: DESIGN. No production code. The spec's own Wave 9 list names
these rows as cooldown polish (`docs/plans/2026-11-21-next-spec-2.md`
§8: "themes, `paa init` onboarding wizard, docs site; observability
validated (C5.5); Docker live-server pass"). This document orders them
by what each one unblocks, states the honest boundaries, and leaves the
build order to the operator's own priorities.

## 1. `paa init` — the onboarding wizard

The gap: a first-run operator meets `paa doctor` (good — it never
overstates) but has no guided path from zero to a first green loop.

Design (small, honest, testable):
- `paa init` walks the doctor's own checklist in order: home directory,
  storage backend, sandbox backend probe (with the degradation warning
  stated, never silent), model provider (the offline echo provider as
  the default first-run provider — a first loop that works with no key,
  and says so), permission mode.
- Every step writes `.env`-shaped settings with the WHY as a comment
  beside each (the file teaches while it configures).
- The wizard ends by offering the scaffold door: `paa pipeline new` —
  the C9.5 kit is the natural "first loop" for a new operator, proven
  green through the gates as part of its own tests.
- Exit contract like every door: 0 configured, 1 a step refused (named),
  2 usage. Idempotent: re-running reports what is already set and why.

Boundary: the wizard never edits an existing setting without naming the
old value and asking — an operator's configured runtime is production,
not a setup artifact.

## 2. Themes

The TUI's palette is currently the kit's default. The design question is
narrow and worth stating: themes are a *rendering* concern, and the
console's own rule ("a view + thin dispatcher: no new authority, no new
gate") is the constraint. A theme is a mapping from the existing
semantic tokens (verdict colors, trust levels, evidence-tier badges) to
colors — declared as data, loaded like overlays (tighten-only has no
meaning here; the loader's *shape* is the reuse), never a code path.
`paa chat --theme <name>` with a small builtin set (default, high-
contrast, light) and per-user overrides under `PAA_HOME/themes/`.

Boundary: no theme may change what is *shown*, only how it renders — a
theme that could hide a verdict is a policy change smuggled in as
cosmetics.

## 3. The docs site

The in-tree docs (`docs/modules/*`, the ADRs, AGENTS.md, README.md) are
the source of truth and the conventions lock their citations — the site
must be a *rendered view* of them, never a second copy that can drift.

Design: a static generator whose ONLY job is layout — it reads the
in-tree markdown and the citation graph (which the conventions suite
already builds as data) and renders; the CI gate renders and diffs, so a
citation that would render broken on the site fails the same gate that
keeps it honest in-tree. The compatibility matrix (C9.5's
`docs/modules/authoring.md`) and the ADR index are the two pages that
justify a site beyond GitHub's own rendering — both are tables the
conventions suite keeps true.

Boundary: no docs-content PRs ever target the site layer; the site layer
never contains prose.

## 4. The two validation rows the spec names

- **Observability validated (C5.5):** the OTel pipeline exists; the
  validation row is a real end-to-end pass — a run whose spans arrive at
  a collector, asserted against the ledger's own rows. Design note: the
  assertion target should be the SPAN-to-EVENT correspondence, because
  that correspondence is the claim observability makes.
- **Docker live-server pass:** `paa serve` under the deployment topology
  (`AGENTS.md` §Docker) with the live S3/postgres stack, the same
  services CI's gate (all) already runs — the pass is the operator-run
  confirmation CI cannot substitute for (CI proves the tests; the pass
  proves the deployment).

## Suggested order (what each unblocks)

1. `paa init` — unblocks every other adoption row.
2. Observability validation + the Docker pass — prove the existing
   runtime before polishing its surface.
3. Themes — small, self-contained, riding the overlay loader's shape.
4. The docs site — last, because it renders a corpus that should stop
   moving first.

## Related documents

- `docs/plans/2026-11-21-next-spec-2.md` §8 — the Wave 9 polish list.
- `docs/modules/authoring.md` — the scaffold door `paa init` would end on.
- `docs/operations/` — the deployment topology the Docker pass confirms.
