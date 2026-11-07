"""SQLite backend tests: no server needed. Reuses the Postgres core tests that apply to the core (no extras),
run against a SQLite file, plus SQLite-specific checks. Run:  python3 -m pytest test_sqlite.py"""
import json
import os
import sqlite3
import subprocess
import sys
import tempfile

import pytest

import test_ctxlayer as T
from briefs import BriefStore  # noqa: F401
from ctxlayer import StaleSummary
from sqlite_store import SqliteBriefStore, SqliteStore, open_store

HERE = os.path.dirname(os.path.abspath(__file__))


def cli(dsn, *args, stdin=None):
    """Run the shipped CLI against one SQLite path. No network."""
    return subprocess.run(
        [sys.executable, os.path.join(HERE, "cli.py"), "--dsn", dsn, *args],
        input=stdin, capture_output=True, text=True, cwd=HERE,
    )


@pytest.fixture(autouse=True)
def sqlite_backend(tmp_path, monkeypatch):
    path = str(tmp_path / "ctx.db")
    monkeypatch.setattr(T, "DSN", path)
    monkeypatch.setattr(T, "ContextStore", SqliteStore)
    return path


# core behavior shared with the Postgres tests
def test_external_id_dedupes():
    T.test_external_id_dedupes()


def test_fresh_thread_returns_everything_and_flags_summary_need():
    T.test_fresh_thread_returns_everything_and_flags_summary_need()


def test_maintain_then_read_is_thin():
    T.test_maintain_then_read_is_thin()


def test_tail_after_summary_is_small():
    T.test_tail_after_summary_is_small()


def test_cursor_and_has_new_ignore_own_messages():
    T.test_cursor_and_has_new_ignore_own_messages()


def test_cursor_never_moves_backwards():
    T.test_cursor_never_moves_backwards()


def test_stale_summary_is_rejected():
    T.test_stale_summary_is_rejected()


def test_summarized_upto_never_regresses():
    T.test_summarized_upto_never_regresses()


def test_max_tail_tokens_truncates_to_most_recent():
    T.test_max_tail_tokens_truncates_to_most_recent()


def test_usage_report_nets_out_summarization_cost():
    T.test_usage_report_nets_out_summarization_cost()


def test_render_includes_message_time_in_store_tz():
    T.test_render_includes_message_time_in_store_tz()


def test_search_messages_finds_identifiers_and_ranks_by_matches():
    T.test_search_messages_finds_identifiers_and_ranks_by_matches()


def test_get_messages_and_expand_cost_in_ledger():
    T.test_get_messages_and_expand_cost_in_ledger()


# SQLite-specific
def test_install_is_idempotent_and_extras_are_refused(sqlite_backend):
    s = SqliteStore(sqlite_backend)
    s.install()
    s.install()
    with pytest.raises(RuntimeError):
        SqliteStore(sqlite_backend, extras=True)
    with pytest.raises(RuntimeError):
        s.listen()


def test_open_store_picks_backend_from_the_dsn(sqlite_backend):
    assert isinstance(open_store(sqlite_backend), SqliteStore)
    assert isinstance(open_store("sqlite:///" + sqlite_backend), SqliteStore)
    assert not isinstance(open_store("postgresql://x@localhost/y"), SqliteStore)


def test_has_new_with_empty_ignore_counts_everything(sqlite_backend):
    s = SqliteStore(sqlite_backend)
    s.install()
    s.append_message("t", "a", "one")
    s.append_message("t", "b", "two")
    assert s.has_new("a", "t", ignore_authors=[]) == 2
    assert s.has_new("a", "t") == 1


def test_two_stores_cannot_overwrite_each_others_summary(sqlite_backend):
    a, b = SqliteStore(sqlite_backend), SqliteStore(sqlite_backend)
    a.install()
    m = a.append_message("t", "x", "hello")
    v = a.get_context("a", "t", log=False).version
    a.commit("a", "t", upto=m, new_summary="from a", expected_version=v)
    with pytest.raises(StaleSummary):
        b.commit("b", "t", upto=m, new_summary="from b", expected_version=v)
    assert b.get_context("b", "t", log=False).summary == "from a"


