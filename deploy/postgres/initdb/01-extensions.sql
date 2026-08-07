-- Run once by postgres:16 on first cluster init (docker-entrypoint-initdb.d).
--
-- pg_trgm powers the entity trigram index the RFC specified and
-- schema_postgres.sql builds (idx_hot_entity_trgm_*). It is a "trusted"
-- extension since PostgreSQL 13, so the application role could create it too;
-- doing it here means the extension is present the instant the runtime first
-- connects, regardless of which role applies the schema.
CREATE EXTENSION IF NOT EXISTS pg_trgm;
