"""Requirements ledger and handoff brief.

Workflow:
  1. Refine: capture requirements, constraints, decisions and open questions in a local ledger. Free (no model call)
     unless you use the optional extractor to pull them out of a chat transcript.
  2. Gate: readiness() lists what is missing before a coding agent should start.
  3. Hand off: render() produces a short deterministic brief (plus run rules) for a fresh chat or agent run.
  4. Return: complete() the items the agent reports done; supersede() anything that changed.

Superseded items show up only as "Was: X | Now: Y" lines under the new item, never as bare statements.
"""
from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from dataclasses import dataclass, field
from pathlib import Path

try:
    import psycopg
    from psycopg.rows import dict_row
except ImportError:  # pragma: no cover
    psycopg = None  # type: ignore[assignment]
    dict_row = None  # type: ignore[assignment]

from ctxlayer import estimate_tokens

SCHEMA_PATH = Path(__file__).with_name("schema_brief.sql")
KINDS = ("goal", "requirement", "constraint", "non_goal", "decision", "open_question")

DEFAULT_RUN_RULES = (
    "Run rules: work only on the requirements above. Stop and report after about 40 tool calls, or after 2 failed "
    "attempts at the same step. Write long output (logs, diffs, test runs) to a file and return only the failing "
    "lines or a one-line result. Do not re-read files you have already read. When finished, reply with: ids done, "
    "new decisions, open questions."
)


@dataclass
class Brief:
    text: str
    tokens: int
    over_budget: bool = False
    omitted: list[str] = field(default_factory=list)


