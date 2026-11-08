"""Command-line access to ctxlayer for bots that don't hold a Postgres connection
(woken by Slack mentions or routines). Each call is one short-lived connection.

DSN comes from --dsn or the CTX_DSN environment variable.

  python3 cli.py install
  python3 cli.py ingest   --thread myproject --author agent2 --external-id 1730000000.123 --body "text"   (or body on stdin)
  python3 cli.py has-new  --agent agent1 --thread myproject        # exit 0 new, 1 quiet, 3 SQLite store error (gate)
  python3 cli.py context  --agent agent1 --thread myproject        # prompt-ready slice on stdout, token stats on stderr
  python3 cli.py commit   --agent agent1 --thread myproject --upto 123 [--summary-file s.txt --expected-version 4]
  python3 cli.py maintain --thread myproject [--force] [--summarizer extractive|anthropic]
  python3 cli.py search   --agent agent1 --thread myproject --query "error code abc1234" [--limit 5]   # raw-message fallback
  python3 cli.py expand   --agent agent1 --thread myproject --ids 12 57                                  # raw messages by id
  python3 cli.py report   [--agent agent1]

Requirements ledger and handoff brief (separate table; run `brief init` once):
  python3 cli.py brief init
  python3 cli.py brief add       --project myproject --kind requirement --text "retry once on a 409" --accept "..." [--blocking]
  python3 cli.py brief supersede --id 7 --text "new wording" [--accept "..."]
  python3 cli.py brief resolve   --id 9 [--answer "SQLite"]     # close an open question (answer becomes a decision)
  python3 cli.py brief complete  --id 7                          # also: brief drop --id 7
  python3 cli.py brief list      --project myproject
  python3 cli.py brief ready     --project myproject                 # exit 0 if ready to hand off, 1 with a list of gaps
  python3 cli.py brief render    --project myproject [--budget 1500] [--no-rules]
  python3 cli.py brief extract   --project myproject < chat.txt     # optional: ledger ops from a transcript (needs an API key)
"""
from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys

from ctxlayer import ContextStore, StaleSummary  # noqa: F401
from sqlite_store import open_brief_store, open_store


def _safe_store_message(exc: BaseException, dsn: str) -> str:
    """SQLite failure text for stderr. Never echo a path or a DSN."""
    msg = " ".join(str(exc).split()) or "database unavailable"
    if dsn and dsn in msg:
        msg = msg.replace(dsn, "database")
    if "://" in msg or "/" in msg or "\\" in msg:
        return "database unavailable"
    return msg


