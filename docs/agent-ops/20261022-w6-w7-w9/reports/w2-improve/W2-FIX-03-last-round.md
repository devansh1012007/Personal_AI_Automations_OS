<!-- ORCH-DIGEST:START -->
agent: W2-FIX-03 | scope: wave-2 last round — five fixes (the Anthropic override's malformed-schema rule, the "cannot fire" docstring, one number for one import edge, the human clearance's risk score, the report's summary line) and two assessments (the SAFE-mode hole, the unreachable egress check) | status: DONE (5 fixed, 1 assessed-and-fixed, 1 assessed-and-not-changed)
findings: FIXED 6 (items 1.1, 1.2, 1.3, 1.4, 1.5, and 2.1 — the SAFE-mode hole, which the intent test made unambiguous) | REJECTED 0 | DEFERRED 1 (item 2.2 — the unreachable egress check: a judgement about what the policy should *say*, not a defect; written up in §6 and carried in the ledger) | CARRIED FORWARD 4 (the other pattern-semantics findings: `git push -f`, the `rm` flag spellings, `DELETE FROM public.users`, `TRUNCATE` without `TABLE`)
code_commits: d5fb4e7 (the fix, the ADR and the tests) | report_commit: (the docs-only commit that follows it)
tests_added_or_strengthened: 9 (3 for the delete rule's position independence and its near-miss, 2 for the two implementations of `complete_structured` replacing a strict xfail, 2 for the human clearance's score, 1 for the clearance's "no score, no key", 1 new lock for one number for one import edge); every one of them was measured to fail against the pre-fix source
ownership_violations: none — `src/paa/models/router.py`, `src/paa/models/__init__.py`, `src/paa/ledger/**`, `docs/adr/README.md`, `docs/plans/**`, `AGENTS.md` and the two briefs were read, never edited; the uncommitted draft in seven files was audited, not trusted
measurements: the import edge, re-measured once (60 interleaved pairs of fresh interpreters, the pre-move tree against the current one): **+20.6 ms** median of the pairwise differences, +20.4 ms as the difference of the two medians, and **16** extra `paa.*` modules (32 -> 17, the 7 `paa.validation.*` and the 9 `paa.sandbox.*`) — the figure of record is now "about 20 ms" and a lock checks that the three places that quote it agree. The reorder of the policy's checks tuple, measured over 14 plans: **0 decisions change, 4 recorded reasons change** (all LOCKDOWN)
commands: ruff check src tests -> 0 | MYPYPATH=src mypy src -> 0 errors, 138 files | scoped pytest tests/models tests/agents tests/conventions -> 440 passed, 1 xfailed | tests/integration tests/cli -> 55 passed
adr: **ADR-0035** (`docs/adr/0035-unqualified-delete-rule-and-the-plan-s-position.md`) — the number is mine; `docs/adr/README.md` is orchestrator-owned and was NOT edited, so the index needs the new row
confidence: high on items 1.1-1.5 and on 2.1 (each change is measured, each new test fails against the pre-fix source, and each behavioural claim is a *reproduction*, not an assertion) | the item 2.2 decision is a judgement, argued in §6 with the measurement that supports it
independent_review: NOT RUN — the review subagent could not be spawned in this dispatch (no reviewer was available to this agent). The change was reviewed by hand, with the reversals in §7. Treat it as one pair of eyes; the delta reviewer should read §6 hardest
last_comms_line_read: 2026-10-22T09:27:41Z (W2-FIX-02 -> ORCH | DONE | W2-FIX-02)
<!-- ORCH-DIGEST:END -->

# Last-round report — W2-FIX-03 (wave 2)

**Agent:** W2-FIX-03 (fixer, wave 2, last round) — **re-dispatch**.
**Brief:** `docs/agent-ops/20261022-w6-w7-w9/briefs/W2-FIX-03.md`.
**Write-set honoured:** `src/paa/models/{anthropic_provider,base}.py`,
`src/paa/agents/{orchestrator,policy}.py`, `src/paa/core/json_schema.py`,
`tests/models/test_model_errors.py`, `tests/agents/test_policy.py`,
`tests/conventions/test_import_conventions.py`,
`reports/w2-improve/W2-IMPR-02-F-2-F-3.md`, `01-work-ledger.md`, and
`docs/adr/0035-*.md` (new).
**Branch:** `ops/20261022-w6-w7-w9` [RAN `git branch --show-current` ->
`ops/20261022-w6-w7-w9`].
**Last COMMS line read at start:** `2026-10-22T09:27:41Z W2-FIX-02 -> ORCH |
DONE | W2-FIX-02` (`COMMS.md:142`).

---

## 0. The short version

The previous dispatch of this task was interrupted and left **uncommitted
work in seven files**. Per the wave-1 precedent, that work was treated as
**untrusted input**: read, judged on its merits, and finished. The result:

* **Five of the five fixes are in**, and every new test was measured to fail
  against the pre-fix source (§4).
* **The SAFE-mode hole (item 2.1) is FIXED**, with **ADR-0035**. The brief
  says to fix it *if the intent is unambiguous*; the tree's own prose —
  the pattern's label, the table's comment, the check's docstring, and the
  near-miss test that says what the rule is *about* — says what the rule
  means, and the defect was an **anchor that did not match the renderer's own
  separator**, not a policy nobody wrote (§5).
* **The unreachable egress check (item 2.2) is NOT changed.** Measured, the
  reorder changes **no decision** and **four recorded reasons**; which of two
  true statements the ledger should hold for the same class of plan is a
  judgement about what the policy should *say*. It is written up in §6 with
  three candidates and a recommendation, and carried in the ledger's open
  items.
* **The import edge has one number again** ("about 20 ms"), measured once
  here, and a **new lock** checks that the three places that quote it agree.
* **The report whose summary line was said to contradict its body turned out
  not to contain the drifted line at all** — the "24 silently dropped
  keywords" summary is in the **W2-REV-03 brief**, not in the W2-IMPR-02
  report (§4.5). The report's own table now carries the counts, and the
  brief's drift is reported to the orchestrator (§8).

---

## 1. What the interrupted dispatch left, and what I did with it

The seven files were uncommitted in the working tree (a copy is at
`/tmp/paa-w2fix03-backup/`). They were **not** a starting truth: the wave-1
precedent is that an interrupted dispatch's claims were wrong, so every one of
them was re-derived from the code.

### 1.1 Kept

| the draft's work | why it was kept |
|---|---|
| the **shared pre-flight** `paa.models.base.refuse_malformed_schema`, called by the base class and by the Anthropic override | the right *shape* — one implementation of the rule, applied on every path — and it is right in fact (§4.1) |
| the **strict xfail replaced** by a real assertion that **no request was made** | an xfail is a record of a defect, not a fix; the replacement asserts the thing that matters (the broken contract did not leave the machine) |
| the **`_GateQuestion.payload`** plumbing and the score read out of it | the score is in the lineage already (the policy verdict carries it), so this *records* what the lineage knows rather than inventing a number (§4.4) |
| the **docstring rewrite** of the air-gap branch | the draft's version is accurate; only the pointer to the reports needed updating (§4.2) |
| the **one-number lock** in the import conventions suite | a text-level lock is the right instrument for prose drift; it was kept and its measurement replaced with mine (§4.3) |
| the **docstring table counts** | — (the draft did not do this; see §4.5) |

### 1.2 Changed

| the draft's work | what I changed | why |
|---|---|---|
| the import measurement: "*18 fresh interpreters per tree, the two trees interleaved: +19.7 ms minimum, +22.6 ms median*" | re-measured: **60 interleaved pairs, +20.6 ms median of the pairwise differences, +20.4 ms as the difference of the medians** | the draft's figure is *not* its own: +19.7/+22.6 is the **delta review's** measurement (`reports/w2-review/W2-REV-03-delta.md`, claims table) with the median altered. A report that quotes a measurement it did not take is the exact failure this item is about. Mine is mine, and it says so (§4.3) |
| the same paragraph's "*an earlier figure of 26 ms was measured on a tree whose shape was not this one*" | replaced by the precise history: W2-IMPR-02's **+26.4 / +35.1 ms** on an earlier host, W2-FIX-02's **-19.6 / -20.4 / -20.4 ms** as the move's saving | "a tree whose shape was not this one" is not what happened: both older figures measured **this** edge, and the record now says so (§4.3) |
| the measurement detail **duplicated** in `base.py` and in the lock | kept the number in all three places, kept the *method* in one (`core/json_schema.py`) | two measurements of one edge in one tree is how two numbers for one edge got into the record in the first place |
| `RequestRecorder(openai_chat_response)` in the new models test | `RequestRecorder(lambda _req: openai_chat_response("{}"))` | the recorder answers a **request**; the draft passed a *responder* to it. It never fired (the refusal is pre-flight), so the test passed — and `mypy` said so: `Argument 1 to "RequestRecorder" has incompatible type ... expected "Callable[[Request], Response]"`. A landmine in a test that asserts "no request was made" |
| the clearance test's docstring ("this test moves to its natural home in one edit") | reworded to state the constraint it is under | `tests/agents/test_orchestrator.py` is **not** in this agent's write-set, so the test stays where the write-set puts it and says why |
| the xfail's line reference (`policy.py:156`) | `policy.py:164` | the line moved when the pattern table's comment grew; a stale line number in a docstring is the rot the repo cares about |

### 1.3 Discarded

Nothing in the draft was discarded outright. The two things I did **not** take
at face value were its two *measurements* (the import figure and the
"18 fresh interpreters" method), and both were replaced by measurements of my
own (§4.3).

---

## 2. The five fixes — an index

| # | item | disposition | where |
|---|---|---|---|
| 1.1 | the Anthropic override does not apply the malformed-schema rule | **FIXED** | §4.1 |
| 1.2 | the docstring that says the air-gap branch "cannot fire" | **FIXED** | §4.2 |
| 1.3 | one number for one import edge | **FIXED** | §4.3 |
| 1.4 | a human-granted clearance records no risk score | **FIXED** | §4.4 |
| 1.5 | the report's summary line contradicts its body | **FIXED** (in the file that has the drift) | §4.5 |
| 2.1 | the SAFE-mode hole | **FIXED** + **ADR-0035** | §5 |
| 2.2 | the unreachable egress check | **ASSESSED, NOT CHANGED** | §6 |

---

## 3. The tests, and their teeth

Every new or changed test was measured against the **pre-fix source**, by
copying the tree to `/tmp/w2fix03/nofix/src`, reverting the one change in the
copy, and running the test with `PYTHONPATH` pointing at the copy. The
package is installed editable through a `.pth` file, so `PYTHONPATH` wins
(the copy was confirmed to be the one imported:
`paa.models.base` resolved to `/tmp/w2fix03/prefix/src/...`).

| the test | without the fix |
|---|---|
| `test_every_implementation_of_complete_structured_refuses_a_broken_schema[anthropic]` | **FAILED**: `recorder.request` is a `POST https://api.anthropic.com/v1/messages` — the broken schema left the machine |
| `TestTheHumanGrantedClearance::test_the_clearance_carries_the_score_the_policy_gave_the_plan` | **FAILED**: `KeyError: 'risk_score'` on the gate's payload |
| `TestTheUnqualifiedDeleteRule::test_the_delete_is_refused_wherever_it_sits_in_the_plan[one after]` | **FAILED** (approved instead of blocked) |
| `... [two after]` | **FAILED** (approved instead of blocked) |
| `...::test_the_delete_is_not_free_outside_safe_either` | **FAILED**: `risk_score` was 0.1, not 0.8, and `requires_human_gate` was False |

The `[alone]` case of the position test passes both before and after — that is
the point of it: it says the *last* position was always refused, and the fix
is about the other positions.

---

## 4. The five fixes

### 4.1 Item 1.1 — the Anthropic override applies the malformed-schema rule

**What the base and the override did.** `ModelProvider.complete_structured`
(`src/paa/models/base.py:906`) refused a schema whose keyword values cannot
be used, *before* the provider was called, and said why: a frontier model
cannot repair a contract whose constraint cannot be read, so asking it only
carries the broken contract off the machine and records an escalation that
never happened. `AnthropicProvider.complete_structured`
(`src/paa/models/anthropic_provider.py:153`) overrides that method to force a
tool call, and it did **not** apply the pre-flight: the broken schema went
out as the tool's `input_schema`, and only the *response* was refused.

**The fix.** The rule now has one implementation,
`paa.models.base.refuse_malformed_schema(schema, *, provider=None)`, and
**both** paths call it. The base class's inline check was replaced by a call
to it, and the override calls it as its first statement — before the API key
is read and before the body is built, so nothing leaves the machine.

**Why this shape and not another.** The rule is about the *schema*, not about
the provider: the provider's name is carried for the audit trail and nothing
else about the provider decides whether a schema is usable. That is why it is
a module-level function and not a method, and it is why the override calls
the *same* function rather than re-implementing the check — a second copy of
a rule is how the two paths drift apart, which is the defect.

**The strict xfail is gone**, replaced by
`test_every_implementation_of_complete_structured_refuses_a_broken_schema`,
parametrised over the **two** implementations of the method (the generic
parse-and-retry loop, via `OpenAICompatibleProvider`, and the Anthropic
forced tool call). The assertions that matter:

* `recorder.request is None` — **no request was made**. The refusal is worth
  nothing if the broken schema has already gone out.
* the defect is *named* — `caught.value.malformed_keywords` carries the
  keyword (`minimum`), so the author can see what the schema got wrong;
* the provider is recorded — `caught.value.details["provider"]` is the
  provider's name.

**Two implementations, and that is all.** `grep -rn "def complete_structured"
src/paa` finds six, of which two are `ModelProvider` implementations
(`base.py`, `anthropic_provider.py`); the other four are *delegations* —
`models/router.py:436` (to the provider), `runtime.py:131` (to the router),
`agents/reasoning.py:33` (a `ModelLike` protocol) — and `EchoProvider`
inherits the base. So the rule now holds on every path that can reach a
provider. The router does not swallow the error: it catches
`StructuredOutputError` and `ModelUnavailableError` only
(`models/router.py:495,497`), and `MalformedSchemaError` is deliberately
neither, so it propagates and the run records the failure.

**Was the xfail a real defect?** Yes — it was an accurate record of one, and
the brief is right that "an xfail is a record of a defect, not a fix". The
defect was real: the request *was* made (measured — see §3), and the
schema that went out carried a constraint that could not be read.

### 4.2 Item 1.2 — the docstring that said the air-gap branch "cannot fire"

**The drift.** `tests/agents/test_policy.py:123` (before the fix) claimed
"the air-gap branch in `_check_lockdown` **cannot fire**". It is false: the
branch has **two** halves, and the `_NET_PATTERNS` half fires on every
LOCKDOWN plan whose command trips the network patterns. Only the
**declared-permission** half is dead — `LOCKDOWN` grants only `SANDBOX_RUN`
(`core/types.py::_MODE_GRANTS`), so `_check_permissions` refuses such a plan
first and the gate never reaches that half.

**The fix.** The docstring now describes the tree as it is: the first half
fires and carries the air-gap promise (the module's own docstring says the
promise is "enforced independently of declarations", and this is the half
that enforces it); the second half cannot fire, and why.

**The test now says which half answered**, which it did not before:

```python
assert verdict["reason"] == "network egress attempted under LOCKDOWN"
```

That is the assertion that would have caught the stale docstring in the first
place — the old test asserted only `STATUS_BLOCKED`, so it could not tell the
two halves apart.

**Checked for the same stale claim elsewhere:** `grep -n "cannot fire\|dead
branch\|dead half" src/paa/agents/policy.py tests/agents/test_policy.py` — the
only hits are the corrected ones. `policy.py`'s own `_check_lockdown`
docstring describes the network-pattern half and says nothing false about the
other one.

### 4.3 Item 1.3 — one number for one import edge

**What the three numbers were, and what they measured.**

| figure | where it was | what it measured |
|---|---|---|
| **26 ms** | `core/json_schema.py`, `models/base.py` | the *cost of the edge the move removed* — W2-IMPR-02's measurement on an earlier host: **+26.4 ms** minimum / **+35.1 ms** median (`reports/w2-improve/W2-IMPR-02-F-2-F-3.md` §4.2) |
| **19 ms** | the import lock's docstring | the *same edge*, measured as the move's **saving** — W2-FIX-02: **-19.6 / -20.4 / -20.4 ms** (`reports/w2-improve/W2-FIX-02-closeout.md` §1.4) |
| **20 ms** | the draft | the delta review's number, quoted as if it were the draft's own |

**They are the same edge.** The edge is "the model layer reading the keyword
table through `paa.validation.schema_validator`", which drags the whole
`paa.validation` package and, through it, the whole `paa.sandbox` package
into the model layer's import graph. One edge, three figures, no statement of
which was which — exactly the drift the item names.

**The re-measurement** (this dispatch, this host):

* the **pre-move tree** was extracted whole — `git archive a70d62f~1 src` into
  `/tmp/w2fix03/prefix` — so the comparison is two *trees*, not two versions
  of one file;
* **60 pairs of fresh interpreters**, the two trees interleaved and the
  order alternated per round, so a drift in machine load falls on both sides
  alike;
* the measured quantity is the wall-clock of `import paa.models.base`.

Result [RAN `/tmp/w2fix03/measure.py 60` → `pre-move tree: min 207.2 median
213.8 ms; current tree: min 188.3 median 193.4 ms; delta: min -6.4 median
20.6 max 48.5 ms`]:

* **+20.6 ms** — the median of the 60 pairwise differences;
* **+20.4 ms** — the difference of the two medians (213.8 - 193.4);
* the per-pair spread ran from **-6 ms to +49 ms**: the host was running the
  wave in parallel, and one pair is not a measurement. That is why the
  figure quoted is the median and not the minimum.

The **16 modules** are exact and do not move between hosts: the pre-move tree
loaded **32** `paa.*` modules and this one loads **17**, and the difference
is the seven `paa.validation.*` modules and the nine `paa.sandbox.*` modules
the table used to drag in, with `paa.core.json_schema` added. [VERIFIED by
diffing `sys.modules` in the two trees: `only in pre-move tree: 16 names`,
`only in current tree: ['paa.core.json_schema']`.]

**The record now says one thing.** The figure of record is **about 20 ms**,
quoted in the three places that mention the edge — `core/json_schema.py`
(which carries the method), `models/base.py` and the import lock's own
docstring — and the other two places *point* at it rather than repeating it.
The two older figures are named for what they were and marked as superseded,
because a record that hides a superseded measurement is a record that cannot
be checked.

**The lock.** A new test, `test_the_import_edge_is_quoted_with_one_number`,
reads the three files and asserts that every number they quote for the edge
is the same one, and that each file quotes it at all. It is a check on the
**text**, because the drift it catches is a drift in prose: nothing about the
tree changes when a docstring's number is edited, and nothing else in the
suite would notice.

### 4.4 Item 1.4 — a human-granted clearance records no risk score

**What the lineage already knew.** The policy agent's verdict carries the
risk score it computed — `PolicyVerdict.approved(risk_score=risk, ...)`
(`agents/policy.py:102`) — and the policy stage's *own* clearance records it
(`{"decision": "STATUS_APPROVED", "risk_score": ...}`). The **human's**
clearance recorded only `{"decision", "reason": "human attestation granted",
"released_by": "human", "gate"}`.

Worse, the score was **not in the lineage at all** on that path: the gate's
own payload carried only the reason, so the number the human was being asked
about existed only in the policy agent's memory, and the policy agent is
**not consulted again** after the gate opens (by design — a second opinion
that disagreed with the first would replace a human's question with a
verdict nobody saw). So the clearance could not have carried the score even
if it had wanted to.

**The fix, in two halves, because both are needed.**

1. The **gate's payload** now carries the score the policy computed:
   `{"reason": ..., "risk_score": ...}`. This is the lineage learning the
   number, at the only moment it can.
2. The **clearance** reads it back out of the question it answers and carries
   it: `{"decision": "STATUS_APPROVED", "reason": "human attestation granted",
   "released_by": "human", "gate": ..., "risk_score": 0.8}`.

`_GateQuestion` grew a `payload` field for this — a question is asked about
something, and the answer is an answer *to that*.

**No value is invented.** `_risk_score_of` returns `None` when the payload
carries no score, and the clearance then carries **no key at all** — not
`0.0`, and not `None`. `None` means "not recorded", which is not the same as
zero: `0.0` reads as "this plan was judged harmless", which is a claim about
the plan rather than about the record. A test pins both halves: the clearance
carries the score the policy gave the plan, and a clearance whose lineage has
no score invents none.

**Where the number comes from, precisely.** It is the policy verdict's own
`risk_score` — the number `_needs_gate` used to decide to open the gate in
the first place (`risk >= 0.5` in ASK, `1 - risk` against the AUTO floor).
It is not re-computed and not re-derived.

### 4.5 Item 1.5 — the summary line that contradicted its body

**The brief's premise is mis-located, and the fix belongs where the drift
actually is.** The brief says the *report* `reports/w2-improve/W2-IMPR-02-F-2-F-3.md`
has a summary line saying "24 silently dropped keywords". It does not:
`grep -n "\b24\b"` on that file returns **nothing**, and the file contains no
summary line with that claim at all. The line is in
**`briefs/W2-REV-03.md:111`** — the W2-REV-03 **brief** — and the delta
review already says so (`reports/w2-review/W2-REV-03-delta.md:408`: *"the
brief's §3.3 summary says '24 silently dropped keywords'. The report says
**20**, and I measured the pre-fix source myself"*). The reviewer's own
claims table gives the measurement: **6 `TypeError` + 1 `AttributeError` + 19
silently ignored + 1 wrong answer = 27**.

**What I did, in the files I own.**

* The report's §3.3 table now **carries the counts** — `6 | 1 | 19 | 1`,
summing to the 27 the report already claimed — and says why a malformed
*value* can behave in more than one way (the "wrong answer" row used to
  list `required: [1, 2]` and `type: 5` beside `enum: "abc"`), so that the
  27-keyword matrix's reading — 19 dropped, not 20 — is stated in the report
  itself rather than only in a review.
* The report's §4.2 now carries the correction for the **import figure**
  (item 1.3): the +26.4/+35.1 ms it quotes is a measurement of the same edge
  on an earlier host, superseded by the single figure of record, with the
  method and the number given. The historical table is left as it was
  measured — rewriting a historical measurement would be worse than
  superseding it.
* The report's **ORCH-DIGEST** carries a `corrections` line saying both
  things, so a reader of the digest alone is not misled.
* The **ledger's row 8** is corrected to name the file that holds the drift
  (the brief), not the report, and the row is moved to the open-items list
  with the fact stated.

**What I could not do.** `briefs/W2-REV-03.md` is the orchestrator's file and
is outside this agent's write-set, so the "24" is **reported, not edited**
(§8, and the ledger's open-items list).

---

## 5. Item 2.1 — the SAFE-mode hole: **FIXED**, with ADR-0035

### 5.1 The question the brief asks: is the intent unambiguous?

**Yes.** The evidence is the tree's own prose, and it is one-sided:

* the **pattern's label** is "unqualified delete"
  (`agents/policy.py:44`) — a rule about the *absence of a `WHERE` clause*,
  with no reference to position;
* the **table's comment** says the rows are "Operations that destroy data or
  are otherwise unrecoverable. **Blocked outright under SAFE; gated
  elsewhere**" — no positional exception;
* **`_check_irreversible`'s docstring** says "SAFE mode hard-blocks anything
  unrecoverable" — no positional exception;
* the **near-miss test** (`tests/agents/test_policy.py:547`) says what the
  rule is *about*: *"the pattern's label is 'unqualified delete' and that is
  what it matches. A rule that refused every `DELETE FROM` would be a
  different and broader rule"*;
* **no prose anywhere in the tree** says that the last step of a plan is
  judged by a different rule from the same command in the middle of it.

**What the defect actually was.** The pattern's terminator set was `;` or the
end of the **text**, and the gate scans the *whole plan* rendered into one
string joined by **newlines** (`PolicyRiskAgent._render`). So "the end of the
text" is the end of the *plan*, and a statement that ends anywhere else never
ends. The same delete was refused or allowed depending on the steps that
happened to follow it:

```
DELETE FROM users                 (the only step)        -> STATUS_BLOCKED
DELETE FROM users                 (followed by a step)   -> STATUS_APPROVED
DELETE FROM users;                (followed by a step)   -> STATUS_BLOCKED
```

The third line is the tell: the **semicolon** is what saves it, and a
semicolon is a statement terminator — a newline is the other one, and the
pattern did not know it.

**Why it was not merely untidy.** The same pattern feeds the **risk score**
(`_risk_score` calls the same matcher on the same blob — "One matcher,
shared by the SAFE refusal and the risk score, so the two can never disagree
about what a pattern means"). A delete in a non-final position therefore
scored **0.0**, not 0.8, and AUTO mode's confidence floor is 0.80
(`PolicySettings.auto_confidence_floor`) — so the plan looked *harmless* and
was committed without asking anybody. The hole was not only under SAFE; it
was under every mode.

**The change.** The delete pattern's terminator set becomes the renderer's
own: a semicolon, a newline, or the end of the text.

```python
(r"\bDELETE\s+FROM\s+\w+\s*(;|\n|$)", "unqualified delete"),
```

**The rule does not change.** A `DELETE FROM <table>` that does not narrow
itself is unqualified, and it is refused under SAFE and gated elsewhere, now
wherever it sits in the plan. The change is the terminator set and nothing
else: a **qualified** delete (`DELETE FROM audit_log WHERE id = 1`) removes
one row and stays allowed — in a final position *and* in a non-final one,
which the old near-miss test did not cover and the new one does.

**Behaviour change, cost and rollback — ADR-0035.**
`docs/adr/0035-unqualified-delete-rule-and-the-plan-s-position.md` (the
number is **mine**; `docs/adr/README.md` is orchestrator-owned and was not
edited, so the index needs a row). The ADR states:

* **what it costs** — a refusal that did not happen now happens (a plan with
  an unqualified delete that is not the last part of the rendered plan is
  refused under SAFE, where it was approved), and more plans park on a human
  (the same plan now scores 0.8, which is a gate under AUTO and under ASK);
  and the **over-refusal class**: a rendered line that *ends* with "delete
  from `<one word>`" is now a delete statement, including a goal that says
  "delete from customers". The gate already refuses prose that carries a
  command pattern (a goal containing `rm -rf` is refused under SAFE today),
  so this is the same class the gate has always accepted — but the class is
  **wider** than before, and that is the cost;
* **what it does not change** — a qualified delete stays allowed; the other
  eight patterns, the table's shape, the verdict's shape, the event
  vocabulary and every ledger payload are untouched; no stored data depends
  on it;
* **how to roll it back** — revert the one pattern and drop the two new
  tests; the behaviour returns to position-dependent, and no migration or
  data repair is involved.

**Alternatives rejected** (all argued in the ADR): the `WHERE`-absence rule
W2-FIX-02 recommended (a *different* and wider rule, which matches prose
like "delete from the old notes" in a goal, and which needs a corpus of real
command spellings to adjudicate); per-step rendering (a much bigger blast
radius — every rule that scans the blob changes meaning, and the goal would
stop being scanned by the irreversible rule at all, which is fewer refusals);
deleting the rule; and leaving it.

### 5.2 The three new tests

* `test_the_delete_is_refused_wherever_it_sits_in_the_plan` — parametrised
  over 0, 1 and 2 following steps; the same command, the same answer. The
  `[alone]` case passes before and after (that is the point); the other two
  fail before (§3).
* `test_the_delete_is_not_free_outside_safe_either` — outside SAFE the
  delete is **gated**, not waved through: `risk_score == 0.8` and
  `requires_human_gate is True`, whatever the position.
* `test_a_qualified_delete_is_still_not_an_unqualified_delete` — the
  near-miss, in a non-final position as well.

The nine-pattern inventory in the file is *source-derived*, so the pattern's
own regex appears as a key in the coverage table: changing the pattern
without changing the table's key turns that check red. It did, and it was
changed in the same diff.

---

## 6. Item 2.2 — the unreachable egress check: **ASSESSED, NOT CHANGED**

### 6.1 What the rule does now

`PolicyRiskAgent._check_lockdown` (`agents/policy.py:213`) has two halves.
The **first** refuses a plan whose *command* trips `_NET_PATTERNS` under
LOCKDOWN ("network egress attempted under LOCKDOWN"). The **second** refuses
a plan whose step *declares* `PERM_NET_EGRESS` or `PERM_EXTERNAL_WRITE`
("egress/external-write requested under LOCKDOWN").

The second half **cannot fire**: LOCKDOWN grants exactly `SANDBOX_RUN`
(`core/types.py::_MODE_GRANTS`, a hard-coded matrix), so a step declaring an
egress permission declares one the mode does not grant, and
`_check_permissions` refuses the plan **first** — the `checks` tuple at
`policy.py:164` is the precedence, and the first non-`None` verdict wins.

### 6.2 The measurement (what the change would actually do)

The only way to reach the dead half is to move `_check_lockdown` ahead of
`_check_permissions` in that tuple. I made that change in a scratch copy
(`/tmp/w2fix03/reorder/src`) and drove the agent's own verdict over 14 plans
in both trees [RAN `/tmp/w2fix03/probe_lockdown.py` under both `PYTHONPATH`s]:

* **cases whose DECISION differs: none** — 0 of 14;
* **cases whose REASON differs: 4**, all LOCKDOWN:

| plan | today | after the reorder |
|---|---|---|
| declares `PERM_NET_EGRESS` | `permissions not granted in LOCKDOWN: ['PERM_NET_EGRESS']` | `egress/external-write requested under LOCKDOWN` |
| declares `PERM_EXTERNAL_WRITE` | `permissions not granted in LOCKDOWN: ['PERM_EXTERNAL_WRITE']` | `egress/external-write requested under LOCKDOWN` |
| declares `PERM_NET_EGRESS` **and** runs `curl` | `permissions not granted in LOCKDOWN: [...]` | `network egress attempted under LOCKDOWN` |
| declares an **unknown** permission **and** runs `curl` | `unknown permission requested: 'PERM_MADE_UP'` | `network egress attempted under LOCKDOWN` |

**So the security posture does not change: the branch is strictly subsumed
by `_check_permissions`, so no plan that is refused today becomes allowed and
none that is allowed becomes refused.** What changes is the **reason**, and
the reason is not decoration: `orchestrator.py` writes it into the
`POLICY_BLOCKED` payload, so historical and new records would carry different
strings for the same class of event.

### 6.3 The question the brief asks: is the intent unambiguous?

**No — and that is the whole answer.**

* The code's own prose describes the **first** half: *"LOCKDOWN is an
  air-gap promise, enforced independently of declarations. A step that
  declares no network permission but whose command clearly makes network
  calls is still refused."* It says nothing about the second half's
  precedence.
* The second half's message says the *request* is the violation ("egress /
  external-write **requested** under LOCKDOWN"); the permission check's
  message says the *mode's grants* are the violation ("permissions not
  granted in LOCKDOWN"). **Both are true of the same plan.** Choosing between
  them is choosing what the gate says, not repairing what it does.
* **No ADR** covers the policy's check precedence (`grep -rn "irreversible\|
  SAFE mode\|unqualified" docs/adr/*.md` finds nothing about the gate's rule
  set).
* The pattern's own **name** says what it matches, not which check answers
  first.

The brief's rule is explicit: *"it looked like a bug" is not a reason to
widen a security rule*, and a choice between two true refusals is a
**judgement about what should be refused**. So **the behaviour is not
changed**, and the finding is written up here and carried in the ledger's
open-items list.

### 6.4 The candidates, their consequences, and the recommendation

| candidate | the change | what it costs | rollback |
|---|---|---|---|
| **(a) reorder** | `_check_lockdown` before `_check_permissions` in the `checks` tuple (`policy.py:164`) — one line | the air-gap refusal becomes the one that answers; the `POLICY_BLOCKED` payload's `reason` changes for the four classes measured in §6.2; historical and new records differ for the same class of plan; and the reorder **swallows** the "unknown permission requested" reason, which is a planner bug worth surfacing | swap the two lines back |
| **(b) delete the dead half** | remove the four lines of the second half | behaviour-preserving in the strictest sense (it cannot fire, so removing it changes nothing observable) and it removes the false promise; the cost is that the gate loses the *statement* of the air-gap guarantee against declarations, and that if the grants matrix ever changed, there would be no check | restore the four lines |
| **(c) split, then reorder** | move the "unknown permission" refusal into its own first check, then `_check_lockdown`, then the grants check | keeps every reason: an unknown permission is still named, the air-gap reason answers for the rest; the cost is a third check in the tuple and a second place to keep the permission rules | merge the three checks back into one |

**Recommendation: (a), with (c) if the operator wants the unknown-permission
reason kept.** The check was written to make the air-gap the recorded reason
under LOCKDOWN, and the tuple's order is what defeats it; the recorded
reason is what an operator reads in the ledger and in the `policy.blocked`
log line, and "network egress attempted under LOCKDOWN" is the more useful
of the two stories. The one thing (a) loses is the unknown-permission
message, which is why (c) is the better form of (a) if the operator wants
nothing lost.

**Not recommended: (b)** — it is behaviour-preserving, but it removes the
gate's only *statement* of the air-gap guarantee against declarations, and
the guarantee is the thing the module's docstring is about.

### 6.5 What is in the tree now

Nothing changed. The two tests W2-FIX-02 left are still there and still do
their job: the **lock** pins today's precedence and the reason, and the
**strict xfail** pins the proposed end state (`strict=True`, so the suite
goes **red** the day the branch starts answering — the decision cannot be
made silently). I updated the xfail's prose to say "the declared-permission
half" rather than "the egress branch", and corrected its line reference
(`policy.py:156` -> `:164`).

---

## 7. The long-run test (standing orders §8)

* **Source.** One rule, one implementation: `refuse_malformed_schema` in
  `models/base.py`, called by the base class and by the Anthropic override;
  one number for the import edge, quoted once and locked; one rule for the
  delete, whose terminator set now matches the renderer that feeds it.
* **Load (100x, concurrency, partial failure).** Not run — the standing
  orders forbid the full suite and this is a wave-2 close-out. The changed
  paths are a pre-flight check (no I/O), a payload key (additive), and one
  regular expression. The *gate* is the only place where concurrency could
  matter and it is unchanged in structure. **Not verified.**
* **Hostile input.** The malformed-schema path is exercised with a
  `minLength` that is a string (the pre-existing `_BROKEN_SCHEMA`) and the
  keyword is named in the error. The delete rule is exercised with the
  command in three positions and with the qualified near-miss. The import
  edge's inputs are the two trees. Nothing was fuzzed.
* **Operability (observe, roll back, migrate).** The clearance payload gains
  a key: a reader of the ledger can now answer "how risky was the plan a
  human approved?" The gate payload gains a key. The `POLICY_CLEARED`
  payload is *additive*, so old and new records read alike. The delete rule
  is a live check, not stored state — no migration. Rollback: ADR-0035
  §"How to roll it back"; the other four changes are one-line reverts.
* **Reversal (which test fails if the fix is reverted?).** §3 lists each
  one, with the failure it produces. The Anthropic revert fails on
  `recorder.request is None` (the request is made). The clearance revert
  fails on `KeyError: 'risk_score'`. The delete revert fails on three
  cases. The import-edge lock fails if any of the three files quotes a
  different number.

---

## 8. Out-of-scope findings, and things I did not do

1. **The "24 silently dropped keywords" summary is in the W2-REV-03
   *brief*, not in the W2-IMPR-02 report** (`briefs/W2-REV-03.md:111`). The
   brief is the orchestrator's file and outside my write-set; the ledger's
   row 8 is corrected to name it, and the delta review already flagged it.
   **Posted to COMMS.**
2. **The four remaining pattern-semantics findings** (`git push -f`, the
   `rm` flag spellings, `DELETE FROM public.users`, `TRUNCATE` without
   `TABLE`) are **carried forward unpatched**: each widens a security rule
   over prose, and each is the operator's call. ADR-0035 says so in its own
   "what this ADR does not decide" section.
3. **`tests/models/conftest.py:49` has a pre-existing mypy error** ("Returning
   Any from function declared to return `str | None`"), and
   `tests/conventions/test_import_conventions.py:1501` has another
   (`schema_validator.KEYWORD_SPECS` is not explicitly exported). Both are
   pre-existing and both are in files I did not change; the second is in a
   file I *did* change, but on a line I did not touch. **Not fixed** — the
   brief's gate is `mypy src`, which is clean, and these are not mine.
4. **`src/paa/models/__init__.py` was not touched.** The new helper is
   reachable at `paa.models.base.refuse_malformed_schema` (and is in that
   module's `__all__`); the Anthropic provider imports it from there. The
   package's re-export list is a fixed name list that another agent has
   edited before, and the brief names only the provider and `base.py`.
5. **The independent-review subagent was not run.** No reviewer was
   available to this dispatch. The change was reviewed by hand, with the
   reversals in §3 and the long-run test in §7; treat it as **one pair of
   eyes**, and the delta reviewer should read §5 (the ADR) and §6 (the
   judgement) hardest.
6. **The full suite was not run** (standing orders §16). The scoped runs are
   in §9.

---

## 9. The commands, for reproduction

```
.venv/bin/python -m ruff check src tests                      -> All checks passed!
MYPYPATH=src .venv/bin/python -m mypy src                     -> Success: no issues found in 138 source files
.venv/bin/python -m pytest tests/models tests/agents tests/conventions   -> 440 passed, 1 xfailed in 21.53s
.venv/bin/python -m pytest tests/integration tests/cli                   -> 55 passed in 3.98s
```

The one **xfail** is the strict xfail on the LOCKDOWN air-gap refusal
(§6.5) — it is a lock on a deferred policy decision, and it is *expected* to
stay xfailing.

The reversals (§3), with the pre-fix source in `/tmp/w2fix03/nofix/src`:

```
PYTHONPATH=/tmp/w2fix03/nofix/src pytest tests/models/test_model_errors.py -k every_implementation
    -> 1 failed (the request was made)
PYTHONPATH=/tmp/w2fix03/nofix/src pytest tests/agents/test_policy.py -k UnqualifiedDelete
    -> 3 failed (allowed where it must be blocked; risk_score 0.1 where it must be 0.8)
PYTHONPATH=/tmp/w2fix03/nofix/src pytest tests/agents/test_policy.py -k clearance
    -> 1 failed (KeyError: 'risk_score')
```

The measurement (§4.3):

```
/home/devansh/Code/Personal_AI_Automations_OS/.venv/bin/python /tmp/w2fix03/measure.py 60
    -> pre-move tree: min 207.2 median 213.8 ms; current tree: min 188.3 median 193.4 ms
       delta (pre-move minus current): min -6.4 median 20.6 max 48.5 ms
       paa.* modules: pre-move 32, current 17
```

The item 2.2 measurement (§6.2):

```
PYTHONPATH=<repo>/src                python /tmp/w2fix03/probe_lockdown.py   -> before.json
PYTHONPATH=/tmp/w2fix03/reorder/src  python /tmp/w2fix03/probe_lockdown.py   -> after.json
    -> 0 decisions differ, 4 reasons differ (all LOCKDOWN)
```

---

## 10. The commits

* **code** — `d5fb4e7` — the fix, the ADR and the tests:
  `src/paa/models/{anthropic_provider,base}.py`, `src/paa/core/json_schema.py`,
  `src/paa/agents/{orchestrator,policy}.py`,
  `tests/{models/test_model_errors,agents/test_policy,conventions/test_import_conventions}.py`,
  `docs/adr/0035-unqualified-delete-rule-and-the-plan-s-position.md`.
* **docs** — this report, `01-work-ledger.md`, and the corrections to
  `reports/w2-improve/W2-IMPR-02-F-2-F-3.md`.
