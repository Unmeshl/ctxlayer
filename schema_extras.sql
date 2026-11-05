-- Optional extras. Install with ContextStore(..., extras=True).install().
-- Requires the pgvector extension (0.5+ for HNSW). Dimension substituted at install time.

CREATE EXTENSION IF NOT EXISTS vector;

-- Distilled facts / decisions with embeddings, for retrieval of "what do we know about X".
CREATE TABLE IF NOT EXISTS ctx_facts (
    id            BIGSERIAL PRIMARY KEY,
    scope         TEXT        NOT NULL,          -- thread id, project id, or 'global'
    kind          TEXT        NOT NULL DEFAULT 'fact',  -- fact | decision | state | preference
    content       TEXT        NOT NULL,
    tokens        INTEGER     NOT NULL,
    embedding     vector(__DIM__),
    author        TEXT,
    superseded_by BIGINT REFERENCES ctx_facts(id),
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS ctx_facts_scope_idx ON ctx_facts (scope) WHERE superseded_by IS NULL;
CREATE INDEX IF NOT EXISTS ctx_facts_embedding_idx
    ON ctx_facts USING hnsw (embedding vector_cosine_ops);

-- Event-driven wakes: notify listeners when a message lands, so bots sleep until needed
-- instead of polling. Payload is small JSON: {"thread": ..., "id": ..., "author": ...}.
CREATE OR REPLACE FUNCTION ctx_notify_message() RETURNS trigger AS $$
BEGIN
    PERFORM pg_notify('ctx_messages',
        json_build_object('thread', NEW.thread_id, 'id', NEW.id, 'author', NEW.author)::text);
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS ctx_messages_notify ON ctx_messages;
CREATE TRIGGER ctx_messages_notify AFTER INSERT ON ctx_messages
    FOR EACH ROW EXECUTE FUNCTION ctx_notify_message();
