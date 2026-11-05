"""Per-run usage baseline from a Grok Bot usage export (CSV). Prints aggregates only: no ids, no emails.

  python3 usage_baseline.py export.csv [--product grok-bot] [--since YYYY-MM-DD] [--gap-min 5]

A "run" is a burst of billed steps separated by gaps longer than --gap-min minutes. Each CSV row is one model
step. Tokens per step = cached prompt + fresh prompt + cache write + completion. Files may overlap; rows are
deduplicated by Billing Item ID. Prints runs per day, steps and tokens per run, median step size, share of
tokens in the largest runs, and cached share. The script does not include anyone's export.
"""
from __future__ import annotations

import argparse
import csv
import json
import statistics
import sys
from collections import Counter
from datetime import datetime, timedelta

FIELDS = ("cached-prompt-text-tokens", "prompt-text-tokens", "cache-write-text-tokens", "completion-text-tokens")


def load(paths, product, since):
    seen, steps = set(), []
    for p in paths:
        with open(p, newline="", encoding="utf-8") as f:
            for r in csv.DictReader(f):
                if product and r.get("Product") != product:
                    continue
                if r["Billing Item ID"] in seen:
                    continue
                seen.add(r["Billing Item ID"])
                t = datetime.fromisoformat(r["Event Time"].replace("Z", "+00:00"))
                if since and t.date() < since:
                    continue
                try:
                    u = {k: int(v) for k, v in json.loads(r["Usage"]).items()}
                except (ValueError, TypeError):
                    continue
                steps.append({"t": t, "u": u, "cost": float(r.get("Pricing Contracted") or 0)})
    steps.sort(key=lambda s: s["t"])
    return steps


def runs_of(steps, gap):
    runs, cur = [], []
    for s in steps:
        if cur and s["t"] - cur[-1]["t"] > gap:
            runs.append(cur)
            cur = []
        cur.append(s)
    if cur:
        runs.append(cur)
    return runs


def total(s):
    return sum(s["u"].get(k, 0) for k in FIELDS)


def pct(xs, q):
    xs = sorted(xs)
    return xs[min(len(xs) - 1, int(q * len(xs)))] if xs else 0


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("files", nargs="+")
    ap.add_argument("--product", default="grok-bot")
    ap.add_argument("--since", help="YYYY-MM-DD")
    ap.add_argument("--gap-min", type=float, default=5)
    a = ap.parse_args(argv)
    since = datetime.fromisoformat(a.since).date() if a.since else None
    steps = load(a.files, a.product, since)
    if not steps:
        print("no matching rows", file=sys.stderr)
        return 1
    runs = runs_of(steps, timedelta(minutes=a.gap_min))
    tok = [total(s) for s in steps]
    run_tok = sorted((sum(total(s) for s in r) for r in runs), reverse=True)
    run_steps = [len(r) for r in runs]
    all_tok = sum(tok)
    cached = sum(s["u"].get("cached-prompt-text-tokens", 0) for s in steps)
    days = Counter(s["t"].date() for s in steps)
    runs_day = Counter(r[0]["t"].date() for r in runs)
    top = max(1, round(len(runs) * 0.05)) if len(runs) >= 20 else min(10, len(runs))
    out = {
        "period": f"{steps[0]['t'].date()} to {steps[-1]['t'].date()}",
        "active_days": len(days),
        "steps": len(steps),
        "runs": len(runs),
        "runs_per_active_day": round(len(runs) / len(days), 1),
        "steps_per_run": {"median": statistics.median(run_steps), "mean": round(statistics.mean(run_steps), 1), "max": max(run_steps)},
        "tokens_per_run": {"median": int(statistics.median(run_tok)), "mean": int(statistics.mean(run_tok)), "max": run_tok[0]},
        "step_size_tokens": {"median": int(statistics.median(tok)), "p90": pct(tok, 0.9), "over_200k": sum(1 for x in tok if x > 200_000)},
        "cached_share_pct": round(100 * cached / all_tok, 1),
        "share_in_largest_runs_pct": {f"top {top}": round(100 * sum(run_tok[:top]) / all_tok, 1)},
        "runs_over_100_steps": sum(1 for n in run_steps if n > 100),
        "list_cost_usd": round(sum(s["cost"] for s in steps), 2),
        "busiest_day_runs": max(runs_day.values()),
    }
    print(json.dumps(out, indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
