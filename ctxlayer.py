"""ctxlayer: a shared, Postgres-backed context layer that lets agents read a thin slice
of context instead of replaying whole threads.

Core ideas
----------
* Every message in a thread is stored once (ctx_messages).
* Each thread has ONE rolling summary (ctx_threads) that covers messages up to
  `summarized_upto`. An agent reads: summary + the short unsummarized tail (+ optional
  retrieved facts), not the full history.
* Each agent has a cursor (ctx_cursors) so "is there anything new for me?" is a cheap
  SQL check that happens BEFORE any model call.
* Distilled facts/decisions live in ctx_facts with pgvector embeddings, for "what do we
  know about X" lookups across threads.
* Every read is logged to ctx_usage next to what a full replay would have cost, so the
  saving is measured, not guessed.

Framework-agnostic: nothing here knows about Slack, Claude, or any bot framework.
Token counts are ESTIMATES (chars/4 by default); pass count_tokens= to use a real tokenizer.
"""
from __future__ import annotations

import hashlib
import json
import math
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Iterator, Optional, Sequence
from zoneinfo import ZoneInfo

try:  # Postgres driver is optional: the SQLite backend (sqlite_store.py) needs only the standard library
    import psycopg
    from psycopg.rows import dict_row
except ImportError:  # pragma: no cover
    psycopg = None  # type: ignore[assignment]
    dict_row = None  # type: ignore[assignment]

DEFAULT_DIM = 256
SCHEMA_PATH = Path(__file__).with_name("schema.sql")
EXTRAS_SCHEMA_PATH = Path(__file__).with_name("schema_extras.sql")


# --------------------------------------------------------------------------- tokens
def estimate_tokens(text: str) -> int:
    """Rough token estimate (~4 chars/token). Replace with a real tokenizer in production."""
    return max(1, math.ceil(len(text) / 4)) if text else 0


# ----------------------------------------------------------------------- embeddings
class HashEmbedder:
    """Dependency-free, deterministic bag-of-words hashing embedder.

    This is a TEST/DEMO embedder: it captures lexical overlap only. In production pass a real
    embedding function (any callable str -> list[float] of length `dim`).
    """

    _STOP = frozenset("a an and are as at be by for from in is it of on or that the this to was with we our".split())

    def __init__(self, dim: int = DEFAULT_DIM):
        self.dim = dim

    def __call__(self, text: str) -> list[float]:
        vec = [0.0] * self.dim
        for w in re.findall(r"[a-z0-9]+", text.lower()):
            if w in self._STOP:
                continue
            h = int.from_bytes(hashlib.blake2b(w.encode(), digest_size=8).digest(), "big")
            vec[h % self.dim] += 1.0 if (h >> 40) & 1 else -1.0
        norm = math.sqrt(sum(x * x for x in vec)) or 1.0
        return [x / norm for x in vec]


def _vec_literal(v: Sequence[float]) -> str:
    return "[" + ",".join(f"{x:.6f}" for x in v) + "]"


_QUERY_STOP = frozenset(
    "a an and are as at be by for from in is it of on or that the this to was what which who when where why "
    "how with we our did does do has have had".split()
)


def _query_terms(query: str) -> list[str]:
    """Search terms from free text: keeps identifiers (abc1234, 1.2.3, #9), drops filler."""
    terms: list[str] = []
    for raw in re.findall(r"[A-Za-z0-9_#./:~=-]+", query):
        t = raw.strip(".,:;-/~").lower()
        if not t or t in _QUERY_STOP:
            continue
        if len(t) < 3 and not any(c.isdigit() for c in t):
            continue
        terms.append(t)
    return list(dict.fromkeys(terms))


# ------------------------------------------------------------------------- dataclasses
@dataclass
class Message:
    id: int
    author: str
    body: str
    tokens: int
    ts: object
    is_new: bool = True  # newer than the reading agent's cursor
    when: str = ""  # human-readable message time in the store's tz, e.g. "Fri Oct 30 9:06 PM EDT"

    def line(self) -> str:
        """One prompt-ready line: [#id author time] body."""
        seen = "" if self.is_new else " (seen)"
        return f"[#{self.id} {self.author}{' ' + self.when if self.when else ''}{seen}] {self.body}"


@dataclass
class Fact:
    content: str
    kind: str = "fact"  # fact | decision | state | preference
    scope: Optional[str] = None  # defaults to the thread id
    supersedes: Optional[int] = None  # id of an older fact this one replaces


