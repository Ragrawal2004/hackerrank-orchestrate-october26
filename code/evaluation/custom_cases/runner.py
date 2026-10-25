"""Run the custom cases through the real CLI and score them against the hand-written answers.

    python3 code/evaluation/main.py --custom [--keep DIR]

Builds the synthetic dataset in a temp dir (or DIR), runs `code/main.py --dataset <dir> --no-llm`
as a subprocess with no .env and a throwaway runs/ dir, then compares the output with
expected_output.csv using evaluation.scoring (strict and relaxed) and checks the output
invariants. Never reads or writes dataset/.
"""
import json
import os
import subprocess
import sys
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

from ghost import invariants
from evaluation import scoring
from . import build

CODE_DIR = Path(__file__).resolve().parents[2]
MAIN = CODE_DIR / "main.py"


@dataclass
class CaseResult:
    case_id: str
    label: str
    expected_status: str
    actual_status: str
    expected_steps: int
    actual_steps: int
    reasons: list = field(default_factory=list)  # strict mismatches (empty = exact match)
    missing_events: list = field(default_factory=list)

    @property
    def ok(self):
        return not self.reasons and not self.missing_events


@dataclass
class Result:
    cases: list
    invariant_violations: list
    cli_returncode: int
    cli_stderr: str
    score: dict
    out_dir: str

    @property
    def ok(self):
        return (self.cli_returncode == 0 and not self.invariant_violations
                and all(c.ok for c in self.cases))


def run_cli(dataset, out_csv, runs_dir, extra=()):
    """The real entry point, with no .env and its own runs/ dir."""
    env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"),
           "GHOST_ENV_FILE": str(Path(runs_dir) / "no-such-env-file"),
           "GHOST_RUNS_DIR": str(runs_dir)}
    return subprocess.run([sys.executable, str(MAIN), "--dataset", str(dataset), "--out",
                           str(out_csv), "--no-llm", *extra],
                          capture_output=True, text=True, env=env)


def read_events(runs_dir):
    path = Path(runs_dir) / "diagnostics.jsonl"
    if not path.exists():
        return []
    return [json.loads(l) for l in path.read_text(encoding="utf-8").splitlines() if l.strip()]


def _field(ev, dotted):
    cur = ev
    for part in dotted.split("."):
        if not isinstance(cur, dict) or part not in cur:
            return None
        cur = cur[part]
    return cur


def missing_events(spec, events):
    """Expected diagnostics events (type, {dotted field: value}) that the run did not log."""
    out = []
    for cid, wanted in spec.diagnostics.items():
        mine = [e for e in events if e.get("case_id") == cid]
        for etype, fields in wanted:
            hit = any(e.get("event") == etype and all(_field(e, k) == v for k, v in fields.items())
                      for e in mine)
            if not hit:
                out.append(f"{cid}: no {etype} event" + (f" with {fields}" if fields else ""))
    return out


def evaluate(work, spec, runs_dir, proc):
    """Score a finished run in `work` against the spec's expected answers."""
    pred_path, gold_path = Path(work) / "pred.csv", Path(work) / "expected_output.csv"
    if not pred_path.exists():
        return Result([], [f"no output written (exit {proc.returncode})"], proc.returncode,
                      proc.stderr, {}, str(work))
    gold, pred = scoring.load_rows(gold_path), scoring.load_rows(pred_path)
    score = scoring.score(gold, pred)
    reasons = {m["case_id"]: m["reasons"] for m in score["mismatches"]}
    events = read_events(runs_dir)
    absent = missing_events(spec, events)
    results = []
    for cid in sorted(gold):
        g, p = gold[cid], pred.get(cid)
        results.append(CaseResult(
            cid, spec.labels.get(cid, "?"), g[0], p[0] if p else "(no row)", len(g[1] or []),
            len(p[1] or []) if p and p[1] is not None else 0, reasons.get(cid, []),
            [a for a in absent if a.startswith(cid + ":")]))
    inv = invariants.check_file(pred_path, Path(work) / "deletion_requests.csv", work,
                                Path(runs_dir) / "diagnostics.jsonl")
    return Result(results, list(inv.violations), proc.returncode, proc.stderr, score, str(work))


def run(keep=None):
    """Build, run and score the custom cases. keep: directory to leave the files in."""
    with tempfile.TemporaryDirectory() as tmp:
        work = Path(keep) if keep else Path(tmp) / "custom_dataset"
        work.mkdir(parents=True, exist_ok=True)
        runs_dir = Path(tmp) / "runs"
        runs_dir.mkdir()
        spec = build.build_dataset(work)
        proc = run_cli(work, work / "pred.csv", runs_dir)
        return evaluate(work, spec, runs_dir, proc)


def format_result(res):
    lines = [f"{'case':<10}{'label':<7}{'expected':<16}{'actual':<16}{'steps e/a':<11}match"]
    for c in res.cases:
        lines.append(f"{c.case_id:<10}{c.label:<7}{c.expected_status:<16}{c.actual_status:<16}"
                     f"{c.expected_steps}/{c.actual_steps:<8}{'OK' if c.ok else 'MISMATCH'}")
        lines += [f"    - {r}" for r in c.reasons] + [f"    - {m}" for m in c.missing_events]
    if res.cli_returncode != 0:
        lines.append(f"CLI exit {res.cli_returncode}: {res.cli_stderr.strip()[:300]}")
    for v in res.invariant_violations:
        lines.append(f"invariant violation: {v}")
    passed = sum(c.ok for c in res.cases)
    lines.append(f"{passed}/{len(res.cases)} custom cases match (strict); "
                 f"invariant violations: {len(res.invariant_violations)}")
    return "\n".join(lines)
