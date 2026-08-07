-- =====================================================================
-- PAA v4.1 relational schema — SQLite dialect
--
-- SPEC DEVIATION (docs/adr/0001): the RFC targets PostgreSQL. SQLite in WAL
-- mode is used instead because the target hardware has no Docker and ~3.5 GB
-- of free RAM. At single-user scale SQLite provides the same ACID guarantees
-- the ledger depends on, with ~0 MB of resident overhead.
--
-- PostgreSQL has no `schema` concept in SQLite, so RFC schema qualifiers
-- become table-name prefixes:
--     system_state.ledger  ->  system_state_ledger
--     cold_lake.signals    ->  cold_lake_signals
--     hot_serving.*        ->  hot_serving_*
--
-- The PostgreSQL equivalent lives in schema_postgres.sql. Both are kept in
-- lockstep by tests/storage/test_schema_parity.py, which fails the build if a
-- table or column exists in one dialect and not the other.
--
-- STRICT tables are used throughout: SQLite's default type affinity would
-- silently coerce a bad insert, and a ledger must never accept a malformed row.
-- =====================================================================

PRAGMA foreign_keys = ON;

-- =====================================================================
-- 1. SYSTEM STATE LEDGER — the append-only source of truth
-- =====================================================================

CREATE TABLE IF NOT EXISTS system_state_ledger (
    sequence_id        INTEGER PRIMARY KEY AUTOINCREMENT,
    event_id           TEXT    NOT NULL UNIQUE,
    correlation_id     TEXT    NOT NULL,
    session_id         TEXT,
    causation_id       TEXT,

    -- Per-correlation monotonic counter starting at 1. Replay orders by this
    -- rather than sequence_id so a lineage remains reconstructable even if the
    -- global sequence has gaps from rolled-back transactions.
    state_version      INTEGER NOT NULL,

    -- SPEC DEVIATION (docs/adr/0008): includes an attempt counter and
    -- discriminator so genuine retries are representable. See
    -- paa.ledger.events.compute_idempotency_key.
    idempotency_key    TEXT    NOT NULL,
    attempt            INTEGER NOT NULL DEFAULT 0,
    discriminator      TEXT,

    event_type         TEXT    NOT NULL,
    execution_mode     TEXT    NOT NULL DEFAULT 'STANDARD',
    agent_role         TEXT,
    allocated_worker_image TEXT NOT NULL DEFAULT 'paa/base_worker:v4.1',

    payload            TEXT    NOT NULL DEFAULT '{}',

    -- Tamper-evident chain. prev_hash of the first event in a correlation is
    -- 64 zeroes (GENESIS_HASH).
    prev_hash          TEXT    NOT NULL,
    event_hash         TEXT    NOT NULL,

    recorded_at        TEXT    NOT NULL,

    CONSTRAINT uq_ledger_idempotency UNIQUE (idempotency_key),
    CONSTRAINT uq_ledger_version     UNIQUE (correlation_id, state_version),
    CONSTRAINT ck_ledger_payload_json CHECK (json_valid(payload)),
    CONSTRAINT ck_ledger_version_positive CHECK (state_version >= 1),
    CONSTRAINT ck_ledger_hash_len CHECK (length(event_hash) = 64 AND length(prev_hash) = 64)
) STRICT;

CREATE INDEX IF NOT EXISTS idx_ledger_correlation_seq
    ON system_state_ledger(correlation_id, state_version ASC);
CREATE INDEX IF NOT EXISTS idx_ledger_recorded_at
    ON system_state_ledger(recorded_at DESC);
CREATE INDEX IF NOT EXISTS idx_ledger_event_type
    ON system_state_ledger(event_type, recorded_at DESC);
CREATE INDEX IF NOT EXISTS idx_ledger_session
    ON system_state_ledger(session_id, sequence_id ASC);

