"""Score the screenshot/PDF reader against the hand-checked key (a test oracle, never a predictor).

    python3 code/evaluation/screen_key_check.py --extractions DIR --diagnostics FILE [--usage FILE]

Per file: did read A and read B agree (an extraction exists / no image_read_disagreement note) and
do the agreed, validated facts equal the key. With --usage the real cost per model is summed."""
import argparse
import json
from pathlib import Path

KEY_PATH = Path(__file__).resolve().parent / "screen_key.json"
FIELDS = ("screen_kind", "resource_id", "enabled", "restore_allowed", "identifiers_present",
          "listed_present", "identity_conflict")


def load_key(path=KEY_PATH):
    return {k: v for k, v in json.loads(Path(path).read_text(encoding="utf-8")).items()
            if not k.startswith("_")}


def facts_of(doc):
    """The comparable fields of a stored extraction."""
    ind = doc["indicators"]
    return {"screen_kind": doc["screen_kind"], "resource_id": doc["detail_resource_id"],
            "enabled": ind["enabled"], "restore_allowed": ind["restore_allowed"],
            "identifiers_present": ind["identifiers_present"],
            "listed_present": ind["listed_present"], "identity_conflict": ind["identity_conflict"]}


def expected_of(entry):
    return {"screen_kind": entry["screen_kind"], "resource_id": entry["expected_resource_id"],
            **{k: entry[k] for k in FIELDS[2:]}}


def compare(key, extractions_dir, diagnostics):
    """{file_id: {"agree": bool, "match": bool|None, "diffs": {field: (got, want)}}}."""
    notes = {}
    for line in Path(diagnostics).read_text(encoding="utf-8").splitlines():
        row = json.loads(line)
        if row.get("event") == "multimodal_note":
            notes.setdefault(row["case_id"].removeprefix("file:"), []).append(row)
    out = {}
    for file_id, entry in sorted(key.items()):
        path = Path(extractions_dir) / f"{file_id}.json"
        if not path.is_file():
            why = [n.get("note") for n in notes.get(file_id, [])]
            out[file_id] = {"agree": "image_read_disagreement" not in why, "match": None,
                            "diffs": {}, "why": why}
            continue
        got, want = facts_of(json.loads(path.read_text(encoding="utf-8"))), expected_of(entry)
        diffs = {k: (got[k], want[k]) for k in FIELDS if got[k] != want[k]}
        out[file_id] = {"agree": True, "match": not diffs, "diffs": diffs, "why": []}
    return out


def usage_cost(usage_path, prices):
    """(calls, cache_hits, input_tokens, output_tokens, usd) for the screenshot purpose."""
    calls = hits = tin = tout = 0
    usd = 0.0
    for line in Path(usage_path).read_text(encoding="utf-8").splitlines():
        r = json.loads(line)
        if r["purpose"] != "screenshot":
            continue
        if r["cache_hit"]:
            hits += 1
            continue
        calls += 1
        tin, tout = tin + r["input_tokens"], tout + r["output_tokens"]
        p = prices[r["model"]]
        usd += (r["input_tokens"] * p["input_per_mtok"] + r["output_tokens"] * p["output_per_mtok"]) / 1e6
    return calls, hits, tin, tout, usd


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--extractions", type=Path, required=True)
    ap.add_argument("--diagnostics", type=Path, required=True)
    ap.add_argument("--usage", type=Path)
    ap.add_argument("--key", type=Path, default=KEY_PATH)
    a = ap.parse_args(argv)
    res = compare(load_key(a.key), a.extractions, a.diagnostics)
    for file_id, r in res.items():
        print(file_id, "agree" if r["agree"] else "DISAGREE",
              {None: "no-facts", True: "match", False: "MISMATCH"}[r["match"]],
              r["diffs"] or r["why"] or "")
    if a.usage:
        import sys
        sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
        from ghost import config
        print("usage (calls, hits, in, out, usd):", usage_cost(a.usage, config.PRICES))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
