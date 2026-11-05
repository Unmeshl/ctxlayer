"""Tests against a real Postgres + pgvector. Run:  CTX_DSN=postgresql://... python3 test_ctxlayer.py
(also pytest-compatible)."""
import os
import threading
import time

from ctxlayer import ContextStore, Fact, StaleSummary
from summarizers import extractive_summarizer

DSN = os.environ.get("CTX_DSN", "postgresql://postgres@localhost:5433/ctxdb")


def fresh(extras: bool = False) -> ContextStore:
    s = ContextStore(DSN, extras=extras)
    s.uninstall()
    s.install()
    return s


def fill(s, thread, n, author="bao", prefix="update"):
    return [s.append_message(thread, author, f"{prefix} {i}: " + "detail " * 40) for i in range(n)]


def test_install_is_idempotent():
    for extras in (False, True):
        s = fresh(extras)
        s.install()  # second install must not fail


def test_external_id_dedupes():
    s = fresh()
    a = s.append_message("t", "bao", "hello", external_id="slack-1")
    b = s.append_message("t", "bao", "hello", external_id="slack-1")
    assert a is not None and b is None


def test_fresh_thread_returns_everything_and_flags_summary_need():
    s = fresh()
    fill(s, "t", 15)
    ctx = s.get_context("ada", "t")
    assert len(ctx.messages) == 15 and ctx.summary == ""
    assert ctx.needs_summary
    assert ctx.tokens_delivered == ctx.tokens_baseline  # nothing summarized yet, no saving yet


def test_maintain_then_read_is_thin():
    s = fresh()
    fill(s, "t", 100)
    base = s.get_context("ada", "t", log=False).tokens_baseline
    assert s.maintain("t", extractive_summarizer(400)) is not None
    ctx = s.get_context("ada", "t")
    assert ctx.summarized_upto > 0 and ctx.messages == []
    assert ctx.tokens_delivered <= 400 + 5
    assert ctx.tokens_delivered < base * 0.1
    assert "## Thread summary" in ctx.render()


def test_tail_after_summary_is_small():
    s = fresh()
    fill(s, "t", 30)
    s.maintain("t", extractive_summarizer(400))
    fill(s, "t", 3, author="cam", prefix="new")
    ctx = s.get_context("ada", "t")
    assert [m.author for m in ctx.messages] == ["cam"] * 3
    assert not ctx.needs_summary


def test_cursor_and_has_new_ignore_own_messages():
    s = fresh()
    ids = fill(s, "t", 3, author="ada")
    assert s.has_new("ada", "t") == 0  # only her own messages
    assert s.has_new("bao", "t") == 3
    s.ack("bao", "t", ids[-1])
    assert s.has_new("bao", "t") == 0
    new = s.append_message("t", "cam", "ping")
    assert s.has_new("bao", "t") == 1
    ctx = s.get_context("bao", "t", only_new=True)
    assert [m.id for m in ctx.messages] == [new]


def test_cursor_never_moves_backwards():
    s = fresh()
    ids = fill(s, "t", 5)
    s.ack("ada", "t", ids[4])
    s.ack("ada", "t", ids[1])
    assert s.has_new("ada", "t", ignore_authors=[]) == 0


def test_stale_summary_is_rejected():
    s = fresh()
    fill(s, "t", 5)
    ctx = s.get_context("a", "t")
    s.commit("a", "t", upto=ctx.last_id, new_summary="first", expected_version=ctx.version)
    try:
        s.commit("b", "t", upto=ctx.last_id, new_summary="second", expected_version=ctx.version)
        raise AssertionError("expected StaleSummary")
    except StaleSummary:
        pass
    assert s.get_context("a", "t").summary == "first"


def test_summarized_upto_never_regresses():
    s = fresh()
    ids = fill(s, "t", 6)
    c = s.get_context("a", "t")
    r = s.commit("a", "t", upto=ids[5], new_summary="late", expected_version=c.version)
    s.commit("b", "t", upto=ids[2], new_summary="early-but-newer-write", expected_version=r["version"])
    assert s.get_context("a", "t").summarized_upto == ids[5]


def test_max_tail_tokens_truncates_to_most_recent():
    s = fresh()
    ids = fill(s, "t", 10)
    ctx = s.get_context("ada", "t", max_tail_tokens=150)
    assert ctx.truncated and ctx.messages and ctx.messages[-1].id == ids[-1]
    assert sum(m.tokens for m in ctx.messages) <= 150


def test_core_has_no_extras_and_guards_them():
    s = fresh()
    with s._connect() as conn:
        n = conn.execute(
            "SELECT count(*) AS n FROM information_schema.tables WHERE table_name='ctx_facts'"
        ).fetchone()["n"]
        t = conn.execute("SELECT count(*) AS n FROM pg_trigger WHERE tgname='ctx_messages_notify'").fetchone()["n"]
    assert n == 0 and t == 0
    for call in (
        lambda: s.get_context("a", "t", query="x"),
        lambda: s.commit("a", "t", facts=[Fact("x")]),
        lambda: next(s.listen(timeout=0.1)),
    ):
        try:
            call()
            raise AssertionError("expected RuntimeError")
        except RuntimeError as e:
            assert "extras" in str(e)


