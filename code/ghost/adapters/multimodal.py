"""Adapter 8: screenshots and PDFs as facts (DESIGN §3, SPEC MM1/MM2).

A reader (ghost.multimodal_read, the only caller of the model) returns two independent reads of one
file in a fixed schema. This module turns them into facts and never trusts either blindly:

  - the resource id must already exist in the dataset (an activity, or another adapter's record);
    an id seen only in the image is dropped, never invented (`unknown_resource_id_dropped`);
  - an email, phone or user reference counts only if it equals a stored identity value; the rest
    is `foreign_identifier` (counted, never logged);
  - only the detail pane yields a resource; the other list rows are counted and ignored;
  - both reads must give the same validated facts, otherwise nothing is emitted for the file
    (`image_read_disagreement`);
  - a state is written only when the screen shows the field that decides it (connector `enabled`,
    backup `restore_allowed`); otherwise `no_state_field`;
  - `record_at` is the file's manifest capture time, and no capture scope or join key is written.

Agreed facts are kept in `extractions_dir/<file_id>.json` (hashes and ids only, no raw text), so a
run without a key reproduces the same observations.
"""
import hashlib
import json
import re
from pathlib import Path

from .. import policy, schema, store
from . import common, ledger_rows as lr

name = "multimodal"
SCHEMA_VERSION = 1
VARIANTS = ["A", "B"]
SCREEN_TIME_TOLERANCE_US = 24 * 3600 * 1_000_000

KINDS = ("account", "search", "analytics", "export", "support", "security", "registry",
         "object_storage", "connector", "backup", "marketing", "document_statement",
         "document_attachment", "document_audit_extract", "document_register", "other")

_STR_LIST = {"type": "array", "items": {"type": "string"}}
SCHEMA = {
    "type": "object",
    "required": ["screen_kind", "detail_resource_id", "detail_reference", "detail_identifiers",
                 "indicators", "screen_time", "context_row_count"],
    "properties": {
        "screen_kind": {"type": "string", "enum": list(KINDS)},
        "detail_resource_id": {"type": ["string", "null"]},
        "detail_reference": {"type": ["string", "null"]},
        "detail_identifiers": {
            "type": "object", "required": ["emails", "user_refs", "phones", "names"],
            "properties": {"emails": _STR_LIST, "user_refs": _STR_LIST, "phones": _STR_LIST,
                           "names": _STR_LIST},
            "additionalProperties": False},
        "indicators": {
            "type": "object",
            "required": ["enabled", "restore_allowed", "identifiers_present", "listed_present"],
            "properties": {
                "enabled": {"type": "string", "enum": ["enabled", "disabled", "not_shown"]},
                "restore_allowed": {"type": "string", "enum": ["allowed", "blocked", "not_shown"]},
                "identifiers_present": {"type": "boolean"},
                "listed_present": {"type": "boolean"}},
            "additionalProperties": False},
        "screen_time": {"type": ["string", "null"]},
        "context_row_count": {"type": "integer"},
    },
    "additionalProperties": False,
}

# screen kind -> (system the console belongs to, policy category). The console's own origin
# decides the system; nothing is inferred from activity rows.
KIND_MAP = {
    "account": ("account_service", "USER_PROFILE"),
    "search": ("search_service", "SEARCH_INDEX_ENTRY"),
    "analytics": ("analytics_platform", "ANALYTICS_PROFILE"),
    "export": ("export_service", "USER_EXPORT"),
    "support": ("support_platform", "SUPPORT_MESSAGE"),
    "security": ("security_service", "SECURITY_AUDIT_LOG"),
    "registry": ("identity_service", "ACCOUNT_TOMBSTONE"),
    "object_storage": ("object_storage", "SUPPORT_ATTACHMENT"),
    "connector": ("integration_service", policy.DATA_FLOW),
    "backup": ("backup_service", "BACKUP_ARCHIVE"),
    "marketing": ("marketing_platform", "MARKETING_PROFILE"),
    "document_statement": ("export_service", "USER_EXPORT"),
    "document_attachment": ("support_platform", "SUPPORT_ATTACHMENT"),
    "document_audit_extract": ("security_service", "SECURITY_AUDIT_LOG"),
}
_PRESENT = {"account", "search", "export", "security", "object_storage", "marketing",
            "document_statement", "document_attachment", "document_audit_extract"}
