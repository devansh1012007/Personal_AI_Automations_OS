# ADR-0022: The coder agent's memory writes go through the cold lake

## Status

Accepted. Correctness class — this is not a departure from the RFC but a
return to it. The RFC's own design already routes ingestion through the cold
lake (RFC §1.2 for the lake, RFC §2.1 agent 8 for the creator that drains
it); the coder agent's `remember_fact` tool was the one writer that skipped
that path, and the write it produced could not be replayed. The decision
recorded here is how the tool goes through the lake, and what that costs.

## Context

`remember_fact` (`src/paa/coder/runtime_tools.py`) writes an assertion into
long-term memory. It used to do that by handing a `SignalEnvelope` with
`persisted=False` straight to `MemoryCreator.process_signal`, which wrote
`hot_serving_entity_index` and `hot_serving_active_facts` rows directly.

Three things were wrong with that, and only the first is obvious.

The fact was not derivable from anything. `cold_lake_signals` stayed empty, no
ledger event was written, and the fact's `source_signal_id` was `None` while
its `provenance` blob named an ephemeral envelope id that resolved to no row
in any table. AGENTS.md's second rule — everything else must be replayable —
has no answer for that state: there is no event to replay and no snapshot and
recovery rule to rebuild it from. The agent's own remembered facts were the
one kind of memory in the system that could not be rebuilt. Every other
memory in the runtime is derived: the daemon's path is `cold_lake_signals` →
`MemoryCreator` → hot serving, and the creator's rule-based extraction is
deterministic precisely so that replaying a signal reproduces the same facts
(`paa/memory/creator.py`, module docstring).

The docstring explained this backwards. It attributed the rationale to the
"creator module docstring: the cold-lake row is the event-before-side-effect
analogue" — a phrase that appears nowhere in `creator.py`; the creator's
docstring says the ledger is the source of truth, which is a different
assertion. It also claimed that "the chain of evidence survives; only the
foreign key does not", which is false in the way that matters: a surviving id
that resolves to nothing is a dangling pointer, not a walkable chain.

And the rationale itself was wrong. It said that routing through
`SignalRepository.record` would make the daemon's batch re-process the signal
and write a duplicate fact, and that `SignalRepository` "additionally demands a
`ContentAddressedStore` this tool has no business constructing". The first
half is true about `record` — it files rows as `unprocessed`, which is exactly
what the daemon's `run_batch` claims — but it is a race to be *won*, not a
reason to bypass the lake. The second half is not true: `ContentAddressedStore`
is two `mkdir`s and a directory, the ingestion API already builds one per
request over the same path (`paa/api/app.py::_signal_repo`), and the store is
only *used* for a payload over 64 KB, which an assertion never is.

The relevant mechanics, read out of the code:

- `SignalRepository.record` is idempotent on `(channel, external_id)`, backed
  by the `uq_cold_signals_external` unique index. A retry returns the
  incumbent row.
- `MemoryCreator._claim` selects the rows whose `sync_status` is
  `unprocessed` and flips them to `processing` inside one transaction;
  `MemoryCreator._commit` writes the facts and sets the row to `processed` in
  the same transaction; `_set_status` and `_quarantine_malformed` close the
  row out on the other outcomes.
- `daemon.py::_ingestion_loop` runs `run_batch` on a one-second interval, so
  a row left `unprocessed` will be claimed by the daemon.

## Decision

`remember_fact` files the assertion in the cold lake first and derives the
facts from that row.

- **The row.** The assertion is filed with
  `SignalRepository.record("coder", dumps(text), external_id=...)`, over a
  `SignalRepository` built on the runtime's database and the same
  `ContentAddressedStore` the ingestion API uses
  (`settings.storage.cold_lake_path`). The payload is stored as the JSON
  encoding of the text, so replaying the row re-derives the same facts: the
  creator's extractor sees the same string it saw the first time.
