<!-- ORCH-DIGEST:START -->
agent: W0-INV-04 | domain: integration-fix — P-001 redaction, P-004 touch dedup + coder semantic fallback, P-006 5 TUI panels | status: DONE
items: 3 verified EXISTS / 0 ALREADY-FIXED / 0 NOT-REPRODUCIBLE / 0 PARTIAL / 0 MISCLASSIFIED (all 3 plan items exist as described; several plan/brief *line refs* are stale — detailed in §1)
proposed_tasks: 3 (SURFACE 2 — P-01 redaction, P-06 snapshot panels+empty-states; DEEP 1 — P-04 touch unification + vector fallback incl. embedder plumbing)
top_risks: (1) Runtime has NO `embedder` field — P-04's coder vector fallback has no embedder to call today; (2) 3 of 5 P-06 panels have no live data source (attention_top needs P-07; decision-quality + agent-registry tables are never written by any code); (3) `tests/tui` has 1 pre-existing failure (`test_ledger_with_events`, Windows strftime) that will mask T-07 verification
cross_domain_conflicts: none found (P-04's 4 touch sites are all inside P-04's own F-table; `storage/vector` + `runtime.py` are shared with INV-01's P-002 domain — see §5 ordering)
out_of_plan_discoveries: 5 (see §3: unredacted `str(exc)` siblings of P-01, unreachable except at runtime.py:481, stale `_RecallFacts` docstring, 2 never-written tables, pre-existing TUI test failure)
confidence: high — every code claim verified at HEAD (source byte-identical to 88e648b, `git diff 88e648b -- src/ tests/` empty), 4-way touch diff machine-generated (AST), 4 scoped test runs executed
last_comms_line_read: 2026-11-07T00:01:14Z ORCH -> ALL | DECISION | W0 | FACT wave 0 start
<!-- ORCH-DIGEST:END -->

# Investigation report — W0-INV-04 — integration-fix (P-001 / P-004 / P-006)

## 0. Scope and method

Question answered: do plan items P-001 (redaction of two `str(exc)` sites), P-004 (unify 4 `_touch_recalled` copies + coder `recall_facts` vector fallback) and P-006 (5 named TUI panels on the existing shell) match live code at HEAD `88e648b`, and what exactly must a fix touch?

- Branch verified: `ops/20261107-integration-fix` [RAN `git branch --show-current` -> `ops/20261107-integration-fix`], HEAD `c15b665` = `88e648b` + two docs-only commits (`4abf38b`, `c15b665`) [RAN `git diff --stat 88e648b..HEAD` -> 14 files, all under `docs/agent-ops/`, 0 src/test files; re-verified at `c15b665` by the closing agent]. **All source claims below therefore hold at 88e648b and at current HEAD.** [RAN `.venv\Scripts\python.exe -m pytest tests/coder/test_runtime_tools.py -q -k "TestRecallTouch"` at `c15b665` -> 5 passed]
- Required reading done in order: STANDING-ORDERS.md, AGENTS.md (this session's instructions), 00-mission.md, plan rev 5 (P01/P04/P06 rows + T01/T04/T05/T07), report template, then the code.
- Files read in full or in the briefed ranges: `runtime.py`, `cli/main.py`, `coder/runtime_tools.py`, `models/base.py`, `api/app.py`, `agents/context_agents.py`, all four `tui/*` files, `memory/{decay,creator,facts,__init__}.py` (targeted ranges), `storage/vector/store.py`, `context/metrics.py`/`gatherer.py` (grep), `api/schemas.py`, `config.py` (targeted), `skills/registry.py` (targeted), plan + Doc A §PART 9, prior-run SUMMARY/mission.
- Commands run (all read-only or scoped tests): `git branch/log/diff/show`, `grep`, 4 targeted `pytest` selections (§9), one AST-diff script in a temp dir (§13).
- Deliberately NOT run: full suite (mission owns baseline), containers, any server, any migration, ruff/mypy over the tree (not my gate), `requires_postgres/s3` suites.
- Not examined: P-002/P-003/P-005..P-14 bodies (other investigators / later waves), `context/gatherer.py` internals beyond the semantic hook, Doc A formulas (D7 excludes verbatim adoption).

## 1. Per plan item

### P-001 Redact `str(exc)` at `runtime.py:481` and `cli/main.py:649`

- **Existence verdict:** EXISTS (both sites, both line numbers exact at 88e648b).
- **Evidence:**

  Exact 4-line context of `runtime.py:481` [VERIFIED src/paa/runtime.py:476-482]:

  ```python
  embedding_sink: Any = None
  if vector_store is not None and embedder is not None:
      try:
          embedding_sink = _creator_mod.VectorEmbeddingSink(vector_store, embedder)
      except Exception as exc:  # degradation path: never crash boot on vectors
          log.warning("memory.embedding_sink", status="degraded", error=str(exc))   # <- :481 UNREDACTED
          embedding_sink = None
  ```

  The **same function already redacts one line above** [VERIFIED src/paa/runtime.py:474]: `log.warning("runtime.llm_extractor_unavailable", error=redact(str(exc)))`, with `redact` imported at `runtime.py:43` (`from paa.models.base import redact`). So :481 is an inconsistency inside one function, not a missing dependency.

  Exact context of `cli/main.py:649` [VERIFIED src/paa/cli/main.py:642-649]:

  ```python
  # C02: drain the pending-embedding backlog after the signal batch. ... (comment :642-645)
  try:
      await creator.drain_pending_embeddings()
  except Exception as exc:
      console.print(f"[red]embedding drain failed: {exc}[/red]")   # <- :649 UNREDACTED, printed to operator console
  ```

  **What is actually in scope at each site (the reviewer's "no secrets in scope" nuance, resolved with evidence):**

  - `runtime.py:481`: the try wraps only `VectorEmbeddingSink(vector_store, embedder)`, and that `__init__` performs three plain attribute assignments with no validation [VERIFIED src/paa/memory/creator.py:1042-1045]. It cannot raise from its own logic; reachable exceptions are `AttributeError` (class missing) or `TypeError` (arity) — construction of the class, not of the embedder or any provider call. **INFERENCE (high confidence): this except branch is effectively unreachable today, and no provider error, URL or key can reach it.** The provider-adjacent construction in the same function (`LLMFactExtractor(model=model_adapter, ...)` :468-472) *can* carry provider-shaped text and it is the one already redacted at :474.
  - `cli/main.py:649`: `drain_pending_embeddings` catches its own sink errors internally and logs them **already redacted** [VERIFIED src/paa/memory/creator.py:1534-1549, `error=redact(str(exc))` at :1543]. What can escape to the CLI `except` is only the un-guarded DB work: `self._facts.pending_embeddings(limit)` (:1519), `self._pending_embedding_count()` (:1525/:1540/:1554), `self._facts.mark_embedded(...)` (:1552) — i.e. relational-backend/driver error strings. **INFERENCE (medium):** plausible secret-shaped content is (a) a DSN/connection string echoed by a driver connect error, (b) a constraint error echoing a *stored fact value* (user data can itself contain a credential — `sk-…` facts are exactly what `remember_fact` stores). No model-provider error reaches this site.
  - Verdict on T01's "no secrets in scope": **correct as to provider secrets (none in scope), wrong as a reason to skip** — the site prints arbitrary exception text to the operator console, and (b) above is a real channel. The plan's T01 shape (sk- fixture + 200-char best-effort case) is satisfiable at both sites.

  What `redact` / `redact_detail` already handle [VERIFIED src/paa/models/base.py:243-275,278-295]:

  | pattern / rule | line |
  |---|---|
  | `sk-[A-Za-z0-9_\-]{12,}` | :244 |
  | `\bBearer\s+[A-Za-z0-9._\-]{12,}` (i case) | :245 |
  | `\b(?:api[_-]?key\|x-api-key\|authorization)\b\s*[:=]\s*\S+` (i case) | :246 |
  | literal `*secrets` replacement, only if `len(secret) >= 8` (`_MIN_REDACTABLE_LENGTH`) | :253, :270-272 |
  | `redact_detail`: every **string value** at any depth through `redact`; **keys never rewritten** | :278-295 |
  | **no truncation anywhere** — `redact` returns the full scrubbed string | :256-275 |

  Where the 200-char truncation spec lives: **ADR-0036 §P01** per plan P01 ("spec in ADR-0036 §P01, not silent") [VERIFIED plan line 63]. ADR-0036 does not exist yet — `docs/adr/` ends at `0035-unqualified-delete-rule…` [RAN `ls docs/adr/` -> last file 0035]. In-repo idioms to copy rather than invent: `marketplace/registry_client.py:112` `_MAX_EXCERPT_CHARS = 200` with an explicit `truncated: bool` flag (:201-213), and `storage/queue/sqlite_backend.py:46,327` `_MAX_ERROR_CHARS`/`error=detail[:200]`. Note the plan constraint: *"do NOT invent new redactor"* — truncation is a separate concern from `redact`, and `redact`'s semantics are pinned by `tests/models/test_model_errors.py` (must not change).

  Existing redaction tests and what they cover:

  | test | covers |
  |---|---|
  | `tests/models/test_model_errors.py:1017,1072,1101,1122` | `redact_detail` recursion/keys, structured-output error, **one shared policy across model layer + router** |
  | `tests/models/test_key_secrecy.py:143`, `test_router_policy.py:277,299,324` | provider error bodies/details scrubbed before attachment |
  | `tests/models/test_gemini.py:140`, `test_openai_platforms.py:115`, `test_http_retry.py:12` | per-provider error-body redaction |
  | `tests/memory/test_llm_extractor.py:402,462` | extraction/quarantine path redacted |
  | `tests/runtime/test_memory_creator.py:151-195` | **`runtime.llm_extractor_unavailable` (:474) only** — asserts the warning kwargs are redacted |
  | `tests/api/test_memory.py:232,258` | `redact(str(exc))` in HTTPException detail (memory routes) |
  | — | **NO test covers `runtime.py:481` or `cli/main.py:649`** [RAN `grep "embedding drain failed\|memory.embedding_sink" tests/` -> no matches for those two strings] |

  Provenance (why it is still open): it is the deferred **F-02 remainder of the previous run** — "3 unredacted error lines flagged … 1 fixed in-set (drain error, `creator.py:1543`), 2 remain" [VERIFIED docs/agent-ops/20261106-memory-remembering-agent/SUMMARY.md:69,84,110 — "`runtime.py:481`, `cli/main.py:649` outside FINAL-IMPR-02 write-set → NEED posted; owner of those files to redact"]. Both lines were introduced by commit `c5766c8` (2026-11-06 23:16, "Wire VectorEmbeddingSink into Runtime and drain pending embeddings (C01+C01b+C02)") [RAN `git log -L 476,483:src/paa/runtime.py` and `-L 646,649:src/paa/cli/main.py` -> both first appear in c5766c8].

- **Root cause chain:** C01/C02 wired a new degradation path and a new console error path in one commit → both formatted `str(exc)` by hand → the run's final review caught them as F-02 but they sat outside that run's write-set → carried forward as a NEED → plan P01. Not caught by tests because neither site has a test that inspects its output (table above), and the model-layer redaction tests only assert the model layer's own raise sites.
- **Blast radius:** 2 call sites; 0 callers of a helper to change (the fix is local). Consumers of the log line: structlog output (`memory.embedding_sink` event — grep `memory.embedding_sink` -> `runtime.py:481,489,492` + tests none); console output of `paa memory drain` (`cli/main.py:649` — no test greps it). Docs: plan P01, ADR-0036 §P01 (to be written), SUMMARY F-02 line.
- **Fix options:**
  - **A (plan-aligned):** at each site, `error=redact(str(exc))` (runtime) and `redact(str(exc))` bounded to 200 chars with an explicit marker (cli), truncation constant named in code, rule written in ADR-0036 §P01. Pros: 2-line-per-site, mirrors the already-correct :474, no new helper, no change to shared `redact`. Cons: two sites must each remember the bound — the same drift that created F-02; mitigated by T01 asserting both.
  - **B:** add `redact_bounded(text, *, limit=200)` beside `redact` in `models/base.py`, used by both sites. Pros: one home for the bound, unit-testable once, mirrors `registry_client._MAX_EXCERPT_CHARS`. Cons: widens the model layer's public surface (`__all__` is asserted by `test_model_errors.py:1165-1169`) and brushes the plan's "do NOT invent new redactor" line — needs the ADR to say it is a *bound*, not a second redactor.
  - **C:** skip `runtime.py:481` as unreachable. Rejected: the plan names it, the branch can become reachable the day `__init__` validates anything, and defence-in-depth on a log line is cheap.
  - **Recommended: A**, with the 200 constant defined once next to the call it serves and cited from ADR-0036 §P01; **Why:** smallest diff that satisfies T01, no shared-surface risk, matches the "name the helper in code" instruction.
- **Long-run test:** *Source* — exceptions at both sites are formatter-agnostic (`str(exc)`), so any exception class works. *Load* — irrelevant (cold path). *Hostile* — sk- fixture, Bearer fixture, 10 KB exception string (truncation), NUL/ANSI in exception text (console render). *Operability* — grep for `error=str(exc)` at the two sites must return nothing; ADR-0036 documents the bound. *Reversal* — revert the 2 lines → T01's two assertions fail (that is the reversal test).
- **Classification:** SURFACE (2 sites, one helper already imported, no schema/config/EventType).
- **WRITE-SET:** `src/paa/runtime.py` (:481), `src/paa/cli/main.py` (:649), new `docs/adr/0036-*.md` §P01 (ADR body is the orchestrator's/plan's to file — flag if it lands in another wave), tests: new cases in `tests/runtime/test_memory_creator.py` (or a new `tests/runtime` redaction test) + a CLI test asserting the printed drain error.
  **CONTRACT-SET:** `models.base.redact` signature/semantics (unchanged); log event names `memory.embedding_sink` and console string `embedding drain failed`.
  **DEPENDS-ON:** none. **ENV-SET:** none (no markers, no extras).
- **Test plan (must fail before the fix):**
  - *boundary:* exception whose text is exactly 200 / 201 chars → output ≤200 + marker.
  - *hostile:* exception text containing `sk-` + 12 chars, `Bearer …`, `api_key: …` → no substring survives in captured structlog kwargs / captured console output.
  - *malformed:* non-ASCII, NUL, ANSI escapes in `str(exc)` (there is a repo-wide ANSI guard pattern at `creator.py:1707` for payloads).
  - *reversal:* restoring `error=str(exc)` fails the above.
  - Note: a test that only calls `redact()` directly does **not** cover P01 — it must drive `_build_memory_creator`'s except branch (monkeypatch `VectorEmbeddingSink` to raise, as `tests/runtime/test_memory_creator.py` already does for the extractor) and the `paa memory drain` command path.
- **Conflicts/ordering:** none with hotspots (`config.py`, schema, `core/types.py`, `ledger/*` untouched). Ordering: **before P-006** (plan Depends P06→P01; pre-mortem 8 "Panels leak secrets → P01 redaction before API render"). Shares `models/base.py` with nothing else this wave.
- **Acceptance criteria (measurable):** (1) `grep -n "error=str(exc)" src/paa/runtime.py src/paa/cli/main.py` at the two sites returns nothing; (2) T01's sk-fixture case: no `sk-` substring in the captured warning kwargs / console line; (3) 10 KB exception → rendered text ≤200 chars + explicit truncation marker; (4) ADR-0036 §P01 states the bound; (5) `ruff/mypy` stay at 0/3-baseline, existing redaction tests still pass.

### P-004 Unify `_touch_recalled` + coder semantic fallback

- **Existence verdict:** EXISTS — 4 copies confirmed, plus the drift the plan predicted (in docstrings/annotations), plus one *additional* gap the plan does not mention (no embedder on `Runtime`).
- **Evidence:**

  **The 4 copies (defs, exact at HEAD):**

  | # | file:line | span |
  |---|---|---|
  | 1 | `src/paa/api/app.py:346-366` | 21 lines |
  | 2 | `src/paa/coder/runtime_tools.py:886-910` | 25 lines |
  | 3 | `src/paa/agents/context_agents.py:403-425` | 23 lines |
  | 4 | `src/paa/cli/main.py:699-723` | 25 lines |

  **Line-by-line diff (machine-generated, AST-normalised with docstrings stripped)** [RAN `.venv\Scripts\python.exe C:\Users\devan\AppData\Local\Temp\opencode\w0-inv-04\diff_touch.py` -> pairwise unified diffs; output reproduced below]:

  - `coder` vs `cli` → **0 diff lines (byte-identical executable body)**.
  - `api` vs `coder`/`cli` → api does *not* import locally: it binds module-level `log` and `redact`; the other two do `import structlog` + `from paa.models.base import redact` *inside* the function and build `log = structlog.get_logger(__name__)`.
  - `api` vs `context_agents` → annotation drift: `db: Any` (api/coder/cli) vs **`db: RelationalBackend | None`** (context_agents only) + the same local-vs-module `redact` import difference.
  - docstrings differ per site by design ("the routes" / "this tool" / "this module" / "these commands") — cosmetic only.

  **Verdict:** the *executable* core is identical in all four — `if db is None or not fact_ids: return` → `ids = list(dict.fromkeys(fact_ids))` (dedupe, order-preserving) → `try: await DecaySweeper(db).touch_many(ids)` → `except Exception: log.warning("memory.recall_touch_failed", count=…, error=redact(str(exc)))` → `log.debug("memory.recall_touched", count=…)`. Drift is confined to the **signature annotation, import binding style, and docstring wording** — i.e. exactly the drift a single home removes. No behavioural divergence to reconcile; the unified function must pick `RelationalBackend | None` (the stricter, correct annotation) and module-level bindings (the `memory/decay.py` house style).

  **Stale line refs in brief/plan (report, do not chase):**
  - brief `coder/runtime_tools.py:746 recall_facts` → :746 is inside the tool's `description` string; the class is `_RecallFacts` at :721, `run()` at :772.
  - brief `:807 note` → the `note` key is at :809-812 (:807 is `],`).
  - brief `:901 touch` → :901 is `from paa.models.base import redact`; the def is at :886.
  - brief `agents/context_agents.py:197 touch` → :197 is a SQL string line; the def is at :403. Call sites are :329, :395 (plus api :210/:233, coder :794, cli :540/:566) [RAN `grep _touch_recalled src/` -> 12 hits].
  - plan's "already stale `note:807` once" → the note text itself is current (FINAL-01 pinned it: `tests/memory/test_e2e_remember_recall.py:236-241` asserts `"planner-side"`/`"embedded vectors"`); what *is* stale is the **class docstring** at `coder/runtime_tools.py:732-738`: *"Semantic (vector) recall is honestly absent in v1 … nothing in the runtime wires one"* — contradicted by its own `description` (:748-749 "C01 wired the embedding sink") and by `runtime.py:479`. That contradiction is a genuine P-04 cleanup item.

  **`recall_facts` current flow** [VERIFIED src/paa/coder/runtime_tools.py:772-814]: strip/validate query (:773-783, NUL refused as a *value*, repo rule mirrored) → `FactRepository.resolve_entity(query)` (:788) → `query_facts(entity_id=…)` (:790) → **if no facts** `search_facts_by_text(query)` (:792, keyword LIKE tier) → `_touch_recalled(...)` (:794) → emit `{query, facts[], count, note}` (:795-814). **There is no vector tier at any point.** Confidence in the payload comes from `FactRepository.query_facts`, which decays at read time by design (docstring :736-738).

  **Vector-search API available for the coder to call:**
  - `VectorStore.search(collection, query_vector, limit=10, score_threshold=None, filters=None) -> list[SearchHit]` (abstract, implemented by both backends) [VERIFIED src/paa/storage/vector/store.py:493-506]; numpy impl :258, qdrant impl :379.
  - Collection constant `ACTIVE_FACTS` exported from `paa.storage.vector` [VERIFIED src/paa/storage/vector/__init__.py:19,51,70]; dimensions pinned by tests (`test_e2e_remember_recall.py:167`).
  - `SearchHit` payload keys written by the sink: `object_value`, `confidence`, `importance`, `predicate`, `memory_domain` (+ writer keys) [VERIFIED src/paa/memory/creator.py:1066-1072] — reader defaults 0.8/0.5 noted there.
  - The runtime already exposes `Runtime.vector_store: Any = None` [VERIFIED src/paa/runtime.py:169], used today by `_CheckRuntime` (`coder/runtime_tools.py:154-156`).

  **How planner-side semantic recall does it (the pattern to copy):** `ContextBuilder._semantic` [VERIFIED src/paa/agents/context_agents.py:241-262] — guard `if self._vectors is None or self._embedder is None: return []` → `vector = (await self._embedder.embed([goal]))[0]` → `hits = await self._vectors.search("active_facts", vector, limit=limit)` → `except Exception: log.warning("context.vector_search_failed", error=str(exc)); return []` → map `SearchHit` → `ContextElement`. `_vectors`/`_embedder` are constructor-injected at :109-124, fed from `_build_agents(...)` in `Runtime.build` [VERIFIED src/paa/runtime.py:325-333].

  **The gap the plan does not state (blocker-level for the fallback half):** `Runtime` has **no `embedder` field** [VERIFIED src/paa/runtime.py:163-190 — fields: settings, db, ledger, orchestrator, recovery, model_router, vector_store, graph_store, queue, agents, sandbox, memory_creator, _boot_report]. The embedder is a *local* in `Runtime.build` (runtime.py:312-335) handed to `_build_agents` and `_build_memory_creator`, then dropped. Coder tools hold only `rt` (`_RuntimeTool.__init__` runtime_tools.py:115-116). So today a `recall_facts` vector tier can reach the store but **cannot embed the query** without one of:
  - (A) add `embedder: Any = None` to `Runtime` (one field; `runtime.py` is already in P04's plan F-table) — **recommended**;
  - (B) call `paa.models.get_embedder(settings.models)` per query [VERIFIED src/paa/models/embeddings.py:26] — constructs a second embedder; cheap for the hash embedder, heavy (sentence-transformers load) for the real one, and it re-reads config;
  - (C) reach `memory_creator._sink._embedder` (private, two underscores deep) — rejected: violates encapsulation and breaks when sink is None (which is the common minimal-install case).

  **Where `memory/touch.py` should live vs module conventions:** `src/paa/memory/` (next to `decay.py`, which owns `touch_many` — the single write the helper performs). Conventions to follow, all observed:
  - module docstring listing the layer + `from __future__ import annotations` + `__all__` [VERIFIED src/paa/memory/decay.py:1-36];
  - module-level `import structlog` and `log = structlog.get_logger(__name__)` [VERIFIED src/paa/memory/decay.py:22,36] — *not* function-local imports (that is the drift inside 3 of the 4 copies);
  - if the signature mentions `RelationalBackend`, import it at **runtime** from `paa.storage.relational.base` [VERIFIED src/paa/memory/decay.py:25] — required by AGENTS.md's "contract resolved the same way everywhere" lock (`tests/conventions/test_import_conventions.py`: a `TYPE_CHECKING`-only mention fails the source half);
  - add the layer to the package docstring and, if exported, to `memory/__init__.py`'s `__all__` [VERIFIED src/paa/memory/__init__.py:1-95] — the `__init__` re-exports `DecaySweeper` etc., so a public `touch_recalled` should be exported consistently or deliberately left internal (pick one and say so in the docstring).

  **Tests that already pin touch behaviour (all four surfaces have coverage):**

  | surface | test | notes |
  |---|---|---|
  | coder | `tests/coder/test_runtime_tools.py::TestRecallTouch` (:1026-1114, 5 tests) | use_count/last_queried advance, touch-failure tolerated, empty/NUL touch nothing; monkeypatches `DecaySweeper.touch_many` **on the class** (:1066) → survives the move |
  | api | `tests/api/test_memory.py::TestRecallTouch` (:463-576, 6 tests) | incl. 400/429/empty → no touch |
  | cli | `tests/cli/test_memory.py` (:423-515, 5 tests) | incl. failed search → no touch |
  | planner | `tests/agents/test_context_agents.py:409,471` | semantic hit touches; empty recall touches nothing |

  Because every failure-path test patches `DecaySweeper.touch_many` (the shared callee), **none of them breaks when the call moves into `memory/touch.py`** — and none of them proves the four sites share one implementation (that is T05's job: `grep _touch_recalled` = single def).

  **T04 dependency test:** `tests/memory/test_e2e_remember_recall.py:210-241 test_recall_facts_synonym_only_miss` currently asserts `count == 0` for `"rapid"`→`"swift"` (DECISION T-007 keyword-only) *and* pins the note text. With a vector fallback it must be updated to expect a hit when the store+embedder are present **and** to still expect 0 when they are absent (both halves matter — the "absent backends" half is the degrade-never-raise contract). The fixture to reuse is right above it: `_SynonymEmbedder` + `NumpyVectorStore` + `VectorEmbeddingSink` [VERIFIED tests/memory/test_e2e_remember_recall.py:150-207], the same fake-vector fixture the A5a criterion uses.

- **Root cause chain:** four call sites each needed "advance retention clocks after a recall" and each grew its own copy (git: copies predate this run — all four present at 88e648b) → annotation/import drift accumulated (`RelationalBackend | None` only in one) → plan proposes one home. The missing vector tier is the C01-era partial wiring: the sink writes facts into `active_facts`, the planner reads them, the coder tool was left keyword-only (DECISION T-007), and the tool's own docstring still claims nothing is wired.
- **Blast radius:** 4 call sites (`api/app.py:210,233`; `coder/runtime_tools.py:794`; `context_agents.py:329,395`; `cli/main.py:540,566`) + 4 defs to delete + `memory/__init__.py` (export decision) + `recall_facts` payload (`note` semantics change once fallback exists) + `tests/memory/test_e2e_remember_recall.py:210` + T05 grep lock. Grep `grep _touch_recalled src/` -> 12 hits [RAN]; `grep touch_many src/` -> definition `decay.py:305` + 4 callers.
- **Fix options:**
  - **A (recommended):** `memory/touch.py::touch_recalled(db, fact_ids)` — module-level `log`/`redact`, `db: RelationalBackend | None`, same docstring policy sentence (keep the "documented derived-state exception" wording once); 4 sites import it and delete their defs; *plus* vector fallback in `_RecallFacts.run` gated on `self._rt.vector_store is not None` and an embedder obtained via a new `Runtime.embedder` field (option A above). Pros: T05 grep lock passes, one docstring, one annotation, fallback mirrors `_semantic` exactly, degrades to today's behaviour when backends are absent. Cons: touches `runtime.py` (shared with INV-01's P-002 domain — coordinate); the `note` text must change again, which means updating the FINAL-01 assertions deliberately rather than silently.
  - **B:** unify touch only; leave the fallback for a later item. Pros: zero runtime.py surface, T04 blocked. Cons: plan binds them as one item (P04) and T04/T05 are paired; splitting needs the orchestrator's word.
  - **C:** keep 4 copies, fix drift by hand. Rejected: plan already rejected it (drift exists), and T05's grep lock makes it unimplementable.
  - **Recommended: A. Why:** the fallback's embedder gap is real but one field wide, and doing the fallback without a shared touch helper would create a *fifth* copy at the new call site.
- **Long-run test:** *Source* — all four surfaces already have per-surface tests; add T05's structural grep. *Load* — touch is one UPDATE batch per recall; concurrency: two recalls of the same fact must not lose an increment (check `touch_many` implementation for `use_count = use_count + 1` vs read-modify-write — **not verified in this pass**, see §11) — worth a test if it is read-modify-write. *Hostile* — duplicate ids in one call (dedupe already), empty list, `db=None`, id list of 10k (bounded by recall limits ≤50). *Operability* — one log event name (`memory.recall_touch_failed`) unchanged, so dashboards/greps keep working. *Reversal* — delete the import at one site and that surface's touch test fails; restore a second copy and T05's grep lock fails.
- **Classification:** DEEP (4 modules + new module + `Runtime` field + payload-note contract + a test that currently pins the *absence* of the feature; ADR-0039 required by plan).
- **WRITE-SET:** `src/paa/memory/touch.py` (new), `src/paa/memory/__init__.py`, `src/paa/api/app.py`, `src/paa/coder/runtime_tools.py`, `src/paa/agents/context_agents.py`, `src/paa/cli/main.py`, `src/paa/runtime.py` (embedder field only), `docs/adr/0039-*.md`, tests: `tests/memory/` (new touch + updated synonym test), `tests/coder/test_runtime_tools.py` (fallback cases).
  **CONTRACT-SET:** `DecaySweeper.touch_many(fact_ids, *, now=None)` (`decay.py:305` — unchanged); `recall_facts` payload shape (`query/facts/count/note`); `FactRepository.resolve_entity`/`query_facts`/`search_facts_by_text`; `VectorStore.search`.
  **DEPENDS-ON:** plan says P04 depends on **P02** (payload confidence becomes provenance-only; the fallback must rank by derived confidence, not the enqueue snapshot). ADR-0039. **ENV-SET:** vector fallback needs no extra *import* at module scope (must keep `storage.vector` import inside the function — the `VectorEmbeddingSink` lazy-import pattern, `creator.py:1037-1040`), otherwise a minimal install stops importing `runtime_tools`.
- **Test plan (must fail before the fix):**
  - *structural (T05):* `grep -r "_touch_recalled" src/` returns exactly one definition; four call sites import `touch_recalled`.
  - *behavioural (T04):* `"rapid"`→`"swift"` with `NumpyVectorStore` + `_SynonymEmbedder` present → `count == 1` and the touched fact's `use_count` increments; **same query with `vector_store=None` → `count == 0`** (degrade path) — the second half is what stops the fallback from becoming a hard dependency.
  - *boundary:* limit=1 and limit=50; duplicate entity+keyword hits deduped by touch; fallback runs only when the keyword tier found nothing (keyword-first ordering asserted).
  - *hostile:* NUL query still refused before any vector call; oversized query string bounded by schema `minLength` only — assert no unbounded vector scan (limit clamp `_clamp_int`).
  - *concurrency/idempotency:* two concurrent recalls → use_count reflects both (or documents the lossy UPDATE); re-running the same recall twice is not an error.
  - *reversal:* remove the fallback → T04 fails; restore a second `_touch_recalled` def → T05 fails.
- **Conflicts/ordering:** `runtime.py` (embedder field) overlaps INV-01's P-002 territory (gatherer/vector read) — announce via COMMS CLAIM before editing. Ordering: **P02 before the fallback half** (derived confidence), touch unification itself has no dependency and can land first as its own atomic phase. Hotspots touched: none (`core/types.py`/`ledger/*`/schema untouched — no new EventType, no new state).
- **Acceptance criteria (measurable):** (1) `grep -rn "_touch_recalled" src/` -> exactly 1 hit (the definition) — T05; (2) `pytest tests/coder/test_runtime_tools.py tests/api/test_memory.py tests/cli/test_memory.py tests/agents/test_context_agents.py -q -k "touch or Recall"` all green; (3) `test_recall_facts_synonym_only_miss` updated to the two-half form and green; (4) minimal-install import: `python -c "import paa.coder.runtime_tools"` in an env without qdrant/sentence-transformers still works (import stays function-local); (5) ADR-0039 filed.

### P-006 Five TUI panels on the existing shell

- **Existence verdict:** EXISTS — shell and data sources verified; **3 of 5 panels have no live data source today** (missing-data gaps called out per panel below).
- **Evidence:**

  `snapshot.py:37-49` — `Snapshot` currently has exactly **two fields** [VERIFIED src/paa/tui/snapshot.py:41-42]: `rows: list[CorrelationRow]`, `runtime_info: dict[str, Any]`, plus `row_for()` (:44-49). `CorrelationRow` (:24-34) = correlation_id, phase, modality, attempts, tokens_consumed, created_at, goal_preview. **None of the 5 planned names exists** (`grep attention_top|memory_browser_counts|decision_tracker|autonomy_state|agent_monitor src/` -> 0 hits; only the plan and my brief contain them).

  `dashboard.py:83-118` — `render_dashboard(snapshot)` [VERIFIED]: header; **empty branch** when `not snapshot.rows` renders `"no active tasks"` + a mode/vector summary (:86-94) — this is the precedent for the plan's per-field `null → "No data yet"` empty states; otherwise one 6-column task table (:96-111) + footer. Other renderers: `render_status` :126, `render_ledger` :151, `render_doctor` :176 (backends/sandbox/open tasks), `render_help` :63. Renderers are pure and testable with a StringIO console (docstring :3-6).

  `dispatcher.py:19-28` — `CommandKind` = NEW, DRIVE, STATUS, LEDGER, GATE, DOCTOR, HELP, IGNORE, QUIT [VERIFIED]. **No panel/view command**; display-only commands are executed by `runner._display_panels` (runner.py:196-220) which handles only DOCTOR / STATUS / LEDGER / HELP. Panels therefore belong in the *dashboard render path*, not behind new commands (D11: no new frontend).

  `runner.py:56-121` — data sources [VERIFIED]:
  - `_gather_rows(rt)` (:56-85): `rt.ledger.open_correlations()` → `project_lineage` per cid → `CorrelationRow` (phase, modality, attempts, tokens, created_at, goal).
  - `_build_info(settings, rt)` (:88-121): `home`, **`policy_mode`** (`settings.policy.mode.value`, :110), `relational/vector_store/graph_store/queue/model_router` types, `sandbox` + `collect_sandbox_facts`, `open_tasks` list.
  - loop `run_tui` (:266-270): rebuild rows+info → `Snapshot` → `render_dashboard` every tick.

  **Panel-by-panel data availability (the answer the brief asks for):**

  | panel | live source today | status |
  |---|---|---|
  | `attention_top` | `attention_allocation_score()` exists (`context/metrics.py:315`, ADR-0012) but **has zero callers** [RAN `grep attention_allocation_score src/` -> 6 hits, all def/export/doc] and no allocator exists (P-07 not built) | **MISSING — blocked on P-07.** Interim (defensible) content: open-task rows ordered by phase/age; must render `"No data yet"`, not a fake score |
  | `memory_browser_counts` | `FactRepository.count_facts(*, include_superseded=False)` (`memory/facts.py:1089-1093`) = **single total**; pending-embedding count is a private `MemoryCreator._pending_embedding_count` (`creator.py:1505-1515`); `cold_lake_signals` counts exist only as list endpoints (limit 50) | **PARTIAL — one aggregate query short.** No `GROUP BY` exists anywhere in `facts.py` [RAN `grep "GROUP BY" src/paa/memory/facts.py` -> 0]. A domain/status counts query is a read-only addition (no schema change) |
  | `decision_tracker` | per-lineage `policy_decision` projection field (`ledger/replay.py:109`, set from payload at :215) and `EventType.POLICY_CLEARED/POLICY_BLOCKED` (`core/types.py:145-146`); readable via `project()` / `ledger.read_correlation` | **PARTIAL.** Raw policy decisions: available from the ledger. Outcome/quality scores: **MISSING — `hot_serving_evaluation_results` is created by schema (`schema_sqlite.sql:462`) and backfilled by migration (`migrations.py:786`) but never written by any Python code** [RAN `grep hot_serving_evaluation_results src/` -> schema + migration only] |
  | `autonomy_state` | `settings.policy.mode: PermissionMode = ASK` (`config.py:336-339`) already in `runtime_info["policy_mode"]` (`runner.py:110`) and rendered by `render_doctor` (`dashboard.py:182`); pending human gates = `projection.awaiting_reason` (`replay.py:127`, cleared by `orchestrator.clear_human_gate` `orchestrator.py:1015`) | **AVAILABLE** (mode + per-lineage awaiting state). No global "gates pending" count yet, but it is derivable from `open_correlations` + `project()` — no new state |
  | `agent_monitor` | `rt.agents` dict (`runtime.py:172`), and ledger events carry `agent_role` (already printed by `render_ledger`, `dashboard.py:161`) | **MISSING (aggregates).** `hot_serving_agent_registry` is never written by any Python code [RAN `grep hot_serving_agent_registry src/` -> schema + migration only]; no per-agent counters. A coarse "agents loaded / recent events by role" view is derivable with no new state; anything richer is P-07+/metrics work |

  **API side (which endpoints could serve the same fields)** [VERIFIED src/paa/api/app.py route decorators]:

  | endpoint | line | already returns |
  |---|---|---|
  | `GET /health` | :69 | status, version, `backends{relational,vector,graph,queue,model_router}`, `open_tasks` count |
  | `GET /tasks/{id}` | :112 | projection incl. `policy_decision`, `awaiting_reason` (`app.py:128`, `schemas.py:61`) |
  | `GET /ledger/{id}` | :154 | events (incl. `agent_role`, `POLICY_*`) |
  | `GET /memory/facts` / `/memory/search` / `/memory/signals` | :204/:224/:247 | lists (facts carry `domain`; signals carry `sync_status`) |
  | `POST /tasks/{id}/gate`, `/correction`, `/memory/drain` | :134/:147/:272 | mutations |
  | — | — | **no endpoint returns counts, attention, autonomy aggregates or agent monitor** |

  T-07's "API 200 + field presence" therefore implies an additive field set — the least-new-surface option is extending `HealthResponse` (`api/schemas.py:135`) or adding one additive read endpoint; plan §J says "API additive only" and F-table already lists `api/app.py` for P06. **Which of the two the plan intends is not stated — see §11.**

  TUI test landscape for T07: `tests/tui/test_dashboard.py` (renders with hand-built `Snapshot`, incl. empty-dashboard test :55-59 and doctor rows :133-191), `test_dispatcher.py`, `test_runner_smoke.py`. T07 = extend these with a `Snapshot` whose 5 fields are `null` and assert `"No data yet"`.

- **Root cause chain:** the TUI was built as a task table + doctor (C-era) while Doc A §PART 9 (`docs/A Disciplined System…md:1070-1143`, dashboard + Memory Browser + Decision & Outcome Tracker mock-ups) was never wired; the plan adds the five fields to the existing `Snapshot` rather than a new frontend (D11). The data gaps are the *same* integration gaps as P-07 (allocator), the evaluation table (never written), and the agent registry (never written) — the panels will honestly render empty until those land, which is exactly what the per-field null state is for.
- **Blast radius:** `tui/snapshot.py` (fields), `tui/dashboard.py` (render blocks), `tui/runner.py` (`_gather_rows`/`_build_info` population), `api/app.py` + `api/schemas.py` (additive response), tests in `tests/tui/` + `tests/api/`. No dispatcher change needed (no new commands). Consumers: only the TUI and the API response model.
- **Fix options:**
  - **A (recommended):** add the 5 nullable fields to `Snapshot` (default `None`), populate what exists (`memory_browser_counts` via one new aggregate query, `autonomy_state` from `settings.policy.mode` + open awaiting gates, `agent_monitor` from `rt.agents` + recent `agent_role`s, `decision_tracker` from recent `POLICY_*` projections), leave `attention_top=None` until P-07; render each as its own sub-panel with `null → "No data yet"`; mirror the same 5 fields additively on `/health`. Pros: one shell, no new commands, empty states make the gaps visible instead of fake, T07 testable today. Cons: two surfaces (TUI + API) must be fed from one builder to avoid drift — reuse `_build_info`-style single source, exactly as `render_doctor` shares `sandbox_report_rows` with the CLI (`dashboard.py:199-201` is the precedent).
  - **B:** panels as new dispatcher commands (`panels`, `attention`, …). Rejected: dispatcher/runner churn, contradicts "dashboard shows everything" and D11's no-new-surface intent.
  - **C:** ship only the 3 panels with data now, defer `attention_top`/`agent_monitor` to P-07. Pros: no null states needed. Cons: T07 explicitly tests the `null → "No data yet"` path — the empty state *is* the acceptance criterion, so C fails T07 as written.
  - **Recommended: A. Why:** T07's pass criterion is the null rendering, and A is the only option that satisfies it while staying inside the existing shell.
- **Long-run test:** *Source* — field presence + empty rendering per panel (T07). *Load* — `_gather_rows` already does one `project()` per open correlation each 2 s tick; adding counts must not add an N+1 per row (counts = bounded `COUNT(*)`, one query per tick — same rule as `creator._pending_embedding_count`). *Hostile* — `goal_preview` and any panel string can contain ANSI/control chars (there is a payload ANSI guard at `creator.py:1707` but the TUI renders raw strings — **flag as a risk**, see §10). *Operability* — a panel with no data must say so and the dashboard must still render when a data source raises (wrap per-panel, never let one panel kill the loop — `_gather_rows` already swallows :62-64, :83-84). *Reversal* — remove the fields → T07's presence assertions fail; remove the null branch → the empty-state assertion fails.
- **Classification:** SURFACE for the shell (fields + render + empty states + additive API), with two **data dependencies that are DEEP and not in P-06**: `attention_top` needs P-07, `decision_tracker` quality + `agent_monitor` aggregates need writers that do not exist. Recommend keeping P-06 scoped to "render what exists, null otherwise" and letting P-07 fill `attention_top`.
- **WRITE-SET:** `src/paa/tui/snapshot.py`, `src/paa/tui/dashboard.py`, `src/paa/tui/runner.py`, `src/paa/api/app.py`, `src/paa/api/schemas.py`, `src/paa/memory/facts.py` (only if the counts query lives in `FactRepository` — the single-writer rule says facts.py owns those tables' reads), tests `tests/tui/*`, `tests/api/*`.
  **CONTRACT-SET:** `Snapshot` dataclass shape (tests construct it positionally/by keyword — `tests/tui/test_dashboard.py:42` uses `rows=`), `render_dashboard` signature, `HealthResponse` (`api/schemas.py:135` — additive fields only, no renames), `CommandKind` (unchanged).
  **DEPENDS-ON:** P-01 (plan Depends; pre-mortem 8). **ENV-SET:** none.
- **Test plan (must fail before the fix):**
  - *boundary (T07):* `Snapshot()` with all 5 fields `None` → render contains `"No data yet"` five times (or per-panel), still renders with `rows=[]`.
  - *non-empty:* each field with sample data renders its rows (attention ranked, counts numeric, tracker entries, autonomy mode, monitor roles).
  - *API:* `GET /health` → 200 and the 5 keys present (value may be null); no existing key removed (assert the old keys too — additive-only lock).
  - *partial failure:* one data source raises → that panel shows the empty state, the other four still render (per-panel isolation).
  - *hostile:* a `goal_preview`/fact string containing ANSI escapes does not corrupt the render (if it fails, that is a finding for the implementer, not something to paper over).
  - *reversal:* delete the field from `Snapshot` → T07 presence assertion fails.
- **Conflicts/ordering:** after **P-01** (plan Depends). `api/app.py` is also P-04's file (touch sites) — sequence P-04's api edit and P-06's api edit as separate commits to avoid a shared-file collision. No hotspot files (`config.py`/schema/`core/types.py`/`ledger/*` untouched unless someone adds Settings for panel toggles — not required by the plan row). Overlaps `facts.py` only if the counts query is added there (P-04 does not touch `facts.py`).
- **Acceptance criteria (measurable):** (1) `Snapshot` has the 5 named fields, default `None`; (2) `pytest tests/tui -q` green **except** the pre-existing `test_ledger_with_events` failure (§9) — i.e. no *new* failures; (3) T07 assertions: null → `"No data yet"`, non-empty → ranked rows, API 200 + field presence; (4) `/health` old keys intact (additive-only); (5) `ruff` 0, `mypy` baseline unchanged.

## 2. Duplicates / shared root causes across items

- **One root cause for P-001 and its siblings:** hand-rolled `str(exc)` formatting at log/console boundaries. It produced F-02 (2 lines), the model layer already solved it centrally (`redact` applied at raise sites, `models/base.py:263-265` docstring: "remember to redact at the raise site … holds right up until the one path nobody tested"), and the two P-01 sites are simply the paths nobody tested. See §3 for the remaining siblings.
- **One root cause for P-004:** "convenience copy at each call site" — same class of defect as F-04/FINAL-04 ("helper dedup", SUMMARY.md:69) from the previous run. The touch copies and (earlier) the sandbox-report renderer were both fixed by extracting one home; `sandbox_report_rows` (`dashboard.py:199-201`) is the in-tree proof of the pattern.
- **P-004 ↔ P-006 share nothing structurally** (recall path vs render path), but both depend on P-001 for the security pre-mortem (panels render strings; recall logs errors).
- **P-006 ↔ P-007 (other wave):** `attention_top` is a pure consumer of P-07's allocator output; do not let P-06 invent a score.

## 3. Out-of-plan discoveries (not auto-added to scope)

1. **Pre-existing test failure (documented, reproduced):** `tests/tui/test_dashboard.py::test_ledger_with_events` fails at `dashboard.py:160` `when.strftime("%H:%:%S")` → `ValueError: Invalid format string` [RAN `pytest tests/tui -q` -> 1 failed / 37 passed; RAN `python -c "datetime.now().strftime('%H:%:%S')"` -> same ValueError on this host]. Already recorded as out of scope in `docs/agent-ops/20261106-memory-remembering-agent/00-mission.md:21`. Same typo in `_fmt` at `dashboard.py:59` (`"%H:%:%M:%S"`). P-06 edits `dashboard.py` — the implementer will trip over this; it is *not* P-06's to fix unless the orchestrator says so.
2. **Unredacted `str(exc)` siblings of P-01 (same defect class):** `context_agents.py:248` (`context.vector_search_failed`, `error=str(exc)`), `memory/creator.py:1053` (`memory.vector_sink_unavailable`), `tui/runner.py:63,84,228,236` (`tui.*_failed`, `error=str(exc)`), `tui/runner.py:105` (`tui.sandbox_diagnostics_failed`). The runner ones are panel-adjacent (P-06 territory) — if the operator wants "redaction before render" to be real, these belong in the same ADR section.
3. **`runtime.py:481`'s except is effectively unreachable** (constructor is three assignments) — evidence in §1 P-001. Worth stating in ADR-0036 §P01 so nobody later treats it as a tested safety net.
4. **Two tables are schema-only (never written):** `hot_serving_evaluation_results`, `hot_serving_agent_registry` [RAN `grep <table> src/paa` -> schema + migration backfill only]. They make two P-06 panels permanently empty and make any future "decision quality" claim unverifiable. Not P-06's job; flag for the orchestrator.
5. **Stale self-contradicting docstring in `_RecallFacts`** (`coder/runtime_tools.py:732-738` "nothing in the runtime wires one" vs :748-749 "C01 wired the embedding sink") — part of P-04's cleanup, listed here because the plan's `note:807` pointer is itself stale.

## 4. Assumptions and unknowns

- **[ASSUMED]** T07's "API 200 + field presence" targets an additive `/health` extension — the plan does not name the endpoint (plan line 123). *Resolve:* orchestrator/plan confirmation, or choose the least-new-surface option and record it in the ADR/task text.
- **[ASSUMED]** `Runtime.embedder` field is acceptable as P-04's embedder plumbing (option A). *Resolve:* orchestrator, since `runtime.py` is shared with INV-01/P-002.
- **[UNKNOWN]** Whether `DecaySweeper.touch_many` increments `use_count` atomically (`UPDATE … SET use_count = use_count + 1`) or read-modify-write — affects the concurrency test in P-04. *Resolve:* read `decay.py:305+` at implementation time (I stopped at the signature; not needed for the existence verdict).
- **[UNKNOWN]** Whether `test_ledger_with_events` also fails on the Linux CI matrix (cannot verify from this Windows host; glibc `strftime` may tolerate `%:`). *Resolve:* CI run / a Linux shell.
- **[UNKNOWN]** Whether ADR-0036 already has a draft wording for the 200-char bound anywhere outside the plan file — searched repo (`grep "200 chars\|truncate to 200"` in docs/ -> plan only) but not the operator's other plan files.
- **[UNKNOWN]** Full-suite baseline (mission's background run) — not executed by me per standing order 16.

## 5. Recommended task breakdown and ordering

| # | task | class | files | depends on | gate |
|---|---|---|---|---|---|
| 1 | P-01 redaction: 2 sites + ADR-0036 §P01 + T01 tests (sk-fixture, 200-char case) | SURFACE | `runtime.py`, `cli/main.py`, ADR, `tests/runtime/`, `tests/cli/` | none | T01 |
| 2 | P-04a touch unification: `memory/touch.py`, delete 3 copies, export decision, T05 grep lock | SURFACE (of a DEEP item) | `memory/touch.py`, `memory/__init__.py`, 4 call-site files | none (can parallel #1 — disjoint files) | T05 |
| 3 | P-04b vector fallback: `Runtime.embedder` field + `recall_facts` keyword→vector + note/docstring update + ADR-0039 + T04 two-half test | DEEP | `runtime.py`, `coder/runtime_tools.py`, `tests/memory/`, `tests/coder/` | **P-02** (derived confidence), #2 | T04 |
| 4 | P-06 shell: 5 nullable `Snapshot` fields + render blocks + null states + runner population | SURFACE | `tui/snapshot.py`, `tui/dashboard.py`, `tui/runner.py`, `tests/tui/` | #1 (plan Depends) | T07 (first half) |
| 5 | P-06 API: additive `/health` (or agreed endpoint) fields + `schemas.py` + API test | SURFACE | `api/app.py`, `api/schemas.py`, `tests/api/` | #4 (same field names), #2 (api file is shared with P-04a) | T07 (second half) |
| 6 | P-06 counts query (if `memory_browser_counts` is to show more than a total) | SURFACE | `memory/facts.py` + test | #4 | part of T07 |

Ordering constraints and conflicts to respect: #2 before #5 (both edit `api/app.py`); #1 before #4 (plan Depends + pre-mortem 8); #3 after P-02 (plan's own Depends) and after #2 (otherwise a fifth touch copy appears at the new fallback call site); announce `runtime.py` and `api/app.py` edits in COMMS (CLAIM) because INV-01 works the vector/gatherer read path. No hotspot files (schema/migrations/`core/types.py`/`ledger/*`/`config.py`/`pyproject.toml`/`ci.yml`) are involved in any of the six tasks; `docs/adr/README.md` (index) is P-14's — the new ADR bodies 0036/0039 must be added to it by whoever owns that file, not by these tasks.

Long-run answer (standing order 8) for the set as a whole: **Source** — every claim in this report is file:line-verified at 88e648b. **Load** — touch is one batch UPDATE per recall; panel counts are bounded `COUNT(*)` per 2 s tick, never per-row. **Hostile** — sk-/Bearer exception text, 10 KB exception strings, NUL queries, ANSI in payloads. **Operability** — one log event name preserved per feature (`memory.recall_touch_failed`, `memory.embedding_sink`), null states visible instead of silent, ADR records each bound. **Reversal** — reverting #1 fails T01; reverting #2 fails T05's grep lock; reverting #3 fails T04; reverting #4/#5 fails T07's presence and empty-state assertions.

## 6–8. (n/a for this template variant — see §1 for contracts, blast radius, hidden coupling)

Contracts, invariants and hidden coupling are recorded per item in §1 (CONTRACT-SET lines) and summarised here:

- **Invariants a fix must not break:** ledger-is-truth (touch is the documented *derived-state* exception — the unified helper's docstring must keep that sentence, `api/app.py:349-355` is the fullest wording); degrade-never-raise (vector fallback must return today's result when backends are absent); `redact` semantics unchanged (model-layer tests pin them); API additive-only (plan §J); heavy extras lazy (no module-scope `storage.vector` import in `runtime_tools`).
- **Hidden coupling found:** embedder lifetime (`Runtime.build` drops it — the single biggest P-04 surprise); `DecaySweeper.touch_many` as the one callee behind all four touch tests (class-level monkeypatch makes the refactor safe but also means no test observes *where* the call happens — hence T05's grep lock); `api/app.py` shared between P-04 and P-06; `runtime.py` shared between P-01, P-04 and INV-01's domain; `memory/facts.py` single-writer rule (any counts query belongs there, not in the TUI).
- **Downstream consumers:** structlog event names (dashboards/greps), `recall_facts` payload `note` (pinned by `test_recall_facts_synonym_only_miss:236-241`), `HealthResponse` consumers (unknown external — additive only), TUI help text (`render_help`, `dashboard.py:63-80`) if any new command were added (none proposed).

## 9. Baseline results (scoped runs only; full suite is the mission's)

| command | result |
|---|---|
| `git diff 88e648b -- src/ tests/` | empty (source == 88e648b) |
| `.venv\Scripts\python.exe -m pytest tests/tui -q` | **1 failed** `test_ledger_with_events` (`ValueError: Invalid format string`, `dashboard.py:160`), 37 passed (38 collected) — **pre-existing**, documented at `20261106-memory-remembering-agent/00-mission.md:21` |
| `.venv\Scripts\python.exe -m pytest tests/coder/test_runtime_tools.py -q -k "RecallTouch or recall_facts or synonym"` | 5 passed |
| `.venv\Scripts\python.exe -m pytest tests/api/test_memory.py -q -k "touch"` | 11 passed (1 StarletteDeprecationWarning about testclient — unrelated) |
| `.venv\Scripts\python.exe -m pytest tests/agents/test_context_agents.py -q -k "touch"` | 5 passed |
| `.venv\Scripts\python.exe -m pytest tests/cli/test_memory.py -q -k "touch"` | 5 passed |
| `.venv\Scripts\python.exe -m pytest tests/memory/test_e2e_remember_recall.py -q` | 19 passed (T04's base fixture file is green at HEAD) |

Pre-existing failures: exactly one observed (`test_ledger_with_events`), already attributed to the host/code bug outside this run's scope. ruff/mypy not run (mission baseline owns them; my changes are docs-only).

## 10. Risks (ranked)

1. **P-04 fallback silently disabled by a missing embedder** — if implemented without the `Runtime.embedder` field, the "fallback" either constructs a second embedder (cost) or never runs (silent no-op, exactly the class of lie the repo bans). Evidence: `runtime.py:163-190` has no field; `runtime.py:312-335` drops it. Mitigation: field + the two-half T04 test (present → hit, absent → 0).
2. **P-06 panels render fake data** — three panels have no source; any "score" shown before P-07 would be invented. Evidence: §1 P-06 table (zero callers of `attention_allocation_score`; two never-written tables). Mitigation: null → `"No data yet"` (which T07 already demands).
3. **Shared-file collisions inside this wave** — `api/app.py` (P-04a + P-06 API), `runtime.py` (P-01 + P-04 + INV-01). Mitigation: COMMS CLAIM + sequential commits per §5.
4. **Pre-existing `dashboard.py` strftime failure** masks T-07 verification in `tests/tui`. Mitigation: run `pytest tests/tui -q` before starting and compare failure sets (1 known failure is the baseline).
5. **ANSI/control characters in panel strings** (hostile-input gap in the TUI render path; payload guard exists only on the ingestion side at `creator.py:1707`). Evidence: renderers print raw `goal_preview` (`dashboard.py:110`). Not in plan scope — flagged, not fixed.

## 11. Unknowns and how to resolve them (also §4)

| unknown | who/what answers it |
|---|---|
| T07's API target (extend `/health` vs new endpoint) | orchestrator / plan rev 6 wording |
| `Runtime.embedder` field approval (shared `runtime.py`) | orchestrator; coordinate with INV-01 |
| `touch_many` increment atomicity | read `memory/decay.py:305+` during implementation |
| `test_ledger_with_events` on Linux CI | CI run on the matrix |
| existing draft of ADR-0036 §P01 wording | operator's plan/docs outside repo |

## 12. Out-of-scope observations

See §3 (5 discoveries). Additionally: `dashboard.py:59` `_fmt` has the same `%H:%:%M:%S` typo as :160 but is currently exercised only through paths that may not hit it; `render_status`'s `when.strftime` twin at `dashboard.py:160` is the failing one. No fixes applied — write-set is this report only.

## 13. Scratch files created

- `C:\Users\devan\AppData\Local\Temp\opencode\w0-inv-04\diff_touch.py` (AST pairwise diff of the 4 `_touch_recalled` copies — output quoted in §1 P-004)
- `C:\Users\devan\AppData\Local\Temp\w0-inv-04-tui.txt`, `w0-inv-04-tui2.txt`, `w0-inv-04-e2e.txt`, `w0-inv-04-e2e2.txt` (pytest output captures for §9)

Nothing else was created, modified, staged or committed; no COMMS line other than the single DONE append at completion.
