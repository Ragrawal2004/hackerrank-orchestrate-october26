"""Adapter 1: replica-drift SQL snapshots (DESIGN §3, adapter 1).

Recognised by structure, never by file name: a `*_primary` table plus at least one other table
with the same columns, either the record-json store (resource_id, origin_system, collection_name,
record_json, updated_at) or the typed profile store (resource_id, user_id, full_name, email,
phone, account_status, updated_at).

Clock decision (SPEC C1): every observation's record_at is the file's capture time; the row's own
updated_at is kept in attrs.row_updated_at. A complete capture's scope rows carry the same
record_at, so an explicit row can never be out-clocked by an omission from its own file.
"""
import json
import re
import sqlite3

from .. import identity, policy, store

name = "replica_sql"
FORMAT_JSON = "sql_replica_json"
FORMAT_TYPED = "sql_replica_typed"

J_COLS = ("resource_id", "origin_system", "collection_name", "record_json", "updated_at")
T_COLS = ("resource_id", "user_id", "full_name", "email", "phone", "account_status", "updated_at")
IGNORED = ("BEGIN", "COMMIT", "PRAGMA")

_EMAIL = re.compile(r"[\w.+'-]+@[\w-]+(?:\.[\w-]+)+")
_EMAIL_ONLY = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
_HASH = re.compile(r"^[0-9a-fA-F]{64}$")
_USER = re.compile(r"^user_\d+$")
_PHONE = re.compile(r"^\+?[\d\s().-]{7,}$")
_CAPTURED = re.compile(r"^--\s*Captured at:\s*(\S+)", re.M)
_TOPOLOGY = re.compile(r"^--\s*Database topology:\s*(\S+)", re.M)
_REGION = re.compile(r"_replica_(\w+)$")


# ---- reading the SQL --------------------------------------------------------------------------

def _deny(action, a, b, db, source):
    if action in (sqlite3.SQLITE_ATTACH, sqlite3.SQLITE_DETACH, sqlite3.SQLITE_PRAGMA):
        return sqlite3.SQLITE_DENY
    if action == sqlite3.SQLITE_FUNCTION and a and a.lower() == "load_extension":
        return sqlite3.SQLITE_DENY
    return sqlite3.SQLITE_OK


def statements(text):
    """Split SQL text into complete statements; a trailing partial statement is returned too."""
    buf, out = [], []
    for line in text.splitlines():
        if not buf and (not line.strip() or line.lstrip().startswith("--")):
            continue
        buf.append(line)
        joined = "\n".join(buf)
        if sqlite3.complete_statement(joined):
            out.append((joined, True))
            buf = []
    if buf:
        out.append(("\n".join(buf), False))
    return out


def _kind(stmt):
    words = stmt.strip().split(None, 2)
    first = words[0].upper().rstrip(";") if words else ""
    if first == "CREATE" and len(words) > 1 and words[1].upper() == "TABLE":
        return "CREATE"
    return first if first in ("INSERT", *IGNORED) else "OTHER"


def load_sql(text):
    """Execute only CREATE TABLE / INSERT statements in a scratch database.

    Returns (db, stats). Anything else (UPDATE, DROP, ATTACH, SELECT...) is never executed and is
    counted as an unparsed meaningful statement, as is a statement that fails to execute.
    """
    db = sqlite3.connect(":memory:")
    stats = {"meaningful": 0, "failed": 0, "notes": []}
    db.set_authorizer(_deny)
    for stmt, complete in statements(text):
        kind = _kind(stmt) if complete else "PARTIAL"
        if kind in IGNORED:
            continue
        stats["meaningful"] += 1
        if kind not in ("CREATE", "INSERT"):
            stats["failed"] += 1
            stats["notes"].append({"type": "unexecuted_statement", "kind": kind,
                                   "head": stmt.strip()[:60]})
            continue
        try:
            db.execute(stmt)
        except sqlite3.Error as e:
            stats["failed"] += 1
            stats["notes"].append({"type": "statement_error", "kind": kind, "error": str(e)[:80],
                                   "head": stmt.strip()[:60]})
    db.set_authorizer(None)
    return db, stats


