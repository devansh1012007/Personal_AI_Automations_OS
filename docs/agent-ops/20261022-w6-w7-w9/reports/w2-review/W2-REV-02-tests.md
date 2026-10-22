<!-- ORCH-DIGEST:START -->
agent: W2-REV-02 | scope: wave 2 — T-002, T-005, T-006, T-007, T-008 (+ W2-FIX-01) | verdict: APPROVE-WITH-CHANGES
tests_reviewed: 850 (validation 436, observability 136, models 154, agents 112, integration 16, conventions 17, storage/live-S3 40 cases) | fail-before/pass-after confirmed: 33 mutations | reasoned: 3 | not-possible: 0
findings: BLOCKER 0 | MAJOR 3 | MINOR 3 | NIT 3
flaky_suspects: none (11 repeat runs, 5 of them the timing-sensitive concurrency file; 0 failures)
missing_edge_cases: P1 1 | P2 2 | P3 3
mutation_survivors: 3 of 36 plausible bugs not caught (1 disclosed-and-explained by the report; 1 contradicts the report's own tally; 1 uncovered gap I found)
changed_behavior_without_tests: the six irreversible-operation patterns in src/paa/agents/policy.py::_IRREVERSIBLE_PATTERNS (plan clause 0.2, undelivered and unrecorded); the exception-chain key leak in src/paa/models/router.py::_local_only (pre-existing, but asserted as covered by tests/models/test_key_secrecy.py)
confidence: high (36 mutations run by me against a shadow copy, the live S3 suite run against two real servers I started myself, 11 repeat runs, every claim audited against its own report)
last_comms_line_read: 2026-10-22T05:23:46Z (W2-FIX-01 DONE)
<!-- ORCH-DIGEST:END -->

# Test-quality review — W2-REV-02 — wave 2 (the five test suites)

## VERDICT

**APPROVE-WITH-CHANGES.** The wave's tests are, in the main, genuinely
reversal-proof: 33 of the 36 plausible bugs I mutated into the source were
caught by the suite that claims to cover them, including every trap the brief
named — the event-sequence assertion is **list equality** everywhere, the
semaphore's cap is asserted `==` and not `<=`, the mediated cycle runs at
`MAX` and asserts the cycle *path* (not merely that an error was raised), the
0.82 threshold is read on **both** sides with an injected embedder, the
percentiles are pinned by **hand-computed** values and by a property over
`rank = (n-1)·q`, and `finish_run`'s returned row is asserted field by field
rather than used as setup. Three findings survive the audit: one real coverage
gap (six of the nine irreversible-operation patterns), one claim that does not
reproduce (the S3 report's mutation 11) and one assertion narrower than the
claim it makes (the key-leak suite looks at the exception but not at its
`__cause__`).

---

## 0. Method

**What I did not do:** I did not edit any code, test or record. The
repository's working tree was never modified (`git status --porcelain` shows
only the two untracked brief files that were already there when I started).

**The scratch copy.** `src/paa` was copied to
`/tmp/opencode/w2rev02/shadow/src/paa`, and every mutation was applied to
that copy. The suite was then run with
`PYTHONPATH=/tmp/opencode/w2rev02/shadow/src`, which shadows the editable
install (the `.pth` in `.venv` merely *appends* the repo's `src` to
`sys.path`, so `PYTHONPATH` wins). `diff -rq src/paa …/shadow/src/paa` at the
end reports **no differences** — the shadow was restored after every
mutation, and the repository's own sources were never touched.

The mutation harness is `/tmp/opencode/w2rev02/mutate.py`: it applies one
exact-string substitution to one file in the shadow, runs a pytest selection,
prints `DETECTED`/`SURVIVED` plus the failing test ids, and restores the file
from the repository. Whole-file reversals (`git show <sha>~1:<path>`) were
done by hand into the shadow.

**The live S3 suite — VERIFIED, not skipped.** I started **my own** two
containers with unique names and loopback-published ephemeral ports:

```
docker run -d --name w2rev02-s3-seaweed -p 127.0.0.1::8333 \
  -e AWS_ACCESS_KEY_ID=paa-test-key -e AWS_SECRET_ACCESS_KEY=paa-test-secret \
  chrislusf/seaweedfs:4.48 server -s3 -dir=/data -ip=0.0.0.0
docker run -d --name w2rev02-s3-rustfs -p 127.0.0.1::9000 \
  -e RUSTFS_ACCESS_KEY=paa-test-key -e RUSTFS_SECRET_KEY=paa-test-secret \
  -e RUSTFS_VOLUMES=/data rustfs/rustfs:latest
```

`docker port` published `127.0.0.1:32777` (SeaweedFS) and `127.0.0.1:32778`
(RustFS); the suite was pointed at them with the labelled
`PAA_TEST_S3_SEAWEEDFS_*` / `PAA_TEST_S3_RUSTFS_*` variables and ran
**40 passed in 16.50s**. Both containers were removed afterwards
(`docker rm -f w2rev02-s3-seaweed w2rev02-s3-rustfs`); the operator's
`paa-stack-*` containers were never started, stopped, rebuilt, pruned or
otherwise touched — they were `Up 32/33 hours (healthy)` before and after.

**Baselines.**

```
$ .venv/bin/python -m pytest tests/validation tests/observability tests/models \
      tests/agents tests/integration tests/conventions -p no:randomly
868 passed, 3 xfailed in 33.35s

$ .venv/bin/python -m pytest tests/storage/test_s3_live.py -rs -p no:randomly
1 skipped in 0.21s
SKIPPED [1] tests/storage/test_s3_live.py:106: needs a live S3 server: set
  PAA_TEST_S3_ENDPOINT (and its two credentials), or a labelled
  PAA_TEST_S3_<LABEL>_ENDPOINT
```

(the second is with no server configured — the skip is loud and names the
variables; with two servers configured it is 40 passed, above.)

**Repeat runs (flakiness).**

| selection | runs | outcome |
|---|---|---|
| the six scoped suites, **reverse** directory order | 3 | 868 passed / 3 xfailed each time |
| `tests/models` | 5 | 154 passed each time (3.26 – 4.49 s) |
| `tests/models/test_router_concurrency.py` (the timing-sensitive file) | 5 | 3 passed each time (0.13 s) |

No ordering dependence, no flake, no shared state between tests. The
observability suite reconfigures structlog **globally**; the fixture at
`tests/observability/test_logging.py:72` saves and restores the configuration,
the root logger's level and the context variables, so the reverse-order runs
exercise exactly that risk and it does not bite.

**Duplication.** An AST sweep over every test file in the five suites found
**zero** byte-identical test bodies and only the near-identical shapes that
are legitimately separate (three hand-computed percentile values that a
failure must name individually; three refusal tests of different kinds). No
copy-paste suite.

---

## 1. FINDINGS

### R-W2-201 — MAJOR — six of the nine irreversible-operation patterns are uncovered, and the omission is unrecorded

* **Where:** `tests/agents/test_policy.py:107` (`TestSafeMode::test_irreversible_ops_are_blocked`,
  three parameters) and `src/paa/agents/policy.py:35` (`_IRREVERSIBLE_PATTERNS`, nine entries).
* **What is wrong.** The ROADMAP 0.2 row says *"policy gates (LOCKDOWN blocks
  egress, SAFE blocks deletion, anti-goal at 0.82)"*, and the **plan**
  (`docs/plans/w6-w7-w9.md` §4, row 0.2) named *"the six untested
  irreversible patterns"* as part of T-006's deliverable. Three of the nine
  patterns are exercised (`rm -rf`, `shutil.rmtree`, `git push --force`). The
  other six — destructive DDL, table truncation, `git reset --hard`, raw
  device write, unqualified `DELETE FROM`, `os.remove/unlink/rmdir` — are
  not, and the ROADMAP's phrase "SAFE blocks deletion" is true only of the
  first three.