-- ---------------------------------------------------------------------
-- Open-correlation projection.
--
-- SPEC DEVIATION (docs/adr/0010): the RFC recovers via a partial index
--     ... WHERE event_type NOT IN ('EXECUTION_COMPLETED','EXECUTION_FAILED')
-- on the ledger. Because the ledger is append-only, rows are never updated out
-- of that predicate: every non-terminal event a completed task ever emitted
-- stays in the index forever. The index grows without bound and the recovery
-- sweep degrades linearly with total system history.
--
-- Instead we maintain a small mutable projection holding exactly one row per
-- correlation, updated in the same transaction as the append. Recovery scans
-- only rows where is_terminal = 0, which is bounded by in-flight work.
-- ---------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS system_state_correlation_head (
    correlation_id     TEXT    PRIMARY KEY,
    session_id         TEXT,
    latest_sequence_id INTEGER NOT NULL,
    latest_state_version INTEGER NOT NULL,
    latest_event_type  TEXT    NOT NULL,
    latest_event_hash  TEXT    NOT NULL,
    execution_mode     TEXT    NOT NULL,
    is_terminal        INTEGER NOT NULL DEFAULT 0,
    opened_at          TEXT    NOT NULL,
    updated_at         TEXT    NOT NULL,
    CONSTRAINT ck_head_terminal_bool CHECK (is_terminal IN (0, 1))
) STRICT;

CREATE INDEX IF NOT EXISTS idx_head_open
    ON system_state_correlation_head(updated_at ASC) WHERE is_terminal = 0;

-- Snapshot checkpoints so replay of a long lineage need not start at v1.
CREATE TABLE IF NOT EXISTS system_state_snapshots (
    correlation_id     TEXT    NOT NULL,
    state_version      INTEGER NOT NULL,
    projection         TEXT    NOT NULL,
    created_at         TEXT    NOT NULL,
    PRIMARY KEY (correlation_id, state_version),
    CONSTRAINT ck_snapshot_json CHECK (json_valid(projection))
) STRICT;

-- =====================================================================
-- 2. COLD LAKE — immutable raw history
-- =====================================================================

CREATE TABLE IF NOT EXISTS cold_lake_signals (
    id             TEXT PRIMARY KEY,
    received_at    TEXT NOT NULL,
    channel        TEXT NOT NULL,
    external_id    TEXT,
    raw_payload    TEXT NOT NULL,
    content_hash   TEXT NOT NULL,
    -- Pointer into the content-addressed blob store for oversized payloads
    -- (ADR-0004 replaces MinIO with a local CAS directory).
    blob_uri       TEXT,
    sync_status    TEXT NOT NULL DEFAULT 'unprocessed',
    processed_at   TEXT,
    error_detail   TEXT,
    CONSTRAINT ck_signal_status CHECK (
        sync_status IN ('unprocessed', 'processing', 'processed', 'malformed', 'quarantined')
    ),
    CONSTRAINT ck_signal_json CHECK (json_valid(raw_payload))
) STRICT;

CREATE INDEX IF NOT EXISTS idx_cold_signals_routing
    ON cold_lake_signals(channel, received_at DESC);
CREATE INDEX IF NOT EXISTS idx_cold_signals_unprocessed
    ON cold_lake_signals(received_at ASC) WHERE sync_status = 'unprocessed';
CREATE UNIQUE INDEX IF NOT EXISTS uq_cold_signals_external
    ON cold_lake_signals(channel, external_id) WHERE external_id IS NOT NULL;

CREATE TABLE IF NOT EXISTS cold_lake_artifacts_archive (
    id                 TEXT PRIMARY KEY,
    signal_id          TEXT REFERENCES cold_lake_signals(id) ON DELETE SET NULL,
    correlation_id     TEXT,
    virtual_uri        TEXT NOT NULL UNIQUE,
    absolute_host_path TEXT NOT NULL,
    sha256_checksum    TEXT NOT NULL,
    size_bytes         INTEGER NOT NULL DEFAULT 0,
    compression        TEXT NOT NULL DEFAULT 'zstd',
    blob_uri           TEXT,
    payload_content    TEXT,
    archived_at        TEXT NOT NULL,
    CONSTRAINT ck_artifact_checksum_len CHECK (length(sha256_checksum) = 64)
) STRICT;

