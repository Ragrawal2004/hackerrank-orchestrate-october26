"""Aggregate runs/usage.jsonl into the usage report (skeleton for T20).

Fails loudly (MissingPrice, non-zero exit) if any model in the usage log has no complete
price entry in ghost.config.PRICES. Prices are never assumed.

    python3 code/evaluation/usage_report.py [--usage runs/usage.jsonl] [--requests N]
                                            [--run-id ID] [--out FILE]
"""
import argparse
import json
import sys
from pathlib import Path

CODE_DIR = Path(__file__).resolve().parents[1]
if str(CODE_DIR) not in sys.path:
    sys.path.insert(0, str(CODE_DIR))

from ghost import config, loaders  # noqa: E402

PRICE_FIELDS = ("input_per_mtok", "output_per_mtok", "source_url", "as_of")


class MissingPrice(Exception):
    pass


def load_usage(path):
    with open(path, encoding="utf-8") as f:
        return [json.loads(line) for line in f if line.strip()]


def _check_prices(models, prices):
    missing = []
    for model in sorted(models):
        entry = prices.get(model)
        if entry is None:
            missing.append(f"{model}: no entry in config.PRICES")
            continue
        absent = [f for f in PRICE_FIELDS if entry.get(f) is None]
        if absent:
            missing.append(f"{model}: missing {', '.join(absent)}")
    if missing:
        raise MissingPrice("price table incomplete (fill config.PRICES): " + "; ".join(missing))


def _blank():
    return {"calls": 0, "cache_hits": 0, "errors": 0, "input_tokens": 0, "output_tokens": 0,
            "cost": 0.0}


def summarize(lines, prices, n_requests, run_id=None):
    if run_id is not None:
        lines = [l for l in lines if l.get("run_id") == run_id]
    _check_prices({l["model"] for l in lines}, prices)
    models = {}
    for l in lines:
        m = models.setdefault(l["model"], _blank())
        if l["cache_hit"]:
            m["cache_hits"] += 1
            continue
        m["calls"] += 1
        m["errors"] += 1 if l.get("error") else 0
        m["input_tokens"] += l["input_tokens"]
        m["output_tokens"] += l["output_tokens"]
    total = _blank()
    for model, m in models.items():
        p = prices[model]
        m["cost"] = (m["input_tokens"] / 1e6 * p["input_per_mtok"]
                     + m["output_tokens"] / 1e6 * p["output_per_mtok"])
        for k in total:
            total[k] += m[k]
    n = max(int(n_requests), 1)
    total["tokens_per_request"] = (total["input_tokens"] + total["output_tokens"]) / n
    total["cost_per_request"] = total["cost"] / n
    return {"models": models, "total": total, "n_requests": n_requests, "run_id": run_id}


def render_markdown(summary, prices):
    out = ["# Usage report", ""]
    out.append(f"Requests: {summary['n_requests']}"
               + (f" | run_id: {summary['run_id']}" if summary["run_id"] else ""))
    out += ["", "| Model | Calls | Cache hits | Errors | Input tokens | Output tokens | Cost (USD) |",
            "|---|---|---|---|---|---|---|"]
    for model, m in sorted(summary["models"].items()):
        out.append(f"| {model} | {m['calls']} | {m['cache_hits']} | {m['errors']} | "
                   f"{m['input_tokens']} | {m['output_tokens']} | {m['cost']:.4f} |")
    t = summary["total"]
    out.append(f"| **Total** | {t['calls']} | {t['cache_hits']} | {t['errors']} | "
               f"{t['input_tokens']} | {t['output_tokens']} | {t['cost']:.4f} |")
    out += ["", f"Average tokens per request: {t['tokens_per_request']:.1f}",
            f"Estimated cost per request (USD): {t['cost_per_request']:.6f}", "", "## Prices used", ""]
    for model in sorted(summary["models"]):
        p = prices[model]
        out.append(f"- {model}: input {p['input_per_mtok']} / output {p['output_per_mtok']} "
                   f"USD per million tokens (source: {p['source_url']}, as of {p['as_of']})")
    return "\n".join(out) + "\n"


def main(argv=None):
    ap = argparse.ArgumentParser(description="Render the LLM usage report")
    root = config.find_repo_root()
    ap.add_argument("--usage", type=Path, default=root / "runs" / "usage.jsonl")
    ap.add_argument("--requests", type=int,
                    help="request count for per-request averages (default: rows in deletion_requests.csv)")
    ap.add_argument("--run-id", help="only count lines from this run")
    ap.add_argument("--out", type=Path, help="write markdown here instead of stdout")
    args = ap.parse_args(argv)
    n = args.requests or len(loaders.load_requests(root / "dataset"))
    try:
        summary = summarize(load_usage(args.usage), config.PRICES, n, args.run_id)
    except MissingPrice as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    md = render_markdown(summary, config.PRICES)
    if args.out:
        args.out.write_text(md, encoding="utf-8")
    else:
        sys.stdout.write(md)
    return 0


if __name__ == "__main__":
    sys.exit(main())