_ALIAS = re.compile(r"(?:linked|confirmed) (\S+) \(([^)]*)\)")


class NoTextLayer(Exception):
    """A PDF that has no extractable text; it is listed, never guessed."""


def detect(data):
    """'png', 'pdf' or None, by the file's own signature (never by its name)."""
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return "png"
    if data[:5] == b"%PDF-":
        return "pdf"
    return None


def sha_text(value):
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


# ---- what the dataset already knows ------------------------------------------------------------

class Known:
    """Ids and identities the image's own content is checked against."""

    def __init__(self, conn):
        self.aliases = {}
        known = set()
        for rid, target, down, details in conn.execute(
                "select resource_id, target_resource, downstream_resource, details from activity"):
            known.update(x for x in (rid, target, down) if x)
            for res, alias in _ALIAS.findall(details or ""):
                self.aliases.setdefault(alias, set()).add(res)
                known.add(res)
        known.update(r[0] for r in conn.execute(
            "select distinct resource_id from observation where resource_id is not null and "
            "file_id not in (select file_id from evidence_file where adapter = ?)", (name,)))
        self.resources = known
        self.users = {}
        self.link_users = {}
        for kind, value, user in conn.execute(
                "select kind, value, user_id from identity where kind in "
                "('email','phone','user_id')"):
            self.users.setdefault((kind, value.strip().lower() if kind != "phone"
                                   else value.strip()), set()).add(user)
        for target, user in conn.execute(
                "select target_resource, linked_user_id from activity "
                "where kind = 'link_identity' and linked_user_id is not null"):
            self.link_users.setdefault(target, set()).add(user)

    def identifier_users(self, kind, value):
        key = (kind, value.strip().lower() if kind != "phone" else value.strip())
        return self.users.get(key, set())

    def by_hash(self):
        """{('email_hash'|'phone_hash', sha256): (kind, value)} for every stored identity value."""
        out = {}
        for (kind, value), _users in self.users.items():
            if kind == "email":
                out[("email_hash", sha_text(value))] = ("email", value)
            elif kind == "phone":
                out[("phone_hash", sha_text(value))] = ("phone", value)
        return out


# ---- reads -> validated facts --------------------------------------------------------------------

def _facts(payload, known, manifest_at):
    """(canonical facts, notes) of one schema-valid read."""
    notes = []
    ident = payload["detail_identifiers"]
    refs, foreign, users = set(), 0, set()
    for kind, field in (("email", "emails"), ("phone", "phones"), ("user_id", "user_refs")):
        for raw in ident[field]:
            value = raw.strip()
            value = value.lower() if kind != "phone" else value
            found = known.identifier_users(kind, value)
            if found:
                refs.add((kind, value))
                users |= found
            else:
                foreign += 1
    visible = bool(ident["emails"] or ident["phones"] or ident["user_refs"])
    if foreign:
        notes.append({"type": "foreign_identifier", "count": foreign})
    if payload["context_row_count"]:
        notes.append({"type": "context_row_ignored", "count": payload["context_row_count"]})
    shown = payload["screen_time"]
    if shown:
        try:
            if abs(store.epoch_us(shown) - store.epoch_us(manifest_at)) > SCREEN_TIME_TOLERANCE_US:
                notes.append({"type": "screen_time_mismatch", "printed": shown,
                              "manifest": manifest_at})
        except (ValueError, TypeError):
            notes.append({"type": "screen_time_unparsed"})

    rid = (payload["detail_resource_id"] or "").strip() or None
    ref = (payload["detail_reference"] or "").strip() or None
    resource, ref_resolved = None, False
    if rid:
        if rid in known.resources:
            resource = rid
        else:
            notes.append({"type": "unknown_resource_id_dropped", "resource_id": rid})
    elif ref and len(known.aliases.get(ref, ())) == 1:
        resource, ref_resolved = next(iter(known.aliases[ref])), True
    if resource is None and not any(n["type"] == "unknown_resource_id_dropped" for n in notes):
        notes.append({"type": "no_resource_id"})

    ind = payload["indicators"]
    linked = known.link_users.get(resource, set()) if resource else set()
    conflict = visible and (not refs or (bool(linked) and not (users & linked)))
    facts = {
        "screen_kind": payload["screen_kind"],
        "resource_id": resource,
        # a label counts (and is compared between the two reads) only when it resolved the resource
        "detail_reference": ref if ref_resolved else None,
        "indicators": {
            "enabled": ind["enabled"], "restore_allowed": ind["restore_allowed"],
            "listed_present": ind["listed_present"], "identity_conflict": bool(conflict),
            "identifiers_present": bool(ind["identifiers_present"] or ident["emails"]
                                        or ident["phones"] or ident["names"])},
        "subject_refs": sorted([k, v] for k, v in refs),
    }
    return facts, notes