CREATE INDEX IF NOT EXISTS idx_artifacts_correlation
    ON cold_lake_artifacts_archive(correlation_id, archived_at DESC);
CREATE INDEX IF NOT EXISTS idx_artifacts_checksum
    ON cold_lake_artifacts_archive(sha256_checksum);

-- =====================================================================
-- 3. HOT SERVING — the cleaned operational layer
-- =====================================================================

CREATE TABLE IF NOT EXISTS hot_serving_entity_index (
    id                TEXT PRIMARY KEY,
    class             TEXT NOT NULL,
    canonical_name    TEXT NOT NULL UNIQUE,
    aliases           TEXT NOT NULL DEFAULT '[]',
    importance_weight REAL NOT NULL DEFAULT 0.5,
    confidence_rating REAL NOT NULL DEFAULT 1.0,
    attributes        TEXT NOT NULL DEFAULT '{}',
    created_at        TEXT NOT NULL,
    updated_at        TEXT NOT NULL,
    CONSTRAINT ck_entity_importance CHECK (importance_weight BETWEEN 0.0 AND 1.0),
    CONSTRAINT ck_entity_confidence CHECK (confidence_rating BETWEEN 0.0 AND 1.0),
    CONSTRAINT ck_entity_aliases_json CHECK (json_valid(aliases)),
    CONSTRAINT ck_entity_attrs_json CHECK (json_valid(attributes))
) STRICT;

CREATE INDEX IF NOT EXISTS idx_hot_entity_lookup ON hot_serving_entity_index(class);
CREATE INDEX IF NOT EXISTS idx_hot_entity_updated ON hot_serving_entity_index(updated_at DESC);

-- Trigram search is a PostgreSQL pg_trgm feature. The SQLite equivalent is an
-- FTS5 virtual table with a trigram tokenizer, kept in sync by triggers.
CREATE VIRTUAL TABLE IF NOT EXISTS hot_serving_entity_fts
    USING fts5(canonical_name, aliases, content='', tokenize="trigram");

CREATE TRIGGER IF NOT EXISTS trg_entity_fts_insert
AFTER INSERT ON hot_serving_entity_index BEGIN
    INSERT INTO hot_serving_entity_fts(rowid, canonical_name, aliases)
    VALUES (new.rowid, new.canonical_name, new.aliases);
END;

CREATE TRIGGER IF NOT EXISTS trg_entity_fts_delete
AFTER DELETE ON hot_serving_entity_index BEGIN
    INSERT INTO hot_serving_entity_fts(hot_serving_entity_fts, rowid, canonical_name, aliases)
    VALUES ('delete', old.rowid, old.canonical_name, old.aliases);
END;

CREATE TRIGGER IF NOT EXISTS trg_entity_fts_update
AFTER UPDATE ON hot_serving_entity_index BEGIN
    INSERT INTO hot_serving_entity_fts(hot_serving_entity_fts, rowid, canonical_name, aliases)
    VALUES ('delete', old.rowid, old.canonical_name, old.aliases);
    INSERT INTO hot_serving_entity_fts(rowid, canonical_name, aliases)
    VALUES (new.rowid, new.canonical_name, new.aliases);
END;

