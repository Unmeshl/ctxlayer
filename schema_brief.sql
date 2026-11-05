-- Requirements ledger for the pre-development brief (optional; independent of the thread tables).
CREATE TABLE IF NOT EXISTS ctx_items (
    id          BIGSERIAL PRIMARY KEY,
    project     TEXT NOT NULL,
    kind        TEXT NOT NULL CHECK (kind IN ('goal','requirement','constraint','non_goal','decision','open_question')),
    text        TEXT NOT NULL,
    accept      TEXT,                       -- acceptance criterion (requirements)
    blocking    BOOLEAN NOT NULL DEFAULT FALSE,  -- open questions that must be answered before building
    status      TEXT NOT NULL DEFAULT 'active' CHECK (status IN ('active','superseded','done','dropped')),
    supersedes  BIGINT REFERENCES ctx_items(id),
    source      TEXT,                       -- where it came from (message id, "chat 2026-11-03")
    ts          TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS ctx_items_project_status ON ctx_items (project, status);
