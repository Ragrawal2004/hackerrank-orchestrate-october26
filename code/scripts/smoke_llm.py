"""Manual smoke test of the real API path. Run it yourself; tests never import or run it.

    pip install -r requirements.txt
    python3 code/scripts/smoke_llm.py [--model MODEL]

Makes ONE tiny cold call through ghost.llm.call, prints the parsed reply and the usage line,
and reports only whether a key is present (never its value). Usage goes to runs/smoke/, not
runs/usage.jsonl, so it cannot pollute the final usage report.
"""
import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from ghost import config, envfile, llm  # noqa: E402

SCHEMA = {"type": "object", "required": ["ok"], "properties": {"ok": {"type": "boolean"}},
          "additionalProperties": False}


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--model", default=config.DEFAULT_INTENT_MODEL)
    args = ap.parse_args(argv)
    root = config.find_repo_root()
    envfile.load_dotenv(envfile.env_file_path(root))
    import os
    print("API key present:", bool(os.environ.get(llm.KEY_ENV)))
    runs = root / "runs" / "smoke"
    llm.configure(runs_dir=runs, cold=True, run_id="smoke")
    try:
        out = llm.call("smoke", args.model, 'Reply with exactly this JSON and nothing else: {"ok": true}',
                       "smoke", SCHEMA)
    except llm.LLMUnavailable as e:
        print("LLMUnavailable:", e)
        return 1
    print("parsed reply:", json.dumps(out))
    print("usage line:", (runs / "usage.jsonl").read_text(encoding="utf-8").splitlines()[-1])
    return 0


if __name__ == "__main__":
    sys.exit(main())