class BriefStore:
    def __init__(self, dsn: str, count_tokens=estimate_tokens):
        self.dsn = dsn
        self.count_tokens = count_tokens

    def _connect(self):
        if psycopg is None:
            raise RuntimeError("the Postgres store needs psycopg (pip install psycopg[binary]); or use a SQLite file: --dsn path.db")
        return psycopg.connect(self.dsn, row_factory=dict_row)

    def install(self) -> None:
        with self._connect() as conn:
            conn.execute(SCHEMA_PATH.read_text())

    def uninstall(self) -> None:
        with self._connect() as conn:
            conn.execute("DROP TABLE IF EXISTS ctx_items CASCADE")

    # ---- writes
    def add(self, project: str, kind: str, text: str, accept: str | None = None, blocking: bool = False,
            source: str | None = None) -> int:
        if kind not in KINDS:
            raise ValueError(f"kind must be one of {KINDS}")
        text = text.strip()
        if not text:
            raise ValueError("text is empty")
        with self._connect() as conn:
            if kind == "goal":  # one active goal; a new one replaces the old with a Was/Now trail
                old = conn.execute("SELECT id FROM ctx_items WHERE project=%s AND kind='goal' AND status='active'",
                                   (project,)).fetchone()
                if old:
                    return self._supersede(conn, old["id"], text, accept, source)
            return conn.execute(
                "INSERT INTO ctx_items (project, kind, text, accept, blocking, source) VALUES (%s,%s,%s,%s,%s,%s) RETURNING id",
                (project, kind, text, accept, blocking, source)).fetchone()["id"]

    def _supersede(self, conn, item_id: int, text: str, accept: str | None, source: str | None) -> int:
        old = conn.execute("SELECT * FROM ctx_items WHERE id=%s AND status='active'", (item_id,)).fetchone()
        if not old:
            raise KeyError(f"no active item #{item_id}")
        conn.execute("UPDATE ctx_items SET status='superseded', updated_at=now() WHERE id=%s", (item_id,))
        return conn.execute(
            "INSERT INTO ctx_items (project, kind, text, accept, blocking, supersedes, source) VALUES (%s,%s,%s,%s,%s,%s,%s) RETURNING id",
            (old["project"], old["kind"], text.strip(), accept if accept is not None else old["accept"], old["blocking"],
             item_id, source)).fetchone()["id"]

    def supersede(self, item_id: int, text: str, accept: str | None = None, source: str | None = None) -> int:
        with self._connect() as conn:
            return self._supersede(conn, item_id, text, accept, source)

    def _set_status(self, item_id: int, status: str) -> None:
        with self._connect() as conn:
            n = conn.execute("UPDATE ctx_items SET status=%s, updated_at=now() WHERE id=%s AND status='active'",
                             (status, item_id)).rowcount
        if not n:
            raise KeyError(f"no active item #{item_id}")

    def complete(self, item_id: int) -> None:
        self._set_status(item_id, "done")

    def drop(self, item_id: int) -> None:
        self._set_status(item_id, "dropped")

    def resolve(self, item_id: int, answer: str | None = None, source: str | None = None) -> int | None:
        """Close an open question. With an answer, record it as a decision and return the decision id."""
        with self._connect() as conn:
            q = conn.execute("SELECT * FROM ctx_items WHERE id=%s AND kind='open_question' AND status='active'",
                             (item_id,)).fetchone()
        if not q:
            raise KeyError(f"no active open question #{item_id}")
        self._set_status(item_id, "done")
        if answer:
            return self.add(q["project"], "decision", f"{q['text']} -> {answer.strip()}", source=source)
        return None

    # ---- reads
    def items(self, project: str, status: str = "active") -> list[dict]:
        with self._connect() as conn:
            return conn.execute("SELECT * FROM ctx_items WHERE project=%s AND status=%s ORDER BY id",
                                (project, status)).fetchall()

    def _previous(self, conn, item: dict) -> str | None:
        return conn.execute("SELECT text FROM ctx_items WHERE id=%s", (item["supersedes"],)).fetchone()["text"] \
            if item["supersedes"] else None

    def render(self, project: str, budget_tokens: int = 1500, run_rules: str | None = DEFAULT_RUN_RULES,
               include_done: bool = True, as_of: str | None = None) -> Brief:
        with self._connect() as conn:
            active = conn.execute("SELECT * FROM ctx_items WHERE project=%s AND status='active' ORDER BY id",
                                  (project,)).fetchall()
            done = conn.execute("SELECT * FROM ctx_items WHERE project=%s AND status='done' AND kind='requirement' ORDER BY id",
                                (project,)).fetchall()
            prev = {i["id"]: self._previous(conn, i) for i in active if i["supersedes"]}
        by = lambda k: [i for i in active if i["kind"] == k]  # noqa: E731

        def line(i, with_was=True):
            s = f"- [#{i['id']}] {i['text']}"
            if i["accept"]:
                s += f" (accept: {i['accept']})"
            if i["blocking"]:
                s += " (BLOCKING)"
            if with_was and prev.get(i["id"]):
                s += f"\n  Was: {prev[i['id']]} | Now: {i['text']}"
            return s

        def build(show_done: bool, with_was: bool) -> str:
            stamp = as_of or datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
            out = [f"# Brief: {project}", f"Generated {stamp}. Dated items may be stale; check them before acting."]
            goal = by("goal")
            if goal:
                out += ["", "## Goal", line(goal[0], with_was)]
            for title, kind in (("Requirements", "requirement"), ("Constraints", "constraint"), ("Non-goals", "non_goal"),
                                ("Decisions", "decision"), ("Open questions", "open_question")):
                rows = by(kind)
                if rows:
                    out += ["", f"## {title}"] + [line(i, with_was) for i in rows]
            if done:
                if show_done:
                    out += ["", "## Done (do not redo)"] + [f"- [#{i['id']}] {i['text']}" for i in done]
                else:
                    out += ["", f"## Done (do not redo)", f"- {len(done)} requirement(s) already complete: " +
                            ", ".join(f"#{i['id']}" for i in done)]
            if run_rules:
                out += ["", run_rules]
            return "\n".join(out) + "\n"

        omitted: list[str] = []
        show_done, with_was = include_done, True
        text = build(show_done, with_was)
        if self.count_tokens(text) > budget_tokens and show_done and done:
            show_done = False
            omitted.append("done items listed by id only")
            text = build(show_done, with_was)
        if self.count_tokens(text) > budget_tokens and any(prev.values()):
            with_was = False
            omitted.append("Was/Now lines dropped")
            text = build(show_done, with_was)
        tokens = self.count_tokens(text)
        # Requirements, constraints, decisions and open questions are never dropped to fit a budget.
        return Brief(text=text, tokens=tokens, over_budget=tokens > budget_tokens, omitted=omitted)

    def readiness(self, project: str, budget_tokens: int = 1500) -> list[str]:
        problems = []
        act = self.items(project)
        if not [i for i in act if i["kind"] == "goal"]:
            problems.append("no goal")
        reqs = [i for i in act if i["kind"] == "requirement"]
        if not reqs:
            problems.append("no requirements")
        for r in reqs:
            if not r["accept"]:
                problems.append(f"requirement #{r['id']} has no acceptance criterion")
        for q in act:
            if q["kind"] == "open_question" and q["blocking"]:
                problems.append(f"blocking question #{q['id']} is open")
        b = self.render(project, budget_tokens)
        if b.over_budget:
            problems.append(f"brief is {b.tokens} tokens, over the {budget_tokens} budget")
        return problems

    # ---- apply structured ops (from a person or the extractor)
    def _owned(self, project: str, item_id) -> int:
        """Ops may only touch active items of their own project (an extractor can hallucinate ids)."""
        with self._connect() as conn:
            ok = conn.execute("SELECT 1 FROM ctx_items WHERE id=%s AND project=%s AND status='active'",
                              (int(item_id), project)).fetchone()
        if not ok:
            raise KeyError(f"no active item #{item_id} in project {project}")
        return int(item_id)

    def apply_ops(self, project: str, ops: list[dict], source: str | None = None) -> dict:
        applied, rejected = [], []
        for op in ops:
            try:
                name = op.get("op")
                if name in ("supersede", "resolve", "complete", "drop"):
                    op = {**op, "id": self._owned(project, op["id"])}
                if name == "add":
                    applied.append(("add", self.add(project, op["kind"], op["text"], op.get("accept"),
                                                    bool(op.get("blocking")), source)))
                elif name == "supersede":
                    applied.append(("supersede", self.supersede(int(op["id"]), op["text"], op.get("accept"), source)))
                elif name == "resolve":
                    applied.append(("resolve", self.resolve(int(op["id"]), op.get("answer"), source)))
                elif name == "complete":
                    self.complete(int(op["id"])); applied.append(("complete", int(op["id"])))
                elif name == "drop":
                    self.drop(int(op["id"])); applied.append(("drop", int(op["id"])))
                else:
                    rejected.append((op, "unknown op"))
            except (KeyError, ValueError, TypeError) as e:
                rejected.append((op, str(e)))
        return {"applied": applied, "rejected": rejected}

    def extract_and_apply(self, project: str, transcript: str, extractor, source: str | None = None) -> dict:
        existing = self.items(project)
        return self.apply_ops(project, extractor(transcript, existing), source)


