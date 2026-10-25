"""Evidence adapters (DESIGN §3). Each adapter detects its files by structure and writes only to
the fact store. `load_evidence` offers every manifest file to the registry; a file no adapter
claims keeps format NULL and is reported as unclaimed (later tasks add the other adapters)."""
from pathlib import Path

from . import (inventory_sql, ledger_json, ledger_log, ledger_text, multimodal, observability_log, replica_sql,
               service_export, snapshot_bundle, trace_log)

ADAPTERS = [replica_sql, snapshot_bundle, service_export, inventory_sql, observability_log,
            trace_log, ledger_json, ledger_log, ledger_text]


class AmbiguousAdapter(ValueError):
    pass


def select(text, adapters=None):
    """(adapter, format) for the single adapter that claims the text, or None."""
    claims = []
    for a in ADAPTERS if adapters is None else adapters:
        fmt = a.detect(text)
        if fmt:
            claims.append((a, fmt))
    if len(claims) > 1:
        raise AmbiguousAdapter("claimed by " + ", ".join(a.name for a, _ in claims))
    return claims[0] if claims else None


def load_evidence(conn, dataset_dir, reader=None, extractions_dir=None):
    """Run the adapters over every manifest file.

    Text files go to the structure-detecting adapters first. PNG and PDF files (found by their
    signature) go to the multimodal adapter second, so it can check ids against what the text
    adapters found. `reader` reads one image/PDF twice (None: only stored extractions are used).
    Returns {"claimed": [...], "unclaimed": [...], "multimodal": {file_id: status},
    "multimodal_notes": {file_id: [note, ...]}}."""
    claimed, unclaimed, modal, notes, binary = [], [], {}, {}, []
    for file_id, path in conn.execute("select file_id, path from evidence_file order by file_id"
                                      ).fetchall():
        try:
            raw = (Path(dataset_dir) / path).read_bytes()
        except OSError:
            unclaimed.append(file_id)
            continue
        if multimodal.detect(raw):
            binary.append((file_id, raw))
            continue
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError:
            unclaimed.append(file_id)
            continue
        picked = select(text)
        if picked is None:
            unclaimed.append(file_id)
            continue
        picked[0].ingest(conn, file_id, text)
        claimed.append(file_id)
    for file_id, raw in binary:
        report = multimodal.ingest(conn, file_id, raw, reader, extractions_dir=extractions_dir)
        modal[file_id] = report["status"]
        notes[file_id] = report["notes"]
        (unclaimed if report["status"] == "skipped" else claimed).append(file_id)
    return {"claimed": claimed, "unclaimed": unclaimed, "multimodal": modal,
            "multimodal_notes": notes}


def summary(conn):
    """Totals over the parse reports: files, observations, unparsed, plus the unparsed files."""
    files, unparsed = conn.execute(
        "select count(*), coalesce(sum(unparsed), 0) from parse_report").fetchone()
    obs = conn.execute("select count(*) from observation where file_id in "
                       "(select file_id from parse_report)").fetchone()[0]
    bad = [r[0] for r in conn.execute(
        "select file_id from parse_report where unparsed > 0 order by file_id")]
    return {"files": files, "observations": obs, "unparsed": unparsed, "unparsed_files": bad}


def format_multimodal(statuses):
    """The `multimodal:` line: how many PNG/PDF files were read, loaded, disagreed or skipped."""
    n = lambda k: sum(1 for v in statuses.values() if v == k)  # noqa: E731
    return (f"multimodal: files={len(statuses)} agreed={n('agreed')} loaded={n('loaded')} "
            f"disagreed={n('disagreed')} skipped={n('skipped')}")


def format_summary(summ, clock_choice, unclaimed):
    line = (f"adapters: files={summ['files']} observations={summ['observations']} "
            f"unparsed={summ['unparsed']} clock_choice_matters={clock_choice} "
            f"unclaimed_files={unclaimed}")
    if summ["unparsed"]:
        line += ("\n!!! WARNING: %d unparsed meaningful lines in %s (target is 0)"
                 % (summ["unparsed"], ", ".join(summ["unparsed_files"])))
    return line
