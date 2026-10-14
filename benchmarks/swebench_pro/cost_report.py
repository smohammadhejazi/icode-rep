"""Token usage and cost of a run, from iCode's saved trajectories.

Sums `model.exchange.finished` usage over every events.jsonl under each
instance's `chrys/` (main agent and sub-agents). iCode's Anthropic adapter
folds cache reads and writes into `input_total`, so uncached input is
input_total - cache_read - cache_creation. Writes runs/<run_id>/cost.json.

    uv run python cost_report.py --run-id <run_id>
"""

import argparse
import json
from collections import defaultdict
from pathlib import Path

HERE = Path(__file__).resolve().parent
# USD per million tokens (5-minute cache writes), by request model.
PRICES = {
    "claude-haiku-4-5-20251001": {"input": 1.00, "cache_creation": 1.25, "cache_read": 0.10, "output": 5.00},
    "claude-opus-4-8": {"input": 5.00, "cache_creation": 6.25, "cache_read": 0.50, "output": 25.00},
}
BUCKETS = ("input", "cache_creation", "cache_read", "output")


def instance_usage(chrys: Path) -> dict:
    by_model = defaultdict(lambda: dict.fromkeys(("calls", *BUCKETS), 0))
    for events in chrys.rglob("events.jsonl"):
        for line in events.open(encoding="utf-8"):
            event = json.loads(line)
            if event.get("event_type") != "model.exchange.finished":
                continue
            payload = event["payload"]
            usage = (payload.get("usage") or {}).get("normalized") or {}
            read, write = usage.get("cache_read", 0), usage.get("cache_creation", 0)
            # Sub-agent exchanges record only the model that answered.
            total = by_model[payload.get("request_model") or payload.get("response_model") or "?"]
            total["calls"] += 1
            total["cache_read"] += read
            total["cache_creation"] += write
            total["input"] += usage.get("input_total", 0) - read - write
            total["output"] += usage.get("output_total", 0)
    cost = 0.0
    for model, total in by_model.items():
        if model not in PRICES:
            raise SystemExit(f"No prices for {model}; add them to PRICES")
        cost += sum(total[k] * PRICES[model][k] for k in BUCKETS) / 1e6
    return {"by_model": dict(by_model), "cost_usd": round(cost, 4)}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--run-id", required=True)
    args = parser.parse_args()
    run_dir = HERE / "runs" / args.run_id
    per_instance = {d.name: instance_usage(d / "chrys") for d in sorted((run_dir / "instances").iterdir())}
    totals = dict.fromkeys(("calls", *BUCKETS), 0)
    for usage in per_instance.values():
        for total in usage["by_model"].values():
            for k in totals:
                totals[k] += total[k]
    totals["cost_usd"] = round(sum(u["cost_usd"] for u in per_instance.values()), 4)
    totals["instances"] = len(per_instance)
    (run_dir / "cost.json").write_text(json.dumps({"prices_per_mtok_usd": PRICES, "totals": totals,
                                                   "per_instance": per_instance}, indent=2))
    print(json.dumps(totals, indent=2))


if __name__ == "__main__":
    main()