EXTRACT_PROMPT = """You maintain a requirements ledger for a software project from a chat transcript. Output ONLY JSON: \
{{"ops": [...]}}. Each op is one of:
{{"op":"add","kind":"goal|requirement|constraint|non_goal|decision|open_question","text":"...","accept":"...or null","blocking":false}}
{{"op":"supersede","id":<existing id>,"text":"new wording","accept":"...or null"}}
{{"op":"resolve","id":<open question id>,"answer":"..."}}
{{"op":"complete","id":<requirement id>}}   (only if the transcript says it is finished)
{{"op":"drop","id":<id>}}                   (only if the transcript says it is no longer wanted)
Rules: record only what the transcript states. Do not invent requirements, acceptance criteria, owners or deadlines; \
use null for accept when none is stated. Use supersede when the transcript changes something already in the ledger \
(use its id) instead of adding a duplicate. A requirement is something to build or change; a decision is a choice already \
made; an open question is something unresolved (blocking=true only if work cannot start without the answer). \
Keep each text one short sentence with exact names, numbers and identifiers. If nothing changed, return {{"ops": []}}.

EXISTING LEDGER (active items):
{existing}

TRANSCRIPT:
{transcript}"""


def make_anthropic_extractor(model: str = "claude-haiku-5-5", max_output_tokens: int = 8000):
    """Optional: pull ledger ops out of a chat transcript with a small model (one call). Needs an API key."""
    from summarizers import _complete

    def fn(transcript: str, existing: list[dict]) -> list[dict]:
        import anthropic

        ex = "\n".join(f"#{i['id']} [{i['kind']}] {i['text']}" for i in existing) or "(empty)"
        text = _complete(anthropic.Anthropic(), model, EXTRACT_PROMPT.format(existing=ex, transcript=transcript),
                         max_output_tokens)
        m = re.search(r"\{.*\}", text, re.S)
        if not m:
            raise RuntimeError("extractor returned no JSON")
        ops = json.loads(m.group(0)).get("ops", [])
        if not isinstance(ops, list):
            raise RuntimeError("extractor ops is not a list")
        return ops

    return fn