@dataclass
class Context:
    thread_id: str
    agent_id: str
    summary: str
    summarized_upto: int
    version: int
    messages: list[Message]
    facts: list[dict]
    last_id: int  # highest message id included; pass to commit(upto=...)
    needs_summary: bool
    truncated: bool
    tokens_delivered: int
    tokens_baseline: int

    def render(self) -> str:
        """Prompt-ready text. Keep this stable-prefix-first so prompt caching still works."""
        parts = []
        if self.summary:
            parts.append(f"## Thread summary (through message #{self.summarized_upto})\n{self.summary}")
        if self.facts:
            parts.append("## Relevant facts\n" + "\n".join(f"- [{f['kind']}] {f['content']}" for f in self.facts))
        if self.messages:
            lines = [m.line() for m in self.messages]
            parts.append("## Recent messages\n" + "\n".join(lines))
        return "\n\n".join(parts)


class StaleSummary(Exception):
    """Another writer updated the thread summary since it was read. Re-read, merge, retry."""


# ------------------------------------------------------------------------------ store
class ContextStore:
    def __init__(
        self,
        dsn: str,
        embed: Optional[Callable[[str], Sequence[float]]] = None,
        dim: int = DEFAULT_DIM,
        count_tokens: Callable[[str], int] = estimate_tokens,
        summarize_after_tokens: int = 600,
        summarize_after_msgs: int = 12,
        extras: bool = False,
        tz: str = "UTC",
    ):
        # tz: timezone used to show message times to agents ("who said what, when" questions need them).
        self.tz = ZoneInfo(tz)
        # extras=True enables facts + pgvector retrieval and LISTEN/NOTIFY wakes (schema_extras.sql).
        # Default is the lean v1 core: messages, summaries, cursors, usage ledger, has_new gate.
        self.extras = extras
        self.dsn = dsn
        self.dim = dim
        self.embed = embed if embed is not None else HashEmbedder(dim)
        self.count_tokens = count_tokens
        self.summarize_after_tokens = summarize_after_tokens
        self.summarize_after_msgs = summarize_after_msgs

    def _connect(self):
        if psycopg is None:
            raise RuntimeError("the Postgres store needs psycopg (pip install psycopg[binary]); or use a SQLite file: --dsn path.db")
        return psycopg.connect(self.dsn, row_factory=dict_row)

    def _fmt_ts(self, ts) -> str:
        return ts.astimezone(self.tz).strftime("%a %b %d %-I:%M %p %Z") if ts is not None else ""

    # ---- setup
    def install(self) -> None:
        sql = SCHEMA_PATH.read_text()
        if self.extras:
            sql += "\n" + EXTRAS_SCHEMA_PATH.read_text().replace("__DIM__", str(self.dim))
        with self._connect() as conn:
            conn.execute(sql)

    def uninstall(self) -> None:
        with self._connect() as conn:
            conn.execute(
                "DROP TABLE IF EXISTS ctx_usage, ctx_facts, ctx_cursors, ctx_threads, ctx_messages CASCADE;"
                "DROP FUNCTION IF EXISTS ctx_notify_message() CASCADE;"
            )

    def _need_extras(self, what: str) -> None:
        if not self.extras:
            raise RuntimeError(f"{what} needs extras: construct ContextStore(..., extras=True) and install()")

    # ---- write path: messages
    def append_message(
        self, thread_id: str, author: str, body: str, external_id: Optional[str] = None, ts=None
    ) -> Optional[int]:
        """Store a message. Returns its id, or None if external_id was already ingested."""
        tokens = self.count_tokens(body)
        with self._connect() as conn:
            row = conn.execute(
                "INSERT INTO ctx_messages (thread_id, author, body, tokens, external_id, ts) "
                "VALUES (%s, %s, %s, %s, %s, COALESCE(%s, now())) "
                "ON CONFLICT (thread_id, external_id) DO NOTHING RETURNING id",
                (thread_id, author, body, tokens, external_id, ts),
            ).fetchone()
        return row["id"] if row else None

    # ---- read path
    def get_context(
        self,
        agent_id: str,
        thread_id: str,
        query: Optional[str] = None,
        k: int = 5,
        fact_scopes: Optional[Sequence[str]] = None,
        min_score: float = 0.0,
        only_new: bool = False,
        max_tail_tokens: Optional[int] = None,
        log: bool = True,
    ) -> Context:
        """Return the thin slice an agent needs: summary + unsummarized tail (+ top-k facts).

        only_new=True returns only tail messages newer than this agent's cursor, for agents that
        keep their own memory of earlier turns. max_tail_tokens keeps the most recent messages
        that fit and sets truncated=True.
        """
        with self._connect() as conn:
            conn.execute("INSERT INTO ctx_threads (thread_id) VALUES (%s) ON CONFLICT DO NOTHING", (thread_id,))
            t = conn.execute(
                "SELECT summary, summary_tokens, summarized_upto, version FROM ctx_threads WHERE thread_id=%s",
                (thread_id,),
            ).fetchone()
            cur = conn.execute(
                "SELECT last_seen FROM ctx_cursors WHERE agent_id=%s AND thread_id=%s", (agent_id, thread_id)
            ).fetchone()
            last_seen = cur["last_seen"] if cur else 0
            rows = conn.execute(
                "SELECT id, author, body, tokens, ts FROM ctx_messages "
                "WHERE thread_id=%s AND id > %s ORDER BY id",
                (thread_id, t["summarized_upto"]),
            ).fetchall()
            baseline = conn.execute(
                "SELECT COALESCE(SUM(tokens),0) AS n FROM ctx_messages WHERE thread_id=%s", (thread_id,)
            ).fetchone()["n"]

            tail_tokens = sum(r["tokens"] for r in rows)
            needs_summary = tail_tokens >= self.summarize_after_tokens or len(rows) >= self.summarize_after_msgs

            msgs = [
                Message(r["id"], r["author"], r["body"], r["tokens"], r["ts"], r["id"] > last_seen, self._fmt_ts(r["ts"]))
                for r in rows
            ]
            if only_new:
                msgs = [m for m in msgs if m.is_new]
            truncated = False
            if max_tail_tokens is not None:
                kept, used = [], 0
                for m in reversed(msgs):
                    if used + m.tokens > max_tail_tokens:
                        truncated = True
                        break
                    kept.append(m)
                    used += m.tokens
                msgs = list(reversed(kept))

            facts: list[dict] = []
            if query and k > 0:
                self._need_extras("fact retrieval (query=...)")
                scopes = list(fact_scopes) if fact_scopes else [thread_id, "global"]
                v = _vec_literal(self.embed(query))
                facts = conn.execute(
                    "SELECT id, kind, content, tokens, scope, 1 - (embedding <=> %s::vector) AS score "
                    "FROM ctx_facts WHERE superseded_by IS NULL AND scope = ANY(%s) "
                    "ORDER BY embedding <=> %s::vector LIMIT %s",
                    (v, scopes, v, k),
                ).fetchall()
                facts = [f for f in facts if f["score"] >= min_score]

            delivered = t["summary_tokens"] + sum(m.tokens for m in msgs) + sum(f["tokens"] for f in facts)
            if log:
                conn.execute(
                    "INSERT INTO ctx_usage (agent_id, thread_id, event, tokens_delivered, tokens_baseline) "
                    "VALUES (%s, %s, 'read', %s, %s)",
                    (agent_id, thread_id, delivered, baseline),
                )

        return Context(
            thread_id=thread_id,
            agent_id=agent_id,
            summary=t["summary"],
            summarized_upto=t["summarized_upto"],
            version=t["version"],
            messages=msgs,
            facts=facts,
            last_id=max([m.id for m in msgs], default=t["summarized_upto"]),
            needs_summary=needs_summary,
            truncated=truncated,
            tokens_delivered=delivered,
            tokens_baseline=baseline,
        )

    def has_new(self, agent_id: str, thread_id: str, ignore_authors: Optional[Sequence[str]] = None) -> int:
        """Cheap gate to run BEFORE waking a model: count messages past this agent's cursor,
        excluding the agent's own (and any other ignored authors)."""
        ignore = list(ignore_authors) if ignore_authors is not None else [agent_id]
        with self._connect() as conn:
            return conn.execute(
                "SELECT count(*) AS n FROM ctx_messages m WHERE m.thread_id=%s AND NOT (m.author = ANY(%s)) "
                "AND m.id > COALESCE((SELECT last_seen FROM ctx_cursors WHERE agent_id=%s AND thread_id=%s), 0)",
                (thread_id, ignore, agent_id, thread_id),
            ).fetchone()["n"]

    # ---- expand: pull exact detail from the raw store when the summary does not have it
    def search_messages(
        self, thread_id: str, query: str, limit: int = 5, agent_id: str = "expander", log: bool = True
    ) -> list[Message]:
        """Keyword search over a thread's raw messages: case-insensitive substring match on each query
        term, ranked by how many terms match, then recency; returned in chronological order. This is the
        fallback for specifics a summary dropped (PR numbers, commit hashes, branch names, versions,
        times). Lexical only and unindexed: fine for threads of a few hundred messages; add a trigram or
        full-text index before using it on much larger ones. Tokens returned are logged as 'expand' cost."""
        terms = _query_terms(query)
        if not terms:
            return []
        esc = lambda s: s.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")  # noqa: E731
        pats = [f"%{esc(t)}%" for t in terms]
        score = " + ".join(["(body ILIKE %s)::int"] * len(pats))
        with self._connect() as conn:
            rows = conn.execute(
                f"SELECT id, author, body, tokens, ts FROM ctx_messages WHERE thread_id=%s AND ({score}) > 0 "
                f"ORDER BY ({score}) DESC, id DESC LIMIT %s",
                [thread_id, *pats, *pats, limit],
            ).fetchall()
            return self._expanded(conn, rows, agent_id, thread_id, log)

    def get_messages(
        self, thread_id: str, ids: Sequence[int], agent_id: str = "expander", log: bool = True
    ) -> list[Message]:
        """Fetch specific raw messages by id (e.g. ids shown in a rendered context)."""
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT id, author, body, tokens, ts FROM ctx_messages WHERE thread_id=%s AND id = ANY(%s)",
                (thread_id, list(ids)),
            ).fetchall()
            return self._expanded(conn, rows, agent_id, thread_id, log)

    def _expanded(self, conn, rows, agent_id: str, thread_id: str, log: bool) -> list[Message]:
        msgs = [
            Message(r["id"], r["author"], r["body"], r["tokens"], r["ts"], True, self._fmt_ts(r["ts"]))
            for r in sorted(rows, key=lambda r: r["id"])
        ]
        if log and msgs:
            conn.execute(
                "INSERT INTO ctx_usage (agent_id, thread_id, event, tokens_delivered, tokens_baseline) "
                "VALUES (%s, %s, 'expand', %s, 0)",
                (agent_id, thread_id, sum(m.tokens for m in msgs)),
            )
        return msgs

    # ---- write path: commit what the agent learned
    def commit(
        self,
        agent_id: str,
        thread_id: str,
        *,
        upto: Optional[int] = None,
        new_summary: Optional[str] = None,
        expected_version: Optional[int] = None,
        facts: Sequence[Fact] = (),
        summarize_tokens: int = 0,
    ) -> dict:
        """Atomically: advance the agent's cursor, optionally replace the thread summary (with an
        optimistic version check), and store distilled facts. Raises StaleSummary on a lost race."""
        if facts:
            self._need_extras("storing facts")
        with self._connect() as conn:
            version = None
            if new_summary is not None:
                if upto is None:
                    raise ValueError("upto is required when new_summary is given")
                row = conn.execute(
                    "UPDATE ctx_threads SET summary=%s, summary_tokens=%s, "
                    "summarized_upto=GREATEST(summarized_upto, %s), version=version+1, updated_at=now() "
                    "WHERE thread_id=%s AND (%s::int IS NULL OR version=%s::int) RETURNING version",
                    (new_summary, self.count_tokens(new_summary), upto, thread_id, expected_version, expected_version),
                ).fetchone()
                if row is None:
                    conn.rollback()
                    raise StaleSummary(f"summary for {thread_id} changed since version {expected_version}")
                version = row["version"]
                if summarize_tokens:
                    conn.execute(
                        "INSERT INTO ctx_usage (agent_id, thread_id, event, tokens_delivered, tokens_baseline) "
                        "VALUES (%s, %s, 'summarize', %s, 0)",
                        (agent_id, thread_id, summarize_tokens),
                    )
            if upto is not None:
                conn.execute(
                    "INSERT INTO ctx_cursors (agent_id, thread_id, last_seen) VALUES (%s, %s, %s) "
                    "ON CONFLICT (agent_id, thread_id) DO UPDATE "
                    "SET last_seen = GREATEST(ctx_cursors.last_seen, EXCLUDED.last_seen), updated_at = now()",
                    (agent_id, thread_id, upto),
                )
            fact_ids = [self._insert_fact(conn, f, agent_id, thread_id) for f in facts]
        return {"version": version, "fact_ids": [i for i in fact_ids if i is not None]}

    def _insert_fact(self, conn, f: Fact, agent_id: str, thread_id: str) -> Optional[int]:
        scope = f.scope or thread_id
        dup = conn.execute(
            "SELECT id FROM ctx_facts WHERE scope=%s AND content=%s AND superseded_by IS NULL", (scope, f.content)
        ).fetchone()
        if dup:
            return None
        row = conn.execute(
            "INSERT INTO ctx_facts (scope, kind, content, tokens, embedding, author) "
            "VALUES (%s, %s, %s, %s, %s::vector, %s) RETURNING id",
            (scope, f.kind, f.content, self.count_tokens(f.content), _vec_literal(self.embed(f.content)), agent_id),
        ).fetchone()
        if f.supersedes is not None:
            conn.execute("UPDATE ctx_facts SET superseded_by=%s WHERE id=%s", (row["id"], f.supersedes))
        return row["id"]

    def ack(self, agent_id: str, thread_id: str, upto: int) -> None:
        """Advance a cursor without touching the summary (agent read, nothing to write back)."""
        self.commit(agent_id, thread_id, upto=upto)

    # ---- summary maintenance
    def maintain(
        self,
        thread_id: str,
        summarize_fn: Callable[[str, list[Message]], str],
        agent_id: str = "summarizer",
        force: bool = False,
    ) -> Optional[dict]:
        """Fold the unsummarized tail into the rolling summary when it has grown past the thresholds.
        summarize_fn(old_summary, tail_messages) -> new_summary (use a cheap model in production).
        The model tokens this costs are logged so net savings stay honest."""
        for _ in range(2):  # one retry on a lost race
            ctx = self.get_context(agent_id, thread_id, log=False)
            if not ctx.messages or not (ctx.needs_summary or force):
                return None
            new = summarize_fn(ctx.summary, ctx.messages)
            cost = self.count_tokens(ctx.summary) + sum(m.tokens for m in ctx.messages) + self.count_tokens(new)
            try:
                return self.commit(
                    agent_id, thread_id, upto=ctx.last_id, new_summary=new,
                    expected_version=ctx.version, summarize_tokens=cost,
                )
            except StaleSummary:
                continue
        return None

    # ---- event-driven wakes
    def listen(self, timeout: Optional[float] = None) -> Iterator[dict]:
        """Yield {'thread','id','author'} for each new message (Postgres LISTEN/NOTIFY).
        Agents sleep until this fires instead of polling; ends after `timeout` seconds of silence.
        Only useful for processes that hold a live connection; mention/routine-woken bots use has_new."""
        self._need_extras("listen()")
        conn = psycopg.connect(self.dsn, autocommit=True)
        try:
            conn.execute("LISTEN ctx_messages")
            for n in conn.notifies(timeout=timeout):
                yield json.loads(n.payload)
        finally:
            conn.close()

    # ---- measurement
    def usage_report(self, agent_id: Optional[str] = None) -> list[dict]:
        """Per-agent totals: tokens delivered vs. full-replay baseline, minus summarization cost."""
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT agent_id, "
                "count(*) FILTER (WHERE event='read') AS reads, "
                "COALESCE(sum(tokens_delivered) FILTER (WHERE event='read'),0) AS delivered, "
                "COALESCE(sum(tokens_baseline) FILTER (WHERE event='read'),0) AS baseline, "
                "COALESCE(sum(tokens_delivered) FILTER (WHERE event='summarize'),0) AS summarize_cost, "
                "COALESCE(sum(tokens_delivered) FILTER (WHERE event='expand'),0) AS expand_cost "
                "FROM ctx_usage WHERE (%s::text IS NULL OR agent_id=%s) GROUP BY agent_id ORDER BY agent_id",
                (agent_id, agent_id),
            ).fetchall()
        for r in rows:
            net = r["delivered"] + r["summarize_cost"] + r["expand_cost"]
            r["net_saved"] = r["baseline"] - net
            r["net_saved_pct"] = round(100 * r["net_saved"] / r["baseline"], 1) if r["baseline"] else 0.0
        return rows
