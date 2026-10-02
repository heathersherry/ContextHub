CREATE EXTENSION IF NOT EXISTS vector;
CREATE TABLE research_config (singleton boolean PRIMARY KEY CHECK(singleton), config jsonb NOT NULL);
CREATE TABLE session_records (
 run_id text NOT NULL, episode_id text NOT NULL, strategy_id text NOT NULL,
 session_seq integer NOT NULL CHECK(session_seq > 0), timestamp text,
 messages jsonb NOT NULL, extraction_output text,
 PRIMARY KEY(run_id, episode_id, strategy_id, session_seq)
);
CREATE TABLE memories (
 run_id text NOT NULL, episode_id text NOT NULL, strategy_id text NOT NULL,
 memory_id uuid NOT NULL, version integer NOT NULL CHECK(version > 0),
 text text NOT NULL CHECK(length(text) > 0), type text NOT NULL,
 origin text NOT NULL CHECK(origin IN ('extracted','derived')),
 status text NOT NULL CHECK(status IN ('unresolved','fresh','stale','deleted')),
 status_reason text, created_in_session integer NOT NULL, timestamp text,
 entities text[] NOT NULL, search_vector tsvector, memory_embedding vector(__DIMENSIONS__),
 PRIMARY KEY(run_id, episode_id, strategy_id, memory_id, version),
 FOREIGN KEY(run_id,episode_id,strategy_id,created_in_session)
   REFERENCES session_records(run_id,episode_id,strategy_id,session_seq),
 CHECK(type <> 'change' OR version = 1)
);
CREATE INDEX memories_text ON memories USING gin(search_vector);
CREATE INDEX memories_entities ON memories USING gin(entities);
CREATE TABLE memory_sources (
 run_id text NOT NULL, episode_id text NOT NULL, strategy_id text NOT NULL,
 memory_id uuid NOT NULL, version integer NOT NULL,
 session_seq integer[] NOT NULL, message_refs jsonb NOT NULL,
 PRIMARY KEY(run_id,episode_id,strategy_id,memory_id,version),
 FOREIGN KEY(run_id,episode_id,strategy_id,memory_id,version) REFERENCES memories
);
CREATE TABLE dependencies (
 run_id text NOT NULL, episode_id text NOT NULL, strategy_id text NOT NULL,
 upstream_id uuid NOT NULL, upstream_version integer NOT NULL,
 downstream_id uuid NOT NULL, downstream_version integer NOT NULL,
 edge_status text NOT NULL CHECK(edge_status IN ('confirmed','pending')), reason text NOT NULL,
 PRIMARY KEY(run_id,episode_id,strategy_id,upstream_id,upstream_version,downstream_id,downstream_version),
 FOREIGN KEY(run_id,episode_id,strategy_id,upstream_id,upstream_version) REFERENCES memories,
 FOREIGN KEY(run_id,episode_id,strategy_id,downstream_id,downstream_version) REFERENCES memories
);
CREATE TABLE change_events (
 run_id text NOT NULL, episode_id text NOT NULL, strategy_id text NOT NULL,
 change_id uuid NOT NULL, operation text CHECK(operation IN ('update','forget')),
 target_refs jsonb NOT NULL DEFAULT '[]', applied_session_seq integer CHECK(applied_session_seq > 0),
 PRIMARY KEY(run_id,episode_id,strategy_id,change_id)
);
-- Same shared attempt contract, validation and cost calculation; no product event/task FKs.
CREATE TABLE execution_attempts (
 account_id text NOT NULL, call_id uuid NOT NULL, attempt_no integer NOT NULL,
 phase text NOT NULL, execution_id uuid NOT NULL, sha256 text NOT NULL, payload jsonb NOT NULL,
 recorded_at timestamptz NOT NULL DEFAULT now(),
 run_id text NOT NULL, episode_id text NOT NULL, strategy_id text NOT NULL,
 session_seq integer NOT NULL, extraction_attempt integer,
 retry_reason text NOT NULL, parse_result text, raw_output text, error text,
 PRIMARY KEY(account_id,call_id,attempt_no,phase)
);
CREATE INDEX execution_attempts_trace ON execution_attempts(run_id,episode_id,strategy_id,session_seq);