def test_fact_retrieval_ranks_relevant_first_and_supersede_hides_old():
    s = fresh(extras=True)
    r = s.commit("bao", "acme", facts=[
        Fact("Launch date is Friday November 14", kind="decision"),
        Fact("Status page needs a screenshot of the audit report", kind="state"),
        Fact("The rate limit is the main constraint on long-running jobs", kind="fact"),
    ])
    assert len(r["fact_ids"]) == 3
    ctx = s.get_context("cam", "acme", query="when is the launch date", k=3)
    assert ctx.facts[0]["content"].startswith("Launch date")
    old = ctx.facts[0]["id"]
    s.commit("bao", "acme", facts=[Fact("Launch date moved to Friday November 21", kind="decision", supersedes=old)])
    ctx2 = s.get_context("cam", "acme", query="when is the launch date", k=3)
    texts = [f["content"] for f in ctx2.facts]
    assert any("November 21" in t for t in texts) and not any("November 14" in t for t in texts)


def test_fact_dedupe_and_global_scope():
    s = fresh(extras=True)
    s.commit("a", "t1", facts=[Fact("Always summarize before sleeping", scope="global")])
    r = s.commit("a", "t1", facts=[Fact("Always summarize before sleeping", scope="global")])
    assert r["fact_ids"] == []
    ctx = s.get_context("b", "other-thread", query="summarize before sleeping")
    assert len(ctx.facts) == 1


def test_listen_wakes_on_new_message():
    s = fresh(extras=True)
    got = []

    def listener():
        for ev in s.listen(timeout=5):
            got.append(ev)
            break

    th = threading.Thread(target=listener)
    th.start()
    time.sleep(0.5)
    mid = s.append_message("acme", "cam", "wake up")
    th.join(timeout=8)
    assert got and got[0]["thread"] == "acme" and got[0]["id"] == mid and got[0]["author"] == "cam"


def test_usage_report_nets_out_summarization_cost():
    s = fresh()
    fill(s, "t", 60)
    for _ in range(3):
        s.get_context("ada", "t")  # no summary yet: delivered == baseline
    s.maintain("t", extractive_summarizer(400))
    for _ in range(3):
        s.get_context("ada", "t")
    rows = {r["agent_id"]: r for r in s.usage_report()}
    b, m = rows["ada"], rows["summarizer"]
    assert b["reads"] == 6 and b["baseline"] > b["delivered"]
    assert m["summarize_cost"] > 0
    assert b["net_saved"] == b["baseline"] - b["delivered"]  # per-agent; cost is attributed to summarizer
    total_saved = sum(r["baseline"] - r["delivered"] - r["summarize_cost"] for r in rows.values())
    assert 0 < total_saved < b["baseline"]


def test_render_includes_message_time_in_store_tz():
    import datetime as dt
    s = ContextStore(DSN, tz="America/New_York")
    s.uninstall(); s.install()
    s.append_message("t", "bao", "verdict: PASS", ts=dt.datetime(2026, 10, 31, 1, 6, tzinfo=dt.timezone.utc))
    out = s.get_context("ada", "t").render()
    assert "Fri Oct 30 9:06 PM EDT" in out and "bao" in out


def test_search_messages_finds_identifiers_and_ranks_by_matches():
    s = fresh()
    a = s.append_message("t", "ada", "1.2.3 built from commit abc1234 on PR #9")
    s.append_message("t", "bao", "Reviewing the panel width tonight")
    c = s.append_message("t", "ada", "PR #8 carries 1.2.4, head release/v124")
    hits = s.search_messages("t", "which PR and commit was 1.2.3 built from? abc1234")
    assert [m.id for m in hits][-1] == a or hits[0].id == a      # the commit message is found
    assert all("panel width" not in m.body for m in hits)         # unrelated message is not
    assert [m.id for m in s.search_messages("t", "release/v124")] == [c]
    assert s.search_messages("t", "the of and") == []             # only filler: no terms, no query
    assert s.search_messages("t", "100%_literal") == []           # LIKE wildcards are escaped, not interpreted


def test_get_messages_and_expand_cost_in_ledger():
    s = fresh()
    ids = fill(s, "t", 20)
    s.maintain("t", extractive_summarizer(400), force=True)
    s.get_context("ada", "t")
    got = s.get_messages("t", [ids[3], ids[1]], agent_id="ada")
    assert [m.id for m in got] == sorted([ids[3], ids[1]])
    rows = {r["agent_id"]: r for r in s.usage_report()}
    assert rows["ada"]["expand_cost"] == sum(m.tokens for m in got) > 0
    assert rows["ada"]["net_saved"] == rows["ada"]["baseline"] - rows["ada"]["delivered"] - rows["ada"]["expand_cost"]


if __name__ == "__main__":
    tests = [(n, f) for n, f in sorted(globals().items()) if n.startswith("test_") and callable(f)]
    failed = 0
    for name, fn in tests:
        try:
            fn()
            print(f"PASS  {name}")
        except Exception as e:  # noqa: BLE001
            failed += 1
            print(f"FAIL  {name}: {type(e).__name__}: {e}")
    print(f"\n{len(tests) - failed}/{len(tests)} passed")
    raise SystemExit(1 if failed else 0)
