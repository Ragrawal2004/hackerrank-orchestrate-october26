"""Score an output.csv against the sample gold answers (read-only).

    python3 code/evaluation/main.py [--pred output.csv] [--gold dataset/sample_output.csv]
"""
import argparse
import sys
from pathlib import Path

CODE_DIR = Path(__file__).resolve().parents[1]
if str(CODE_DIR) not in sys.path:
    sys.path.insert(0, str(CODE_DIR))

from evaluation import scoring  # noqa: E402

REPO_ROOT = CODE_DIR.parent


def main(argv=None):
    p = argparse.ArgumentParser(description="Strict and relaxed scoring of output.csv vs gold")
    p.add_argument("--pred", type=Path, default=REPO_ROOT / "output.csv")
    p.add_argument("--gold", type=Path, default=REPO_ROOT / "dataset" / "sample_output.csv")
    args = p.parse_args(argv)
    try:
        result = scoring.score_files(args.gold, args.pred)
    except scoring.InputError as e:
        print(f"error: {e}", file=sys.stderr)
        return 2
    print(scoring.format_report(result))
    return 0


if __name__ == "__main__":
    sys.exit(main())
