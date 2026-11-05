"""SQLite backend for ctxlayer: the same core API as ContextStore, on one local file, standard library only.

Use it when there is no database server (a solo user, one bot box). Postgres stays the choice for many
agents on many machines. Not included here: facts/pgvector and LISTEN/NOTIFY (extras), which raise a clear error.

  store = SqliteStore("ctx.db"); store.install()      # same methods as ContextStore
  CLI:  python3 cli.py --dsn ctx.db has-new --agent agent1 --thread myproject
"""
from __future__ import annotations

import re
import sqlite3
from datetime import datetime, timezone
from typing import Optional, Sequence

from briefs import BriefStore
from ctxlayer import ContextStore, StaleSummary

HERE = __import__("pathlib").Path(__file__).parent
_NOW = "strftime('%Y-%m-%d %H:%M:%f+00:00','now')"


def is_sqlite_dsn(dsn: str) -> bool:
    return dsn.startswith("sqlite:") or (
        "://" not in dsn and dsn.lower().endswith((".db", ".sqlite", ".sqlite3"))
    )


def sqlite_path(dsn: str) -> str:
    return re.sub(r"^sqlite:(//)?", "", dsn)


def _dict_row(cur, row):
    return {d[0]: v for d, v in zip(cur.description, row)}


def _adapt(v):
    return v.astimezone(timezone.utc).isoformat(sep=" ") if isinstance(v, datetime) else v


class _Conn:
    """Thin wrapper so the store's psycopg-style SQL (%s placeholders, now()) runs on sqlite3.
    As a context manager it commits on success, rolls back on error, and always closes."""

    def __init__(self, path: str):
        self.raw = sqlite3.connect(path, timeout=10)
        self.raw.row_factory = _dict_row
        self.raw.execute("PRAGMA journal_mode=WAL")
        self.raw.execute("PRAGMA busy_timeout=10000")

    def execute(self, sql: str, params: Sequence = ()):
        sql = sql.replace("%s", "?").replace("now()", _NOW)
        return self.raw.execute(sql, tuple(_adapt(p) for p in params))

    def executescript(self, sql: str):
        self.raw.executescript(sql)

    def rollback(self):
        self.raw.rollback()

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        try:
            if exc_type is None:
                self.raw.commit()
            else:
                self.raw.rollback()
        finally:
            self.raw.close()
        return False


def _parse_ts(ts):
    return datetime.fromisoformat(ts) if isinstance(ts, str) else ts


