"""Adapter 3: service read-back export JSON (DESIGN §3, adapter 3; SPEC S1).

Recognised by structure, never by file name: a JSON object whose `service_exports` maps service
ids to sections, each a list of rows that carry a `resource_id` and an `observed_at`.

Clock decision (SPEC S1, the U3 two-clock rule): every observation's record_at is the row's own
`observed_at`, never the file's manifest time. Rows dated after some case's audit_at are stored
like any other and the dossier's clock gate leaves them out.

Scope decision (SPEC S1): the export is a targeted read-back, not a whole-store dump, so a
resource missing from it proves nothing. No capture_scope row is written; the only absence
evidence is an explicit 404 profile lookup or an explicit 0-hit index query. The format holds no
foreign references, so no join_key row is written either.
"""
import json

from .. import policy, store
from . import common

name = "service_export"
FORMAT = "json_service_export"
SECTION = "section_unparsed"


def _text(v):
    return isinstance(v, str) and bool(v.strip())


def _time(v):
    """The value when it is a parseable ISO timestamp string, else None."""
    if not isinstance(v, str):
        return None
    try:
        return v if store.epoch_us(v) is not None else None
    except (ValueError, TypeError, AttributeError):
        return None


def _flag(v):
    if not isinstance(v, bool):
        raise common.Skip("flag is not a boolean")
    return v


def _count(v):
    if isinstance(v, bool) or not isinstance(v, int) or v < 0:
        raise common.Skip("count is not a non-negative integer")
    return v


# ---- section mappers: row -> (state, attrs, subject keys) -------------------------------------

def _profile(row):
    status, profile = row.get("status_code"), row.get("profile")
    if isinstance(status, bool) or not isinstance(status, int):
        raise common.Skip("status_code is not an integer")
    if status == 200 and isinstance(profile, dict):
        return "PRESENT", {"name": profile.get("full_name"), "email": common.lower(profile.get("email")),
                           "phone": profile.get("phone")}, common.keys(
            profile.get("email"), profile.get("phone"), profile.get("customer_reference"),
            profile.get("email_hash"), names=[profile.get("full_name")])
    if status == 404 and profile is None:
        return "ABSENT", {}, set()
    raise common.Skip(f"status {status} does not agree with the profile")


def _analytics(row):
    fields = {"user_id": row.get("customer_reference"), "email": row.get("email"),
              "phone": row.get("phone"), "email_hash": row.get("email_hash")}
    state = "IDENTIFIABLE" if common.any_set(*fields.values()) else "ANONYMIZED"
    return state, {**fields, "email": common.lower(fields["email"]),
                   "email_hash": common.lower(fields["email_hash"]),
                   "audience_cohort": row.get("audience_cohort")}, common.keys(*fields.values())


def _restore(row):
    ok = _flag(row.get("restore_allowed"))
    return ("RESTORABLE" if ok else "BLOCKED"), {"restore_allowed": ok}, common.keys(
        row.get("customer_email"), row.get("customer_reference"))


def _export(row):
    return "PRESENT", {}, common.keys(row.get("owner_email"), row.get("owner_phone"),
                                      row.get("customer_reference"))


def _connector(row):
    on = _flag(row.get("enabled"))
    return ("ACTIVE" if on else "DISABLED"), {"enabled": on}, common.keys(
        row.get("customer_email"), row.get("customer_reference"))


def _query_key(query):
    """The lookup key by shape: email, phone, hash or user id keep their kind; plain text is a
    name key, which never links on its own."""
    return {common.classify(query) or ("name", query.strip())}


def _search(row):
    query, hits = row.get("query"), row.get("hits")
    if not _text(query):
        raise common.Skip("query missing")
    if not isinstance(hits, dict) or not isinstance(hits.get("documents"), list):
        raise common.Skip("hits has no documents list")
    docs, total = hits["documents"], _count(hits.get("total"))
    if total != len(docs):
        raise common.Skip("hits total disagrees with the documents")
    out = _query_key(query)
    for doc in docs:
        if not isinstance(doc, dict) or doc.get("resource_id") != row.get("resource_id"):
            raise common.Skip("hit document is not this resource")
        out |= common.keys(doc.get("email"), doc.get("phone"), doc.get("customer_reference"),
                           doc.get("email_hash"), names=[doc.get("display_name")])
    return ("PRESENT" if docs else "ABSENT"), {"hits": total}, out


def _audit(row):
    if not _text(row.get("subject_reference")):
        raise common.Skip("subject_reference missing")
    until = _time(row.get("retention_until"))
    if until is None:
        raise common.Skip("retention_until is not a time")
    return "PRESENT", {"retention_until": until, "event_code": row.get("event_code")}, common.keys(
        row.get("subject_reference"))


def _ticket(row):
    fields = (row.get("requester_email"), row.get("requester_name"), row.get("requester_phone"))
    email = common.lower(row.get("requester_email"))
    return ("IDENTIFIABLE" if common.any_set(*fields) else "REDACTED"), {
        "email": email if isinstance(email, str) else None}, common.keys(
        row.get("requester_email"), row.get("requester_phone"), row.get("customer_reference"),
        names=[row.get("requester_name")])


