-- ctxlayer schema: shared context layer for agents/bots.
-- Core requires PostgreSQL 14+ only.
-- Core v1: messages, thread summaries, agent cursors, usage ledger. No pgvector needed.
-- Optional extras (facts + pgvector, LISTEN/NOTIFY) live in schema_extras.sql.

-- Raw record of everything said in a thread. Bots do NOT re-read this in full;
-- they read the rolling summary plus only the messages after their own cursor.
CREATE TABLE IF NOT EXISTS ctx_messages (
    id          BIGSERIAL PRIMARY KEY,
    thread_id   TEXT        NOT NULL,
    author      TEXT        NOT NULL,
    body        TEXT        NOT NULL,
    tokens      INTEGER     NOT NULL,           -- estimated token count of body
    ts          TIMESTAMPTZ NOT NULL DEFAULT now(),
    external_id TEXT,                           -- e.g. Slack ts, for idempotent ingest
    UNIQUE (thread_id, external_id)
);
CREATE INDEX IF NOT EXISTS ctx_messages_thread_idx ON ctx_messages (thread_id, id);

-- One row per thread: the rolling summary and how far it covers.
CREATE TABLE IF NOT EXISTS ctx_threads (
    thread_id       TEXT PRIMARY KEY,
    summary         TEXT        NOT NULL DEFAULT '',
    summary_tokens  INTEGER     NOT NULL DEFAULT 0,
    summarized_upto BIGINT      NOT NULL DEFAULT 0,  -- last ctx_messages.id folded into summary
    version         INTEGER     NOT NULL DEFAULT 0,  -- optimistic concurrency token
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Per-agent read cursor: the last message each agent has already seen.
CREATE TABLE IF NOT EXISTS ctx_cursors (
    agent_id    TEXT        NOT NULL,
    thread_id   TEXT        NOT NULL,
    last_seen   BIGINT      NOT NULL DEFAULT 0,
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (agent_id, thread_id)
);

-- Usage log: what an agent actually received vs. what full replay would have cost.
-- This is how the saving is demonstrated instead of guessed.
CREATE TABLE IF NOT EXISTS ctx_usage (
    id               BIGSERIAL PRIMARY KEY,
    agent_id         TEXT        NOT NULL,
    thread_id        TEXT        NOT NULL,
    event            TEXT        NOT NULL,        -- 'read' | 'summarize' | 'expand'
    tokens_delivered INTEGER     NOT NULL,        -- tokens handed to the agent (or spent summarizing)
    tokens_baseline  INTEGER     NOT NULL,        -- tokens a full replay would have cost
    ts               TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS ctx_usage_agent_idx ON ctx_usage (agent_id, ts);
