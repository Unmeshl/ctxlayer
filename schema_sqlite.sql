-- ctxlayer core schema for SQLite (single local file; no server). Same four tables and column meanings as
-- schema.sql. Timestamps are ISO-8601 UTC text. Requires SQLite 3.35+ (RETURNING), bundled with Python 3.10+.
CREATE TABLE IF NOT EXISTS ctx_messages (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    thread_id   TEXT    NOT NULL,
    author      TEXT    NOT NULL,
    body        TEXT    NOT NULL,
    tokens      INTEGER NOT NULL,
    ts          TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%d %H:%M:%f+00:00','now')),
    external_id TEXT,
    UNIQUE (thread_id, external_id)
);
CREATE INDEX IF NOT EXISTS ctx_messages_thread_idx ON ctx_messages (thread_id, id);

CREATE TABLE IF NOT EXISTS ctx_threads (
    thread_id       TEXT PRIMARY KEY,
    summary         TEXT    NOT NULL DEFAULT '',
    summary_tokens  INTEGER NOT NULL DEFAULT 0,
    summarized_upto INTEGER NOT NULL DEFAULT 0,
    version         INTEGER NOT NULL DEFAULT 0,
    updated_at      TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%d %H:%M:%f+00:00','now'))
);

CREATE TABLE IF NOT EXISTS ctx_cursors (
    agent_id    TEXT    NOT NULL,
    thread_id   TEXT    NOT NULL,
    last_seen   INTEGER NOT NULL DEFAULT 0,
    updated_at  TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%d %H:%M:%f+00:00','now')),
    PRIMARY KEY (agent_id, thread_id)
);

CREATE TABLE IF NOT EXISTS ctx_usage (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    agent_id         TEXT    NOT NULL,
    thread_id        TEXT    NOT NULL,
    event            TEXT    NOT NULL,   -- 'read' | 'summarize' | 'expand'
    tokens_delivered INTEGER NOT NULL,
    tokens_baseline  INTEGER NOT NULL,
    ts               TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%d %H:%M:%f+00:00','now'))
);
CREATE INDEX IF NOT EXISTS ctx_usage_agent_idx ON ctx_usage (agent_id, ts);