def _tables(db):
    out = {}
    for (tname,) in db.execute("select name from sqlite_master where type='table' order by name"):
        out[tname] = tuple(r[1] for r in db.execute("select * from pragma_table_info(?)", (tname,)))
    return out


def _shape(tables):
    """(format, primary table, [replica tables], [other tables]) or None."""
    for fmt, cols in ((FORMAT_JSON, J_COLS), (FORMAT_TYPED, T_COLS)):
        match = sorted(t for t, c in tables.items() if c == cols)
        primaries = [t for t in match if t.endswith("primary")]
        replicas = [t for t in match if t not in primaries]
        if len(primaries) == 1 and replicas:
            other_fmt = FORMAT_TYPED if fmt == FORMAT_JSON else FORMAT_JSON
            other_cols = T_COLS if fmt == FORMAT_JSON else J_COLS
            if any(c == other_cols for c in tables.values()):
                return None
            return fmt, primaries[0], replicas, sorted(t for t in tables if t not in match)
    return None


def detect(text):
    """The format name when the text is a replica-drift snapshot, else None."""
    db, _ = load_sql(text)
    try:
        shape = _shape(_tables(db))
    finally:
        db.close()
    return shape[0] if shape else None


# ---- classifying values -----------------------------------------------------------------------

def classify(value):
    """(kind, normalised value) for an identifier-shaped string, else None."""
    if not isinstance(value, str):
        return None
    v = value.strip()
    if _EMAIL_ONLY.match(v):
        return "email", v.lower()
    if _HASH.match(v):
        return "email_hash", v.lower()
    if _USER.match(v):
        return "user_id", v
    if _PHONE.match(v):
        return "phone", v
    return None


def _keys(*pairs, names=(), texts=()):
    out = set()
    for v in pairs:
        c = classify(v)
        if c:
            out.add(c)
    for n in names:
        if isinstance(n, str) and n.strip():
            out.add(("name", n.strip()))
    for t in texts:
        for e in _EMAIL.findall(t or ""):
            out.add(("email", e.lower()))
    return out


def _lower(v):
    return v.strip().lower() if isinstance(v, str) and v.strip() else v


def _any(*vals):
    return any(v not in (None, "") for v in vals)


class _Skip(Exception):
    """The row cannot be turned into an observation (counted as unparsed)."""


def _record(collection, rec):
    """(state, attrs, subject keys) of one record-json row."""
    if collection == "customer_profiles":
        return "PRESENT", {"name": rec.get("full_name"), "email": _lower(rec.get("email")),
                           "phone": rec.get("phone")}, _keys(
            rec.get("email"), rec.get("phone"), rec.get("customer_reference"),
            names=[rec.get("full_name")])
    if collection == "customer_search_documents":
        doc = rec.get("document") or {}
        return "PRESENT", {"email": _lower(doc.get("email"))}, _keys(
            doc.get("email"), doc.get("customer_reference"), names=[doc.get("display_name")])
    if collection == "customer_analytics_reports":
        fields = {f: rec.get(f) for f in ("user_id", "email", "phone", "email_hash")}
        state = "IDENTIFIABLE" if _any(*fields.values()) else "ANONYMIZED"
        return state, {**fields, "email": _lower(fields["email"]),
                       "email_hash": _lower(fields["email_hash"])}, _keys(*fields.values())
    if collection == "support_conversations":
        text = rec.get("message_text") or ""
        emails = [e.lower() for e in _EMAIL.findall(text)]
        return ("IDENTIFIABLE" if emails else "REDACTED"), {
            "email": emails[0] if emails else None}, _keys(
            rec.get("subject_reference"), texts=[text])
    if collection == "ticket_attachments":
        return "PRESENT", {}, _keys(rec.get("uploaded_by"))
    if collection == "customer_export_jobs":
        return "PRESENT", {}, _keys(rec.get("owner_email"))
    if collection == "connector_configurations":
        on = bool(rec.get("enabled"))
        return ("ACTIVE" if on else "DISABLED"), {"enabled": on}, _keys(rec.get("subject_selector"))
    if collection == "backup_catalog":
        ok = bool(rec.get("restore_allowed"))
        return ("RESTORABLE" if ok else "BLOCKED"), {"restore_allowed": ok}, _keys(
            rec.get("subject_reference"))
    if collection == "security_audit_events":
        return "PRESENT", {}, _keys(rec.get("subject_reference"))
    if collection == "retained_order_records":
        return "PRESENT", {"hold_until": rec.get("retained_until")}, _keys(
            rec.get("subject_reference"))
    if collection == "deleted_customer_references":
        fields = {f: rec.get(f) for f in ("name", "email", "phone", "email_hash")}
        state = "IDENTIFIABLE" if _any(*fields.values()) else "REDACTED"
        return state, {**fields, "email": _lower(fields["email"]),
                       "email_hash": _lower(fields["email_hash"]),
                       "subject_reference": rec.get("subject_reference")}, _keys(
            rec.get("subject_reference"), fields["email"], fields["phone"], fields["email_hash"],
            names=[fields["name"]])
    raise _Skip(f"unknown collection {collection!r}")


