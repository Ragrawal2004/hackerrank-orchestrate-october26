import argparse
import csv
import sys
from pathlib import Path

from . import config, loaders

HEADER = ["case_id", "deletion_status", "steps_json"]


def build_parser():
    p = argparse.ArgumentParser(prog="main.py", description="Ghost Data deletion-audit agent")
    p.add_argument("--dataset", type=Path, help="dataset directory (default: <repo>/dataset)")
    p.add_argument("--out", type=Path, help="output CSV (default: <repo>/output.csv)")
    p.add_argument("--cases", help="comma-separated case_ids to run (default: all)")
    p.add_argument("--link-fallback", choices=config.LINK_FALLBACK_VALUES)
    p.add_argument("--category-llm", choices=config.CATEGORY_LLM_VALUES)
    p.add_argument("--no-llm", action="store_true", help="never call an LLM")
    p.add_argument("--cold", action="store_true", help="ignore the LLM response cache")
    return p


def write_output(path, rows):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f, lineterminator="\n")
        w.writerow(HEADER)
        w.writerows(rows)


def main(argv=None):
    args = build_parser().parse_args(argv)
    try:
        cfg = config.build_config(args.link_fallback, args.category_llm, args.no_llm, args.cold)
    except ValueError as e:
        print(f"error: {e}", file=sys.stderr)
        return 2
    dataset = args.dataset or cfg.dataset_dir
    out = args.out or cfg.default_output
    requests = loaders.load_requests(dataset)
    if args.cases:
        wanted = {c.strip() for c in args.cases.split(",") if c.strip()}
        requests = [r for r in requests if r["case_id"] in wanted]
    requests.sort(key=lambda r: loaders.case_sort_key(r["case_id"]))
    # Stub: the pipeline is not built yet, so every case is NOT_APPLICABLE.
    write_output(out, [[r["case_id"], "NOT_APPLICABLE", "[]"] for r in requests])
    print(f"wrote {len(requests)} rows to {out}")
    return 0