-- ---------------------------------------------------------------------
-- Facts. The vector store holds embeddings; this table holds the truth and
-- the decay bookkeeping. Qdrant point ids reference fact ids 1:1.
-- ---------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS hot_serving_active_facts (
    id                 TEXT PRIMARY KEY,
    entity_id          TEXT NOT NULL REFERENCES hot_serving_entity_index(id) ON DELETE CASCADE,
    predicate          TEXT NOT NULL,
    object_value       TEXT NOT NULL,
    memory_domain      TEXT NOT NULL DEFAULT 'semantic',
    memory_scope       TEXT NOT NULL DEFAULT 'global',

    -- C0 in C(t) = C0 * exp(-lambda * t). Immutable after distillation;
    -- effective confidence is always derived, never stored, so that a decay
    -- sweep crash cannot corrupt the underlying score.
    initial_confidence REAL NOT NULL DEFAULT 1.0,
    importance         REAL NOT NULL DEFAULT 0.5,
    use_count          INTEGER NOT NULL DEFAULT 0,

    source_signal_id   TEXT REFERENCES cold_lake_signals(id) ON DELETE SET NULL,
    provenance         TEXT NOT NULL DEFAULT '{}',
    embedding_status   TEXT NOT NULL DEFAULT 'pending',

    created_at         TEXT NOT NULL,
    last_queried_at    TEXT NOT NULL,
    superseded_by      TEXT REFERENCES hot_serving_active_facts(id) ON DELETE SET NULL,

    CONSTRAINT ck_fact_conf CHECK (initial_confidence BETWEEN 0.0 AND 1.0),
    CONSTRAINT ck_fact_imp CHECK (importance BETWEEN 0.0 AND 1.0),
    CONSTRAINT ck_fact_embed CHECK (embedding_status IN ('pending', 'indexed', 'failed', 'skipped')),
    CONSTRAINT ck_fact_prov_json CHECK (json_valid(provenance))
) STRICT;

CREATE INDEX IF NOT EXISTS idx_facts_entity_predicate
    ON hot_serving_active_facts(entity_id, predicate);
CREATE INDEX IF NOT EXISTS idx_facts_decay_sweep
    ON hot_serving_active_facts(memory_domain, last_queried_at ASC)
    WHERE superseded_by IS NULL;
CREATE INDEX IF NOT EXISTS idx_facts_pending_embedding
    ON hot_serving_active_facts(created_at ASC) WHERE embedding_status = 'pending';

-- Quarantine for mutually contradictory facts. RFC §4.2 forbids automatic
-- resolution; rows land here and wait for a human tie-break.
CREATE TABLE IF NOT EXISTS hot_serving_unresolved_buffer (
    id                TEXT PRIMARY KEY,
    entity_id         TEXT NOT NULL,
    predicate         TEXT NOT NULL,
    incumbent_fact    TEXT NOT NULL,
    challenger_fact   TEXT NOT NULL,
    conflict_score    REAL NOT NULL,
    correlation_id    TEXT,
    detected_at       TEXT NOT NULL,
    resolved_at       TEXT,
    resolution        TEXT,
    CONSTRAINT ck_unresolved_incumbent_json CHECK (json_valid(incumbent_fact)),
    CONSTRAINT ck_unresolved_challenger_json CHECK (json_valid(challenger_fact)),
    CONSTRAINT ck_unresolved_resolution CHECK (
        resolution IS NULL OR resolution IN ('incumbent', 'challenger', 'both', 'neither')
    )
) STRICT;

CREATE INDEX IF NOT EXISTS idx_unresolved_open
    ON hot_serving_unresolved_buffer(detected_at ASC) WHERE resolved_at IS NULL;

-- Graph edges. Mirrored into KuzuDB for multi-hop traversal; this table is the
-- durable record so the graph can always be rebuilt from relational truth.
CREATE TABLE IF NOT EXISTS hot_serving_relationships (
    id                 TEXT PRIMARY KEY,
    from_entity_id     TEXT NOT NULL REFERENCES hot_serving_entity_index(id) ON DELETE CASCADE,
    to_entity_id       TEXT NOT NULL REFERENCES hot_serving_entity_index(id) ON DELETE CASCADE,
    rel_type           TEXT NOT NULL,
    weight             REAL NOT NULL DEFAULT 0.5,
    confidence_decay   REAL NOT NULL DEFAULT 0.004,
    evidence_count     INTEGER NOT NULL DEFAULT 1,
    contradiction_score REAL NOT NULL DEFAULT 0.0,
    source_memory_id   TEXT,
    origin_signal_id   TEXT,
    created_by_agent   TEXT NOT NULL DEFAULT 'memory_creator',
    valid_from         TEXT NOT NULL,
    valid_to           TEXT,
    CONSTRAINT uq_relationship UNIQUE (from_entity_id, to_entity_id, rel_type, valid_from),
    CONSTRAINT ck_rel_weight CHECK (weight BETWEEN 0.0 AND 1.0),
    CONSTRAINT ck_rel_type CHECK (rel_type IN (
        'DEPENDS_ON','PART_OF','DERIVED_FROM','INVOLVES','BLOCKS','CAUSES',
        'SUPPORTS','CONTRADICTS','REFERS_TO','TRIGGERED_BY','SIMILAR_TO','MUTATES'
    ))
) STRICT;