* **Evidence.** I deleted the six uncovered patterns from
  `_IRREVERSIBLE_PATTERNS` in the shadow and ran the whole agents and
  integration suites:

  ```
  $ python mutate.py A13-agents-six-irreversible-patterns-deleted \
      agents/policy.py '<six patterns>' '<git push pattern only>' \
      -- tests/agents tests/integration -p no:randomly
  RESULT A13-agents-six-irreversible-patterns-deleted: SURVIVED
    cmd: pytest tests/agents tests/integration -p no:randomly
    summary: 128 passed in 4.98s
  ```

  A SAFE-mode policy gate with six of its nine teeth removed is **green**.
* **Why it matters.** SAFE is the mode an operator reaches for when a plan
  is risky. `DROP TABLE`, `TRUNCATE TABLE` and `git reset --hard` are the
  three that lose data irreversibly, and the gate that is supposed to refuse
  them has no test for any of them.
* **The bookkeeping is the second half of the finding.** T-006's brief
  (`briefs/W2-IMPL-03.md`) narrowed the scope to four clauses and **dropped**
  the irreversible-patterns bullet; the agent neither delivered it nor
  recorded the drop. The report's §12 "Deviations from the plan/brief" does
  not mention it, §13 does not, and `grep -n irreversible` on
  `W2-IMPL-03-T-006.md` returns **nothing**. A clause that leaves the tree
  without a line saying so is a clause that leaves the tree.
* **Suggested fix.** Parametrise the existing test over all nine patterns
  (read the table out of the module's source, as
  `tests/validation/test_ast_scanner.py::_rules_in_source` already does, so a
  tenth pattern added later cannot slip past), and add a line to T-006's
  §12 recording that the plan's bullet was not in the brief.

### R-W2-202 — MAJOR — the S3 report's mutation 11 does not reproduce, and the ADR-0032 correction it proposes is false in the operative respect

* **Where:** `docs/agent-ops/20261022-w6-w7-w9/reports/w2-impl/W2-IMPL-01-T-002.md:394`
  (the mutation table) and §13.1 (the proposed ADR-0032 wording);
  `src/paa/storage/coldlake/minio_backend.py:323` (`_driver_cause`).
* **What is wrong.** The report's mutation table says:

  > | 11 | `put_stream` written through the **wrapping** variant (`upload_file`) |
  > `test_both_write_paths_agree_about_a_missing_bucket[RUSTFS]` | **caught** |

  and §13.1 builds the proposed ADR-0032 correction on the claim that the
  wrapper *"carries neither a response nor a cause, and therefore nothing for
  the translation to read"*. Both halves are wrong.
* **Evidence 1 — the mutation survives.** I routed `put_stream` through
  boto3's `upload_file` (the wrapping variant) in the shadow and ran the
  whole live suite against both real servers:

  ```
  $ python mutate.py S3-11-put-stream-via-upload-file \
      storage/coldlake/minio_backend.py '<upload_fileobj(...)>' '<upload_file(...)>' \
      -- tests/storage/test_s3_live.py -p no:randomly
  RESULT S3-11-put-stream-via-upload-file: SURVIVED
    cmd: pytest tests/storage/test_s3_live.py -p no:randomly
    summary: 40 passed in 27.37s
  ```

  I re-ran the single named test with the mutation applied and the shadow
  confirmed to be carrying it, to rule out a silent no-op:
  `tests/storage/test_s3_live.py::test_both_write_paths_agree_about_a_missing_bucket` →
  **2 passed**.
* **Evidence 2 — why it survives.** boto3's `S3Transfer.upload_file` raises
  `S3UploadFailedError` *inside* an `except ClientError` block, so the
  driver's error is the wrapper's implicit **`__context__`**. And
  `_driver_cause` follows exactly that:

  ```
  src/paa/storage/coldlake/minio_backend.py:328
      cause = exc.__cause__ or exc.__context__
  ```

  Measured against RustFS:

  ```
  wrapper: S3UploadFailedError
    __cause__ : NoneType
    __context__: NoSuchBucket
    _driver_cause(wrapper) -> NoSuchBucket
    _s3_error_code(cause) -> NoSuchBucket
  ```

  So the wrapper is *not* opaque: `__cause__` is unset (which is what the
  report measured) but `__context__` carries the driver's `ClientError` with
  its response, and the store's own unwrapping reads it. The store's answer
  is therefore unchanged by the mutation — and the test correctly does not
  go red.
