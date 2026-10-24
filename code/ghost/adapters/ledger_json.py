"""Adapter 7, JSON container: a ledger exported as {"records": [row, ...]} (SPEC LG1).

Recognised by structure, never by file name (the file names are misleading: "connector
configurations" and "export manifest" both hold mixed-system ledgers): a JSON object whose
`records` list holds, for at least 90 % of its entries, objects with exactly the six ledger
fields. The rows are validated and written by `ledger_rows`; this module only finds them and
accounts for damaged records, a `record_count` that disagrees with the list, and repeats.
"""
import json

from . import common, ledger_rows as lr

name = "ledger_json"
FORMAT = "ledger_json"
DOMINANT = 0.9


def _load(text, strict=True):
    """The decoded document when it is a ledger-shaped JSON object, else None. Ingest is lenient
    (strict=False): it only asks that some record looks like a ledger row (at least half of the
    six fields), so a damaged file is read and its bad records are counted, not refused."""
    try:
        data = json.loads(text)
    except (ValueError, RecursionError):
        return None
    if not isinstance(data, dict) or not isinstance(data.get("records"), list):
        return None
    records = data["records"]
    if not strict:
        like = any(isinstance(r, dict) and len(set(r) & set(lr.FIELDS)) >= len(lr.FIELDS) // 2
                   for r in records)
        return data if like else None
    shaped = sum(1 for r in records if isinstance(r, dict) and set(r) == set(lr.FIELDS))
    return data if shaped and shaped >= DOMINANT * len(records) else None


def detect(text):
    """The format name when the text is a JSON ledger, else None."""
    return FORMAT if _load(text) is not None else None


def ingest(conn, file_id, text):
    """Parse one JSON ledger into the store; returns the parse report dict."""
    manifest_at = lr.manifest_of(conn, file_id)
    data = _load(text, strict=False)
    if data is None:
        raise ValueError(f"{file_id}: not a JSON ledger")
    common.clear(conn, file_id)
    records = data["records"]
    notes, meaningful, unparsed, entries, seen = [], 0, 0, [], {}
    stamp = lr.time_note(data.get("exported_at"), manifest_at)
    if stamp:
        notes.append(stamp)
    count = data.get("record_count")
    if count != len(records):
        meaningful += 1
        unparsed += 1
        notes.append({"type": "record_count_disagrees", "declared": count, "records": len(records)})
    for i, rec in enumerate(records):
        loc = f"records[{i}]"
        if not isinstance(rec, dict) or set(rec) != set(lr.FIELDS):
            meaningful += 1
            unparsed += 1
            why = "not an object" if not isinstance(rec, dict) else (
                "missing " + ",".join(sorted(set(lr.FIELDS) - set(rec))) if set(lr.FIELDS) - set(rec)
                else "unexpected " + ",".join(sorted(set(rec) - set(lr.FIELDS))))
            notes.append({"type": "line_unparsed", "locator": loc, "reason": why[:80]})
            continue
        fingerprint = json.dumps(rec, sort_keys=True)
        if fingerprint in seen:
            notes.append({"type": "duplicate_record", "locator": loc, "first": seen[fingerprint]})
            continue
        seen[fingerprint] = loc
        entries.append((loc, rec, {}))
    return lr.ingest_rows(conn, file_id, name, FORMAT, entries,
                          {"meaningful": meaningful, "unparsed": unparsed, "notes": notes,
                           "noise": {}})