CREATE INDEX IF NOT EXISTS idx_rel_from ON hot_serving_relationships(from_entity_id, rel_type);
CREATE INDEX IF NOT EXISTS idx_rel_to   ON hot_serving_relationships(to_entity_id, rel_type);
CREATE INDEX IF NOT EXISTS idx_rel_live ON hot_serving_relationships(weight DESC) WHERE valid_to IS NULL;

-- ---------------------------------------------------------------------
-- Task workspaces, sessions, skills
-- ---------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS hot_serving_sessions (
    session_id     TEXT PRIMARY KEY,
    session_name   TEXT NOT NULL,
    status         TEXT NOT NULL DEFAULT 'QUEUED',
    permission_mode TEXT NOT NULL DEFAULT 'ASK',
    workspace_path TEXT,
    metadata       TEXT NOT NULL DEFAULT '{}',
    created_at     TEXT NOT NULL,
    updated_at     TEXT NOT NULL,
    closed_at      TEXT,
    CONSTRAINT ck_session_status CHECK (
        status IN ('QUEUED','IN_PROGRESS','PAUSED','COMPLETED','FAILED','ABANDONED')
    ),
    CONSTRAINT ck_session_meta_json CHECK (json_valid(metadata))
) STRICT;

CREATE INDEX IF NOT EXISTS idx_sessions_active
    ON hot_serving_sessions(updated_at DESC) WHERE closed_at IS NULL;

CREATE TABLE IF NOT EXISTS hot_serving_task_workspaces_vault (
    id                TEXT PRIMARY KEY,
    task_id           TEXT NOT NULL REFERENCES hot_serving_entity_index(id) ON DELETE CASCADE,
    filename          TEXT NOT NULL,
    file_content      TEXT NOT NULL DEFAULT '',
    checksum          TEXT NOT NULL,
    last_mutation_by  TEXT NOT NULL,
    revision          INTEGER NOT NULL DEFAULT 1,
    synchronized_at   TEXT NOT NULL,
    CONSTRAINT uq_task_file_mapping UNIQUE (task_id, filename),
    CONSTRAINT ck_vault_mutator CHECK (
        last_mutation_by IN ('HUMAN_USER','SYSTEM_KERNEL','WORKER_CELL','MEMORY_CURATOR')
    )
) STRICT;

CREATE TABLE IF NOT EXISTS hot_serving_skill_registry (
    id                   TEXT PRIMARY KEY,
    skill_name           TEXT NOT NULL UNIQUE,
    provider             TEXT NOT NULL DEFAULT 'claw_hub',
    version              TEXT NOT NULL DEFAULT '0.1.0',
    description          TEXT NOT NULL,
    input_schema         TEXT NOT NULL,
    output_schema        TEXT NOT NULL,
    risk_profile         REAL NOT NULL DEFAULT 0.5,
    required_permissions TEXT NOT NULL DEFAULT '[]',
    reliability_weight   REAL NOT NULL DEFAULT 1.0,
    invocation           TEXT NOT NULL DEFAULT '{}',
    source_uri           TEXT,
    source_checksum      TEXT,
    signature            TEXT,
    is_active            INTEGER NOT NULL DEFAULT 1,
    installed_at         TEXT NOT NULL,
    updated_at           TEXT NOT NULL,
    CONSTRAINT ck_skill_risk CHECK (risk_profile BETWEEN 0.0 AND 1.0),
    CONSTRAINT ck_skill_reliability CHECK (reliability_weight BETWEEN 0.0 AND 1.0),
    CONSTRAINT ck_skill_provider CHECK (provider IN ('claw_hub','mcp_server','native','marketplace')),
    CONSTRAINT ck_skill_active_bool CHECK (is_active IN (0,1)),
    CONSTRAINT ck_skill_in_json CHECK (json_valid(input_schema)),
    CONSTRAINT ck_skill_out_json CHECK (json_valid(output_schema)),
    CONSTRAINT ck_skill_perms_json CHECK (json_valid(required_permissions))
) STRICT;