def _state(facts):
    """(state, field that is missing) for the facts' screen kind."""
    kind, ind = facts["screen_kind"], facts["indicators"]
    if kind in _PRESENT:
        return "PRESENT", None
    if kind == "analytics":
        return ("IDENTIFIABLE" if ind["identifiers_present"] else "ANONYMIZED"), None
    if kind in ("support", "registry"):
        return ("IDENTIFIABLE" if ind["identifiers_present"] else "REDACTED"), None
    if kind == "connector":
        return {"enabled": "ACTIVE", "disabled": "DISABLED"}.get(ind["enabled"]), "enabled"
    if kind == "backup":
        return ({"allowed": "RESTORABLE", "blocked": "BLOCKED"}.get(ind["restore_allowed"]),
                "restore_allowed")
    return None, None


def _observe(conn, file_id, manifest_at, facts):
    """Write the observation the facts support; returns the notes explaining any refusal."""
    kind, ind = facts["screen_kind"], facts["indicators"]
    if facts["resource_id"] is None:
        return []  # already explained (no_resource_id / unknown_resource_id_dropped)
    if ind["identity_conflict"]:
        return [{"type": "identity_mismatch", "resource_id": facts["resource_id"]}]
    if kind not in KIND_MAP:
        return [{"type": "unsupported_kind", "resource_id": facts["resource_id"]}]
    state, missing = _state(facts)
    if state is None:
        return [{"type": "no_state_field", "resource_id": facts["resource_id"],
                 "field": missing}]
    if not ind["listed_present"]:
        return [{"type": "not_listed", "resource_id": facts["resource_id"]}]
    system, category = KIND_MAP[kind]
    cur = conn.execute(
        "insert into observation(file_id, locator, record_at, system_id, resource_id, category, "
        "state, state_basis, attrs_json) values (?,?,?,?,?,?,?,?,?)",
        (file_id, "detail", manifest_at, system, facts["resource_id"], category, state, "explicit",
         json.dumps({"screen_kind": kind, "detail_reference": facts["detail_reference"],
                     "indicators": ind}, sort_keys=True)))
    conn.executemany("insert into subject_key values (?,?,?)",
                     [(cur.lastrowid, k, v) for k, v in facts["subject_refs"]])
    return []


# ---- the stored extraction -----------------------------------------------------------------------

def _store_refs(refs):
    out = []
    for kind, value in refs:
        if kind == "email":
            out.append(["email_hash", sha_text(value)])
        elif kind == "phone":
            out.append(["phone_hash", sha_text(value)])
        else:
            out.append([kind, value])
    return sorted(out)