class SqliteStore(ContextStore):
    def __init__(self, path: str, **kw):
        if kw.get("extras"):
            raise RuntimeError("extras (facts + pgvector, LISTEN/NOTIFY) need the Postgres store; the SQLite store is core only")
        super().__init__(sqlite_path(path), **kw)

    def _connect(self):
        return _Conn(self.dsn)

    def _fmt_ts(self, ts) -> str:
        return super()._fmt_ts(_parse_ts(ts))

    def install(self) -> None:
        with self._connect() as conn:
            conn.executescript((HERE / "schema_sqlite.sql").read_text())

    def uninstall(self) -> None:
        with self._connect() as conn:
            conn.executescript("DROP TABLE IF EXISTS ctx_usage; DROP TABLE IF EXISTS ctx_cursors; "
                               "DROP TABLE IF EXISTS ctx_threads; DROP TABLE IF EXISTS ctx_messages;")

    def listen(self, timeout=None):
        raise RuntimeError("listen() needs the Postgres store with extras")

    # ---- the few queries that use Postgres-only SQL
    def has_new(self, agent_id: str, thread_id: str, ignore_authors: Optional[Sequence[str]] = None) -> int:
        ignore = list(ignore_authors) if ignore_authors is not None else [agent_id]
        cond = f"m.author NOT IN ({','.join('?' * len(ignore))})" if ignore else "1=1"
        with self._connect() as conn:
            return conn.execute(
                f"SELECT count(*) AS n FROM ctx_messages m WHERE m.thread_id=%s AND {cond} "
                "AND m.id > COALESCE((SELECT last_seen FROM ctx_cursors WHERE agent_id=%s AND thread_id=%s), 0)",
                (thread_id, *ignore, agent_id, thread_id),
            ).fetchone()["n"]

    def search_messages(self, thread_id: str, query: str, limit: int = 5, agent_id: str = "expander", log: bool = True):
        from ctxlayer import _query_terms
        terms = _query_terms(query)
        if not terms:
            return []
        esc = lambda s: s.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")  # noqa: E731
        pats = [f"%{esc(t)}%" for t in terms]
        one = "(lower(body) LIKE %s ESCAPE '\\')"
        score = " + ".join([one] * len(pats))
        with self._connect() as conn:
            rows = conn.execute(
                f"SELECT id, author, body, tokens, ts FROM ctx_messages WHERE thread_id=%s AND ({score}) > 0 "
                f"ORDER BY ({score}) DESC, id DESC LIMIT %s",
                [thread_id, *pats, *pats, limit],
            ).fetchall()
            return self._expanded(conn, rows, agent_id, thread_id, log)

    def get_messages(self, thread_id: str, ids: Sequence[int], agent_id: str = "expander", log: bool = True):
        ids = [int(i) for i in ids]
        if not ids:
            return []
        marks = ",".join("?" * len(ids))
        with self._connect() as conn:
            rows = conn.execute(
                f"SELECT id, author, body, tokens, ts FROM ctx_messages WHERE thread_id=%s AND id IN ({marks})",
                (thread_id, *ids),
            ).fetchall()
            return self._expanded(conn, rows, agent_id, thread_id, log)

    def commit(self, agent_id: str, thread_id: str, *, upto: Optional[int] = None, new_summary: Optional[str] = None,
               expected_version: Optional[int] = None, facts: Sequence = (), summarize_tokens: int = 0) -> dict:
        if facts:
            self._need_extras("storing facts")
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")  # take the write lock first: version check and cursor move are atomic
            version = None
            if new_summary is not None:
                if upto is None:
                    raise ValueError("upto is required when new_summary is given")
                conn.execute("INSERT INTO ctx_threads (thread_id) VALUES (%s) ON CONFLICT DO NOTHING", (thread_id,))
                row = conn.execute(
                    "UPDATE ctx_threads SET summary=%s, summary_tokens=%s, summarized_upto=MAX(summarized_upto, %s), "
                    "version=version+1, updated_at=now() WHERE thread_id=%s AND (%s IS NULL OR version=%s) RETURNING version",
                    (new_summary, self.count_tokens(new_summary), upto, thread_id, expected_version, expected_version),
                ).fetchone()
                if row is None:
                    raise StaleSummary(f"summary for {thread_id} changed since version {expected_version}")
                version = row["version"]
                if summarize_tokens:
                    conn.execute(
                        "INSERT INTO ctx_usage (agent_id, thread_id, event, tokens_delivered, tokens_baseline) "
                        "VALUES (%s, %s, 'summarize', %s, 0)", (agent_id, thread_id, summarize_tokens))
            if upto is not None:
                conn.execute(
                    "INSERT INTO ctx_cursors (agent_id, thread_id, last_seen) VALUES (%s, %s, %s) "
                    "ON CONFLICT (agent_id, thread_id) DO UPDATE "
                    "SET last_seen = MAX(ctx_cursors.last_seen, excluded.last_seen), updated_at = now()",
                    (agent_id, thread_id, upto))
        return {"version": version, "fact_ids": []}

    def usage_report(self, agent_id: Optional[str] = None) -> list[dict]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT agent_id, "
                "SUM(CASE WHEN event='read' THEN 1 ELSE 0 END) AS reads, "
                "COALESCE(SUM(CASE WHEN event='read' THEN tokens_delivered END),0) AS delivered, "
                "COALESCE(SUM(CASE WHEN event='read' THEN tokens_baseline END),0) AS baseline, "
                "COALESCE(SUM(CASE WHEN event='summarize' THEN tokens_delivered END),0) AS summarize_cost, "
                "COALESCE(SUM(CASE WHEN event='expand' THEN tokens_delivered END),0) AS expand_cost "
                "FROM ctx_usage WHERE (%s IS NULL OR agent_id=%s) GROUP BY agent_id ORDER BY agent_id",
                (agent_id, agent_id),
            ).fetchall()
        for r in rows:
            net = r["delivered"] + r["summarize_cost"] + r["expand_cost"]
            r["net_saved"] = r["baseline"] - net
            r["net_saved_pct"] = round(100 * r["net_saved"] / r["baseline"], 1) if r["baseline"] else 0.0
        return rows


class SqliteBriefStore(BriefStore):
    def __init__(self, path: str, **kw):
        super().__init__(sqlite_path(path), **kw)

    def _connect(self):
        return _Conn(self.dsn)

    def install(self) -> None:
        with self._connect() as conn:
            conn.executescript((HERE / "schema_brief_sqlite.sql").read_text())

    def uninstall(self) -> None:
        with self._connect() as conn:
            conn.executescript("DROP TABLE IF EXISTS ctx_items;")


def open_store(dsn: str, **kw) -> ContextStore:
    return SqliteStore(dsn, **kw) if is_sqlite_dsn(dsn) else ContextStore(dsn, **kw)


def open_brief_store(dsn: str, **kw) -> BriefStore:
    return SqliteBriefStore(dsn, **kw) if is_sqlite_dsn(dsn) else BriefStore(dsn, **kw)