def _brief(a) -> int:
    from briefs import DEFAULT_RUN_RULES, BriefStore

    bs = open_brief_store(a.dsn)
    try:
        if a.bcmd == "init":
            bs.install(); print("installed (brief)")
        elif a.bcmd == "add":
            print(bs.add(a.project, a.kind, a.text, a.accept, a.blocking, a.source))
        elif a.bcmd == "supersede":
            print(bs.supersede(a.id, a.text, a.accept))
        elif a.bcmd == "resolve":
            d = bs.resolve(a.id, a.answer); print(d if d is not None else "closed")
        elif a.bcmd == "complete":
            bs.complete(a.id); print("done")
        elif a.bcmd == "drop":
            bs.drop(a.id); print("dropped")
        elif a.bcmd == "list":
            for i in bs.items(a.project):
                print(f"#{i['id']} [{i['kind']}] {i['text']}" + (f" (accept: {i['accept']})" if i["accept"] else "")
                      + (" (BLOCKING)" if i["blocking"] else ""))
        elif a.bcmd == "ready":
            problems = bs.readiness(a.project, a.budget)
            print("ready" if not problems else "\n".join(problems))
            return 0 if not problems else 1
        elif a.bcmd == "render":
            b = bs.render(a.project, a.budget, None if a.no_rules else DEFAULT_RUN_RULES)
            print(json.dumps({"tokens": b.tokens, "over_budget": b.over_budget, "omitted": b.omitted}), file=sys.stderr)
            print(b.text)
        elif a.bcmd == "extract":
            from briefs import make_anthropic_extractor
            text = open(a.file).read() if a.file else sys.stdin.read()
            r = bs.extract_and_apply(a.project, text, make_anthropic_extractor())
            print(json.dumps({"applied": len(r["applied"]), "rejected": r["rejected"]}))
    except (KeyError, ValueError) as e:
        print(f"error: {e}", file=sys.stderr)
        return 2
    return 0


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--dsn", default=os.environ.get("CTX_DSN"))
    sub = p.add_subparsers(dest="cmd", required=True)

    sub.add_parser("install").add_argument("--extras", action="store_true")

    s = sub.add_parser("ingest")
    s.add_argument("--thread", required=True)
    s.add_argument("--author", required=True)
    s.add_argument("--external-id")
    s.add_argument("--body", help="message text; read from stdin if omitted")

    s = sub.add_parser("has-new")
    s.add_argument("--agent", required=True)
    s.add_argument("--thread", required=True)
    s.add_argument("--ignore", nargs="*", help="authors to ignore (default: the agent itself)")

    s = sub.add_parser("context")
    s.add_argument("--agent", required=True)
    s.add_argument("--thread", required=True)
    s.add_argument("--only-new", action="store_true")
    s.add_argument("--max-tail-tokens", type=int)
    s.add_argument("--json", action="store_true", help="emit JSON instead of prompt text")

    s = sub.add_parser("commit")
    s.add_argument("--agent", required=True)
    s.add_argument("--thread", required=True)
    s.add_argument("--upto", type=int, required=True)
    s.add_argument("--summary-file")
    s.add_argument("--expected-version", type=int)

    s = sub.add_parser("maintain")
    s.add_argument("--thread", required=True)
    s.add_argument("--force", action="store_true")
    s.add_argument("--summarizer", choices=["extractive", "anthropic"], default="extractive")

    s = sub.add_parser("search")
    s.add_argument("--agent", required=True)
    s.add_argument("--thread", required=True)
    s.add_argument("--query", required=True)
    s.add_argument("--limit", type=int, default=5)

    s = sub.add_parser("expand")
    s.add_argument("--agent", required=True)
    s.add_argument("--thread", required=True)
    s.add_argument("--ids", type=int, nargs="+", required=True)

    sub.add_parser("report").add_argument("--agent")

    b = sub.add_parser("brief")
    bsub = b.add_subparsers(dest="bcmd", required=True)
    bsub.add_parser("init")
    x = bsub.add_parser("add")
    x.add_argument("--project", required=True)
    x.add_argument("--kind", required=True)
    x.add_argument("--text", required=True)
    x.add_argument("--accept")
    x.add_argument("--blocking", action="store_true")
    x.add_argument("--source")
    x = bsub.add_parser("supersede")
    x.add_argument("--id", type=int, required=True)
    x.add_argument("--text", required=True)
    x.add_argument("--accept")
    x = bsub.add_parser("resolve")
    x.add_argument("--id", type=int, required=True)
    x.add_argument("--answer")
    for name in ("complete", "drop"):
        bsub.add_parser(name).add_argument("--id", type=int, required=True)
    bsub.add_parser("list").add_argument("--project", required=True)
    x = bsub.add_parser("ready")
    x.add_argument("--project", required=True)
    x.add_argument("--budget", type=int, default=1500)
    x = bsub.add_parser("render")
    x.add_argument("--project", required=True)
    x.add_argument("--budget", type=int, default=1500)
    x.add_argument("--no-rules", action="store_true")
    x = bsub.add_parser("extract")
    x.add_argument("--project", required=True)
    x.add_argument("--file", help="transcript file; stdin if omitted")

    a = p.parse_args(argv)
    if not a.dsn:
        p.error("set --dsn or CTX_DSN")
    if a.cmd == "brief":
        return _brief(a)
    store = open_store(a.dsn, extras=getattr(a, "extras", False))

    if a.cmd == "install":
        store.install()
        print("installed" + (" (with extras)" if a.extras else ""))
    elif a.cmd == "ingest":
        body = a.body if a.body is not None else sys.stdin.read()
        mid = store.append_message(a.thread, a.author, body, external_id=a.external_id)
        print(mid if mid is not None else "duplicate")
    elif a.cmd == "has-new":
        try:
            n = store.has_new(a.agent, a.thread, ignore_authors=a.ignore)
        except (sqlite3.ProgrammingError, sqlite3.NotSupportedError):
            raise  # bad SQL is a bug; do not report it as a quiet thread or a store outage
        except sqlite3.DatabaseError as e:
            # Missing parent, corrupt file, lock, I/O, or schema not installed. Not exit 1.
            print(f"store error: {_safe_store_message(e, a.dsn)}", file=sys.stderr)
            return 3
        print(n)
        return 0 if n else 1
    elif a.cmd == "context":
        ctx = store.get_context(a.agent, a.thread, only_new=a.only_new, max_tail_tokens=a.max_tail_tokens)
        stats = {"last_id": ctx.last_id, "version": ctx.version, "needs_summary": ctx.needs_summary,
                 "tokens_delivered": ctx.tokens_delivered, "tokens_baseline": ctx.tokens_baseline}
        print(json.dumps(stats), file=sys.stderr)
        if a.json:
            print(json.dumps({**stats, "summary": ctx.summary,
                              "messages": [{"id": m.id, "author": m.author, "body": m.body, "is_new": m.is_new}
                                           for m in ctx.messages]}))
        else:
            print(ctx.render())
    elif a.cmd == "commit":
        summary = open(a.summary_file).read() if a.summary_file else None
        try:
            r = store.commit(a.agent, a.thread, upto=a.upto, new_summary=summary, expected_version=a.expected_version)
        except StaleSummary as e:
            print(f"stale: {e}", file=sys.stderr)
            return 2
        print(json.dumps(r))
    elif a.cmd == "maintain":
        if a.summarizer == "anthropic":
            from summarizers import make_anthropic_summarizer
            fn = make_anthropic_summarizer()
        else:
            from summarizers import extractive_summarizer
            fn = extractive_summarizer()
        r = store.maintain(a.thread, fn, force=a.force)
        print(json.dumps(r) if r else "nothing to do")
    elif a.cmd in ("search", "expand"):
        if a.cmd == "search":
            found = store.search_messages(a.thread, a.query, limit=a.limit, agent_id=a.agent)
        else:
            found = store.get_messages(a.thread, a.ids, agent_id=a.agent)
        print("\n".join(m.line() for m in found) or "No matching messages.")
    elif a.cmd == "report":
        print(json.dumps(store.usage_report(a.agent), indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