CREATE INDEX IF NOT EXISTS idx_skills_active
    ON hot_serving_skill_registry(skill_name) WHERE is_active = 1;

CREATE TABLE IF NOT EXISTS hot_serving_agent_registry (
    id                TEXT PRIMARY KEY,
    agent_name        TEXT NOT NULL UNIQUE,
    role              TEXT NOT NULL,
    description       TEXT NOT NULL DEFAULT '',
    allowed_tools     TEXT NOT NULL DEFAULT '[]',
    granted_permissions TEXT NOT NULL DEFAULT '[]',
    recursion_ceiling INTEGER NOT NULL DEFAULT 0,
    max_retries       INTEGER NOT NULL DEFAULT 1,
    token_ceiling     INTEGER NOT NULL DEFAULT 2048,
    model_preference  TEXT,
    is_active         INTEGER NOT NULL DEFAULT 1,
    created_at        TEXT NOT NULL,
    CONSTRAINT ck_agent_tools_json CHECK (json_valid(allowed_tools)),
    CONSTRAINT ck_agent_perms_json CHECK (json_valid(granted_permissions)),
    CONSTRAINT ck_agent_active_bool CHECK (is_active IN (0,1))
) STRICT;

-- ---------------------------------------------------------------------
-- Telemetry, evaluation, recovery bookkeeping
-- ---------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS hot_serving_execution_runs (
    trace_id          TEXT PRIMARY KEY,
    correlation_id    TEXT NOT NULL,
    session_id        TEXT,
    span_parent_id    TEXT,
    agent_role        TEXT NOT NULL,
    skill_name        TEXT,
    modality          TEXT NOT NULL,
    permission_mode   TEXT NOT NULL,
    started_at        TEXT NOT NULL,
    ended_at          TEXT,
    duration_ms       INTEGER,
    exit_code         INTEGER,
    tokens_consumed   INTEGER NOT NULL DEFAULT 0,
    peak_rss_mb       REAL,
    model_used        TEXT,
    escalated         INTEGER NOT NULL DEFAULT 0,
    telemetry         TEXT NOT NULL DEFAULT '{}',
    error_detail      TEXT,
    CONSTRAINT ck_run_telemetry_json CHECK (json_valid(telemetry)),
    CONSTRAINT ck_run_escalated_bool CHECK (escalated IN (0,1))
) STRICT;

CREATE INDEX IF NOT EXISTS idx_runs_correlation ON hot_serving_execution_runs(correlation_id, started_at ASC);
CREATE INDEX IF NOT EXISTS idx_runs_skill ON hot_serving_execution_runs(skill_name, started_at DESC);
CREATE INDEX IF NOT EXISTS idx_runs_started ON hot_serving_execution_runs(started_at DESC);

CREATE TABLE IF NOT EXISTS hot_serving_evaluation_results (
    id             TEXT PRIMARY KEY,
    correlation_id TEXT NOT NULL,
    evaluator      TEXT NOT NULL,
    verdict        TEXT NOT NULL,
    score          REAL,
    findings       TEXT NOT NULL DEFAULT '[]',
    evaluated_at   TEXT NOT NULL,
    CONSTRAINT ck_eval_verdict CHECK (verdict IN ('PASS','FAIL_REJECT_RETRY','FAIL_ESCALATE','SKIPPED')),
    CONSTRAINT ck_eval_findings_json CHECK (json_valid(findings))
) STRICT;

CREATE INDEX IF NOT EXISTS idx_eval_correlation ON hot_serving_evaluation_results(correlation_id);