* **Consequence 1 — the tally is overstated.** The report claims *"13
  runtime mutations, 10 caught, 3 explained"*. Mutation 11 is not caught, so
  the honest figure is **at most 9 of 13**. (Mutation 10, `_driver_cause →
  None`, *does* survive the live suite — but that one **is** disclosed, and
  wave 1's unit suite catches it: I ran it against the whole `tests/storage`
  and got 7 failures, including
  `tests/storage/test_blob_errors.py::TestTheS3Translation::test_a_wrapper_is_unwrapped`
  and `::test_a_wrapper_is_unwrapped_through_its_context` and
  `tests/storage/test_minio_backend.py::test_the_upload_wrapper_is_unwrapped`.
  That one is fine.)
* **Consequence 2 — the proposed record is false.** §13.1 tells the records
  task to write into ADR-0032 that *"the wrapper `upload_file` produces
  carries neither a response nor a cause"*. If that wording lands, a future
  maintainer reads "the wrapper is opaque" and may delete the unwrapping —
  and on the *other* boto3 versions, or on `upload_file`'s multipart path,
  the wrapper's shape may differ. The record must say what is true: the
  wrapper carries **no response of its own** and **no `__cause__`**, but the
  driver's error is its implicit **`__context__`**, and `_driver_cause`
  follows it.
* **Consequence 3 — the coverage consequence is benign but should be
  written down.** The live suite **cannot** pin the wrapper-unwrapping,
  because the live path never produces a wrapper: `upload_fileobj` propagates
  the raw `ClientError` with its response intact, so the store reads the
  answer directly. The unwrapping is pinned by wave 1's unit suite (the
  names are in Consequence 1). That is a fine place for it — but ADR-0033's
  "limits" and the live suite's docstring should say so rather than leave
  the reader thinking the live suite proves it.
* **Suggested fix.** Correct the mutation table (11 → not caught) and §13.1's
  wording; state in ADR-0033 that the wrapper-unwrapping is covered by the
  unit suite and not by the live suite, and why.

### R-W2-203 — MAJOR — the key-leak suite's assertion is narrower than the file's headline claim: the key survives on the exception's `__cause__` and in the traceback

* **Where:** `tests/models/test_key_secrecy.py:1` (the claim) and
  `:270-289` (`test_the_local_only_path_still_carries_no_key`, the assertion);
  `src/paa/models/router.py:568` (`raise ModelUnavailableError(...) from exc`).
* **What is wrong.** The file's docstring claims *"No API key ever reaches a
  log line, a ledger payload, or an exception."* The assertion is

  ```python
  error = excinfo.value
  assert_no_key_leak(str(error), error.details)
  assert error.details["underlying_body_excerpt"] == "invalid api key: ***REDACTED***"
  ```

  `str(error)` and `error.details` are clean — the router scrubs the copy it
  attaches. But the raised error is `raise … from exc`, so **`exc` — the
  provider's own unscrubbed error — is still the `__cause__`**, and the
  provider's `body_excerpt` carries the key verbatim. The key is therefore
  in the **traceback**, which is the surface an operator actually pastes into
  a bug report. The file's own stated subject ("or an exception") is not
  covered by its assertion.
* **Evidence.** Using the test file's *own* provider and router:

  ```
  raised: ModelUnavailableError
  key in str(error)      : False
  key in error.details   : False
  key in error.__cause__ : True
  key in the TRACEBACK   : True
  cause details: {'provider': 'local', 'status_code': 401,
                  'body_excerpt': 'invalid api key: sk-test-abcdef0123456789ABCDEF'}
  ```
* **On the helper itself.** `assert_no_key_leak`
  (`tests/models/conftest.py:87`) is **not** the weak link: it is used
  everywhere it should be (the two provider suites, the retry suite, the
  openai/gemini suites and the whole of `test_key_secrecy.py`), and it *is*
  given log **records** rather than rendered strings in the log case
  (`assert_no_key_leak(entry)` where `entry` is the structlog event dict, so
  `str(entry)` renders every value, nested included). The gap is the
  **call site's choice of arguments**, not the helper's strength: the helper
  looks at what it is handed, and the chain was never handed to it.
* **Suggested fix.** Either (a) extend the assertion to the chain —
  `assert_no_key_leak(str(error), error.details, error.__cause__,
  getattr(error.__cause__, "details", None))` — and treat the failure as a
  code defect in `router.py::_local_only` (scrub the cause, or do not chain
  an exception that carries an unscrubbed credential), or (b) narrow the
  file's claim to "no key reaches a log line, a ledger payload, or the
  **raised error's own message and details**" and say plainly that the
  provider's original exception is retained as the `__cause__` for
  diagnosis. (a) and (b) cannot both be skipped: the current state is a
  claim that is stronger than the truth, asserted by a test that cannot see
  the difference.
* **Note on ownership.** The `from exc` chain is pre-existing (it dates from
  the first commit); wave 2 did not introduce it. But wave 2 is the wave that
  wrote the file that claims the stronger property, and the wave that wrote
  the escalation path's redaction.

### R-W2-204 — MINOR — the live suite cannot tell one server from another, so "two independent implementations" is a convention rather than a fact the suite establishes

