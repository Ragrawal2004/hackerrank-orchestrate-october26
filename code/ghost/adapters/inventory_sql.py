"""Adapter 4: inventory SQL snapshots (DESIGN §3, adapter 4; SPEC I1).

Recognised by structure, never by file name: a `snapshot_scope(origin_system, captured_at)` table
plus at least one of the typed customer-data tables with its exact column list.

Clock (SPEC I1, the U3 rule): every observation's record_at is the row's own updated_at; the
system's scope time is kept in attrs.captured_at. Scope: one capture_scope row per snapshot_scope
row, with that row's own time, but complete=0. An inventory is a filtered extract, not a whole-store
capture (SAMPLE_DERIVATION U4; case_05's gold keeps a ghost that a later in-scope empty inventory
omits), so it never proves absence. Only its explicit rows count. Systems missing from
snapshot_scope get no scope row at all.
"""
import json
import re

from .. import policy, store
from . import common, replica_sql

name = "inventory_sql"
FORMAT = "sql_inventory"

SCOPE_COLS = ("origin_system", "captured_at")
# table -> (logical system, exact columns)
TABLES = {
    "customer_profiles": ("account_service", (
        "resource_id", "customer_reference", "full_name", "email", "phone", "email_hash",
        "account_status", "updated_at")),
    "customer_search_documents": ("search_service", (
        "resource_id", "customer_reference", "display_name", "email", "phone", "email_hash",
        "index_name", "updated_at")),
    "customer_analytics_profiles": ("analytics_platform", (
        "resource_id", "customer_reference", "email", "phone", "email_hash", "audience_cohort",
        "updated_at")),
    "support_records": ("support_platform", (
        "resource_id", "customer_reference", "requester_name", "requester_email",
        "requester_phone", "message_excerpt", "updated_at")),
    "customer_exports": ("export_service", (
        "resource_id", "customer_reference", "owner_email", "owner_phone", "object_key",
        "updated_at")),
    "connector_subscriptions": ("integration_service", (
        "resource_id", "customer_reference", "customer_email", "connector_name", "enabled",
        "last_delivery_status", "updated_at")),
    "backup_restore_entries": ("backup_service", (
        "resource_id", "customer_reference", "customer_email", "archive_key", "restore_allowed",
        "updated_at")),
    "security_audit_records": ("security_service", (
        "resource_id", "subject_reference", "event_code", "retention_until", "updated_at")),
}
_FOOTER = re.compile(r"^--\s*snapshot_completed_at=(\S+)", re.M)


def _shape(db):
    """(known tables present with exact columns, other tables) or None when not an inventory."""
    tables = replica_sql._tables(db)
    if tables.get("snapshot_scope") != SCOPE_COLS:
        return None
    known = sorted(t for t, (_, cols) in TABLES.items() if tables.get(t) == cols)
    if not known:
        return None
    return known, sorted(t for t in tables if t not in known and t != "snapshot_scope")


def detect(text):
    """The format name when the text is an inventory snapshot, else None."""
    db, _ = replica_sql.load_sql(text)
    try:
        return FORMAT if _shape(db) else None
    finally:
        db.close()


def _time(v):
    if not isinstance(v, str):
        return None
    try:
        return v if store.epoch_us(v) is not None else None
    except (ValueError, TypeError, AttributeError):
        return None


def ingest(conn, file_id, text):
    """Parse one inventory SQL file into the store; returns the parse report dict."""
    row = conn.execute("select manifest_at from evidence_file where file_id = ?",
                       (file_id,)).fetchone()
    if row is None:
        raise ValueError(f"{file_id}: not in evidence_file")
    manifest_at = row[0]
    db, stats = replica_sql.load_sql(text)
    try:
        return _ingest(conn, file_id, manifest_at, text, db, stats)
    finally:
        db.close()


