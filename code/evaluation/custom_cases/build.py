"""Write a complete synthetic dataset directory (and the expected answers) from cases.build_spec.

The output has the same file layout the pipeline reads from dataset/, so it runs through the real
CLI with --dataset. Deterministic: no clock, no randomness, fixed row order.
"""
import csv
import json
from pathlib import Path

from . import cases

HEADERS = {
    "user_identities.csv": ["user_id", "name", "email", "phone", "email_hash", "updated_at"],
    "user_audit_log.csv": ["audit_id", "user_id", "event_time", "field_name", "old_value",
                           "new_value"],
    "system_activity.csv": ["activity_id", "event_time", "event_type", "source_system",
                            "target_system", "resource_id", "reported_result", "details"],
    "deletion_rules.csv": ["rule_id", "data_category", "condition", "required_action"],
    "evidence_files.csv": ["file_id", "file_path", "origin", "captured_at"],
    "deletion_requests.csv": ["case_id", "email", "requested_at", "reported_status", "reported_at",
                              "audit_at", "request_text"],
    "expected_output.csv": ["case_id", "deletion_status", "steps_json"],
}


def _write_csv(path, name, rows):
    with open(Path(path) / name, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f, lineterminator="\n")
        w.writerow(HEADERS[name])
        w.writerows(rows)


def dumps_steps(steps):
    return json.dumps(steps, ensure_ascii=False, separators=(",", ":"))


def audit_rows(users):
    """A paired email / email_hash change for every user with an old email (same timestamp)."""
    rows, n = [], 0
    for u in users:
        for old, at in u.old_emails:
            for field, o, nw in (("email", old, u.email),
                                 ("email_hash", cases.sha(old), cases.sha(u.email))):
                n += 1
                rows.append([f"audit_{9000 + n}", u.user_id, at, field, o, nw])
    return rows


def build_dataset(out_dir, controls=frozenset()):
    """Write the dataset into out_dir (created if needed) and return the Spec."""
    out = Path(out_dir)
    spec = cases.build_spec(controls)
    for ev in spec.evidence:
        target = out / ev.path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(ev.text, encoding="utf-8", newline="\n")
    _write_csv(out, "user_identities.csv",
               [[u.user_id, u.name, u.email, u.phone, cases.sha(u.email), u.updated_at]
                for u in spec.users])
    _write_csv(out, "user_audit_log.csv", audit_rows(spec.users))
    _write_csv(out, "system_activity.csv", sorted([list(a) for a in spec.activities]))
    _write_csv(out, "deletion_rules.csv",
               [[f"rule_{i:02d}", cat, cond, act] for i, (cat, cond, act) in enumerate(cases.RULES, 1)])
    _write_csv(out, "evidence_files.csv",
               [[e.file_id, e.path, e.origin, e.captured_at]
                for e in sorted(spec.evidence, key=lambda e: e.file_id)])
    _write_csv(out, "deletion_requests.csv",
               [[q.case_id, q.email, q.requested_at, q.reported_status, q.reported_at, q.audit_at,
                 q.text] for q in spec.requests])
    _write_csv(out, "expected_output.csv",
               [[cid, status, dumps_steps(cases.numbered(steps))]
                for cid, (status, steps) in sorted(spec.expected.items())])
    return spec
