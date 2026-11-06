# ctxlayer

A small local layer that stops AI agents from waking up and re-reading everything. One SQLite file (or Postgres for teams), a free gate that says "nothing new, don't wake the model", a thin context slice for when there is something new, run limits, and a ledger that measures what it does.

**Status: early beta. We make no savings claim yet.** We are measuring it on our own bots and will publish the numbers, good or bad. If you try it, the `report` command shows what it delivered and what it cost, so you can check for yourself.

## What it does
- `has-new`: a free check before any model call. Exit 0 = something new, 1 = nothing new, so stop.
- `context --only-new`: the summary plus only the messages after this agent's cursor, not the whole thread.
- `commit`: moves the cursor (and optionally saves a summary, guarded against two agents overwriting each other).
- `search` / `expand`: pull exact detail from the raw messages when a summary dropped it.
- `report`: per-agent tokens delivered vs what a full replay would have cost, including this tool's own overhead.
- Run rules for agents: stop after about 40 tool calls or 2 failed attempts, send long output to files, don't re-read files.

## Claude Code

This repository is a self-hosted plugin marketplace. The plugin loads `skills/ctxlayer-local`. Add the marketplace once, then install the plugin:

```
claude plugin marketplace add Unmeshl/ctxlayer
claude plugin install ctxlayer@ctxlayer
```

On Claude Code 2.1.292 or later, one command does both:

```
claude plugin install ctxlayer --marketplace Unmeshl/ctxlayer
```

Inside a session on Claude Code 2.1.275 or later: `/plugin install ctxlayer --marketplace Unmeshl/ctxlayer`

## Install (no server)
Needs Python 3.10+ only.
```
python3 cli.py --dsn ctx.db install
python3 cli.py --dsn ctx.db ingest --thread myproject --author agent1 --external-id 1 --body "hello"
python3 cli.py --dsn ctx.db has-new --agent agent2 --thread myproject      # exit 0
python3 cli.py --dsn ctx.db context --agent agent2 --thread myproject --only-new
python3 cli.py --dsn ctx.db commit --agent agent2 --thread myproject --upto 1
python3 cli.py --dsn ctx.db has-new --agent agent2 --thread myproject      # exit 1
python3 cli.py --dsn ctx.db report
```
A path ending in `.db` selects SQLite. A `postgresql://` DSN selects Postgres (needs `pip install "psycopg[binary]"`; adds facts/pgvector and LISTEN/NOTIFY via `install --extras`).

## Measure your own usage
`python3 usage_baseline.py your-export.csv --since 2026-11-01` turns a Grok Bot usage export into per-run numbers (runs per day, steps per run, cached share, share in the largest runs). Aggregates only.

## Tests
`pip install pytest`, then `python3 -m pytest test_sqlite.py` (no server). Postgres tests need `CTX_DSN` and psycopg. The SQLite file of record for this package is 19 passed.

## Try it (reviewer)
The local SQLite smoke test requires no ctxlayer account or API key. Downloading dependencies may use the network. Optional Anthropic summarization and transcript extraction send selected content to Anthropic when enabled; they are not used in this test. From the directory that contains `cli.py`:

```
python3 -m pip install pytest
python3 -m pytest -q test_sqlite.py
python3 cli.py --dsn /tmp/ctx-review.db install
python3 cli.py --dsn /tmp/ctx-review.db ingest --thread myproject --author agent1 --external-id 1 --body "hello"
python3 cli.py --dsn /tmp/ctx-review.db has-new --agent agent2 --thread myproject
python3 cli.py --dsn /tmp/ctx-review.db context --agent agent2 --thread myproject --only-new
python3 cli.py --dsn /tmp/ctx-review.db commit --agent agent2 --thread myproject --upto 1
python3 cli.py --dsn /tmp/ctx-review.db has-new --agent agent2 --thread myproject
```

Pass looks like this: pytest prints `19 passed`; install prints `installed`; ingest prints `1`; the first `has-new` exits 0; context prints `hello`; commit exits 0; the second `has-new` exits 1.

In the app, after the plugin is installed from this repo, ask the agent to run `has-new` on a thread with nothing new. It should stop and not answer. Do not paste the database file, logs, or account details into a review or an issue.

## What this is for
The free plugin is the local file and the wake gate. A paid service is not part of this package and is not required to use it. We have no savings number to publish. If you try it, open one GitHub issue on this repo and say what you wanted to do and what worked. Do not include code, logs, or account details.

## Limits
- SQLite: one writer at a time (waits up to 10 seconds); not tested with many writers, on Windows, or on Python versions we did not run. Facts/pgvector and LISTEN/NOTIFY are Postgres-only.
- Token counts are estimates (characters / 4).
- Summaries cost model tokens. They are opt-in and only worth it on long threads.
- Search is unindexed; fine for threads of a few hundred messages.

License: Apache-2.0. Security: keep database paths, DSNs and API keys out of chat and out of the repo.
