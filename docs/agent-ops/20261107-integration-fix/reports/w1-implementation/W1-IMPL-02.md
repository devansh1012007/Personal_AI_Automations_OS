<!-- ORCH-DIGEST:START -->
agent: W1-IMPL-02 | domain: P-001 redaction of two unredacted str(exc) paths | status: DONE
items: 2 sites fixed (runtime.py:481, cli/main.py:649); 0 already-fixed; 0 not-reproducible
proposed_tasks: 1 completed (SURFACE — T-001-P001): redact + ≤200-rendered bound + explicit marker, both sites driven by tests, reversal-proven
top_risks: (1) ADR-0036 §P01 read at commit time — implementation matches all three spec bullets; marker deliberately inside the 200 budget (§1); (2) raw ANSI/control passthrough at the drain line is a sibling injection class, FINDING not P-001
cross_domain_conflicts: none (W1-IMPL-03's context_agents.py/creator.py untouched; W1-IMPL-01's config.py/ADRs untouched)
out_of_plan_discoveries: 1 (terminal-escape passthrough at cli drain line §4) + 1 fixture-method note (redact() eats an unseparated sk- run — correct behaviour, documented in the test)
confidence: high — two sites + helper verified at HEAD; 7 new tests green; 113 regression tests green; reversal proven; mypy 3 (baseline); ruff 0
last_comms_line_read: 2026-11-07T13:33:39Z W1-IMPL-02 -> ALL | START | T-001-P001 (re-read at commit; W1-IMPL-01 DONE, W1-IMPL-03 in-flight, disjoint)
<!-- ORCH-DIGEST:END -->

# Implementation report — W1-IMPL-02 — P-001 redaction (T-001-P001)

## 0. Scope and method

Task T-001-P001: redact the 2 unredacted `str(exc)` paths (plan P01 / test T01).
WRITE-SET: `src/paa/runtime.py`, `src/paa/cli/main.py`, `tests/runtime/test_redaction_p001.py` (new), `tests/cli/test_redaction_p001.py` (new). Everything else read-only, including ADR-0036 (owned by W1-IMPL-01; cited, not written).

