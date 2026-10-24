"""Adapter 7, log container: ledger rows as `record_lookup` lines (SPEC LG1).

    TS LEVEL service=S event=record_lookup seq=N system="..." resource_id="..."
        record_type="..." result="..." subject_reference="..."

The level is a bare word, so the lines belong to none of the six observability syntaxes and are
tokenized by `logtok.parse_leveled_line`. Recognised by structure, never by file name: at least
90 % of the non-blank lines are `record_lookup` lines carrying the six ledger fields. The level
(INFO/WARN) is kept as an attribute and decides nothing. A gap in `seq` is a note (lines may have
been dropped; no line is at fault). An unlisted event carrying a resource or subject is unparsed,
never noise.
"""
from . import common, ledger_rows as lr, logtok

name = "ledger_log"
FORMAT = "ledger_log"
DOMINANT = 0.9
EVENT = "record_lookup"


KEYS = tuple(f for f in lr.FIELDS if f != "recorded_at")  # recorded_at is the line timestamp


def _lines(text):
    return [(i, t, logtok.parse_leveled_line(t)) for i, t in enumerate(text.splitlines(), 1)
            if t.strip()]


def _is_row(p):
    return p is not None and p.event == EVENT and all(f in p.kv for f in KEYS)


def detect(text):
    """The format name when the text is a log-line ledger, else None."""
    lines = _lines(text)
    rows = sum(1 for _, _, p in lines if _is_row(p))
    return FORMAT if rows and rows >= DOMINANT * len(lines) else None


def ingest(conn, file_id, text):
    """Parse one log-line ledger into the store; returns the parse report dict."""
    manifest_at = lr.manifest_of(conn, file_id)
    lines = _lines(text)
    if not any(p is not None for _, _, p in lines):
        raise ValueError(f"{file_id}: not a log-line ledger")
    common.clear(conn, file_id)
    notes, noise, entries, seen, seqs = [], {}, [], {}, []
    meaningful = unparsed = 0
    for line_no, raw, p in lines:
        loc = f"L{line_no}"
        if raw in seen:
            notes.append({"type": "duplicate_line", "locator": loc, "first": seen[raw]})
            continue
        seen[raw] = loc
        if p is None:
            meaningful += 1
            unparsed += 1
            notes.append({"type": "line_unparsed", "locator": loc, "head": raw[:60]})
            continue
        kv = p.kv
        if p.event == EVENT:
            if not all(f in kv for f in KEYS):
                meaningful += 1
                unparsed += 1
                notes.append({"type": "line_unparsed", "locator": loc, "reason": "ledger field missing"})
                continue
            if isinstance(kv.get("seq"), str) and kv["seq"].isdigit():
                seqs.append(int(kv["seq"]))
            entries.append((loc, {**{f: kv[f] for f in KEYS}, "recorded_at": p.ts},
                            {"seq": kv.get("seq"), "level": p.level, "service": p.service}))
        elif kv.get("resource_id") or kv.get("subject_reference"):
            meaningful += 1
            unparsed += 1
            notes.append({"type": "unlisted_event", "locator": loc, "event": p.event,
                          "resource_id": kv.get("resource_id")})
        else:
            noise[p.event] = noise.get(p.event, 0) + 1
    ordered = sorted(seqs)
    missing = sorted(set(range(ordered[0], ordered[-1] + 1)) - set(ordered)) if ordered else []
    if missing:
        notes.append({"type": "seq_gap", "missing": missing[:20], "count": len(missing)})
    return lr.ingest_rows(conn, file_id, name, FORMAT, entries,
                          {"meaningful": meaningful, "unparsed": unparsed, "notes": notes,
                           "noise": noise})