def test_brief_ledger_on_sqlite(sqlite_backend):
    bs = SqliteBriefStore(sqlite_backend)
    bs.install()
    bs.add("p", "goal", "Ship the guard")
    r = bs.add("p", "requirement", "has-new exits 1 when nothing is new", accept="exit code is 1")
    q = bs.add("p", "open_question", "Which platform first?", blocking=True)
    assert bs.readiness("p")  # a blocking question is still open: gaps are listed
    bs.resolve(q, "Grok Bot")
    assert bs.readiness("p") == []
    brief = bs.render("p")
    assert "Ship the guard" in brief.text and str(r) in brief.text
    bs.add("p", "goal", "Ship the guard and the report")
    assert "Was:" in bs.render("p").text


def test_cli_end_to_end_on_a_sqlite_file(sqlite_backend):
    def run(*args, stdin=None):
        return subprocess.run([sys.executable, os.path.join(HERE, "cli.py"), "--dsn", sqlite_backend, *args],
                              input=stdin, capture_output=True, text=True, cwd=HERE)

    assert run("install").returncode == 0
    assert run("has-new", "--agent", "ada", "--thread", "t").returncode == 1
    assert run("ingest", "--thread", "t", "--author", "bao", "--external-id", "1", "--body", "hello").stdout.strip() == "1"
    assert run("ingest", "--thread", "t", "--author", "bao", "--external-id", "1", "--body", "hello").stdout.strip() == "duplicate"
    assert run("has-new", "--agent", "ada", "--thread", "t").returncode == 0
    assert "hello" in run("context", "--agent", "ada", "--thread", "t").stdout
    assert run("commit", "--agent", "ada", "--thread", "t", "--upto", "1").returncode == 0
    assert run("has-new", "--agent", "ada", "--thread", "t").returncode == 1
    assert run("search", "--agent", "ada", "--thread", "t", "--query", "hello").returncode == 0
    assert run("report").returncode == 0
    assert run("brief", "init").returncode == 0
    assert run("brief", "add", "--project", "p", "--kind", "goal", "--text", "g").returncode == 0
    assert run("brief", "ready", "--project", "p").returncode == 1  # no requirements yet


def test_cli_has_new_missing_parent_is_store_error_not_quiet():
    dsn = "/nonexistent/dir/x.db"
    r = cli(dsn, "has-new", "--agent", "ada", "--thread", "t")
    assert r.returncode == 3
    assert r.stdout == ""
    assert "Traceback" not in r.stderr
    assert "store error:" in r.stderr
    assert "unable to open database file" in r.stderr
    assert dsn not in r.stderr and dsn not in r.stdout


def test_cli_has_new_corrupt_db_is_store_error_not_quiet(tmp_path):
    db = tmp_path / "corrupt.db"
    db.write_bytes(b"not a sqlite database\n")
    r = cli(str(db), "has-new", "--agent", "ada", "--thread", "t")
    assert r.returncode == 3
    assert r.stdout == ""
    assert "Traceback" not in r.stderr
    assert "store error:" in r.stderr
    assert "not a database" in r.stderr
    assert str(db) not in r.stderr and str(db) not in r.stdout


def test_cli_has_new_uninstalled_db_is_store_error_not_quiet(tmp_path):
    db = tmp_path / "empty.db"
    sqlite3.connect(db).close()  # valid file, no ctxlayer schema
    r = cli(str(db), "has-new", "--agent", "ada", "--thread", "t")
    assert r.returncode == 3
    assert r.stdout == ""
    assert "Traceback" not in r.stderr
    assert "store error:" in r.stderr
    assert "no such table" in r.stderr
    assert str(db) not in r.stderr and str(db) not in r.stdout


def test_cli_has_new_does_not_hide_programming_errors(sqlite_backend, monkeypatch):
    import cli as cli_mod

    def boom(*_a, **_k):
        raise sqlite3.ProgrammingError("Incorrect number of bindings supplied")

    monkeypatch.setattr("sqlite_store.SqliteStore.has_new", boom)
    with pytest.raises(sqlite3.ProgrammingError):
        cli_mod.main(["--dsn", sqlite_backend, "has-new", "--agent", "ada", "--thread", "t"])


