"""CSV loaders and `load_store`. Read-only on the dataset directory."""
import csv
import hashlib
from pathlib import Path

from . import store
from .activity_parse import parse_details


def _read(dataset_dir, name):
    with open(Path(dataset_dir) / name, newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def case_number(case_id):
    return int(case_id.rsplit("_", 1)[1])


def case_sort_key(case_id):
    return (case_number(case_id), case_id)


def load_requests(dataset_dir, sample=False):
    return _read(dataset_dir, "sample_deletion_requests.csv" if sample else "deletion_requests.csv")


def load_identities(dataset_dir):
    rows = _read(dataset_dir, "user_identities.csv")
    for r in rows:
        r["email"] = r["email"].strip().lower()
    return rows


def load_audit_log(dataset_dir):
    return _read(dataset_dir, "user_audit_log.csv")


def load_activity(dataset_dir):
    return _read(dataset_dir, "system_activity.csv")


def load_rules(dataset_dir):
    return _read(dataset_dir, "deletion_rules.csv")


def load_evidence_manifest(dataset_dir):
    return _read(dataset_dir, "evidence_files.csv")


def _sha(value):
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _norm(field_name, value):
    value = value.strip()
    return value.lower() if field_name == "email" else value


def _identity_rows(identities, audit):
    """Yield (user_id, kind, value, valid_from, valid_to, source) rows.

    Current values come from the snapshot; every audit old/new value is added with the
    interval it was valid. The sha256 of every email is added as a computed email_hash.
    """
    rows = []
    for r in identities:
        u, t = r["user_id"], r["updated_at"]
        rows.append((u, "user_id", u, None, None, "snapshot"))
        rows.append((u, "name", r["name"].strip(), None, None, "snapshot"))
        rows.append((u, "email", r["email"], t, None, "snapshot"))
        rows.append((u, "phone", r["phone"].strip(), t, None, "snapshot"))
        rows.append((u, "email_hash", r["email_hash"].strip().lower(), t, None, "snapshot"))
    for a in sorted(audit, key=lambda x: (x["event_time"], x["audit_id"])):
        u, f, t, src = a["user_id"], a["field_name"], a["event_time"], "audit:" + a["audit_id"]
        old, new = _norm(f, a["old_value"]), _norm(f, a["new_value"])
        if f == "email_hash":
            old, new = old.lower(), new.lower()
        rows.append((u, f, old, None, t, src))
        rows.append((u, f, new, t, None, src))
    for u, kind, value, vf, vt, _src in list(rows):
        if kind == "email":
            rows.append((u, "email_hash", _sha(value), vf, vt, "computed"))
    return rows


def load_store(dataset_dir):
    conn = store.connect()
    store.create_schema(conn)
    identities = load_identities(dataset_dir)
    audit = load_audit_log(dataset_dir)
    conn.executemany("insert into identity values (?,?,?,?,?,?)", _identity_rows(identities, audit))
    conn.executemany(
        "insert into evidence_file(file_id,path,origin,format,adapter,manifest_at) "
        "values (?,?,?,null,null,?)",
        [(r["file_id"], r["file_path"], r["origin"], r["captured_at"])
         for r in load_evidence_manifest(dataset_dir)],
    )
    conn.executemany(
        "insert into rule values (?,?,?,?)",
        [(r["rule_id"], r["data_category"], r["condition"], r["required_action"])
         for r in load_rules(dataset_dir)],
    )
    act = []
    for r in load_activity(dataset_dir):
        p = parse_details(r["event_type"], r["resource_id"], r["details"])
        act.append((r["activity_id"], r["event_time"], r["event_type"], r["source_system"],
                    r["target_system"], r["resource_id"], r["reported_result"], r["details"],
                    p["kind"], p["target_resource"], p["downstream_resource"],
                    p["linked_email"], p["linked_user_id"]))
    conn.executemany("insert into activity values (?,?,?,?,?,?,?,?,?,?,?,?,?)", act)
    conn.commit()
    return conn
