"""Record mapping shared by the adapters whose evidence holds the same store records.

`record` turns one store record (the `record_json` of a replica row, or a bundle record) into
(state, attrs, subject keys) for its collection. `LEDGER_STATE` is the vocabulary of ledger rows.
"""
import re

_EMAIL = re.compile(r"[\w.+'-]+@[\w-]+(?:\.[\w-]+)+")
_EMAIL_ONLY = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
_HASH = re.compile(r"^[0-9a-fA-F]{64}$")
_USER = re.compile(r"^user_\d+$")
_PHONE = re.compile(r"^\+?[\d\s().-]{7,}$")

# Ledger `result` -> canonical state (DESIGN §1). BACKUP_ARCHIVE rows say ACTIVE for a restorable
# archive (assumption A-1 of T10); every other category keeps the ledger word.
LEDGER_STATE = {"FOUND": "PRESENT", "NOT_FOUND": "ABSENT", "IDENTIFIABLE": "IDENTIFIABLE",
                "ANONYMIZED": "ANONYMIZED", "REDACTED": "REDACTED", "ACTIVE": "ACTIVE",
                "DISABLED": "DISABLED", "BLOCKED": "BLOCKED"}
LEDGER_BACKUP_STATE = {"ACTIVE": "RESTORABLE", "BLOCKED": "BLOCKED"}


def clear(conn, file_id):
    """Forget everything an earlier ingest of this file wrote (re-ingest is idempotent)."""
    conn.execute("delete from subject_key where obs_id in "
                 "(select obs_id from observation where file_id = ?)", (file_id,))
    for t in ("observation", "join_key", "capture_scope", "parse_report"):
        conn.execute(f"delete from {t} where file_id = ?", (file_id,))


class Skip(Exception):
    """The row cannot be turned into an observation (counted as unparsed)."""


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


def keys(*pairs, names=(), texts=()):
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


def lower(v):
    return v.strip().lower() if isinstance(v, str) and v.strip() else v


def any_set(*vals):
    return any(v not in (None, "") for v in vals)


def record(collection, rec):
    """(state, attrs, subject keys) of one store record."""
    if collection == "customer_profiles":
        return "PRESENT", {"name": rec.get("full_name"), "email": lower(rec.get("email")),
                           "phone": rec.get("phone")}, keys(
            rec.get("email"), rec.get("phone"), rec.get("customer_reference"),
            names=[rec.get("full_name")])
    if collection == "customer_search_documents":
        doc = rec.get("document") or {}
        return "PRESENT", {"email": lower(doc.get("email"))}, keys(
            doc.get("email"), doc.get("customer_reference"), names=[doc.get("display_name")])
    if collection == "customer_analytics_reports":
        fields = {f: rec.get(f) for f in ("user_id", "email", "phone", "email_hash")}
        state = "IDENTIFIABLE" if any_set(*fields.values()) else "ANONYMIZED"
        return state, {**fields, "email": lower(fields["email"]),
                       "email_hash": lower(fields["email_hash"])}, keys(*fields.values())
    if collection == "support_conversations":
        text = rec.get("message_text") or ""
        emails = [e.lower() for e in _EMAIL.findall(text)]
        return ("IDENTIFIABLE" if emails else "REDACTED"), {
            "email": emails[0] if emails else None}, keys(
            rec.get("subject_reference"), texts=[text])
    if collection == "ticket_attachments":
        return "PRESENT", {}, keys(rec.get("uploaded_by"))
    if collection == "customer_export_jobs":
        return "PRESENT", {}, keys(rec.get("owner_email"))
    if collection == "connector_configurations":
        on = bool(rec.get("enabled"))
        return ("ACTIVE" if on else "DISABLED"), {"enabled": on}, keys(rec.get("subject_selector"))
    if collection == "backup_catalog":
        ok = bool(rec.get("restore_allowed"))
        return ("RESTORABLE" if ok else "BLOCKED"), {"restore_allowed": ok}, keys(
            rec.get("subject_reference"))
    if collection == "security_audit_events":
        return "PRESENT", {}, keys(rec.get("subject_reference"))
    if collection == "retained_order_records":
        return "PRESENT", {"hold_until": rec.get("retained_until")}, keys(
            rec.get("subject_reference"))
    if collection == "deleted_customer_references":
        fields = {f: rec.get(f) for f in ("name", "email", "phone", "email_hash")}
        state = "IDENTIFIABLE" if any_set(*fields.values()) else "REDACTED"
        return state, {**fields, "email": lower(fields["email"]),
                       "email_hash": lower(fields["email_hash"]),
                       "subject_reference": rec.get("subject_reference")}, keys(
            rec.get("subject_reference"), fields["email"], fields["phone"], fields["email_hash"],
            names=[fields["name"]])
    raise Skip(f"unknown collection {collection!r}")