- Branch verified: [RAN `git branch --show-current` -> `ops/20261107-integration-fix`]. HEAD `294fb31` at start.
- Working tree at start: `M docs/agent-ops/20261107-integration-fix/COMMS.md` (ORCH-owned), `M src/paa/config.py` (W1-IMPL-01-owned, untracked-dir `reports/w1-implementation/` present) — neither is mine; I will not touch, stage, or commit them.
- Required reading done in order: STANDING-ORDERS.md, AGENTS.md, 00-mission.md, plan rev 5 (§E P01, §H T01, §K), W0-INV-04 §P-001 (authoritative write-set, Fix A), report/gate templates, then the code sites.
- Code verified at HEAD: `src/paa/runtime.py:474` (already `redact(str(exc))`, imported at :43) vs `:481` (unredacted) — one-function inconsistency as INV-04 states. [VERIFIED src/paa/runtime.py:474,481; :43]
- `src/paa/cli/main.py:649` prints `f"[red]embedding drain failed: {exc}[/red]"` unredacted, unbounded. [VERIFIED src/paa/cli/main.py:642-649]
- Helper: `redact` at `src/paa/models/base.py:256` — stdlib+pydantic+`paa.core` imports only, safe at module scope in cli/main.py. [VERIFIED src/paa/models/base.py:67-78]
- cli/main.py today imports `redact` only function-locally inside `_touch_recalled` (:714) — the drift INV-04 flagged. Fix A uses a module-level import instead (see §1).
- ADR-0036 §P01 comparison at commit time (W1-IMPL-01's `docs/adr/0036-allocator-and-attention-loop.md:102-117`): all three spec bullets match (see §1). No divergence FINDING needed.

## 1. Implementation

### Phase 1 — runtime.py:481 (one line, mirrors :474)

[VERIFIED via `git diff src/paa/runtime.py` post-edit]

```python
except Exception as exc:  # degradation path: never crash boot on vectors
    # PLAN:integration-fix:P001
    log.warning("memory.embedding_sink", status="degraded", error=redact(str(exc)))
    embedding_sink = None
```

Event name (`memory.embedding_sink`), status kwarg, and degrade-to-None behaviour unchanged. `redact` was already imported at `runtime.py:43` (`from paa.models.base import redact`), so the site needed no import change. No truncation at this site: the ADR §P01 bound is for rendered console text; the structlog kwarg is unbounded structured logging (the :474 sibling already logs unboundedly), and the plan assigns the 200-char bound to the CLI path only.

### Phase 2 — cli/main.py:649 (redact + 200-char bound + marker)

[VERIFIED via `git diff src/paa/cli/main.py` post-edit]

**Imports.** Added module-level `from paa.models.base import redact` (alphabetical position, between `paa.core.types` and `paa.sandbox.base`). Rationale: my brief forbids creating "the function-local-import drift INV-04 flagged in touch copies"; `models/base.py` imports stdlib+pydantic+`paa.core` only, so a module-scope import keeps a minimal install importable, and `runtime.py` already imports it at module scope. The now-redundant function-local `from paa.models.base import redact` inside `_touch_recalled` was removed (same file, one line; the name still resolves to the module-level binding, pinned by the existing TestRecallTouch suite — all 22 tests in `tests/cli/test_memory.py` green after the change).

**Constants** (defined immediately above the `drain` command, with `#:` rationale comments citing ADR-0036 §P01 and carrying the `PLAN:integration-fix:P001` tag):

- `_MAX_DRAIN_ERROR_CHARS = 200` — max **rendered** chars of the error body, marker included.
- `_TRUNCATION_MARKER = "... [truncated; redaction best-effort]"` — explicit marker, bracket-free (a bracketed marker is consumed by rich's markup parser — probed), and it carries T01's "redaction best-effort" note verbatim.

**Except block:**

```python
except Exception as exc:
    # PLAN:integration-fix:P001
    safe = redact(str(exc))
    if len(safe) > _MAX_DRAIN_ERROR_CHARS:
        safe = safe[: _MAX_DRAIN_ERROR_CHARS - len(_TRUNCATION_MARKER)]
        safe = f"{safe}{_TRUNCATION_MARKER}"
    # markup=False: the exception text is data, never console markup;
    # a '[/red]' in it would otherwise crash the command (MarkupError).
    console.print(f"embedding drain failed: {safe}", style="red", markup=False)
```

**Order: redact, then slice.** Slicing first could split an `sk-` token and leak the remainder; slicing the already-redacted text cannot (the straddle test in §2 proves it).

**Marker inside the 200 budget (deliberate reading).** The bound is `_MAX_DRAIN_ERROR_CHARS` on the total rendered error body (text + marker). ADR-0036 §P01 says "truncate the scrubbed text to 200 characters; append an explicit truncation marker" — under this implementation the scrubbed-text portion is ≤162 ≤ 200 ✓, the total is ≤200 ✓, the marker is explicit ✓. The strict reading (total ≤200) also satisfies the plan's T01 "output ≤200 chars" wording, whereas the looser reading (200 + marker = 238) would fail it; strict is safer and no spec bullet is violated. Boundary semantics: redacted length ≤200 ⇒ byte-identical render, no marker; >200 ⇒ truncated to exactly 200 rendered chars with marker.

**Why `markup=False` + `style="red"` instead of the `[red]…[/red]` wrapper.** Probed in an isolated script before coding: `[/red]` inside exception text raises `MarkupError: closing tag '[/red]' at position 41 doesn't match any open tag` out of `console.print` — i.e. the original line crashed the whole drain command on hostile exception text. `console.print(..., style="red", markup=False)` renders the same red line, treats the exception text as literal data, and cannot raise on text content (the `[/red]` reversal attempt in §2 shows exit_code 1 on the old line vs 0 on the new one). This is a change at the same line, not scope creep: the brief's hostile-input requirement ("NUL/ANSI in exception text" must not break the command) is unmeetable without it.

### Phase 3 — tests driving both sites

**`tests/runtime/test_redaction_p001.py`** — drives `_build_memory_creator`'s except branch per the sibling pattern (`monkeypatch` `VectorEmbeddingSink` to raise; capture `paa.runtime.log.warning` kwargs). Two tests:

- `test_secret_shaped_exception_text_is_redacted` — exception text contains an `sk-` fixture, a `Bearer …` fixture, and an `api_key:` fixture (each matching `models/base.py:244-246`); asserts the captured `memory.embedding_sink` kwargs contain `***REDACTED***` (positive control), no `sk-` substring, neither raw secret value, and the event/kwargs contract (`status="degraded"`, `_sink is None`). **Reversal-proven:** on the reverted line the captured error is the verbatim fixture text.
- `test_benign_exception_text_passes_through` — pinning: ordinary text logs unchanged (guards over-redaction).

**`tests/cli/test_redaction_p001.py`** — drives the real `paa memory drain` command via Typer CliRunner against a real `Runtime.build` (empty backlog, real `run_batch` report) with `MemoryCreator.drain_pending_embeddings` patched to raise (mirrors the existing drain-isolation test in `tests/cli/test_memory.py`). Five tests:

- secret fixtures (sk-/Bearer) never survive in console output; batch table still printed;
- boundary 200 chars ⇒ rendered in full, no marker;
- boundary 201 chars ⇒ exactly `_BODY_X_RUN` (200 − marker) x's + marker;
- 10 KB exception with the sk- fixture straddling the slice point ⇒ `***REDACTED***` in the render, no token fragment, total exactly 200 (redact-before-truncate is the only order that passes);
- hostile text (é non-ASCII + NUL + ANSI + `[/red]` markup attack + secret + 300-char run) ⇒ exit 0, no `sk-`, `REDACTED` positive control, marker present, batch table printed (proves markup=False + isolation).

Rich folds lines at width 80 in capture; assertions run on whitespace-stripped output with single-word fixture bodies so the reconstruction is exact. **Methodology note (fixture bug found and fixed during verification):** my first huge-fixture was `"x"*140 + sk_token + "x"*10000`; `redact()` returned 154 chars because `x` ∈ `[A-Za-z0-9_\-]`, so the `sk-` pattern legitimately ate the entire 10,000-char run (verified by direct evaluation) and the truncation branch never fired. That is correct `redact()` behaviour, not a binding bug — the fixtures now use a `!` separator and the order-sensitivity is pinned by the straddle test. The lesson is recorded in the test comment.

## 2. Verification (gate-report shape)

Baseline (00-mission.md): ruff 0 · mypy 3 (`subprocess_backend.py:548` POSIX symbols) · pytest 14 pre-existing failures.

- Tree state: branch `ops/20261107-integration-fix`; code commit `f023b2618237287f6f9e62b33e30810e93118440` (4 files: runtime.py, cli/main.py, 2 new test files). Sibling files (`context_agents.py`, `creator.py`, `config.py`, COMMS.md) never staged or committed by me.
- HEAD at report time: see §5.

| Step | Command | Result | vs baseline |
|---|---|---|---|
| Lint (scoped) | `.venv\Scripts\python.exe -m ruff check src/paa/runtime.py src/paa/cli/main.py tests/runtime/test_redaction_p001.py tests/cli/test_redaction_p001.py` | All checks passed (0 findings) [RAN -> as stated] | baseline 0 ✓ |
| Type (src) | `.venv\Scripts\python.exe -m mypy src` | 3 errors, all `sandbox/subprocess_backend.py:548` (POSIX symbols) [RAN -> as stated] | baseline 3 ✓ (my files 0) |
| Unit (new) | `.venv\Scripts\python.exe -m pytest tests/runtime/test_redaction_p001.py -q` | 2 passed | new ✓ |
| Unit (new) | `.venv\Scripts\python.exe -m pytest tests/cli/test_redaction_p001.py -q` | 5 passed (after the fixture-separator fix; first run 3 passed/2 failed on the fixture bug, not the code) | new ✓ |
| Regression | `.venv\Scripts\python.exe -m pytest tests/models/test_model_errors.py tests/runtime/test_memory_creator.py -q` | 113 passed | no P-001 regressions ✓ |
| Regression (touched functions) | `.venv\Scripts\python.exe -m pytest tests/cli/test_memory.py -q` | 22 passed (TestDrain + TestRecallTouch; `_touch_recalled` import removal + drain print change) | no regressions ✓ |
| Import safety | fresh-interpreter `import paa.cli.main` / `import paa.runtime` | both OK | no cycle from the new module-level import ✓ |
| Reversal | manual revert of both lines to the original (no stash), rerun `pytest tests/runtime/test_redaction_p001.py tests/cli/test_redaction_p001.py -q` | 5 failed / 2 passed: runtime secret test (verbatim fixtures in log), cli secret test (no REDACTED), cli 201 (201 raw chars, no marker), cli huge (raw sk- token in tail), cli hostile (exit 1, `MarkupError`). The 2 passes are the design-pinning tests (benign text; boundary 200) which must pass both ways. Fix restored; all 7 re-run green. | ✓ |
| Fresh container / load / migrations | — | NOT-RUN (out of scope, standing order 16) | — |

Ad-hoc evidence gathered en route: rich markup probe [wrote+ran `p001_rich_probe.py` -> `[/red]` raises MarkupError; bracket marker consumed by markup; style+markup=False renders literally with red style; NUL/ANSI pass through; é mangled to � under StringIO capture — encoding-dependent, asserted only as no-crash]; `redact()` runaway-match probe [10181-char fixture -> 154 after redact; root-caused the fixture bug].

`tests/conventions/test_import_conventions.py` (full) timed out at 120s (16 passed before cutoff — it spawns a subprocess per module). Mitigation: fresh-interpreter imports of both touched modules pass, and the new import cannot cycle (`models/base.py` imports stdlib/pydantic/`paa.core` only; already imported at module scope by `runtime.py`). Recorded as partial verification, not a failure.

New failures introduced: **none**. Pre-existing failures unchanged (not re-run — full suite out of scope).

## 3. Long-run answers (source / load / hostile / operability / reversal)

- **Source:** both sites now format exception text through the single house redactor (`models/base.py:redact`) — the same function the :474 sibling already used, so the two lines of one function can no longer drift apart. The CLI bound is a named constant with its ADR citation at the single site that prints exception text to a console.
- **Load (100×, concurrency, partial failure):** unchanged semantics otherwise — the runtime warning is a degrade-path log emitted once at boot; the CLI print is one line per failed drain call, now capped at 200 rendered chars so an unbounded exception cannot flood the operator's scrollback. No new state, no allocation growth; `len()` + slice are O(1) beyond the redact pass already incurred.
- **Hostile input:** `sk-`/`Bearer`/`api_key:` fixtures never survive at either site; 10 KB exception renders ≤200 chars; the token-straddle case proves redact-before-truncate; `[/red]`/markup content is data, not markup (no MarkupError crash); non-ASCII/NUL/ANSI do not crash or leak (see §4 for the residual raw-escape passthrough).
- **Operability:** grep `error=str(exc)` at the two sites is clean; the marker states both truncation and best-effort redaction in operator-visible text; log event names (`memory.embedding_sink`) and the console prefix (`embedding drain failed:`) are preserved, so existing greps/dashboards keep working. Rollback = two-line revert (reversal-proven).
- **Reversal:** revert `runtime.py:481` to `error=str(exc)` → `test_secret_shaped_exception_text_is_redacted` fails (verbatim fixtures in captured kwargs). Revert `cli/main.py` to the `[red]…[/red]` line → 4/5 CLI tests fail (secret leak, unbounded render, missing marker, MarkupError crash). Proven by execution (§2).

## 4. Out-of-scope findings (reported, not fixed)

1. **Terminal-escape passthrough at the drain line.** Raw ANSI escapes and NUL bytes in exception text pass through to the operator's terminal verbatim (`console.print` with `markup=False` does not interpret them, but terminal emulators do — a malicious error string can recolour a log line or inject control sequences, cf. `marketplace/registry_client.py:_excerpt` which escapes to ASCII). Sanitizing control chars (strip or unicode-escape) is a console-rendering policy decision beyond P-001's redact+bound spec; the hostile test pins the current no-crash/bounded behaviour so a future policy change has a seam to land on. Suggested owner: orchestrator to assign (console-capture class, or carry into a TUI/panels hardening item).
2. **`redact()`'s `sk-` pattern is a greedy character-class run.** An unseparated `sk-` token followed by a long `[A-Za-z0-9_\-]` run is consumed whole (no word-boundary, no length cap) — e.g. a 10,181-char string redacted to 154 (`[RAN redact() -> 154]`). This is over-redaction (safe direction) plus a subtle test-authoring trap; pinned and documented in the straddle test's comment. No `redact()` change proposed (its semantics are pinned by `tests/models/test_model_errors.py`).

## 5. Commits and Git state

- Code commit: `f023b2618237287f6f9e62b33e30810e93118440` — `fix(redaction): P-001 redact the 2 unredacted str(exc) paths (T-001-P001)` — exactly 4 files via explicit paths (`git add -- <paths>` + `git commit -- <paths>`). Trailers: `Task: T-001-P001`, `Agent: W1-IMPL-02`, `Wave: W1`, `Report: docs/agent-ops/20261107-integration-fix/reports/w1-implementation/W1-IMPL-02.md`.
- Report commit: separate docs-only commit listing the code SHA above (this file).
- Git discipline: branch verified `ops/20261107-integration-fix` before committing; never switched; no `-a`/`-A`/stash/reset/checkout/push. Sibling state at commit time: W1-IMPL-01 committed (`d1278dd` + report `89d996f`), W1-IMPL-03 has uncommitted `context_agents.py`/`creator.py` edits (disjoint, untouched).
- Self-review before commit (fresh reads of the diff): correctness (both branches + boundary semantics verified against the landed tests), security (order of operations, no new redactor, markup crash fixed), edge cases (empty exception, exact-200, hostile, benign), maintainability (constants + rationale comments in house style), mutation mentality (all 5 behavioural tests fail on reverted code for the right reason; 2 pinning tests pass both ways by design). Independent reviewer: **not obtained** — this is a scoped SURFACE change executed and self-reviewed by the implementer; flag for orchestrator if a fresh-context reviewer is required before merge.

## 6. Unverified assumptions and remaining risks

- ADR-0036 §P01's marker-inside-the-200 reading is my interpretation (documented in §1); if the ADR owner intended marker-outside-200, the difference is display length only — the security property (nothing unbounded, redaction first, explicit marker) is identical. No code change needed unless the operator wants the looser accounting.
- Rich console behaviour in capture (folding at width 80, box characters, é mangling under StringIO) is host-encoding dependent; tests assert only stable outcomes (exit code, secret absence, bounded body region, exact markers) and were green on this Windows host. On a Linux TTY the wrap points differ but the no-whitespace reconstruction holds (single-word fixtures).
- `runtime.py:481`'s except branch is near-unreachable today (constructor is three attribute assignments — INV-04); the new test drives it by raising from the constructor, so the coverage is real even though production reachability is low.
- The full import-conventions lock was not completed (timeout, §2); fresh-interpreter imports of both touched modules pass. Full-suite and container checks were out of scope by brief.
