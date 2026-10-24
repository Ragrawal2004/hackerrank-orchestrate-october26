"""Ledger rows (DESIGN §3, adapter 7; SPEC LG1): the one row schema shared by the three containers.

A ledger row is `recorded_at, system, resource_id, record_type, result, subject_reference`. The
containers (JSON list, `record_lookup` log lines, shift-handover text) only find the rows and
account for their own damaged units; this module validates a row and writes it.

Clock (SPEC U3): record_at is the row's own `recorded_at`, never the file's manifest time.
Scope: none. A ledger is a list of targeted checks, not a whole-store capture, so a resource
missing from it proves nothing; no capture_scope row and no join_key row is written.
Category and state: `record_type` is the policy category and `result` the state (SPEC B1 words:
FOUND->PRESENT, NOT_FOUND->ABSENT, the rest unchanged, BACKUP_ARCHIVE ACTIVE->RESTORABLE).
A result that does not fit its category, an unknown record_type or a malformed field is unparsed.
"""
import json

from .. import policy, store
from . import common

FIELDS = ("recorded_at", "system", "resource_id", "record_type", "result", "subject_reference")
_PRESENCE = frozenset({"FOUND", "NOT_FOUND"})
RESULTS = {  # category -> results that make sense for it; the default is presence
    "SUPPORT_MESSAGE": frozenset({"IDENTIFIABLE", "REDACTED"}),
    "ANALYTICS_PROFILE": frozenset({"IDENTIFIABLE", "ANONYMIZED"}),
    "ACCOUNT_TOMBSTONE": frozenset({"IDENTIFIABLE", "REDACTED"}),
    policy.DATA_FLOW: frozenset({"ACTIVE", "DISABLED"}),
    "BACKUP_ARCHIVE": frozenset({"ACTIVE", "BLOCKED"}),
}


def _text(v):
    return isinstance(v, str) and bool(v.strip())


def state_of(rtype, result):
    if rtype == "BACKUP_ARCHIVE":
        return common.LEDGER_BACKUP_STATE[result]
    return common.LEDGER_STATE[result]


def check(fields):
    """(state, attrs, subject keys, notes) of one row, or raise common.Skip."""
    for f in ("recorded_at", "system", "resource_id", "record_type", "result"):
        if not _text(fields.get(f)):
            raise common.Skip(f"{f} missing or not text")
    if not isinstance(fields.get("subject_reference"), str):
        raise common.Skip("subject_reference missing or not text")
    try:
        store.epoch_us(fields["recorded_at"])
    except (ValueError, TypeError, AttributeError):
        raise common.Skip(f"recorded_at is not a time: {fields['recorded_at']!r}")
    rtype, result = fields["record_type"], fields["result"]
    if rtype not in policy.CATEGORIES:
        raise common.Skip(f"unknown record_type {rtype!r}")
    if result not in RESULTS.get(rtype, _PRESENCE):
        raise common.Skip(f"result {result!r} does not fit {rtype}")
    subject, notes = fields["subject_reference"].strip(), []
    keys = set()
    if not subject:
        if result != "ANONYMIZED":
            raise common.Skip("blank subject_reference on a record that is not anonymized")
        notes.append("blank_subject")
    elif (c := common.classify(subject)) is not None:
        keys.add(c)
    else:
        keys.add(("name", subject))
        notes.append("non_identifier_subject")
    return state_of(rtype, result), {"record_type": rtype, "result": result}, keys, notes


def ingest_rows(conn, file_id, adapter, fmt, entries, base):
    """Write the rows and return the parse report dict.

    entries: [(locator, fields dict, extra attrs)] for the rows the container could read.
    base: the container's own accounting {"meaningful", "unparsed", "notes", "noise"}."""
    manifest_at = conn.execute("select manifest_at from evidence_file where file_id = ?",
                               (file_id,)).fetchone()[0]
    limit = store.epoch_us(manifest_at)
    notes = list(base["notes"])
    meaningful, unparsed = base["meaningful"], base["unparsed"]
    n_obs, seen = 0, {}
    for loc, fields, extra in entries:
        meaningful += 1
        try:
            state, attrs, keys, flags = check(fields)
        except common.Skip as e:
            unparsed += 1
            notes.append({"type": "line_unparsed", "locator": loc, "reason": str(e)[:80]})
            continue
        rid = fields["resource_id"]
        for flag in flags:
            notes.append({"type": flag, "locator": loc, "resource_id": rid})
        if rid in seen:
            notes.append({"type": "repeated_resource", "locator": loc, "resource_id": rid,
                          "first": seen[rid]})
        seen.setdefault(rid, loc)
        cur = conn.execute(
            "insert into observation(file_id, locator, record_at, system_id, resource_id, "
            "category, state, state_basis, attrs_json) values (?,?,?,?,?,?,?,?,?)",
            (file_id, loc, fields["recorded_at"], fields["system"], rid,
             fields["record_type"], state, "explicit",
             json.dumps({**attrs, "container": fmt, **extra}, sort_keys=True)))
        conn.executemany("insert into subject_key values (?,?,?)",
                         [(cur.lastrowid, k, v) for k, v in sorted(keys)])
        n_obs += 1
        if store.epoch_us(fields["recorded_at"]) > limit:
            notes.append({"type": "record_after_manifest", "locator": loc, "resource_id": rid})
    notes.append({"type": "noise_events", "counts": dict(sorted(base["noise"].items()))})
    parsed = meaningful - unparsed
    report = {"meaningful": meaningful, "parsed": parsed, "unparsed": unparsed,
              "observations": n_obs, "join_keys": 0, "noise": sum(base["noise"].values()),
              "notes": notes}
    conn.execute("insert into parse_report values (?,?,?,?,?,?)",
                 (file_id, adapter, meaningful, parsed, unparsed, json.dumps(notes, sort_keys=True)))
    conn.execute("update evidence_file set format = ?, adapter = ? where file_id = ?",
                 (fmt, adapter, file_id))
    return report


def manifest_of(conn, file_id):
    row = conn.execute("select manifest_at from evidence_file where file_id = ?",
                       (file_id,)).fetchone()
    if row is None:
        raise ValueError(f"{file_id}: not in evidence_file")
    return row[0]


def time_note(stamp, manifest_at):
    """A capture_time_mismatch note when `stamp` is a time different from the manifest, else None."""
    try:
        if stamp is not None and store.epoch_us(stamp) != store.epoch_us(manifest_at):
            return {"type": "capture_time_mismatch", "header": stamp, "manifest": manifest_at}
    except (ValueError, TypeError, AttributeError):
        pass
    return None