# (service, section) -> (store collection, mapper)
SECTIONS = {
    ("account_service", "profile_lookups"): ("customer_profiles", _profile),
    ("analytics_platform", "profiles"): ("customer_analytics_reports", _analytics),
    ("backup_service", "restore_entries"): ("backup_catalog", _restore),
    ("export_service", "exports"): ("customer_export_jobs", _export),
    ("integration_service", "connectors"): ("connector_configurations", _connector),
    ("search_service", "index_queries"): ("customer_search_documents", _search),
    ("security_service", "audit_records"): ("security_audit_events", _audit),
    ("support_platform", "tickets"): ("support_conversations", _ticket),
}


# ---- detection ---------------------------------------------------------------------------------

def _is_row(r):
    return isinstance(r, dict) and _text(r.get("resource_id")) and _text(r.get("observed_at"))


def _load(text):
    """The parsed export when the text is one, else None."""
    try:
        data = json.loads(text)
    except (ValueError, RecursionError):
        return None
    if not isinstance(data, dict):
        return None
    services = data.get("service_exports")
    if not isinstance(services, dict) or not any(
            isinstance(sections, dict) and any(
                isinstance(rows, list) and any(_is_row(r) for r in rows)
                for rows in sections.values())
            for sections in services.values()):
        return None
    return data


def detect(text):
    """The format name when the text is a service read-back export, else None."""
    return FORMAT if _load(text) is not None else None


# ---- ingest ------------------------------------------------------------------------------------

def ingest(conn, file_id, text):
    """Parse one service export into the store; returns the parse report dict."""
    row = conn.execute("select manifest_at from evidence_file where file_id = ?",
                       (file_id,)).fetchone()
    if row is None:
        raise ValueError(f"{file_id}: not in evidence_file")
    manifest_at = row[0]
    data = _load(text)
    if data is None:
        raise ValueError(f"{file_id}: not a service export")
    limit = store.epoch_us(manifest_at)
    notes, meaningful, unparsed, n_obs = [], 0, 0, 0
    exported = data.get("exported_at")
    if _time(exported) and store.epoch_us(exported) != limit:
        notes.append({"type": "capture_time_mismatch", "header": exported, "manifest": manifest_at})

    common.clear(conn, file_id)
    envelope = {"region": data.get("region"), "source": data.get("source"), "exported_at": exported}

    def bad_section(svc, sec, n):
        nonlocal meaningful, unparsed
        meaningful += n
        unparsed += n
        notes.append({"type": SECTION, "service": svc, "section": sec, "rows": n})

    for svc, sections in data["service_exports"].items():
        if not isinstance(sections, dict):
            bad_section(svc, None, 1)
            continue
        for sec, rows in sections.items():
            known = SECTIONS.get((svc, sec))
            if known is None or not isinstance(rows, list):
                bad_section(svc, sec, max(1, len(rows)) if isinstance(rows, list) else 1)
                continue
            collection, mapper = known
            for i, r in enumerate(rows):
                meaningful += 1
                where = f"service_exports.{svc}.{sec}[{i}]"
                try:
                    if not isinstance(r, dict):
                        raise common.Skip("row is not an object")
                    rid, at = r.get("resource_id"), _time(r.get("observed_at"))
                    if not _text(rid):
                        raise common.Skip("resource_id missing")
                    if at is None:
                        raise common.Skip("observed_at missing or not a time")
                    state, attrs, keys = mapper(r)
                except (common.Skip, ValueError, TypeError, AttributeError, KeyError) as e:
                    unparsed += 1
                    notes.append({"type": "row_unparsed", "locator": where, "reason": str(e)[:80]})
                    continue
                if store.epoch_us(at) > limit:
                    notes.append({"type": "record_after_manifest", "locator": where,
                                  "resource_id": rid})
                attrs = {**attrs, **envelope, "collection": collection, "section": sec,
                         "service": svc, "observed_at": at}
                cur = conn.execute(
                    "insert into observation(file_id, locator, record_at, system_id, resource_id, "
                    "category, state, state_basis, attrs_json) values (?,?,?,?,?,?,?,?,?)",
                    (file_id, where, at, svc, rid, policy.COLLECTION_CATEGORY[collection], state,
                     "explicit", json.dumps(attrs, sort_keys=True)))
                conn.executemany("insert into subject_key values (?,?,?)",
                                 [(cur.lastrowid, k, v) for k, v in sorted(keys)])
                n_obs += 1

    parsed = meaningful - unparsed
    report = {"meaningful": meaningful, "parsed": parsed, "unparsed": unparsed,
              "observations": n_obs, "join_keys": 0, "notes": notes}
    conn.execute("insert into parse_report values (?,?,?,?,?,?)",
                 (file_id, name, meaningful, parsed, unparsed, json.dumps(notes, sort_keys=True)))
    conn.execute("update evidence_file set format = ?, adapter = ? where file_id = ?",
                 (FORMAT, name, file_id))
    return report
