"""Pluggable summarizers for ContextStore.maintain().

A summarizer is any callable: (old_summary: str, new_messages: list[Message]) -> str
"""
from __future__ import annotations

from typing import Callable

from ctxlayer import Message, estimate_tokens


def extractive_summarizer(budget_tokens: int = 400, line_chars: int = 160) -> Callable[[str, list[Message]], str]:
    """Deterministic, free, offline summarizer for tests and benchmarks: keeps one trimmed line per
    message and drops the oldest lines to stay inside the budget. It is NOT a quality summarizer;
    it just has the right size/shape so the token model is realistic."""

    def fn(old_summary: str, messages: list[Message]) -> str:
        lines = [l for l in old_summary.split("\n") if l.strip()]
        for m in messages:
            body = " ".join(m.body.split())
            lines.append(f"{m.author}: {body[:line_chars]}")
        while len(lines) > 1 and estimate_tokens("\n".join(lines)) > budget_tokens:
            lines.pop(0)
        return "\n".join(lines)

    return fn


SUMMARY_PROMPT = """You maintain a running summary of a team thread for other AI agents. They will read ONLY this \
summary plus the newest messages, so it must stand alone. Update the summary with the new messages.

Rules:
1. Write each topic's CURRENT state on its own line, as a present-tense fact.
2. If something changed (a bug fixed, a decision reversed, a plan replaced), put the old state ONLY on a separate \
line in exactly this form: "Was: <old> | Now: <new>". Never write old behavior as a bare statement, and never use \
arrows or shorthand that could be read as current.
3. Keep exact identifiers for anything still relevant, and for build/PR history: PR numbers, commit hashes, branch \
names (head and base), versions, test counts, file paths, times.
4. Say who said or decided things. Never assign an owner, deadline or decision the thread does not state. If a \
question was asked and nobody answered, write "Open: <question> (asked by <name>)".
5. Drop greetings, chatter, and anything fully superseded that no longer matters.
Stay under {budget} tokens. Output only the summary.

CURRENT SUMMARY:
{old}

NEW MESSAGES:
{transcript}"""

COMPRESS_PROMPT = """This running thread summary is about {have} tokens; it must be under {budget}. Compress it. \
Remove the lowest-value detail first. Keep every rule it follows: current state on its own line, old state only on \
"Was: ... | Now: ..." lines, all exact identifiers (PR numbers, commit hashes, branch names, versions, counts, file \
paths, times), and who said or decided what. Output only the compressed summary.

SUMMARY:
{text}"""


def _complete(client, model: str, prompt: str, cap: int) -> str:
    """One model call. Thinking can use part of the output cap, so on truncation retry once with double the
    cap, then raise: a truncated summary must never be committed to the shared store."""
    for c in (cap, 2 * cap):
        resp = client.messages.create(model=model, max_tokens=c, messages=[{"role": "user", "content": prompt}])
        if resp.stop_reason != "max_tokens":
            text = "".join(b.text for b in resp.content if getattr(b, "type", None) == "text").strip()
            if not text:
                raise RuntimeError("summarizer returned no text block")
            return text
    raise RuntimeError(f"summarizer truncated even at max_tokens={2 * cap}")


def make_anthropic_summarizer(
    model: str = "claude-haiku-5-5",
    budget_tokens: int = 400,
    max_output_tokens: int = 8000,
    enforce: bool = True,
    tolerance: float = 1.25,
    max_compress: int = 2,
) -> Callable[[str, list[Message]], str]:
    """LLM summarizer using a small model.

    The budget is advisory to the model (it routinely overshoots), so with enforce=True a draft longer than
    budget_tokens * tolerance is sent back for compression, up to max_compress times. The returned function has a
    `.stats` dict (calls, compress_calls, extra_tokens spent on compression, over_budget_final) so the extra
    cost and any remaining overshoot are visible. Requires an API key; token counts are chars/4 estimates."""
    stats = {"calls": 0, "compress_calls": 0, "extra_tokens": 0, "over_budget_final": 0}

    def fn(old_summary: str, messages: list[Message]) -> str:
        import anthropic  # lazy import: core library has no LLM dependency

        transcript = "\n".join(f"[{m.author}{' ' + m.when if m.when else ''}] {m.body}" for m in messages)
        client = anthropic.Anthropic()
        text = _complete(
            client, model,
            SUMMARY_PROMPT.format(budget=budget_tokens, old=old_summary or "(none yet)", transcript=transcript),
            max_output_tokens,
        )
        stats["calls"] += 1
        if enforce:
            for _ in range(max_compress):
                have = estimate_tokens(text)
                if have <= budget_tokens * tolerance:
                    break
                shorter = _complete(
                    client, model, COMPRESS_PROMPT.format(have=have, budget=budget_tokens, text=text), max_output_tokens
                )
                stats["compress_calls"] += 1
                stats["extra_tokens"] += have + estimate_tokens(shorter)
                if estimate_tokens(shorter) >= have:
                    break
                text = shorter
            if estimate_tokens(text) > budget_tokens * tolerance:
                stats["over_budget_final"] += 1
        return text

    fn.stats = stats  # type: ignore[attr-defined]
    return fn
