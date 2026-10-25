"""Invariant checker over any output.csv (DESIGN §8.2). Never touches the pipeline: it only
reads the finished file, plus optional dataset timestamps for the evidence-time check.

Violations are `Violation(invariant, case_id, detail)`; `format_report` groups them by invariant
with every offending case_id.
"""
import csv
import json
from collections import defaultdict, namedtuple
from datetime import datetime
from pathlib import Path

from . import steps as st

EXPECTED_HEADER = ["case_id", "deletion_status", "steps_json"]
STATUSES = ("COMPLETE", "INCOMPLETE", "NOT_APPLICABLE")
RECORD_STATES = {"ABSENT", "PRESENT", "REDACTED", "ANONYMIZED"}

Violation = namedtuple("Violation", "invariant case_id detail")

TITLES = {
    "S1": "header is exactly case_id,deletion_status,steps_json",
    "S2": "case_ids unique, numeric order, equal to the expected set",
    "S3": "deletion_status is COMPLETE, INCOMPLETE or NOT_APPLICABLE",
    "S4": "steps_json is a JSON list of steps with exactly the 7 typed keys",
    "S5": "steps_json is compact (no whitespace outside strings)",
    "S6": "step_no runs 1..n",
    "S7": "phase, operation and state are valid and consistent",
    "S8": "steps are in phase / canonical op / system_id / resource_id order",
    "S9": "no duplicate (phase, operation, system_id, resource_id)",
    "P1": "NOT_APPLICABLE if and only if steps_json is []",
    "P2": "COMPLETE has only cited verification steps",
    "P3": "INCOMPLETE: findings cited; remediation and verification uncited",
    "P4": "every finding has its remediation and verification; every remediation a finding",
    "P5": "no resource gets two conflicting end states",
    "E1": "every cited id exists and is dated at or before audit_at",
    "A1": "every deletion row with steps has an account step or a logged link excuse",
}
ORDER = list(TITLES)

NOT_CHECKED = (
    "every deletion case has an account step or a logged link_unresolved (A1: pass diagnostics)",
    "no retained category is ever DELETEd (enforced by an assert in policy.to_steps, T8)",
    "no fact dated after audit_at was used; trap families F-B4/F-D4 (dossier.assert_clock, T7)",
    "determinism: two runs give a byte-identical output.csv (test_cli_pipeline.test_deterministic)",
)

SKIP_REASONS = {
    "E1": "no dataset timestamps supplied",
    "P1": "stub rows (deletion case with []) accepted by --allow-stub",
    "P3": "stub rows (INCOMPLETE with []) accepted by --allow-stub",
}


class Result:
    def __init__(self):
        self.violations = []
        self.skipped = {}
        self.not_checked = list(NOT_CHECKED)
        self.stats = {"rows": 0, "guessed_account_uncited": 0, "no_steps_excused": 0}

    def add(self, invariant, case_id, detail):
        self.violations.append(Violation(invariant, case_id, detail))


# ---------------------------------------------------------------- loaders

def load_output(path):
    """(header, rows) where each row is a dict over the header; missing cells are None."""
    with open(path, newline="", encoding="utf-8") as f:
        reader = csv.reader(f)
        header = next(reader, [])
        rows = []
        for raw in reader:
            if not raw:
                continue
            d = {h: (raw[i] if i < len(raw) else None) for i, h in enumerate(header)}
            rows.append(d)
    return header, rows