* **Where:** `tests/storage/test_s3_live.py:115` (`pytest_generate_tests`)
  and `:13-19` (the docstring's claim).
* **What is wrong.** The suite's value is precisely that the same body runs
  against *two different programs*. Nothing in the suite establishes that
  the two endpoints are different programs: the server identity is carried
  entirely by the operator's choice of environment-variable **names**, and
  the test ids (`[SEAWEEDFS]`, `[RUSTFS]`) are the labels the operator
  typed. Two containers of the same image, wired to the same two variables,
  would satisfy **every** assertion in the file, and the suite would
  report 40 passed.
* **Evidence.** `tests/storage/test_s3_live.py:103` —
  `SERVERS: list[S3Server] = s3_servers()` — reads the environment and
  nothing else; the fixture at `tests/storage/conftest.py:337` builds a
  boto3 client against `server.endpoint` and never asks the server what it
  is.
* **Suggested fix.** Either record the server's identity in the fixture — a
  `head_bucket`/`list_buckets` response header, or a bucket-name convention
  the server echoes — and assert the two labels answer *differently* where
  they are known to differ (ADR-0033 records one such difference: SeaweedFS
  creates a bucket a write names, RustFS refuses it), or state in the
  docstring that the independence is asserted by the operator and by ADR-0033
  and **not** by the suite. The suite already contains the assertion that
  would make the first option cheap: the "the two write paths must answer
  alike" test passes on both servers, and the *bucket-creation* difference
  is observable in `outcomes`.

### R-W2-205 — MINOR — T-005's "902 (schema, payload) pairs, 0 differences" is not reproducible from the tree

* **Where:** `docs/agent-ops/20261022-w6-w7-w9/reports/w2-impl/W2-IMPL-02-T-005.md:360,427,546,573`
  and `tests/validation/test_schema_validator.py:479` (`VALID_SCHEMA_CASES`).
* **What is wrong.** The 902-pair differential harness (41 valid schemas ×
  22 payloads, against the pre-change module) was a **script run outside
  the repository**; the report says so itself at line 573 (*"measured that …
  rather than asserting it"*). The committed pin is the 47-case
  `VALID_SCHEMA_CASES` table, whose expected counts are hand-written — a
  genuine pin, but a different and smaller artefact. A reader auditing the
  claim from the tree finds a 47-case table, not a 902-pair harness.
* **Evidence.** `tests/validation/test_schema_validator.py:479-535` — 47
  `(schema, payload, expected-count)` triples; the report's §7 harness is
  not committed.
* **Suggested fix.** Commit the harness as a test (a differential test
  against a frozen copy of the pre-change module is cheap and would keep
  the claim alive), or restate the claim in the report as a one-off
  measurement with the harness inlined.

### R-W2-206 — MINOR — the models report's "measured flakiness range" is five green runs and a duration range, not a flakiness measurement

* **Where:** `docs/agent-ops/20261022-w6-w7-w9/reports/w2-impl/W2-IMPL-04-T-007.md` §7.4.
* **What is wrong.** §7.4 reports *"154 passed every time (2.39–3.65 s)"* and
  calls it flakiness evidence. Five consecutive green runs is evidence of
  *absence of observed flakes*, not a measurement of a flakiness *rate*; and
  the range quoted is a wall-clock range, which is a different thing
  entirely. For the one genuinely scheduling-sensitive file
  (`tests/models/test_router_concurrency.py`, whose assertions are on a
  **peak** in-flight count reached by *held* providers) five runs is thin.
* **Evidence.** My own measurement: `tests/models` 5/5 green
  (3.26–4.49 s), `tests/models/test_router_concurrency.py` 5/5 green
  (0.13 s each), and the six scoped suites in reverse order 3/3 green.
  **0 flakes observed by either of us** — so the finding is about the
  *evidence's strength*, not about an observed flake.
* **Why it is still worth saying.** The house rule is that a timing-sensitive
  test must be provably deterministic. The concurrency tests *are* — the
  providers are held at a gate the test releases and the assertion is on a
  peak, which extra concurrency can only raise and less concurrency can
  only lower. The report says exactly this, and it is right. What it does
  not do is say how many runs back the "not flaky" claim.
* **Suggested fix.** Say "5 consecutive runs, 0 failures" rather than
  "measured flakiness range", and state the determinism argument (which the
  report already makes) instead of implying a statistical measurement was
  performed.

### R-W2-207 — NIT — `tests/observability/test_metrics.py:119` compares the function with itself

* The assertion `summary["p95"] == pytest.approx(percentile(sorted(values),
  0.95))` is the shape the brief warns about. It is **honestly labelled** —
  the docstring says it *"deliberately says nothing about what a p95 **is**"*
  and points at `TestTheHandComputedValues` for the number — and the number
  *is* pinned by hand elsewhere. This is the correct handling of the trap,
  not an instance of it. Recorded so that a later reader does not "tidy" the
  function-against-itself line away or mistake it for the pin.

### R-W2-208 — NIT — `tests/validation/test_patch.py:291,296,301` — three same-shaped refusal tests where a parametrised table belongs

* `test_a_hunk_before_any_file_header_is_refused`,
  `test_a_file_header_without_a_hunk_is_refused` and
  `test_a_dangling_minus_header_is_refused` share a body shape and differ
  only in the malformed header. A `@pytest.mark.parametrize` table would
  read better and would make the third case cheap to add. Cosmetic only.

### R-W2-209 — NIT — `reports/w2-impl/W2-IMPL-01-T-002.md:394` cites a section that does not exist

* The mutation-10 row says *"not caught — and the reason is §8.1"*. §8 of
  that report has **no** subsections; the explanation is in §13.1. A
  dangling cross-reference in a report that is being asked to justify an ADR
  correction is worth fixing while the section is still being edited.

---

## 2. MUTATIONS YOU RAN

36 mutations in total, applied to a shadow copy of `src/paa` and run against
the suite that claims to cover them. **33 detected, 3 survived.** Two of the
three survivors are the ones the report already discloses (one of them
correctly, one of them incorrectly); the third is R-W2-201.

### 2.1 T-005 — the validation suite (the whole-fix reversal)

| # | mutation | outcome |
|---|---|---|
| V1 | `src/paa/validation/schema_validator.py` reverted to `7ca81c8~1` (the pre-fix module) | **DETECTED** — `81 failed, 352 passed, 3 xfailed in 9.20s` |

**The report's number reproduces exactly: 81.** The failing set is the
13 crashing keywords, the 7 silently-accepted ones, the non-string-key case,
the two "reported and not applied" cases, the two-mode case, and the two
completeness locks (`test_every_supported_keyword_has_a_declared_shape`,
`test_the_malformed_table_covers_every_constrained_keyword`).

### 2.2 T-006 — the agents suite (10 of the report's 13, plus one of my own)

| # | mutation | outcome |
|---|---|---|
| A1 | `would_cycle` — self-delegation branch disabled | DETECTED — 2 failed |
| A2 | `would_cycle` — always `None` (the guard deleted, as the report describes) | **DETECTED** — 7 failed, incl. `TestMediatedCycle::test_a_mediated_three_node_cycle_is_refused` and `::test_the_cycle_guard_is_what_fires_at_max` |
| A3 | anti-goal threshold hard-coded at `0.9` | DETECTED — 2 failed |
| A4 | anti-goal threshold hard-coded at `0.5` (the other side) | **DETECTED** — 1 failed, `test_just_below_the_threshold_passes` |
| A5 | the keyword fallback also runs when vectors are configured | DETECTED — 4 failed |
| A6 | the critic's event carries the `attempt` (the idempotency trap removed) | DETECTED — 7 failed |
| A7 | the gate's history is never consulted | **DETECTED** — 1 failed, `test_an_unanswered_escalation_cannot_be_walked_past` |
| A8 | the human's answer is not honoured | DETECTED — 1 failed, `test_a_human_answer_is_asked_once` |
| A9 | an unparseable model verdict is read as a pass | DETECTED — 1 failed, `test_an_unparseable_model_verdict_is_coerced_to_a_failure` |
| A10 | the model's verdict is ignored (always `PASS` after a deterministic pass) | DETECTED — 2 failed |
| A11 | an embedder failure passes the gate instead of blocking | DETECTED — 1 failed, `test_an_embedder_failure_blocks_rather_than_passes` |
| A12 | **the whole fix reverted** (`8527fe7~1` orchestrator) | **DETECTED** — `7 failed, 121 passed in 5.08s`, all seven in `TestHardStopEscalation` |
| A13 | **the six uncovered irreversible patterns deleted** (mine, not the report's) | **SURVIVED — 128 passed. R-W2-201.** |

The brief's traps are all genuinely closed:

* **the sequence, not a subsequence** — A12 shows the fix's own tests go red;
  A6 (the critique's `attempt`) turns the exact-list assertions red;
  `tests/integration/test_end_to_end.py:48` is now a list equality.
* **the cycle guard, at `MAX`** — A2 is the mutation the brief describes
  ("delete `would_cycle`") and `test_a_mediated_three_node_cycle_is_refused`
  goes red on it. The test asserts `error["cycle"] == ["alpha","beta","gamma","alpha"]`,
  `error["depth"] == 3`, `error["ceiling"] == 4`, and the companion
  `test_at_the_default_modality_the_depth_ceiling_fires_first` pins *why*
  `MAX` is required. The trap is closed.
* **the threshold, read on both sides** — A3 (too high) is caught by
  `test_the_cosine_path_blocks_at_the_threshold` and
  `test_the_threshold_is_read_from_the_settings`; A4 (too low) is caught by
  `test_just_below_the_threshold_passes`. Both sides of 0.82 are pinned, and
  the tests go through the **cosine** branch (an injected embedder, an
  explicit `settings=`), not the keyword fallback — A5 proves the fallback
  is not consulted.

### 2.3 T-007 — the models suite (7 of the report's 7)

| # | mutation | outcome |
|---|---|---|
| B1 | `asyncio.Semaphore(1)` instead of the configured cap | DETECTED — 2 failed, incl. `test_the_cap_bounds_the_calls_in_flight` |
| B2 | one semaphore **per tier** | DETECTED — 1 failed, `test_the_cap_is_one_semaphore_for_the_whole_router` |
| B3 | `base.py` stops redacting `schema_errors` | DETECTED — 1 failed, `test_the_schema_failure_carries_no_key` |
| B4 | the escalation payload stops redacting | DETECTED — 1 failed, `test_the_escalation_payload_scrubs_the_provider_details` |
| B5 | the discriminator is a per-instance counter again | DETECTED — 1 failed, `test_the_escalation_ordinal_survives_a_router_restart` |
| B6 | the healthcheck probes the frontier unconditionally | DETECTED — 1 failed, `test_lockdown_does_not_probe_the_frontier` |
| B7 | the router ignores the permission mode | **DETECTED** — 3 failed, exactly the three the report names |

The semaphore's cap is asserted `==`, not `<=` (`test_router_concurrency.py:156,162,200,206`),
so a semaphore that serialised everything would be caught (B1). The LOCKDOWN
spy asserts `frontier.call_count == 0` **and** `local.call_count == 1` — a
router that refused the call outright would satisfy the first and break the
second.

### 2.4 T-008 — the observability suite (9 of the report's 17)

| # | mutation | outcome |
|---|---|---|
| C1 | `percentile` → nearest rank | **DETECTED** — 11 failed |
| C2 | `rank = n·q` instead of `(n-1)·q` | **DETECTED** — 24 failed |
| C3 | `exit_code` not coalesced | DETECTED — 1 failed, `test_a_finish_that_supplies_no_exit_code_does_not_erase_one` |
| C4 | `_safely` catches `BaseException` too | DETECTED — 3 failed (the whole `TestTheBaseExceptionBoundary`) |
| C5 | the timestamps are not UTC | DETECTED — 1 failed, `test_the_timestamp_is_iso_8601_utc` |
| C6 | `purge_older_than` filters on the age alone | DETECTED — 3 failed |
| C7 | `query_runs` has no total order | DETECTED — 1 failed, `test_the_order_is_total_when_two_runs_started_at_the_same_moment` |
| C8 | `_inject_correlation` overwrites an explicit keyword | DETECTED — 2 failed |
| C9 | `bind_correlation` clears instead of restoring | DETECTED — 1 failed, `test_the_previous_correlation_id_comes_back` |

The hand-computed values are genuinely hand-computed: C1 and C2 both leave
`test_the_p95_of_one_to_five_is_4_8` red, and the *numbers* in
`TestTheHandComputedValues` are written out in the docstrings
(`rank = 4 · 0.95 = 3.8 → 4 + 0.8 · (5 - 4) = 4.8`) rather than read back off
the function. The hypothesis properties **draw** the adversarial value:
`_positions` draws the fraction from a strategy that puts `0.0` and `1.0` in
**explicitly** (`_FRACTION`), so the "the answer is a sample" / "the answer is
between two samples" boundary is crossed on purpose rather than by luck.
`finish_run`'s returned row is asserted **field by field** (18 fields in
`test_the_row_that_comes_back_is_the_row_that_was_written`), so the baseline's
"used as setup and never asserted" defect is closed.

### 2.5 T-002 — the live S3 suite (5 of the report's 13, against two real servers)

| # | mutation | outcome |
|---|---|---|
| S3-1 | `_namespace_is_absent` → always `False` (the bucket probe reverted) | **DETECTED** — 2 failed, `test_a_missing_bucket_is_not_found_with_the_bucket_namespace[RUSTFS]` and `[SEAWEEDFS]` |
| S3-4 | `get` stops verifying the hash | **DETECTED** — 2 failed, `test_content_corrupted_out_of_band_is_corrupt[both]` |
| S3-7 | the dedup short-circuit removed (the second put always writes) | **DETECTED** — 2 failed, `test_a_second_put_of_the_same_bytes_is_a_no_op[both]` |
| S3-10 | `_driver_cause` → `None` (the unwrapping reverted) | **SURVIVED the live suite** (40 passed) — **disclosed** by the report; and it **is** caught by wave 1's unit suite (7 failures across `tests/storage`, incl. `test_blob_errors.py::test_a_wrapper_is_unwrapped` and `::test_a_wrapper_is_unwrapped_through_its_context`) |
| S3-11 | `put_stream` routed through the **wrapping** `upload_file` variant | **SURVIVED — 40 passed. The report claims this one is caught. R-W2-202.** |

The suite does exercise the **real** path: the "spy" is a `__getattr__`
wrapper around a real boto3 client, so every request reaches the server and
the server's answer comes back unchanged — the spy adds the *request*, not
the answer. The two servers are genuinely different programs (SeaweedFS in
Go, RustFS in Rust); the suite does not itself prove that (R-W2-204).
Hermeticity is real: the bucket is `paa-s3-test-{pid}-{uuid8}` — the
operator's `PAA_TEST_S3_BUCKET` is a **base name**, never the bucket — the
run's bucket is dropped in teardown, the "absent" buckets are dropped by the
function-scoped fixture even on failure, and the run's bucket is emptied
between tests so a test cannot pass on what the previous test left behind.
The skip is loud: with no server configured the module skips with the
variable names in the message.

### 2.6 W2-FIX-01 — the import-conventions lock

| # | mutation | outcome |
|---|---|---|
| D1 | `src/paa/observability/runs.py` put back behind `if TYPE_CHECKING:` | **DETECTED** — 5 failed, exactly the five the W2-FIX-01 report names |

### 2.7 The three `xfail(strict=True)` markers

| xfail | reproduction verified? | reason accurate? | would it fail loudly when fixed? |
|---|---|---|---|
| `tests/validation/test_ast_scanner.py:449` — the scanner raises `RecursionError` on a 2000-deep attribute chain | **YES** — `AstSecurityScanner().scan("x = " + "a."*2000 + "b")` raises `RecursionError` today | **YES** — the try/except at `ast_scanner.py:364-392` guards `ast.parse`, and the walk that follows it is outside the guard | **YES** — the fix would return the `unparseable_source` CRITICAL finding, `assert findings` + `all(severity is CRITICAL)` would pass, and `strict=True` turns the XPASS into a failure |
| `tests/validation/test_patch.py:390` — a created file gains a trailing newline | **YES** — measured: `created bytes: b'created\n\n'` (the diff says `created\n`) | **YES** — the planner models an empty prior file as one empty line | **YES** — the fix makes the bytes `b"created\n"`, the assert passes, `strict=True` fails the run |
| `tests/validation/test_engine.py:218` — an artifact with nothing to check is reported as PASS | **YES** — measured: `empty artifact verdict: ValidationVerdict.PASS checks_run: []` | **YES** — the `ValidationArtifact` docstring says an empty artifact cannot quietly PASS | **YES** — the fix makes the verdict `FAIL`, the assert passes, `strict=True` fails the run |

All three are `strict=True`, all three reproduce the defect they name, all
three name the file and the function that must change, and all three would
**fail loudly** rather than silently pass when the defect is fixed. I found
no xfail with a vague reason and no xfail that would still pass after its
fix.

---

## 3. VERIFIED / UNVERIFIED

### VERIFIED

* **The scoped suites are green and order-independent.** 868 passed, 3
  xfailed; 3/3 in reverse directory order.
* **T-005's reversal claim reproduces exactly**: 81 failures against the
  pre-fix `schema_validator.py`.
* **T-006's 13/13 and T-007's 7/7**: every mutation I ran from those
  batteries was caught (10 of 13 and 7 of 7 respectively). I did not run
  T-006's M7/M10/M12/M13 or T-007's remaining rows beyond the seven; those
  are *not* claimed as verified here.
* **T-008's 17/17**: 9 of 17 run, all caught. The other 8 not run.
* **The live S3 suite is real, hermetic, and green against two servers** —
  40 passed — and it does not mock the thing under test.
* **All three `xfail(strict=True)` markers**: real reproductions, accurate
  reasons, and they would fail loudly when the defect is fixed.
* **Every hypothesis test draws its adversarial value from the strategy**:
  `test_metrics.py::_FRACTION` draws `0.0`/`1.0` explicitly;
  `test_runs.py::_NOT_AN_IDENTIFIER` is a `st.one_of` over eight shapes that
  are *definitely* not identifiers. No coin-flip property test found.
* **`filterwarnings`**: the suite runs with the project's own
  `error::DeprecationWarning:paa.*` filter and no `paa` DeprecationWarning
  was raised in any run.
* **No duplication**: zero byte-identical test bodies across the five suites.
* **The `assert_no_key_leak` helper** is used at every site that needs it and
  is handed log *records*, not rendered strings — with one exception, which
  is R-W2-203.
* **The conventions lock has teeth**: reverting one `TYPE_CHECKING` guard
  turns 5 checks red.

### UNVERIFIED

* **T-006's mutations M7, M10, M12, M13** and **T-008's mutations M7-M10,
  M12, M14-M17** — not run by me (time and diminishing returns; the ones I
  did run are the ones whose claims sound strongest).
* **T-002's mutations 2, 3, 3a, 3b, 5, 6, 8, 9, 11a** — not run by me.
  Mutation 3a/3b (one half of the unreachable classification broken at a
  time) is the report's most interesting negative and I did not reproduce
  it.
* **The 902-pair differential harness** — not in the tree (R-W2-205); I could
  not re-run it.
* **The claim that the two S3 servers are "independent implementations"** is
  accepted on the images' provenance, not established by the suite
  (R-W2-204).
* **The PostgreSQL-backed tests** — out of the brief's scope and no
  `PAA_TEST_POSTGRES_DSN` was set; I did not run them and make no claim
  about them.
* **The full repository suite** — the standing orders forbid it and five
  agents were writing in the same tree; I ran only the six scoped
  directories plus `tests/storage`.

---

## 4. COVERAGE LEDGER

Each ROADMAP clause in 0.2, 0.3, 0.4 and 0.5, and the test that pins it.

### 0.2 — Test `agents/`

| ROADMAP clause | Pinned by | Status |
|---|---|---|
| *orchestrator emits the **exact** ledger event sequence end-to-end* | `tests/agents/test_orchestrator.py::TestExactEventSequence::test_happy_path_emits_the_exact_event_sequence` (list equality, 10 events), `::test_failure_path_emits_the_exact_event_sequence`, `::test_block_path_emits_the_exact_event_sequence`, `::test_anti_goal_block_emits_the_exact_event_sequence`, `::test_gate_path_emits_the_exact_event_sequence`, `::test_a_retried_step_records_one_critique_not_three`; and `tests/integration/test_end_to_end.py::TestHappyPath::test_full_event_sequence_is_recorded` (membership → list equality) | **COVERED** — VERIFIED (A6, A12) |
| *delegation refuses a **real** 3-node cycle* | `tests/agents/test_delegation.py::TestMediatedCycle::test_a_mediated_three_node_cycle_is_refused` (at `MAX`; asserts `error["cycle"] == ["alpha","beta","gamma","alpha"]`, `depth == 3`, `ceiling == 4`), `::test_the_cycle_guard_is_what_fires_at_max`, `::test_at_the_default_modality_the_depth_ceiling_fires_first` | **COVERED** — VERIFIED (A2) |
| *policy gates — LOCKDOWN blocks egress* | `tests/agents/test_policy.py::TestLockdown::test_network_command_blocked_even_without_declared_permission` | **COVERED** |
| *policy gates — SAFE blocks deletion* | `tests/agents/test_policy.py::TestSafeMode::test_irreversible_ops_are_blocked` — **3 of 9 patterns** | **PARTIAL** — R-W2-201; the six uncovered patterns can be deleted with the suite green |
| *policy gates — anti-goal at 0.82* | `tests/agents/test_policy.py::TestAntiGoalCosine::test_the_cosine_path_blocks_at_the_threshold` (0.82 blocks, asserts `"similarity 0.820 >= 0.82"`), `::test_just_below_the_threshold_passes` (0.8199), `::test_the_threshold_is_read_from_the_settings`, `::test_an_embedder_failure_blocks_rather_than_passes`, `::test_the_cosine_path_replaces_the_keyword_path` | **COVERED** — VERIFIED (A3, A4, A5, A11) |
| *critic's deterministic FAIL cannot be overridden by an LLM PASS* | `tests/agents/test_reasoning.py::TestCriticOverride::test_deterministic_fail_beats_model_pass`, `::test_model_is_not_even_consulted_on_deterministic_fail`; mirror half `TestCriticDowngrade::test_the_model_can_downgrade_a_deterministic_pass`, `::test_an_unparseable_model_verdict_is_coerced_to_a_failure`, `::test_the_model_is_consulted_only_after_the_deterministic_pass` | **COVERED** — VERIFIED (A9, A10) |
| *(plan §4 extra) the six untested irreversible patterns* | — | **STILL UNCOVERED** — R-W2-201 |
| *(the task's own fix) `HARD_STOP_ESCALATE_TO_USER` parks the task* | `tests/agents/test_orchestrator.py::TestHardStopEscalation` (7 tests, incl. `::test_the_escalation_parks_the_task` and `::test_an_unanswered_escalation_cannot_be_walked_past`); ADR-0034 | **COVERED** — VERIFIED (A7, A8, A12) |

### 0.3 — Test `models/`

| ROADMAP clause | Pinned by | Status |
|---|---|---|
| *LOCKDOWN never escalates (spy provider, assert zero frontier calls)* | `tests/models/test_router_lockdown.py::test_lockdown_never_calls_the_frontier` (parametrised over every modality; `frontier.call_count == 0` **and** `local.call_count == 1`), `::test_force_escalate_does_not_bypass_lockdown`, `::test_a_failing_local_under_lockdown_does_not_reach_the_frontier`, `::test_lockdown_does_not_probe_the_frontier` | **COVERED** — VERIFIED (B6, B7) |
| *escalation writes exactly one `MODEL_ESCALATED` event* | `tests/models/test_escalation_audit.py::test_an_escalation_writes_one_model_escalated_event`, `::test_two_escalations_in_one_lineage_are_two_events`, `::test_the_escalation_ordinal_survives_a_router_restart` | **COVERED** — VERIFIED (B5) |
| *semaphore caps concurrency* | `tests/models/test_router_concurrency.py::test_the_cap_bounds_the_calls_in_flight` (`max_in_flight == cap`, not `<=`), `::test_the_cap_is_one_semaphore_for_the_whole_router`, `::test_the_usage_counters_account_for_every_concurrent_call` | **COVERED** — VERIFIED (B1, B2) |
| *no API key ever appears in logs or exceptions* | `tests/models/test_key_secrecy.py::test_the_schema_failure_carries_no_key`, `::test_the_escalation_payload_carries_no_key`, `::test_no_log_line_carries_no_key`, `::test_the_escalation_payload_scrubs_the_provider_details`, `::test_the_local_only_path_still_carries_no_key`; helper `tests/models/conftest.py::assert_no_key_leak` | **PARTIAL** — the log line, the ledger payload, the raised error's message and details are covered (VERIFIED, B3, B4); the raised error's **`__cause__`** and the traceback are **not** — R-W2-203 |
| *(plan §4 extra) the `escalation_seq` per-instance-counter defect* | `tests/models/test_escalation_audit.py::test_the_escalation_ordinal_survives_a_router_restart` | **COVERED** — VERIFIED (B5) |

### 0.4 — Test `observability/`

| ROADMAP clause | Pinned by | Status |
|---|---|---|
| *metric percentiles against **hand-computed** values* | `tests/observability/test_metrics.py::TestTheHandComputedValues::test_the_p95_of_one_to_five_is_4_8`, `::test_the_p99_of_one_to_a_hundred_is_99_01`, `::test_the_p95_of_one_to_a_hundred_is_95_05`, `::test_the_p50_of_one_to_a_hundred_is_50_5`, `::test_the_result_is_not_one_of_the_samples`, `::test_the_result_lies_strictly_between_the_samples_it_interpolates`; the rule as a property: `::TestTheAlgorithm::test_the_value_sits_where_the_rank_formula_says`, `::test_each_sample_sits_at_its_own_quantile`, `::test_the_value_never_leaves_the_range_of_the_samples`, `::test_the_value_does_not_fall_as_the_quantile_rises`, `::test_the_ends_are_the_ends_of_the_sample` | **COVERED** — VERIFIED (C1, C2) |
| *run repository round-trips* | `tests/observability/test_runs.py::TestFinishRun::test_the_row_that_comes_back_is_the_row_that_was_written` (18 fields), `::test_the_returned_row_is_the_row_a_later_reader_sees`, `::test_a_partial_finish_does_not_destroy_what_is_already_written`, `::test_a_finish_that_supplies_no_exit_code_does_not_erase_one`; `::TestQueryRuns` (9 filter/order tests), `::TestPurgeOlderThan` (4), `::TestTheRowConversion` (4), `::TestSkillStats` (3) | **COVERED** — VERIFIED (C3, C6, C7) |
| *a failing exporter never breaks the traced operation* | `tests/observability/test_tracing.py::TestTheGoverningRule::test_an_exporter_that_raises_does_not_break_the_body`, `::test_a_metrics_registry_that_raises_does_not_break_the_body`, `::test_a_disabled_tracer_still_finishes_the_span`; the boundary: `::TestTheBaseExceptionBoundary` (4 tests) | **COVERED** — VERIFIED (C4) |
| *(plan §4 extra) `logging.py`'s five claims* | `tests/observability/test_logging.py::TestTheCorrelationIdIsAField`, `::TestTheCallSiteWins`, `::TestTheBindingIsRestored`, `::TestTheRendering::test_the_timestamp_is_iso_8601_utc`, `::test_json_rendering_is_opt_in` | **COVERED** — VERIFIED (C5, C8, C9) |

### 0.5 — Wire CI (GitHub Actions)

| ROADMAP clause | Pinned by | Status |
|---|---|---|
| *a workflow that runs the full suite + `ruff` + `mypy --strict` on every push, with a matrix over the optional extras, a PostgreSQL service container and `PAA_TEST_POSTGRES_DSN`, and the `python-is-python3` landmine* | — | **STILL UNCOVERED.** There is no `.github/` directory, no `Makefile`, no `tox.ini` and no `.pre-commit-config.yaml` in the tree. This is task **T-009**, which the plan schedules in **wave 3** (`docs/plans/w6-w7-w9.md` §5: *"T-009 | 0.5 | ci | SURFACE | `.github/workflows/*.yml` | 3"*), and no wave-2 task touches it. The honest answer for 0.5 in this wave is *still uncovered*, and nothing in the five suites under review pretends otherwise. |

---

## 5. Missing edge cases, prioritised

* **P1 (must add).** The six uncovered irreversible patterns
  (`tests/agents/test_policy.py::TestSafeMode`) — R-W2-201. Sketch:
  `@pytest.mark.parametrize(("command", "label"), [(c, l) for c, l in _IRREVERSIBLE_PATTERNS])`,
  read from the module's own source so a tenth pattern cannot slip past; the
  expected verdict is `STATUS_BLOCKED` under `SAFE`.
* **P2 (should add).** The exception-chain assertion in
  `tests/models/test_key_secrecy.py::test_the_local_only_path_still_carries_no_key`
  — R-W2-203. The chain is either scrubbed (a code change in
  `router.py::_local_only`) or the file's claim is narrowed; the test must
  say which.
* **P2 (should add).** A server-identity assertion in
  `tests/storage/test_s3_live.py` — R-W2-204. The cheapest form is to
  assert that the two servers answer *differently* where ADR-0033 says they
  do (SeaweedFS creates a bucket a write names, RustFS refuses it), so two
  containers of the same image cannot both pass.
* **P3 (nice to have).** The differential harness for T-005, committed —
  R-W2-205.
* **P3 (nice to have).** A `parametrize` table for the three malformed-header
  refusals in `tests/validation/test_patch.py` — R-W2-208.
* **P3 (nice to have).** The S3 report's mutation-11 row and §8.1
  cross-reference corrected — R-W2-202, R-W2-209.

---

## 6. What I could not verify

Listed in §3 under UNVERIFIED. In short: 8 of T-008's 17 mutations, 4 of
T-006's 13, 9 of T-002's 13; the 902-pair harness (not in the tree); the
independence of the two S3 servers as an assertion (it is a convention); the
PostgreSQL-backed tests (no DSN, out of scope); and the full repository
suite (forbidden by the standing orders, and five agents were writing
concurrently).

---

## 7. Commands run, verbatim

```
$ .venv/bin/python -m pytest tests/validation tests/observability tests/models \
      tests/agents tests/integration tests/conventions -p no:randomly
868 passed, 3 xfailed in 33.35s

$ .venv/bin/python -m pytest tests/conventions tests/integration tests/agents \
      tests/models tests/observability tests/validation -p no:randomly     # ×3
868 passed, 3 xfailed in 39.42s / 40.99s / 49.86s

$ .venv/bin/python -m pytest tests/models -p no:randomly                     # ×5
154 passed in 4.20s / 3.42s / 4.17s / 4.49s / 3.26s

$ .venv/bin/python -m pytest tests/models/test_router_concurrency.py -p no:randomly  # ×5
3 passed in 0.13s  (×5)

$ PAA_TEST_S3_{SEAWEEDFS,RUSTFS}_* pytest tests/storage/test_s3_live.py -p no:randomly
40 passed in 16.50s

$ pytest tests/storage/test_s3_live.py -rs -p no:randomly                    # no server configured
1 skipped in 0.21s

$ PYTHONPATH=/tmp/opencode/w2rev02/shadow/src pytest tests/validation -p no:randomly
  # with 7ca81c8~1's schema_validator.py in the shadow:
81 failed, 352 passed, 3 xfailed in 9.20s

$ PYTHONPATH=/tmp/opencode/w2rev02/shadow/src pytest tests/agents tests/integration -p no:randomly
  # with 8527fe7~1's orchestrator.py in the shadow:
7 failed, 121 passed in 5.08s

$ python /tmp/opencode/w2rev02/mutate.py <name> <relpath> <old> <new> -- <pytest args>
  # 36 mutations; see §2 for the table and the surviving three.
```

The mutation harness lives outside the repository, in
`/tmp/opencode/w2rev02/`. The shadow tree is at
`/tmp/opencode/w2rev02/shadow/src/paa` and is byte-identical to
`src/paa` (`diff -rq` reports nothing).

No container of the operator's was started, stopped, rebuilt or pruned; the
two S3 containers I started (`w2rev02-s3-seaweed`, `w2rev02-s3-rustfs`) were
removed. No database was touched. No process was left running. The only
files I wrote in the repository are this report.
