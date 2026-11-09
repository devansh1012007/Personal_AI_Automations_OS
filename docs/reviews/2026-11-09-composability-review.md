# Review note — VISION.md composability appendix vs arXiv 2608.25512

*Date of this review: 2026-11-09. Reviewer: W0-IMPL-03 (T-N0-3, run 20261109-next-spec).*
*Scope: `docs/VISION.md` "Appendix: Temporal and spatial composability for PAA", lines 401–964*
*(line numbers as of 2026-11-09; the appendix has been deliberately NOT trimmed, per owner decision).*
*Paper: "A Programming Paradigm for Spatiotemporal Composability", Yifan Shi, Wei Zhang, Tianyi Cui,*
*arXiv:2608.25512 [cs.PL], submitted 26 August 2026, 92 pages.*

## Fetch record (what was actually verified, and how far)

The arXiv abstract page `https://arxiv.org/abs/2608.25512` was fetched on 2026-11-09 and returned
successfully. The abstract page confirms the paper's existence, title, authors, submission date,
categories (cs.PL, cs.SE), length (92 pages, 2 tables, 1 figure), and the abstract text quoted in
rows C1–C7 below.

What was **not** done: the 92-page PDF body was not retrieved, and no theorem, calculus, or
metatheory claim has been checked against the full text. All "Supported" verdicts below therefore
mean **supported at abstract level**. If the body qualifies or contradicts the abstract, rows
C1–C7 must be revisited. Nothing in this note summarises the full paper, because the full paper
was not read.

The paper is dated 26 August 2026 — post-cutoff/future-dated relative to model knowledge, but
publicly fetchable and real. That is why the appendix carries the banner

> UNVERIFIED — paper not fetched or dated 2026, claims subject to review

(the "dated 2026" disjunct holds; an abstract-page fetch occurred on 2026-11-09 — see above for
its exact limits).

## Verdict key

- **Supported** — the cited claim matches content confirmed in the fetched abstract.
- **Unsupported** — the cited claim is contradicted by the fetched abstract. (None found.)
- **Unverifiable** — the abstract neither confirms nor contradicts it; or it is a PAA design
  proposal rather than a paper claim, so the paper cannot support it either way.
  "Design extrapolation" rows are the appendix author applying paper discipline to PAA; the paper
  does not assert them, and the appendix does not present them as quotations.

## Per-claim table