def _ingest(conn, file_id, manifest_at, text, db, stats):
    shape = _shape(db)
    if shape is None:
        raise ValueError(f"{file_id}: not an inventory snapshot")
    known, others = shape
    notes = list(stats["notes"])
    unparsed = stats["failed"]
    foot = _FOOTER.search(text)
    if foot and _time(foot.group(1)) and store.epoch_us(foot.group(1)) != store.epoch_us(manifest_at):
        notes.append({"type": "capture_time_mismatch", "header": foot.group(1),
                      "manifest": manifest_at})

    for t in others:
        n = db.execute(f'select count(*) from "{t}"').fetchone()[0]
        if n:
            unparsed += n
            notes.append({"type": "unrecognised_table", "table": t, "rows": n})

    common.clear(conn, file_id)
    scope_at = {}
    for system, at in db.execute("select origin_system, captured_at from snapshot_scope "
                                 "order by origin_system"):
        if not (isinstance(system, str) and system.strip() and _time(at)):
            unparsed += 1
            notes.append({"type": "scope_unparsed", "origin_system": system, "captured_at": at})
            continue
        scope_at[system] = at
        conn.execute("insert into capture_scope values (?,?,?,0)", (file_id, system, at))

    n_obs = 0
    limit = store.epoch_us(manifest_at)
    for table in known:
        system, cols = TABLES[table]
        for rowid, *vals in db.execute(f'select rowid, * from "{table}" order by rowid'):
            r = dict(zip(cols, vals))
            try:
                state, attrs, keys = _row(table, r)
                updated = _time(r["updated_at"])
                if updated is None:
                    raise common.Skip("updated_at is not a timestamp")
            except (common.Skip, ValueError, TypeError, AttributeError) as e:
                unparsed += 1
                notes.append({"type": "row_unparsed", "table": table, "rowid": rowid,
                              "reason": str(e)[:80]})
                continue
            captured = scope_at.get(system)
            if captured is None:
                notes.append({"type": "row_outside_scope", "table": table, "rowid": rowid,
                              "resource_id": r["resource_id"], "system_id": system})
            elif store.epoch_us(updated) > store.epoch_us(captured) or \
                    store.epoch_us(updated) > limit:
                notes.append({"type": "row_after_capture", "table": table, "rowid": rowid,
                              "resource_id": r["resource_id"]})
            attrs = {**attrs, "collection": table, "table": table, "captured_at": captured,
                     "row_updated_at": updated, "in_scope": captured is not None}
            cur = conn.execute(
                "insert into observation(file_id, locator, record_at, system_id, resource_id, "
                "category, state, state_basis, attrs_json) values (?,?,?,?,?,?,?,?,?)",
                (file_id, f"{table}#{rowid}", updated, system, r["resource_id"],
                 policy.COLLECTION_CATEGORY[table], state, "explicit",
                 json.dumps(attrs, sort_keys=True)))
            conn.executemany("insert into subject_key values (?,?,?)",
                             [(cur.lastrowid, k, v) for k, v in sorted(keys)])
            n_obs += 1

    parsed = stats["meaningful"] - unparsed
    report = {"meaningful": stats["meaningful"], "parsed": parsed, "unparsed": unparsed,
              "observations": n_obs, "join_keys": 0, "notes": notes}
    conn.execute("insert into parse_report values (?,?,?,?,?,?)",
                 (file_id, name, report["meaningful"], parsed, unparsed,
                  json.dumps(notes, sort_keys=True)))
    conn.execute("update evidence_file set format = ?, adapter = ? where file_id = ?",
                 (FORMAT, name, file_id))
    return report


def _flag(v):
    if v not in (0, 1):
        raise common.Skip(f"flag is not 0 or 1: {v!r}")
    return bool(v)


def _row(table, r):
    """(state, attrs, subject keys) of one typed row."""
    rid = r["resource_id"]
    if not (isinstance(rid, str) and rid.strip()):
        raise common.Skip("no resource_id")
    low, anyset, keys = common.lower, common.any_set, common.keys
    if table == "customer_profiles":
        return "PRESENT", {"name": r["full_name"], "email": low(r["email"]), "phone": r["phone"],
                           "account_status": r["account_status"]}, keys(
            r["email"], r["phone"], r["email_hash"], r["customer_reference"],
            names=[r["full_name"]])
    if table == "customer_search_documents":
        return "PRESENT", {"email": low(r["email"])}, keys(
            r["email"], r["phone"], r["email_hash"], r["customer_reference"],
            names=[r["display_name"]])
    if table == "customer_analytics_profiles":
        fields = {"user_id": r["customer_reference"], "email": r["email"], "phone": r["phone"],
                  "email_hash": r["email_hash"]}
        state = "IDENTIFIABLE" if anyset(*fields.values()) else "ANONYMIZED"
        return state, {**fields, "email": low(fields["email"]),
                       "email_hash": low(fields["email_hash"])}, keys(*fields.values())
    if table == "support_records":
        text = r["message_excerpt"] or ""
        emails = [k for k in keys(texts=[text])]
        exposed = anyset(r["requester_name"], r["requester_email"], r["requester_phone"]) or emails
        return ("IDENTIFIABLE" if exposed else "REDACTED"), {
            "email": low(r["requester_email"])}, keys(
            r["requester_email"], r["requester_phone"], r["customer_reference"],
            names=[r["requester_name"]], texts=[text])
    if table == "customer_exports":
        return "PRESENT", {}, keys(r["owner_email"], r["owner_phone"], r["customer_reference"])
    if table == "connector_subscriptions":
        on = _flag(r["enabled"])
        return ("ACTIVE" if on else "DISABLED"), {"enabled": on}, keys(
            r["customer_email"], r["customer_reference"])
    if table == "backup_restore_entries":
        ok = _flag(r["restore_allowed"])
        return ("RESTORABLE" if ok else "BLOCKED"), {"restore_allowed": ok}, keys(
            r["customer_email"], r["customer_reference"])
    if table == "security_audit_records":
        return "PRESENT", {"retention_until": r["retention_until"]}, keys(r["subject_reference"])
    raise common.Skip(f"unknown table {table!r}")