-- Filesystem checkpoints used to detect and undo post-crash drift.
CREATE TABLE IF NOT EXISTS hot_serving_recovery_marks (
    id             TEXT PRIMARY KEY,
    correlation_id TEXT NOT NULL,
    state_version  INTEGER NOT NULL,
    workspace_path TEXT NOT NULL,
    manifest       TEXT NOT NULL,
    manifest_hash  TEXT NOT NULL,
    created_at     TEXT NOT NULL,
    CONSTRAINT uq_recovery_mark UNIQUE (correlation_id, state_version),
    CONSTRAINT ck_mark_manifest_json CHECK (json_valid(manifest))
) STRICT;

CREATE INDEX IF NOT EXISTS idx_marks_correlation
    ON hot_serving_recovery_marks(correlation_id, state_version DESC);

-- Registry describing each logical memory domain's policy (RFC §4.1).
CREATE TABLE IF NOT EXISTS hot_serving_memory_registry (
    domain           TEXT PRIMARY KEY,
    storage_engine   TEXT NOT NULL,
    decay_lambda     REAL NOT NULL DEFAULT 0.0,
    prune_floor      REAL NOT NULL DEFAULT 0.15,
    retrieval_protocol TEXT NOT NULL,
    is_immutable     INTEGER NOT NULL DEFAULT 0,
    description      TEXT NOT NULL DEFAULT '',
    CONSTRAINT ck_memreg_immutable_bool CHECK (is_immutable IN (0,1))
) STRICT;

CREATE TABLE IF NOT EXISTS hot_serving_policy_rules (
    id           TEXT PRIMARY KEY,
    rule_name    TEXT NOT NULL UNIQUE,
    rule_kind    TEXT NOT NULL,
    pattern      TEXT NOT NULL,
    severity     TEXT NOT NULL DEFAULT 'block',
    threshold    REAL,
    source_file  TEXT,
    is_active    INTEGER NOT NULL DEFAULT 1,
    created_at   TEXT NOT NULL,
    CONSTRAINT ck_policy_kind CHECK (rule_kind IN ('regex','ast','anti_goal_vector','permission','path_guard')),
    CONSTRAINT ck_policy_severity CHECK (severity IN ('block','gate','warn')),
    CONSTRAINT ck_policy_active_bool CHECK (is_active IN (0,1))
) STRICT;

-- =====================================================================
-- 4. DURABLE QUEUE
--
-- SPEC DEVIATION (docs/adr/0005): the RFC uses 11 Redis Streams. Redis is not
-- installed and, per the RFC's own §17.4, must never be the source of truth.
-- The SQLite-backed queue below is durable by construction, so a restart
-- cannot lose in-flight messages. A Redis backend implementing the same
-- interface is available for multi-process deployments.
-- =====================================================================

CREATE TABLE IF NOT EXISTS queue_messages (
    id              TEXT PRIMARY KEY,
    stream          TEXT NOT NULL,
    correlation_id  TEXT,
    session_id      TEXT,
    priority        INTEGER NOT NULL DEFAULT 100,
    payload         TEXT NOT NULL,
    status          TEXT NOT NULL DEFAULT 'ready',
    attempts        INTEGER NOT NULL DEFAULT 0,
    max_attempts    INTEGER NOT NULL DEFAULT 3,
    claimed_by      TEXT,
    claimed_at      TEXT,
    visible_after   TEXT NOT NULL,
    enqueued_at     TEXT NOT NULL,
    completed_at    TEXT,
    last_error      TEXT,
    CONSTRAINT ck_queue_status CHECK (status IN ('ready','claimed','done','dead')),
    CONSTRAINT ck_queue_payload_json CHECK (json_valid(payload))
) STRICT;

-- Dispatch hot path: lowest priority value first, then FIFO by enqueue time.
CREATE INDEX IF NOT EXISTS idx_queue_dispatch
    ON queue_messages(stream, priority ASC, visible_after ASC, enqueued_at ASC)
    WHERE status = 'ready';