# ---- ingest -----------------------------------------------------------------------------------

def _clear(conn, file_id):
    conn.execute("delete from subject_key where obs_id in "
                 "(select obs_id from observation where file_id = ?)", (file_id,))
    for t in ("observation", "join_key", "capture_scope", "parse_report"):
        conn.execute(f"delete from {t} where file_id = ?", (file_id,))


def ingest(conn, file_id, text):
    """Parse one replica SQL file into the store; returns the parse report dict."""
    row = conn.execute("select manifest_at from evidence_file where file_id = ?",
                       (file_id,)).fetchone()
    if row is None:
        raise ValueError(f"{file_id}: not in evidence_file")
    manifest_at = row[0]
    db, stats = load_sql(text)
    try:
        return _ingest(conn, file_id, manifest_at, text, db, stats)
    finally:
        db.close()


def _ingest(conn, file_id, manifest_at, text, db, stats):
    notes = list(stats["notes"])
    unparsed = stats["failed"]
    shape = _shape(_tables(db))
    if shape is None:
        raise ValueError(f"{file_id}: not a replica-drift snapshot")
    fmt, primary, replicas, others = shape
    header = _CAPTURED.search(text)
    topo = _TOPOLOGY.search(text)
    if header and store.epoch_us(header.group(1)) != store.epoch_us(manifest_at):
        notes.append({"type": "capture_time_mismatch", "header": header.group(1),
                      "manifest": manifest_at})
    topology = topo.group(1) if topo else None
    limit = store.epoch_us(manifest_at)

    for t in others:
        n = db.execute(f'select count(*) from "{t}"').fetchone()[0]
        if n:
            unparsed += n
            notes.append({"type": "unrecognised_table", "table": t, "rows": n})

    _clear(conn, file_id)
    cols = J_COLS if fmt == FORMAT_JSON else T_COLS
    in_primary = {r[0] for r in db.execute(f'select resource_id from "{primary}"')}
    by_replica = {t: {r[0] for r in db.execute(f'select resource_id from "{t}"')}
                  for t in replicas}
    systems, n_obs = set(), 0
    for table in [primary, *replicas]:
        role = "primary" if table == primary else "replica"
        region = None if role == "primary" else (_REGION.search(table) or [None, None])[1]
        for rowid, *vals in db.execute(f'select rowid, * from "{table}" order by rowid'):
            r = dict(zip(cols, vals))
            try:
                system, collection, state, attrs, keys = _row(fmt, r)
            except (_Skip, ValueError, TypeError, AttributeError) as e:
                unparsed += 1
                notes.append({"type": "row_unparsed", "table": table, "rowid": rowid,
                              "reason": str(e)[:80]})
                continue
            drift = None
            if role == "replica" and r["resource_id"] not in in_primary:
                drift = "replica_only"
            elif role == "primary" and any(r["resource_id"] not in s for s in by_replica.values()):
                drift = "primary_only"
            u = store.epoch_us(r["updated_at"])
            if u is not None and u > limit:
                notes.append({"type": "row_after_capture", "table": table, "rowid": rowid,
                              "resource_id": r["resource_id"]})
            attrs = {**attrs, "collection": collection, "table": table, "role": role,
                     "region": region, "topology": topology, "drift": drift,
                     "row_updated_at": r["updated_at"]}
            cur = conn.execute(
                "insert into observation(file_id, locator, record_at, system_id, resource_id, "
                "category, state, state_basis, attrs_json) values (?,?,?,?,?,?,?,?,?)",
                (file_id, f"{table}#{rowid}", manifest_at, system, r["resource_id"],
                 policy.COLLECTION_CATEGORY[collection], state, "explicit",
                 json.dumps(attrs, sort_keys=True)))
            conn.executemany("insert into subject_key values (?,?,?)",
                             [(cur.lastrowid, k, v) for k, v in sorted(keys)])
            systems.add(system)
            n_obs += 1
    for system in sorted(systems):
        conn.execute("insert into capture_scope values (?,?,?,1)", (file_id, system, manifest_at))
    parsed = stats["meaningful"] - unparsed
    report = {"meaningful": stats["meaningful"], "parsed": parsed, "unparsed": unparsed,
              "observations": n_obs, "join_keys": 0, "notes": notes}
    conn.execute("insert into parse_report values (?,?,?,?,?,?)",
                 (file_id, name, report["meaningful"], parsed, unparsed,
                  json.dumps(notes, sort_keys=True)))
    conn.execute("update evidence_file set format = ?, adapter = ? where file_id = ?",
                 (fmt, name, file_id))
    return report


