"""Tests for the requirements ledger and brief. Run:  CTX_DSN=postgresql://... python3 test_briefs.py"""
import os

from briefs import BriefStore, DEFAULT_RUN_RULES

DSN = os.environ.get("CTX_DSN", "postgresql://postgres@localhost:5433/ctxdb")


def fresh() -> BriefStore:
    s = BriefStore(DSN)
    s.uninstall()
    s.install()
    return s


def test_render_has_ids_sections_and_run_rules():
    s = fresh()
    s.add("vq", "goal", "Ship Live Assist v1")
    r = s.add("vq", "requirement", "Skip contact on 409", accept="409 skips to next contact")
    s.add("vq", "constraint", "Chrome extension only")
    b = s.render("vq")
    assert f"[#{r}] Skip contact on 409 (accept: 409 skips to next contact)" in b.text
    assert "## Goal" in b.text and "## Constraints" in b.text and DEFAULT_RUN_RULES in b.text
    assert not b.over_budget and b.tokens > 0


def test_supersede_renders_was_now_and_hides_old_as_bare_statement():
    s = fresh()
    r = s.add("vq", "requirement", "409 stops the session")
    new = s.supersede(r, "409 skips to next contact")
    b = s.render("vq")
    assert "Was: 409 stops the session | Now: 409 skips to next contact" in b.text
    assert b.text.count("409 stops the session") == 1  # only inside the Was line
    assert [i["id"] for i in s.items("vq")] == [new]


def test_new_goal_replaces_old_goal():
    s = fresh()
    s.add("vq", "goal", "Build dialer")
    s.add("vq", "goal", "Build dialer with Live Assist")
    goals = [i for i in s.items("vq") if i["kind"] == "goal"]
    assert len(goals) == 1 and "Live Assist" in goals[0]["text"]
    assert "Was: Build dialer | Now:" in s.render("vq").text


def test_readiness_gate_lists_problems_then_passes():
    s = fresh()
    assert "no goal" in s.readiness("vq") and "no requirements" in s.readiness("vq")
    s.add("vq", "goal", "g")
    r = s.add("vq", "requirement", "do x")
    q = s.add("vq", "open_question", "Which host?", blocking=True)
    probs = s.readiness("vq")
    assert any("no acceptance" in p for p in probs) and any("blocking question" in p for p in probs)
    s.supersede(r, "do x", accept="x works")
    s.resolve(q, answer="Studio")
    assert s.readiness("vq") == []
    assert "Which host? -> Studio" in s.render("vq").text


def test_budget_drops_done_then_was_but_never_requirements():
    s = fresh()
    s.add("vq", "goal", "g")
    for i in range(30):
        d = s.add("vq", "requirement", f"finished thing {i} " + "detail " * 10, accept="ok")
        s.complete(d)
    a = s.add("vq", "requirement", "keep me " + "detail " * 5, accept="ok")
    s.supersede(a, "keep me v2", accept="ok")
    b = s.render("vq", budget_tokens=300)
    assert "done items listed by id only" in b.omitted and "keep me v2" in b.text
    assert "finished thing 3 " not in b.text
    tiny = s.render("vq", budget_tokens=10)
    assert tiny.over_budget and "keep me v2" in tiny.text  # flagged, not truncated


def test_apply_ops_reports_rejects_and_extractor_hook():
    s = fresh()
    ops = [{"op": "add", "kind": "goal", "text": "g"}, {"op": "add", "kind": "bogus", "text": "x"},
           {"op": "complete", "id": 99999}, {"op": "wat"}]
    res = s.apply_ops("vq", ops)
    assert len(res["applied"]) == 1 and len(res["rejected"]) == 3

    def fake(transcript, existing):
        return [{"op": "supersede", "id": existing[0]["id"], "text": "g2"}]

    res = s.extract_and_apply("vq", "chat text", fake)
    assert res["applied"][0][0] == "supersede" and "Now: g2" in s.render("vq").text


def test_ops_cannot_touch_another_projects_items():
    s = fresh()
    other = s.add("other", "requirement", "not yours", accept="x")
    s.add("vq", "goal", "g")
    res = s.apply_ops("vq", [{"op": "complete", "id": other}, {"op": "supersede", "id": other, "text": "hijack"}])
    assert len(res["rejected"]) == 2 and not res["applied"]
    assert [i["text"] for i in s.items("other")] == ["not yours"]


def test_invalid_inputs():
    s = fresh()
    for bad in (lambda: s.add("vq", "nope", "x"), lambda: s.add("vq", "goal", "  "), lambda: s.complete(12345)):
        try:
            bad()
        except (ValueError, KeyError):
            continue
        raise AssertionError("expected an error")


if __name__ == "__main__":
    fails = 0
    tests = [(n, f) for n, f in sorted(globals().items()) if n.startswith("test_")]
    for n, f in tests:
        try:
            f(); print("PASS ", n)
        except Exception as e:  # noqa: BLE001
            fails += 1; print("FAIL ", n, repr(e))
    print(f"\n{len(tests) - fails}/{len(tests)} passed")
    raise SystemExit(1 if fails else 0)
