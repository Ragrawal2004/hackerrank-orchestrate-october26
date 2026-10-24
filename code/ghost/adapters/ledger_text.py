"""Adapter 7, text container: ledger rows in a shift-handover note (SPEC LG1).

    - TS | system | resource_id | record_type | result | subject_reference

Prose (a title, a `Snapshot: TS` line, sentences) around the rows is ignored, but a `Snapshot:`
time that differs from the manifest is a note. Recognised by structure: at least one valid row and
at least 90 % of the `|` lines are valid rows. Any other line holding ` | ` that is not a valid
row is unparsed (a damaged row is never dropped as prose); a dash line with no pipe is prose.
"""
import re

from . import common, ledger_rows as lr

name = "ledger_text"
FORMAT = "ledger_text"
DOMINANT = 0.9
_SNAPSHOT = re.compile(r"^\s*Snapshot:\s*(\S+)\s*$", re.I)


def _pipe_lines(text):
    """[(line_no, raw, fields dict or None)] for every line that looks like a table row."""
    out = []
    for n, raw in enumerate(text.splitlines(), 1):
        if " | " not in raw and not raw.rstrip().endswith(" |"):
            continue
        body = raw.strip()
        body = body[2:] if body.startswith("- ") else body
        parts = [p.strip() for p in body.split("|")]  # a blank last field survives the split
        ok = raw.lstrip().startswith("- ") and len(parts) == len(lr.FIELDS)
        out.append((n, raw, dict(zip(lr.FIELDS, parts)) if ok else None))
    return out


def detect(text):
    """The format name when the text is a handover ledger, else None."""
    rows = _pipe_lines(text)
    good = sum(1 for _, _, f in rows if f is not None)
    return FORMAT if good and good >= DOMINANT * len(rows) else None


def ingest(conn, file_id, text):
    """Parse one handover ledger into the store; returns the parse report dict."""
    if not any(f is not None for _, _, f in _pipe_lines(text)):
        raise ValueError(f"{file_id}: not a handover ledger")
    manifest_at = lr.manifest_of(conn, file_id)
    common.clear(conn, file_id)
    notes, entries, seen = [], [], {}
    meaningful = unparsed = 0
    for line in text.splitlines():
        m = _SNAPSHOT.match(line)
        if m:
            note = lr.time_note(m.group(1), manifest_at)
            if note:
                notes.append(note)
    for n, raw, fields in _pipe_lines(text):
        loc = f"L{n}"
        if raw in seen:
            notes.append({"type": "duplicate_line", "locator": loc, "first": seen[raw]})
            continue
        seen[raw] = loc
        if fields is None:
            meaningful += 1
            unparsed += 1
            notes.append({"type": "line_unparsed", "locator": loc, "head": raw.strip()[:60]})
        else:
            entries.append((loc, fields, {}))
    return lr.ingest_rows(conn, file_id, name, FORMAT, entries,
                          {"meaningful": meaningful, "unparsed": unparsed, "notes": notes,
                           "noise": {}})