def test_cli_only_new_returns_messages_arrived_together(tmp_path):
    db = str(tmp_path / "slice.db")
    assert cli(db, "install").returncode == 0
    assert cli(db, "ingest", "--thread", "t", "--author", "bao", "--external-id", "m1",
               "--body", "alpha-one").stdout.strip() == "1"
    assert cli(db, "ingest", "--thread", "t", "--author", "bao", "--external-id", "m2",
               "--body", "alpha-two").stdout.strip() == "2"
    ctx = cli(db, "context", "--agent", "ada", "--thread", "t", "--only-new")
    assert ctx.returncode == 0
    assert ctx.stdout.index("alpha-one") < ctx.stdout.index("alpha-two")
    assert cli(db, "commit", "--agent", "ada", "--thread", "t", "--upto", "2").returncode == 0
    assert cli(db, "ingest", "--thread", "t", "--author", "bao", "--external-id", "m3",
               "--body", "beta-three").returncode == 0
    assert cli(db, "ingest", "--thread", "t", "--author", "bao", "--external-id", "m4",
               "--body", "beta-four").returncode == 0
    later = cli(db, "context", "--agent", "ada", "--thread", "t", "--only-new")
    assert later.stdout.index("beta-three") < later.stdout.index("beta-four")
    assert "alpha-one" not in later.stdout and "alpha-two" not in later.stdout


def test_cli_same_external_id_retry_is_duplicate_and_edit_is_ignored(tmp_path):
    """Known gap: a new body for an existing external id is not stored."""
    db = str(tmp_path / "dedupe.db")
    assert cli(db, "install").returncode == 0
    first = cli(db, "ingest", "--thread", "t", "--author", "bao", "--external-id", "same",
                "--body", "original-body")
    assert first.returncode == 0 and first.stdout.strip() == "1"
    again = cli(db, "ingest", "--thread", "t", "--author", "bao", "--external-id", "same",
                "--body", "original-body")
    assert again.returncode == 0 and again.stdout.strip() == "duplicate"
    edited = cli(db, "ingest", "--thread", "t", "--author", "bao", "--external-id", "same",
                 "--body", "edited-body")
    assert edited.returncode == 0 and edited.stdout.strip() == "duplicate"
    ctx = cli(db, "context", "--agent", "ada", "--thread", "t", "--only-new")
    assert "original-body" in ctx.stdout
    assert "edited-body" not in ctx.stdout
    assert sqlite3.connect(db).execute("SELECT count(*) FROM ctx_messages").fetchone()[0] == 1
    assert sqlite3.connect(db).execute("SELECT body FROM ctx_messages").fetchone()[0] == "original-body"


def test_cli_wake_context_read_does_not_advance_cursor(tmp_path):
    db = str(tmp_path / "wake.db")
    assert cli(db, "install").returncode == 0
    ingested = cli(db, "ingest", "--thread", "wake", "--author", "bao", "--external-id", "ext-1",
                   "--body", "synthetic-known-row")
    assert ingested.returncode == 0
    msg_id = ingested.stdout.strip()
    assert msg_id.isdigit()

    opened = cli(db, "has-new", "--agent", "ada", "--thread", "wake")
    assert opened.returncode == 0 and int(opened.stdout.strip()) >= 1

    ctx = cli(db, "context", "--agent", "ada", "--thread", "wake", "--only-new")
    assert ctx.returncode == 0 and "synthetic-known-row" in ctx.stdout
    stats = json.loads(ctx.stderr)
    assert str(stats["last_id"]) == msg_id

    # A second read still does not move the cursor: the gate stays open.
    again = cli(db, "context", "--agent", "ada", "--thread", "wake", "--only-new")
    assert "synthetic-known-row" in again.stdout
    still_open = cli(db, "has-new", "--agent", "ada", "--thread", "wake")
    assert still_open.returncode == 0
    cursor = sqlite3.connect(db).execute(
        "SELECT last_seen FROM ctx_cursors WHERE agent_id=? AND thread_id=?", ("ada", "wake")
    ).fetchone()
    assert cursor is None

    committed = cli(db, "commit", "--agent", "ada", "--thread", "wake", "--upto", msg_id)
    assert committed.returncode == 0
    quiet = cli(db, "has-new", "--agent", "ada", "--thread", "wake")
    assert quiet.returncode == 1 and quiet.stdout.strip() == "0"
    last_seen = sqlite3.connect(db).execute(
        "SELECT last_seen FROM ctx_cursors WHERE agent_id=? AND thread_id=?", ("ada", "wake")
    ).fetchone()[0]
    assert last_seen == int(msg_id)


def test_docs_say_runners_alert_unless_has_new_exits_0_or_1():
    readme = open(os.path.join(HERE, "README.md"), encoding="utf-8").read()
    skill = open(os.path.join(HERE, "skills", "ctxlayer-local", "SKILL.md"), encoding="utf-8").read()
    for text in (readme, skill):
        assert "anything other than 0 or 1" in text
        assert "exit 3" in text.lower() or "Exit 3" in text
        assert "alert" in text.lower()
    assert "known gap" in skill.lower() or "known gap" in readme.lower()
