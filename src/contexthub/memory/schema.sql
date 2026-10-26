CREATE EXTENSION IF NOT EXISTS vector WITH SCHEMA public;
CREATE TABLE memory_config (singleton boolean PRIMARY KEY CHECK(singleton), config jsonb NOT NULL);
CREATE TABLE session_records (
 tenant_id text NOT NULL, namespace text NOT NULL, owner_id text NOT NULL,
 session_seq integer NOT NULL CHECK(session_seq > 0), timestamp text,
 messages jsonb NOT NULL, extraction_output text,
 PRIMARY KEY(tenant_id, namespace, owner_id, session_seq)
);
CREATE TABLE memories (
 tenant_id text NOT NULL, namespace text NOT NULL, owner_id text NOT NULL,
 memory_id uuid NOT NULL, version integer NOT NULL CHECK(version > 0),
 text text NOT NULL CHECK(length(text) > 0), type text NOT NULL CHECK(type IN ('fact','rule')),
 origin text NOT NULL CHECK(origin IN ('extracted','derived')),
 status text NOT NULL CHECK(status IN ('unresolved','fresh','stale','deleted')),
 temporal_scope text CHECK(temporal_scope IN ('current','historical')),
 accepted boolean NOT NULL DEFAULT false, invalidated boolean NOT NULL DEFAULT false,
 basis_refs jsonb NOT NULL DEFAULT '[]',
 condition_matter_ids uuid[] NOT NULL DEFAULT '{}'::uuid[],
 affected_matter_ids uuid[] NOT NULL DEFAULT '{}'::uuid[],
 status_reason text, created_in_session integer NOT NULL, timestamp text,
 entities text[] NOT NULL, topics text[] NOT NULL,
 search_vector tsvector, memory_embedding vector(__DIMENSIONS__),
 PRIMARY KEY(tenant_id, namespace, owner_id, memory_id, version),
 FOREIGN KEY(tenant_id,namespace,owner_id,created_in_session)
   REFERENCES session_records(tenant_id,namespace,owner_id,session_seq),
 CHECK(type='rule' OR (condition_matter_ids='{}'::uuid[] AND affected_matter_ids='{}'::uuid[]))
);
CREATE INDEX memories_text ON memories USING gin(search_vector);
CREATE INDEX memories_conditions ON memories USING gin(condition_matter_ids) WHERE type='rule';
CREATE INDEX memories_affected ON memories USING gin(affected_matter_ids) WHERE type='rule';
CREATE INDEX memories_entities ON memories USING gin(entities);
-- One embedding per normalized topic in this database's fixed embedding space.
CREATE TABLE topic_embeddings (
 topic text PRIMARY KEY CHECK(length(topic) > 0),
 embedding vector(__DIMENSIONS__) NOT NULL
);
CREATE TABLE memory_sources (
 tenant_id text NOT NULL, namespace text NOT NULL, owner_id text NOT NULL,
 memory_id uuid NOT NULL, version integer NOT NULL,
 session_seq integer[] NOT NULL,
 PRIMARY KEY(tenant_id,namespace,owner_id,memory_id,version),
 FOREIGN KEY(tenant_id,namespace,owner_id,memory_id,version) REFERENCES memories
);
-- Same shared attempt contract, validation and cost calculation; no product event/task FKs.
CREATE TABLE execution_attempts (
 account_id text NOT NULL, call_id uuid NOT NULL, attempt_no integer NOT NULL,
 phase text NOT NULL, execution_id uuid NOT NULL, sha256 text NOT NULL, payload jsonb NOT NULL,
 recorded_at timestamptz NOT NULL DEFAULT now(),
 tenant_id text NOT NULL, namespace text NOT NULL, owner_id text NOT NULL,
 session_seq integer NOT NULL, extraction_attempt integer,
 retry_reason text NOT NULL, parse_result text, raw_output text, error text,
 PRIMARY KEY(account_id,call_id,attempt_no,phase)
);
CREATE INDEX execution_attempts_trace ON execution_attempts(tenant_id,namespace,owner_id,session_seq);

-- Ordered process evidence, not a second billing ledger or a task scheduler.
CREATE TABLE memory_steps (
 step_id bigserial PRIMARY KEY,
 tenant_id text NOT NULL, namespace text NOT NULL, owner_id text NOT NULL,
 session_seq integer NOT NULL, stage text NOT NULL, payload jsonb NOT NULL,
 recorded_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX memory_steps_trace ON memory_steps(tenant_id,namespace,owner_id,session_seq,step_id);
