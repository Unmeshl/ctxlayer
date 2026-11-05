-- Requirements ledger for SQLite (optional; same meaning as schema_brief.sql).
CREATE TABLE IF NOT EXISTS ctx_items (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    project     TEXT NOT NULL,
    kind        TEXT NOT NULL CHECK (kind IN ('goal','requirement','constraint','non_goal','decision','open_question')),
    text        TEXT NOT NULL,
    accept      TEXT,
    blocking    INTEGER NOT NULL DEFAULT 0,
    status      TEXT NOT NULL DEFAULT 'active' CHECK (status IN ('active','superseded','done','dropped')),
    supersedes  INTEGER REFERENCES ctx_items(id),
    source      TEXT,
    ts          TEXT NOT NULL DEFAULT (strftime('%Y-%m-%d %H:%M:%f+00:00','now')),
    updated_at  TEXT NOT NULL DEFAULT (strftime('%Y-%m-%d %H:%M:%f+00:00','now'))
);
CREATE INDEX IF NOT EXISTS ctx_items_project_status ON ctx_items (project, status);