- **The external id** is `coder:<sha256 of the text>`. The assertion itself is
  what is made idempotent, so the key is derived from it: a retried call
  re-uses the signal it filed the first time instead of filing a second one.
  A per-turn id would have made the key depend on the caller remembering to
  supply one, and the model is not a reliable source of ids.
- **The claim.** The tool takes the same claim the daemon takes, scoped to
  one row: a conditional `UPDATE ... SET sync_status = 'processing' WHERE id =
  ? AND sync_status = 'unprocessed'`. Whoever wins writes the facts; the loser
  writes nothing. `SignalRepository.claim_unprocessed` cannot serve here — it
  claims by arrival order and would take other channels' rows with it.
- **The processing.** The envelope is the cold-lake row (`id` = the row id,
  `persisted=True`), so the fact carries a real `source_signal_id` foreign key
  and the creator closes the row out itself. `raw` is the assertion as it was
  spoken, not the JSON the row stores, so the creator's containment check sees
  the original bytes.
- **The loser waits.** If the daemon claimed the row first, the tool waits up
  to two seconds for it to settle and then reports what the row says —
  `processed`, `quarantined` or `malformed`. A row that is still in flight when
  the wait runs out is reported as `pending`, with the row's own status in the
  report, rather than as a result the tool does not have.
- **A failed write gives the row back.** If processing raises, the row is
  released back to `unprocessed` so the daemon can still process it, and the
  error propagates.
- **No ledger event.** Memory is not a task lineage, so there is no lineage to
  record it in. The cold-lake row is the durable record of the assertion, in
  the same way the ledger is the durable record of a task, and it is written
  before the facts.

## Consequences

- The fact's evidence is walkable. `source_signal_id` resolves to a
  `cold_lake_signals` row holding the verbatim assertion; that row is where
  the evidence lives, not the fact's `provenance` blob, which only names the
  extractor and the rule that produced it.
- The agent's memories are derivable like everyone else's: wipe hot serving,
  put the cold-lake rows back to `unprocessed` and replay them through the
  creator, and the same facts come back, because extraction is deterministic.
  The runtime does not automate that rebuild today — it is the same manual
  procedure every other memory in the system needs — but this change removes
  the one kind of memory that could not be rebuilt at all.
- The daemon cannot duplicate the fact. The row is claimed exactly once, and
  the creator's commit closes it out; a daemon-style `run_batch` afterwards
  claims nothing. This is asserted, not argued, in
  `tests/coder/test_runtime_tools.py::TestRememberFactColdLake::test_the_daemon_batch_cannot_reprocess_a_remembered_signal`.
- The tool writes one extra row per assertion — the cold-lake signal — on top
  of the entity and fact rows it wrote before — and it takes a claim it can
  lose. The window between filing the row and claiming it is real: a daemon
  running against the same database can claim the row in it. The tool handles
  that by not writing facts and reporting the settled state, so the race costs
  latency and never a duplicate.
- A crash between the claim and the creator's commit leaves the row in
  `processing`, where no poller will pick it up. That is the same exposure the
  daemon's own claim has — `SignalRepository`'s docstring is explicit that
  recovering stale claims is the queue layer's job, and that the repository
  deliberately does not invent a heuristic for it. The tool releases the row
  when processing raises; only a hard kill leaves it stuck.
- A repeated assertion is now a no-op rather than a second identical fact.
  The creator does not deduplicate facts, so before this change two calls with
  the same text wrote two rows and recall returned both.
- The tool's report changed shape: `provenance` is now an object naming the
  cold-lake row rather than the string "signal not persisted", and
  `source_signal_id` is a real id. `idempotent_replay` distinguishes "I wrote
  it" from "it was already there".
- Covered by `tests/coder/test_runtime_tools.py::TestRememberFactColdLake`: the
  row exists, the foreign key resolves, the fact is re-derivable from the row
  alone, a repeated assertion writes one row and one fact, the daemon's batch
  cannot re-process the signal, and a signal another worker claimed is not
  written twice.