def load_requests_rows(path):
    with open(path, newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def load_audit_at(requests_path):
    return {r["case_id"]: r["audit_at"] for r in load_requests_rows(requests_path)}


def load_evidence_times(dataset_dir):
    """file_id / activity_id / audit_id -> timestamp, from the participant-facing CSVs."""
    d = Path(dataset_dir)
    times = {}
    for name, id_col, ts_col in (("evidence_files.csv", "file_id", "captured_at"),
                                 ("system_activity.csv", "activity_id", "event_time"),
                                 ("user_audit_log.csv", "audit_id", "event_time")):
        for r in load_requests_rows(d / name):
            times[r[id_col]] = r[ts_col]
    return times


def _load_events(path, name):
    p = Path(path)
    if not p.exists():
        return []
    out = []
    for line in p.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        ev = json.loads(line)
        if isinstance(ev, dict) and ev.get("event") == name:
            out.append(ev)
    return out


def load_guessed_uncited(path):
    """{(case_id, resource_id)} for every guessed_account_uncited event; empty if no file."""
    return {(ev["case_id"], ev["resource_id"]) for ev in _load_events(path, "guessed_account_uncited")}


def load_account_excused(path):
    """{case_id} with a logged reason for having no account step: link_unresolved, a contradicted
    guess, or an evidence account that no file could cite (unciteable)."""
    out = {ev["case_id"] for name in ("link_unresolved", "guessed_account_contradicted")
           for ev in _load_events(path, name)}
    out |= {ev["case_id"] for ev in _load_events(path, "unciteable")
            if ev.get("system_id") == "account_service"}
    return out


def load_no_steps(path):
    """{case_id} for every no_steps event (a deletion case the pipeline could not state)."""
    return {ev["case_id"] for ev in _load_events(path, "no_steps")}


# ---------------------------------------------------------------- helpers

def _case_number(case_id):
    if isinstance(case_id, str) and case_id.startswith("case_") and case_id[5:].isdigit():
        return int(case_id[5:])
    return None


def _ts(text):
    return datetime.fromisoformat(text.replace("Z", "+00:00"))


def _parse_steps(raw, res, cid):
    """Parsed list of valid step dicts, or None after recording an S4 violation."""
    if not isinstance(raw, str):
        res.add("S4", cid, "steps_json cell is missing")
        return None
    try:
        parsed = json.loads(raw)
    except ValueError as e:
        res.add("S4", cid, f"steps_json is not valid JSON ({e})")
        return None
    if not isinstance(parsed, list):
        res.add("S4", cid, "steps_json is not a JSON list")
        return None
    ok = True
    for i, s in enumerate(parsed, start=1):
        if not isinstance(s, dict):
            res.add("S4", cid, f"step {i} is not an object")
            ok = False
            continue
        if set(s) != set(st.STEP_KEYS):
            res.add("S4", cid, f"step {i} keys are {sorted(s)}, expected {sorted(st.STEP_KEYS)}")
            ok = False
            continue
        if not isinstance(s["step_no"], int) or isinstance(s["step_no"], bool):
            res.add("S4", cid, f"step {i}: step_no must be an integer")
            ok = False
        for k in st.STEP_KEYS[1:]:
            if not isinstance(s[k], str):
                res.add("S4", cid, f"step {i}: {k} must be a string")
                ok = False
    if not ok:
        return None
    compact = {json.dumps(parsed, ensure_ascii=a, separators=(",", ":")) for a in (False, True)}
    if raw not in compact:
        res.add("S5", cid, "steps_json is not compactly serialized")
    return parsed


def _s7_clean(parsed, res, cid):
    clean = True
    for s in parsed:
        n = s["step_no"]
        if s["phase"] not in st.PHASES:
            res.add("S7", cid, f"step {n}: unknown phase {s['phase']!r}")
            clean = False
        elif s["operation"] not in st.OP_PHASE:
            res.add("S7", cid, f"step {n}: unknown operation {s['operation']!r}")
            clean = False
        elif st.OP_PHASE[s["operation"]] != s["phase"]:
            res.add("S7", cid, f"step {n}: {s['operation']} is a {st.OP_PHASE[s['operation']]} "
                               f"operation, not {s['phase']}")
            clean = False
        elif st.OP_STATE[s["operation"]] != s["state"]:
            res.add("S7", cid, f"step {n}: {s['operation']} has state {st.OP_STATE[s['operation']]}, "
                               f"not {s['state']}")
            clean = False
    return clean


def _key(s):
    return (s["system_id"], s["resource_id"])


# ---------------------------------------------------------------- checks

def _check_header_and_ids(header, rows, expected_case_ids, res):
    if list(header) != EXPECTED_HEADER:
        res.add("S1", "", f"header is {list(header)}")
    seen = set()
    numbers = []
    for r in rows:
        cid = r.get("case_id")
        if cid in seen:
            res.add("S2", cid, "duplicate case_id")
        seen.add(cid)
        n = _case_number(cid)
        if n is None:
            res.add("S2", cid, "malformed case_id")
        else:
            numbers.append((n, cid))
    for (prev, _), (cur, cid) in zip(numbers, numbers[1:]):
        if cur < prev:
            res.add("S2", cid, "rows are not in numeric case_id order")
    if expected_case_ids is not None:
        expected = set(expected_case_ids)
        for cid in sorted(expected - seen, key=lambda c: (_case_number(c) or 0, c)):
            res.add("S2", cid, "expected case_id is missing")
        for cid in sorted(seen - expected, key=lambda c: (_case_number(c) or 0, str(c))):
            res.add("S2", cid, "case_id is not in the request file")


def _check_case_status(cid, status, parsed, res, allow_stub, guessed, used, no_steps):
    if status == "NOT_APPLICABLE":
        if parsed:
            res.add("P1", cid, "NOT_APPLICABLE but steps_json is not []")
        return
    if not parsed and status == "INCOMPLETE" and cid in no_steps:
        res.stats["no_steps_excused"] += 1
        return
    if not parsed:
        if allow_stub:
            res.skipped["P1"] = SKIP_REASONS["P1"]
            if status == "INCOMPLETE":
                res.skipped["P3"] = SKIP_REASONS["P3"]
            return
        res.add("P1", cid, f"{status} but steps_json is []")
        if status == "INCOMPLETE":
            res.add("P3", cid, "INCOMPLETE without any finding")
        return
    if status == "COMPLETE":
        _check_complete(cid, parsed, res, guessed, used)
    else:
        _check_incomplete(cid, parsed, res)


def _check_complete(cid, parsed, res, guessed, used):
    for s in parsed:
        n = s["step_no"]
        if s["phase"] != "VERIFICATION":
            res.add("P2", cid, f"step {n}: COMPLETE case has a {s['phase']} step")
        elif not s["evidence_id"]:
            is_account = (s["operation"] == "VERIFY_RECORD_ABSENT"
                          and s["system_id"] == "account_service"
                          and s["resource_id"].startswith("account_"))
            if is_account and (cid, s["resource_id"]) in guessed:
                used.add((cid, s["resource_id"]))
            else:
                res.add("P2", cid, f"step {n}: verification without evidence_id")


def _check_incomplete(cid, parsed, res):
    findings = [s for s in parsed if s["phase"] == "FINDING"]
    if not findings:
        res.add("P3", cid, "INCOMPLETE without any finding")
    for s in parsed:
        if s["phase"] == "FINDING" and not s["evidence_id"]:
            res.add("P3", cid, f"step {s['step_no']}: finding without evidence_id")
        if s["phase"] != "FINDING" and s["evidence_id"]:
            res.add("P3", cid, f"step {s['step_no']}: {s['phase']} step cites {s['evidence_id']!r}, "
                               f"must be empty")
    _check_templates(cid, parsed, res)


def _check_templates(cid, parsed, res):
    rems, vers, finds = defaultdict(set), defaultdict(set), defaultdict(set)
    for s in parsed:
        bucket = {"REMEDIATION": rems, "VERIFICATION": vers, "FINDING": finds}[s["phase"]]
        bucket[_key(s)].add(s["operation"])
    for k, f_ops in sorted(finds.items()):
        for f in sorted(f_ops):
            rem_ops, ver_ops = st.FINDING_TEMPLATES[f]
            have = [i for i, o in enumerate(rem_ops) if o in rems.get(k, ())]
            if not have:
                res.add("P4", cid, f"{f} on {k[0]}/{k[1]} has no {'/'.join(rem_ops)} remediation")
            elif not any(ver_ops[i] in vers.get(k, ()) for i in have):
                res.add("P4", cid, f"{f} on {k[0]}/{k[1]} has no matching "
                                   f"{'/'.join(ver_ops[i] for i in have)} verification")
    for k, r_ops in sorted(rems.items()):
        for o in sorted(r_ops):
            if not any(o in st.FINDING_TEMPLATES[f][0] for f in finds.get(k, ())):
                res.add("P4", cid, f"{o} on {k[0]}/{k[1]} has no finding")


def _check_conflicts(cid, parsed, res):
    states = defaultdict(set)
    for s in parsed:
        if s["phase"] in ("REMEDIATION", "VERIFICATION") and s["state"] in RECORD_STATES:
            states[_key(s)].add(s["state"])
    for k, v in sorted(states.items()):
        if len(v) > 1:
            res.add("P5", cid, f"{k[0]}/{k[1]} gets conflicting end states {sorted(v)}")


def _check_evidence(cid, parsed, times, audit_at, res):
    if cid not in audit_at:
        if any(s["evidence_id"] for s in parsed):
            res.add("E1", cid, "no audit_at known for this case")
        return
    cutoff = _ts(audit_at[cid])
    for s in parsed:
        e = s["evidence_id"]
        if not e:
            continue
        if e not in times:
            res.add("E1", cid, f"step {s['step_no']}: {e!r} is not a file_id, activity_id or audit_id")
        elif _ts(times[e]) > cutoff:
            res.add("E1", cid, f"step {s['step_no']}: {e} is dated {times[e]}, after audit_at "
                               f"{audit_at[cid]}")


def _check_account_step(cid, parsed, res, excused):
    if parsed and cid not in excused and not any(s["system_id"] == "account_service" for s in parsed):
        res.add("A1", cid, "no account_service step and no link_unresolved / unciteable excuse")


def check_table(header, rows, expected_case_ids=None, evidence_times=None, audit_at=None,
                guessed_uncited=None, allow_stub=False, no_steps=None, account_excused=None):
    res = Result()
    if account_excused is not None:
        res.not_checked = [n for n in res.not_checked if "account step" not in n]
    res.stats["rows"] = len(rows)
    guessed = set(guessed_uncited or ())
    no_steps = set(no_steps or ())
    used = set()
    e1_on = evidence_times is not None and audit_at is not None
    if not e1_on:
        res.skipped["E1"] = SKIP_REASONS["E1"]

    _check_header_and_ids(header, rows, expected_case_ids, res)
    for r in rows:
        cid = r.get("case_id")
        parsed = _parse_steps(r.get("steps_json"), res, cid)
        if parsed is None:
            continue
        status = r.get("deletion_status")
        if status not in STATUSES:
            res.add("S3", cid, f"unknown deletion_status {status!r}")
        numbers = [s["step_no"] for s in parsed]
        if numbers != list(range(1, len(parsed) + 1)):
            res.add("S6", cid, f"step_no is {numbers}, expected 1..{len(parsed)}")
        dup = defaultdict(int)
        for s in parsed:
            dup[(s["phase"], s["operation"], s["system_id"], s["resource_id"])] += 1
        for k, c in sorted(dup.items()):
            if c > 1:
                res.add("S9", cid, f"{c} identical steps {k}")
        if not _s7_clean(parsed, res, cid):
            continue
        keys = [st.sort_key(s) for s in parsed]
        if keys != sorted(keys):
            res.add("S8", cid, "steps are not in the canonical order")
        if status in STATUSES:
            _check_case_status(cid, status, parsed, res, allow_stub, guessed, used, no_steps)
            _check_conflicts(cid, parsed, res)
            if account_excused is not None and status != "NOT_APPLICABLE":
                _check_account_step(cid, parsed, res, account_excused)
        if e1_on:
            _check_evidence(cid, parsed, evidence_times, audit_at, res)
    res.stats["guessed_account_uncited"] = len(used)
    return res


def check_file(path, requests_path=None, dataset_dir=None, diagnostics_path=None, allow_stub=False):
    header, rows = load_output(path)
    expected = ([r["case_id"] for r in load_requests_rows(requests_path)]
                if requests_path else None)
    times = audit = None
    if dataset_dir and Path(dataset_dir).is_dir() and requests_path:
        times = load_evidence_times(dataset_dir)
        audit = load_audit_at(requests_path)
    guessed = load_guessed_uncited(diagnostics_path) if diagnostics_path else set()
    no_steps = load_no_steps(diagnostics_path) if diagnostics_path else set()
    excused = load_account_excused(diagnostics_path) if diagnostics_path else None
    return check_table(header, rows, expected, times, audit, guessed, allow_stub, no_steps, excused)


# ---------------------------------------------------------------- report

def _wrap(items, indent="    ", width=100):
    lines, cur = [], indent
    for it in items:
        piece = it + ", "
        if len(cur) + len(piece) > width and cur.strip():
            lines.append(cur.rstrip())
            cur = indent
        cur += piece
    if cur.strip():
        lines.append(cur.rstrip().rstrip(","))
    return lines


def format_report(res, name):
    by = defaultdict(list)
    for v in res.violations:
        by[v.invariant].append(v)
    out = [f"Invariant check: {name} ({res.stats['rows']} rows)"]
    for i in ORDER:
        if i in by:
            vs = by[i]
            case_ids = []
            for v in vs:
                label = v.case_id or "(file)"
                if label not in case_ids:
                    case_ids.append(label)
            out.append(f"FAIL {i} {TITLES[i]}: {len(vs)} violation(s) in {len(case_ids)} case(s)")
            out.extend(_wrap(case_ids))
            for d in sorted({v.detail for v in vs})[:3]:
                out.append(f"    e.g. {d}")
        elif i == "A1" and any("account step" in n for n in res.not_checked):
            continue  # not run: listed under NOT CHECKED YET
        elif i in res.skipped:
            out.append(f"SKIPPED {i} {TITLES[i]}: {res.skipped[i]}")
        else:
            out.append(f"PASS {i} {TITLES[i]}")
    out.append(f"guessed_account_uncited exceptions used: {res.stats['guessed_account_uncited']}")
    out.append(f"no_steps rows excused (INCOMPLETE [] logged in diagnostics): "
               f"{res.stats['no_steps_excused']}")
    out.append("NOT CHECKED YET:")
    out.extend(f"  - {n}" for n in res.not_checked)
    cases = {v.case_id for v in res.violations}
    out.append("RESULT: PASS" if not res.violations
               else f"RESULT: FAIL ({len(res.violations)} violations in {len(cases)} cases)")
    return "\n".join(out)
