"""Adapter 2: snapshot bundle JSON (DESIGN §3, adapter 2; SPEC B1).

Recognised by structure, never by file name: a JSON object whose `snapshots` list holds objects
with captured_at, data_store, collection and records.

Clock decision (SPEC B1, the U3 two-clock rule): every observation's record_at is its own clock,
the snapshot's captured_at (a history row's recorded_at), never the file's manifest time. The
store holds no case, so records dated after some case's audit_at are written like any other and
the dossier's clock gate leaves them out.
"""
import json

from .. import policy, store
from . import common

name = "snapshot_bundle"
FORMAT = "json_snapshot_bundle"
HISTORY = "operational_record_history"
ENVELOPE = ("captured_at", "data_store", "collection", "records")


def _load(text):
    """The snapshots list when the text is a bundle, else None."""
    try:
        data = json.loads(text)
    except (ValueError, RecursionError):
        return None
    if not isinstance(data, dict):
        return None
    snaps = data.get("snapshots")
    if not isinstance(snaps, list) or not any(
            isinstance(s, dict) and all(k in s for k in ENVELOPE) for s in snaps):
        return None
    return data


def detect(text):
    """The format name when the text is a snapshot bundle, else None."""
    return FORMAT if _load(text) is not None else None


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


def _envelope_ok(snap):
    return (isinstance(snap, dict) and _time(snap.get("captured_at")) and _text(snap.get("data_store"))
            and _text(snap.get("collection")) and isinstance(snap.get("records"), list)
            and (snap["collection"] == HISTORY or snap["collection"] in policy.COLLECTION_CATEGORY))


def ingest(conn, file_id, text):
    """Parse one snapshot bundle into the store; returns the parse report dict."""
    row = conn.execute("select manifest_at from evidence_file where file_id = ?",
                       (file_id,)).fetchone()
    if row is None:
        raise ValueError(f"{file_id}: not in evidence_file")
    manifest_at = row[0]
    data = _load(text)
    if data is None:
        raise ValueError(f"{file_id}: not a snapshot bundle")
    limit = store.epoch_us(manifest_at)
    notes, meaningful, unparsed, n_obs = [], 0, 0, 0
    exported = data.get("exported_at")
    if _time(exported) and store.epoch_us(exported) != limit:
        notes.append({"type": "capture_time_mismatch", "header": exported, "manifest": manifest_at})

    common.clear(conn, file_id)
    source = data.get("source")
    valid = []  # (index, snapshot) of well-formed regular snapshots, for the scope rule

    def insert(locator, record_at, system, rid, category, state, attrs, keys):
        nonlocal n_obs
        if store.epoch_us(record_at) > limit:
            notes.append({"type": "record_after_manifest", "locator": locator, "resource_id": rid})
        cur = conn.execute(
            "insert into observation(file_id, locator, record_at, system_id, resource_id, "
            "category, state, state_basis, attrs_json) values (?,?,?,?,?,?,?,?,?)",
            (file_id, locator, record_at, system, rid, category, state, "explicit",
             json.dumps(attrs, sort_keys=True)))
        conn.executemany("insert into subject_key values (?,?,?)",
                         [(cur.lastrowid, k, v) for k, v in sorted(keys)])
        n_obs += 1

    for i, snap in enumerate(data["snapshots"]):
        records = snap.get("records") if isinstance(snap, dict) else None
        n = len(records) if isinstance(records, list) else 0
        if not _envelope_ok(snap):
            meaningful += 1 + n
            unparsed += 1 + n
            notes.append({"type": "snapshot_unparsed", "index": i})
            continue
        meaningful += 1 + n
        at, system, collection = snap["captured_at"], snap["data_store"], snap["collection"]
        if collection != HISTORY:
            valid.append((i, snap))
        for j, rec in enumerate(records):
            where = f"snapshots[{i}].records[{j}]"
            try:
                if collection == HISTORY:
                    _history(insert, where, system, at, source, exported, rec)
                else:
                    _regular(insert, where, system, at, collection, source, exported, i, rec)
            except (common.Skip, ValueError, TypeError, AttributeError, KeyError) as e:
                unparsed += 1
                notes.append({"type": "row_unparsed", "locator": where, "reason": str(e)[:80]})

    _scopes(conn, file_id, valid)
    parsed = meaningful - unparsed
    report = {"meaningful": meaningful, "parsed": parsed, "unparsed": unparsed,
              "observations": n_obs, "join_keys": 0, "notes": notes}
    conn.execute("insert into parse_report values (?,?,?,?,?,?)",
                 (file_id, name, meaningful, parsed, unparsed, json.dumps(notes, sort_keys=True)))
    conn.execute("update evidence_file set format = ?, adapter = ? where file_id = ?",
                 (FORMAT, name, file_id))
    return report


def _regular(insert, where, system, at, collection, source, exported, index, rec):
    if not isinstance(rec, dict):
        raise common.Skip("record is not an object")
    rid = rec.get("resource_id")
    if not _text(rid):
        raise common.Skip("resource_id missing")
    state, attrs, keys = common.record(collection, rec)
    attrs = {**attrs, "collection": collection, "snapshot_at": at, "snapshot_index": index,
             "source": source, "exported_at": exported}
    insert(where, at, system, rid, policy.COLLECTION_CATEGORY[collection], state, attrs, keys)


def _history(insert, where, store_name, at, source, exported, row):
    """A history row is a state event: its own recorded_at, its own system and category."""
    if not isinstance(row, dict):
        raise common.Skip("history row is not an object")
    when = _time(row.get("recorded_at"))
    rid, system = row.get("resource_id"), row.get("system")
    category, result = row.get("record_type"), row.get("result")
    if not (when and _text(rid) and _text(system)):
        raise common.Skip("history row missing recorded_at, resource_id or system")
    if category not in policy.CATEGORIES or result not in common.LEDGER_STATE:
        raise common.Skip(f"unknown record_type {category!r} or result {result!r}")
    state = common.LEDGER_STATE[result]
    if category == "BACKUP_ARCHIVE":
        state = common.LEDGER_BACKUP_STATE.get(result, state)
    lookup = row.get("lookup_value")
    attrs = {"collection": HISTORY, "record_type": category, "result": result,
             "lookup_value": lookup if isinstance(lookup, str) else None,
             "ledger_store": store_name, "snapshot_at": at, "source": source, "exported_at": exported}
    insert(where, when, system, rid, category, state, attrs, common.keys(lookup))


def _scopes(conn, file_id, valid):
    """One complete scope row per store and time, but only where the capture covers every
    collection that store carries in this file (the scope has no collection column)."""
    wanted, seen, first = {}, {}, {}
    for _, snap in valid:
        wanted.setdefault(snap["data_store"], set()).add(snap["collection"])
        key = (snap["data_store"], store.epoch_us(snap["captured_at"]))
        seen.setdefault(key, set()).add(snap["collection"])
        first.setdefault(key, snap["captured_at"])
    for (system, _), got in sorted(seen.items(), key=lambda kv: (kv[0][0], kv[0][1])):
        if got == wanted[system]:
            conn.execute("insert into capture_scope values (?,?,?,1)",
                         (file_id, system, first[(system, _)]))
