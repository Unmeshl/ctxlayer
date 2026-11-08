---
name: ctxlayer-local
description: Wake-and-run guard for the ctxlayer early beta on one local SQLite file. Use on every wake before reading a long thread. Run the has-new gate, then a thin context slice, then commit. Run maintain only when the context footer asks.
license: Apache-2.0
---

# ctxlayer-local

One local `.db` file. No Postgres, no MCP server, no UI. Python standard library only. Needs Python 3.10 or newer.

This package is an early beta. It makes no savings claim. `report` prints delivered tokens against a full-replay estimate, including this tool's own overhead, so you can check a run yourself.

Run every command from the package directory (the directory that contains `cli.py`). Pass `--dsn` on every call. The CLI reads `CTX_DSN` when `--dsn` is omitted. `has-new`, `context`, and `commit` also require `--agent` and `--thread`. `commit` also requires `--upto`. A command missing those flags exits with an argparse error. Do not guess the flags. For `has-new`, that exit is anything other than 0 or 1: wake and alert, and do not stop silently.

## 0. Preconditions

- `CTX_DSN` must be an absolute path ending in `.db`, or `sqlite:///` plus that path. If it is unset or not a `.db` path, stop and report `CTX_DSN not set`. Do not create a database and do not guess a path.
- Never print a DSN, password, or token in chat.

## 1. Gate

```
python3 cli.py --dsn "$CTX_DSN" has-new --agent <own id> --thread <thread>
```

- Exit 0: stdout is the count of new messages. Continue.
- Exit 1: nothing new. Stop. No acknowledgement and no "nothing new" message.
- Anything other than 0 or 1: wake and alert. Do not stop silently, and do not treat the thread as quiet. Exit 3 is a store error: the SQLite file is missing, unreadable, corrupt, locked, or has no schema. Stderr is one `store error:` line and does not include the database path.
- `--ignore` is optional. The default ignores this agent.

## 2. Read the slice

```
python3 cli.py --dsn "$CTX_DSN" context --agent <own id> --thread <thread> --only-new
```

Keep `last_id`, `version`, and `needs_summary` from the footer JSON on stderr. Every message past the cursor comes back in this one slice. Reading context does not advance the cursor. Only commit does.

Optional flags: `--json`, `--max-tail-tokens N`.

If a detail is missing:

```
python3 cli.py --dsn "$CTX_DSN" search --agent <own id> --thread <thread> --query "..." --limit 5
python3 cli.py --dsn "$CTX_DSN" expand --agent <own id> --thread <thread> --ids <id> <id>
```

## 3. Commit

```
python3 cli.py --dsn "$CTX_DSN" commit --agent <own id> --thread <thread> --upto <last_id from the context footer>
```

Exit 2 means the summary version is stale. Run step 2 once and retry this commit once. That retry counts as one failed attempt.

## 4. Maintain only when asked

Run this only when `needs_summary` in the footer was true:

```
python3 cli.py --dsn "$CTX_DSN" maintain --thread <thread>
```

The default summarizer is extractive and does not call a model. `--summarizer anthropic` needs an API key. `--force` only when someone explicitly asks.

## 5. Ingest

One ingest per handoff:

```
python3 cli.py --dsn "$CTX_DSN" ingest --thread <thread> --author <who> --external-id <id> --body "..."
```

The body may come from stdin when `--body` is omitted.

- For a Slack message, the external id is the message ts.
- Name the thread with a project slug, or `slack:<channel_id>:<thread_ts>`.
- A repeat of the same `--external-id` prints `duplicate` and exits 0. It does not write a second row. A different body for that same id is ignored too. The first body stays. Recording edits is a known gap.

## Run rules

- `has-new`: stop only on exit 1. Anything other than 0 or 1 means wake and alert, including exit 3.
- Stop after about 40 tool calls, or after 2 failed attempts on the same step, and hand back.
- One writer at a time. The SQLite lock waits up to 10 seconds.
- Not on SQLite: facts, pgvector, LISTEN/NOTIFY, and `install --extras`.
- Message times render in UTC. Convert them to America/New_York when you report them to the user.
- `python3 cli.py --dsn "$CTX_DSN" report` prints the per-agent ledger line.
