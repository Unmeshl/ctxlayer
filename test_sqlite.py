"""SQLite backend tests: no server needed. Reuses the Postgres core tests that apply to the core (no extras),
run against a SQLite file, plus SQLite-specific checks. Run:  python3 -m pytest test_sqlite.py"""
import os
import subprocess
import sys
import tempfile

import pytest

import test_ctxlayer as T
from briefs import BriefStore  # noqa: F401
from ctxlayer import StaleSummary
from sqlite_store import SqliteBriefStore, SqliteStore, open_store

HERE = os.path.dirname(os.path.abspath(__file__))


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