CREATE INDEX IF NOT EXISTS idx_queue_reclaim
    ON queue_messages(visible_after ASC) WHERE status = 'claimed';
CREATE INDEX IF NOT EXISTS idx_queue_dead
    ON queue_messages(stream, enqueued_at DESC) WHERE status = 'dead';

CREATE TABLE IF NOT EXISTS queue_locks (
    lock_key    TEXT PRIMARY KEY,
    holder      TEXT NOT NULL,
    acquired_at TEXT NOT NULL,
    expires_at  TEXT NOT NULL
) STRICT;

CREATE INDEX IF NOT EXISTS idx_locks_expiry ON queue_locks(expires_at ASC);

-- =====================================================================
-- 5. MARKETPLACE
-- =====================================================================

CREATE TABLE IF NOT EXISTS marketplace_packages (
    id              TEXT PRIMARY KEY,
    package_name    TEXT NOT NULL,
    version         TEXT NOT NULL,
    kind            TEXT NOT NULL,
    publisher       TEXT NOT NULL,
    publisher_key   TEXT,
    description     TEXT NOT NULL DEFAULT '',
    manifest        TEXT NOT NULL,
    content_hash    TEXT NOT NULL,
    signature       TEXT,
    signature_state TEXT NOT NULL DEFAULT 'unverified',
    source_registry TEXT,
    installed_at    TEXT,
    trust_score     REAL NOT NULL DEFAULT 0.0,
    CONSTRAINT uq_marketplace_pkg UNIQUE (package_name, version),
    CONSTRAINT ck_market_kind CHECK (kind IN ('skill','agent','playbook','config','bundle')),
    CONSTRAINT ck_market_sig CHECK (signature_state IN ('unverified','valid','invalid','untrusted_key')),
    CONSTRAINT ck_market_manifest_json CHECK (json_valid(manifest))
) STRICT;

CREATE INDEX IF NOT EXISTS idx_marketplace_installed
    ON marketplace_packages(installed_at DESC) WHERE installed_at IS NOT NULL;

CREATE TABLE IF NOT EXISTS marketplace_trusted_keys (
    key_id      TEXT PRIMARY KEY,
    public_key  TEXT NOT NULL,
    owner       TEXT NOT NULL,
    added_at    TEXT NOT NULL,
    revoked_at  TEXT
) STRICT;

-- =====================================================================
-- 6. SELF-IMPROVEMENT
-- =====================================================================

CREATE TABLE IF NOT EXISTS improvement_reflections (
    id              TEXT PRIMARY KEY,
    window_start    TEXT NOT NULL,
    window_end      TEXT NOT NULL,
    task_domain     TEXT NOT NULL,
    friction_score  REAL NOT NULL,
    corrections     INTEGER NOT NULL DEFAULT 0,
    rollbacks       INTEGER NOT NULL DEFAULT 0,
    successes       INTEGER NOT NULL DEFAULT 0,
    extracted_rule  TEXT,
    applied_to      TEXT,
    created_at      TEXT NOT NULL
) STRICT;

CREATE INDEX IF NOT EXISTS idx_reflections_domain
    ON improvement_reflections(task_domain, created_at DESC);

CREATE TABLE IF NOT EXISTS improvement_skill_weights (
    skill_name      TEXT PRIMARY KEY,
    reliability     REAL NOT NULL DEFAULT 1.0,
    sample_count    INTEGER NOT NULL DEFAULT 0,
    mean_latency_s  REAL NOT NULL DEFAULT 0.0,
    correction_rate REAL NOT NULL DEFAULT 0.0,
    updated_at      TEXT NOT NULL,
    CONSTRAINT ck_weight_reliability CHECK (reliability BETWEEN 0.0 AND 1.0)
) STRICT;

-- =====================================================================
-- 7. SCHEMA VERSION
-- =====================================================================

CREATE TABLE IF NOT EXISTS schema_migrations (
    version     INTEGER PRIMARY KEY,
    applied_at  TEXT NOT NULL,
    description TEXT NOT NULL
) STRICT;