def _write_extraction(path, file_id, data, model, facts):
    doc = {"file_id": file_id, "sha256": hashlib.sha256(data).hexdigest(),
           "schema_version": SCHEMA_VERSION, "model": model, "variants": VARIANTS,
           "screen_kind": facts["screen_kind"], "detail_resource_id": facts["resource_id"],
           "detail_reference": facts["detail_reference"], "indicators": facts["indicators"],
           "subject_refs": _store_refs(facts["subject_refs"])}
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(doc, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _read_extraction(path, data, known):
    """Facts from a stored extraction, or (None, reason) when it is missing, stale or unusable."""
    if not path.is_file():
        return None, None
    try:
        doc = json.loads(path.read_text(encoding="utf-8"))
        if (doc["schema_version"] != SCHEMA_VERSION
                or doc["sha256"] != hashlib.sha256(data).hexdigest()):
            return None, "file or schema changed"
        inverse = known.by_hash()
        refs = []
        for kind, value in doc["subject_refs"]:
            if kind in ("email_hash", "phone_hash"):
                refs.append(list(inverse[(kind, value)]))  # KeyError: identity no longer known
            else:
                refs.append([kind, value])
        return {"screen_kind": doc["screen_kind"], "resource_id": doc["detail_resource_id"],
                "detail_reference": doc["detail_reference"], "indicators": doc["indicators"],
                "subject_refs": sorted(refs)}, None
    except (OSError, ValueError, KeyError, TypeError):
        return None, "unreadable or no longer matches the identities"


# ---- ingest ----------------------------------------------------------------------------------------

def ingest(conn, file_id, data, reader, extractions_dir=None):
    """Read one PNG/PDF into the store. Returns the report dict (status, notes, observations).

    status: agreed (two live reads agree), loaded (stored extraction), disagreed (nothing
    emitted) or skipped (no reader / reader unavailable / no text layer; the file stays unclaimed).
    """
    from .. import llm  # the gateway's error type only; the SDK is never touched here
    kind = detect(data)
    if kind is None:
        raise ValueError(f"{file_id}: not a PNG or PDF")
    manifest_at = lr.manifest_of(conn, file_id)
    common.clear(conn, file_id)
    conn.execute("update evidence_file set format = null, adapter = null where file_id = ?",
                 (file_id,))
    known = Known(conn)
    notes, facts, live = [], None, False
    path = Path(extractions_dir) / f"{file_id}.json" if extractions_dir else None

    if path is not None:
        facts, why = _read_extraction(path, data, known)
        if why:
            notes.append({"type": "extraction_stale", "reason": why})
        if facts is not None:
            status = "loaded"
            if facts["resource_id"] is None:
                notes.append({"type": "no_resource_id"})
    if facts is None:
        if reader is None:
            return _report(conn, file_id, "skipped", [{"type": "no_reader"}] + notes, 0)
        try:
            reads = reader(file_id, kind, data)
        except llm.LLMUnavailable as e:
            return _report(conn, file_id, "skipped",
                           [{"type": "read_unavailable", "error": str(e)[:120]}] + notes, 0)
        except NoTextLayer as e:
            return _report(conn, file_id, "skipped",
                           [{"type": "no_text_layer", "error": str(e)[:120]}] + notes, 0)
        parsed = []
        for read in reads:
            try:
                schema.validate(read, SCHEMA)
                parsed.append(_facts(read, known, manifest_at))
            except (schema.SchemaError, ValueError, TypeError, KeyError) as e:
                parsed.append((None, [{"type": "invalid_read", "reason": str(e)[:120]}]))
        a, b = parsed[0], parsed[1]
        if a[0] is None or b[0] is None or a[0] != b[0]:
            notes.append({"type": "image_read_disagreement", "a": a[0], "b": b[0],
                          "invalid": [n for _, ns in parsed for n in ns
                                      if n["type"] == "invalid_read"]})
            return _report(conn, file_id, "disagreed", notes, 0, fmt=kind)
        facts, status, live = a[0], "agreed", True
        notes.extend(a[1])
        if path is not None:
            _write_extraction(path, file_id, data, getattr(reader, "model", None) or "unknown",
                              facts)
    refusal = _observe(conn, file_id, manifest_at, facts)
    notes.extend(refusal)
    n_obs = conn.execute("select count(*) from observation where file_id = ?",
                         (file_id,)).fetchone()[0]
    return _report(conn, file_id, status, notes, n_obs, fmt=kind)


def _report(conn, file_id, status, notes, n_obs, fmt=None):
    report = {"adapter": name, "file_id": file_id, "status": status, "notes": notes,
              "observations": n_obs, "meaningful": 1 if fmt else 0, "unparsed": 0}
    if fmt:  # the file was read (or loaded): claim it so it is not reported as unclaimed
        conn.execute("insert or replace into parse_report values (?,?,?,?,?,?)",
                     (file_id, name, 1, 1 if status != "disagreed" else 0, 0,
                      json.dumps(notes, sort_keys=True)))
        conn.execute("update evidence_file set format = ?, adapter = ? where file_id = ?",
                     (f"multimodal_{fmt}", name, file_id))
    return report