def _row(fmt, r):
    """(logical system, collection, state, attrs, subject keys) for one table row."""
    if fmt == FORMAT_TYPED:
        if not r["resource_id"]:
            raise _Skip("no resource_id")
        keys = _keys(r["email"], r["phone"], r["user_id"], names=[r["full_name"]])
        attrs = {"name": r["full_name"], "email": _lower(r["email"]), "phone": r["phone"],
                 "account_status": r["account_status"]}
        return "account_service", "customer_profiles", "PRESENT", attrs, keys
    rec = json.loads(r["record_json"])
    if not isinstance(rec, dict):
        raise _Skip("record_json is not an object")
    if rec.get("resource_id") != r["resource_id"] or not r["resource_id"]:
        raise _Skip("resource_id missing or different from the column")
    if not r["origin_system"]:
        raise _Skip("no origin_system")
    collection = r["collection_name"]
    if collection not in policy.COLLECTION_CATEGORY:
        raise _Skip(f"unknown collection {collection!r}")
    state, attrs, keys = _record(collection, rec)
    if collection == "deleted_customer_references" or collection == "security_audit_events" \
            or collection in ("retained_order_records", "backup_catalog"):
        attrs.setdefault("subject_reference", rec.get("subject_reference"))
    return r["origin_system"], collection, state, attrs, keys


# ---- clock choice -----------------------------------------------------------------------------

def clock_choice_events(conn, case):
    """Events for rows of this subject that the row-updated_at clock would have used but the
    capture-time clock (SPEC C1) excludes: updated_at <= audit_at < record_at."""
    store.register_functions(conn)
    audit_at = case["audit_at"]
    try:
        uid, _ = identity.resolve_email_detailed(conn, case["email"])
    except identity.UnknownIdentity:
        return []
    ids = identity.identifiers(conn, uid, as_of=audit_at)
    subject = {v.strip().lower() for k in ("user_id", "email", "email_hash", "phone")
               for v in ids[k]}
    rows = conn.execute(
        "select o.obs_id, o.file_id, o.resource_id, o.record_at, o.attrs_json "
        "from observation o join evidence_file f on f.file_id = o.file_id "
        "where f.adapter = ? and ts(o.record_at) > ts(?) order by o.obs_id", (name, audit_at))
    out, seen = [], set()
    for oid, fid, rid, record_at, attrs_json in rows:
        updated = json.loads(attrs_json).get("row_updated_at")
        if updated is None or store.epoch_us(updated) > store.epoch_us(audit_at):
            continue
        if (fid, rid) in seen:
            continue
        mine = any(v.strip().lower() in subject for (v,) in conn.execute(
            "select value from subject_key where obs_id = ? and kind != 'name'", (oid,)))
        if mine:
            seen.add((fid, rid))
            out.append({"type": "clock_choice_matters", "obs_id": oid, "file_id": fid,
                        "resource_id": rid, "row_updated_at": updated, "record_at": record_at})
    return out


def count_clock_choice(conn, cases):
    return sum(len(clock_choice_events(conn, c)) for c in cases)