| # | Appendix claim (with line refs) | Verdict | Evidence / reason |
|---|---|---|---|
| C1 | The paper *A Programming Paradigm for Spatiotemporal Composability* exists, by Yifan Shi, Wei Zhang, and Tianyi Cui, dated 26 August 2026 (`docs/VISION.md:410-411`) | Supported | arXiv abstract page: title, three authors (Shi with Peking University / DeepSeek-AI), `[Submitted on 26 Aug 2026]`, arXiv:2608.25512 [cs.PL]. |
| C2 | Temporal composability = a component can be removed and its effects accounted for and, where reversible, reverted (`docs/VISION.md:414-415`) | Supported | Abstract: "temporal composability, the ability to completely revert a component's side effects upon removal". Note "completely" (abstract) vs the appendix's "where reversible" (appendix) — the appendix is *more* cautious than the abstract; that direction is safe. |
| C3 | Spatial composability = a component declares what it requires and the runtime reacts correctly when requirements become satisfied/unsatisfied/changed (`docs/VISION.md:416-417`) | Supported | Abstract: "spatial composability, the ability to declare and reactively manage inter-component dependencies". |
| C4 | The paper develops *revertible effects* (`docs/VISION.md:419`) | Supported | Abstract: "we formalize revertible effects, in which every context transformation carries an inverse that the runtime holds". Residual nuance: whether the paper's "every context transformation carries an inverse" extends to arbitrary external effects is a body-level question; the appendix (L434-436) already declines to assume external reversibility. |
| C5 | The paper develops *reactive coeffects* (`docs/VISION.md:419-420`) | Supported | Abstract: "we formalize reactive coeffects, in which every context change is classified against a component's coeffect specification to drive its activation and deactivation". |
| C6 | Effects and coeffects are mediated through a shared context; the two context kinds are unified (`docs/VISION.md:419-420`) | Supported | Abstract: "unify the effect context and the coeffect context into a single context type and mediate every effect and coeffect through it, yielding … the context paradigm". |
| C7 | Cordis is the paper's reference implementation (`docs/VISION.md:420`) | Supported | Abstract: "We implement these ideas in Cordis, a meta-framework of spatiotemporal composability that provides a core library with effect tracking and coeffect resolution, as well as a declarative component loader with configuration reconciliation and hot module replacement." |
| C8 | "PAA should adopt the underlying discipline, not copy a plugin-oriented API or assume that every real-world action is reversible" (`docs/VISION.md:420-422`) | Unverifiable (design judgment) | This is the appendix author's engineering recommendation, not a claim about the paper. The abstract neither endorses nor forbids adopting the discipline for PAA. Not presented as a quotation; honestly framed as "should". |
| C9 | PAA redefinitions of temporal/spatial composability (durable scope-owned lifecycle accounting; declarative reactive policy-aware dependency resolution) and the explicit non-claims (`docs/VISION.md:424-436`) | Unverifiable (design extrapolation) | PAA-specific restatement. Consistent in direction with C2/C3; the operational machinery (scopes, leases, mandates) is PAA's, not the paper's. |
| C10 | The three graphs must remain distinct: component dependency graph, task execution DAG, effect ownership graph (`docs/VISION.md:438-451`) | Unverifiable (design extrapolation) | PAA architecture proposal; no counterpart claim in the abstract. The abstract's system-level composition result ("carries spatiotemporal composability from a single component to a whole system of interleaved components") is about interleaving, not about graph separation — the appendix neither cites nor distorts it. |
| C11 | Task-scoped composition model: TaskSpec, Mandate, HarnessSpec, component declarations, resolved context, execution DAG, effect scopes (`docs/VISION.md:453-488`) | Unverifiable (design extrapolation) | PAA proposal. C6 supports the *idea* of a mediated context; the seven PAA objects are not paper entities. |
| C12 | Temporal machinery: owner/scope hierarchy, effect lifecycle record, reversibility classes, prepare/apply/reconcile/dispose semantics, candidate ledger events, component lifecycle, stop/unload/rollback/replay distinctions (`docs/VISION.md:490-634`) | Unverifiable (design extrapolation) | The most detailed PAA elaboration in the appendix. C4 (revertible effects) is the only paper touchpoint; the six-class reversibility table, the `EFFECT_*` event names, and the lifecycle diagram are PAA design, internally consistent and explicitly marked as proposals ("names are illustrative", "exact schema is open"). |
| C13 | Spatial machinery: provider-independent requirements, bounded leases, reactive lifecycle events, mediated `RuntimeContext`, pinned-but-reactive resolution (`docs/VISION.md:636-727`) | Unverifiable (design extrapolation) | C5 (reactive coeffects) and C6 (mediated context) support the *discipline*; the lease/authorization separation ("a requirement is not permission", L657-659) is a PAA security addition the paper does not make. |
| C14 | Worked example (two concurrent tasks), illustrative YAML declaration, immutable-ledger boundary, architecture-relationship table, invariants, validation scenarios, adoption sequence, open decisions, positioning consequence (`docs/VISION.md:729-964`) | Unverifiable (design extrapolation) | Self-contained PAA design content. No paper factual claims; the invariants/scenarios are framed as future tests ("should become executable tests", L846-848), which is the correct epistemic status. |

## Bottom line

- **No unsupported claims found.** Every factual statement the appendix makes *about* the paper
  (C1–C7) matches the fetched abstract.
- **The overwhelming majority of the appendix (C9–C14, roughly 540 of 563 lines) is PAA design
  extrapolation** — it is not supported *or* contradicted by the paper, and the appendix is
  careful to frame it as proposal ("not an implementation claim", "names are illustrative",
  "exact schema is open"). This note changes nothing about that content; the banner exists so no
  reader mistakes extrapolation for paper support.
- **Residual risk:** full-text verification against the 92-page PDF body (in particular the
  scope of "revertible effects" and the metatheory) remains outstanding and is recorded as a
  follow-up in the T-N0-3 implementation report.
- **The appendix was not trimmed.** Per owner decision, it is kept and marked UNVERIFIED.
