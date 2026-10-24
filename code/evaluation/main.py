"""Score an output.csv against the sample gold answers, or check its invariants (read-only).

    python3 code/evaluation/main.py [--pred output.csv] [--gold dataset/sample_output.csv]
    python3 code/evaluation/main.py --invariants [--pred output.csv] [--requests <csv>]
        [--dataset dataset] [--diagnostics runs/diagnostics.jsonl] [--allow-stub]
"""
import argparse
import sys
from pathlib import Path

CODE_DIR = Path(__file__).resolve().parents[1]
if str(CODE_DIR) not in sys.path:
    sys.path.insert(0, str(CODE_DIR))

from evaluation import scoring  # noqa: E402
from ghost import invariants  # noqa: E402

REPO_ROOT = CODE_DIR.parent


def run_invariants(args):
    try:
        res = invariants.check_file(args.pred, args.requests, args.dataset, args.diagnostics,
                                    args.allow_stub)
    except (OSError, ValueError) as e:
        print(f"error: {e}", file=sys.stderr)
        return 2
    print(invariants.format_report(res, str(args.pred)))
    return 1 if res.violations else 0


def main(argv=None):
    p = argparse.ArgumentParser(description="Strict and relaxed scoring of output.csv vs gold")
    p.add_argument("--pred", type=Path, default=REPO_ROOT / "output.csv")
    p.add_argument("--gold", type=Path, default=REPO_ROOT / "dataset" / "sample_output.csv")
    p.add_argument("--invariants", action="store_true",
                   help="check the output invariants (DESIGN §8.2) instead of scoring")
    p.add_argument("--requests", type=Path, default=REPO_ROOT / "dataset" / "deletion_requests.csv",
                   help="request file giving the expected case_ids and audit_at (invariants)")
    p.add_argument("--dataset", type=Path, default=REPO_ROOT / "dataset",
                   help="dataset dir for the cited-evidence time check (invariants)")
    p.add_argument("--diagnostics", type=Path,
                   help="diagnostics.jsonl; its guessed_account_uncited events excuse uncited steps")
    p.add_argument("--allow-stub", action="store_true",
                   help="accept deletion rows with [] (stub output); never use for the final run")
    args = p.parse_args(argv)
    if args.invariants:
        return run_invariants(args)
    try:
        result = scoring.score_files(args.gold, args.pred)
    except scoring.InputError as e:
        print(f"error: {e}", file=sys.stderr)
        return 2
    print(scoring.format_report(result))
    return 0


if __name__ == "__main__":
    sys.exit(main())
