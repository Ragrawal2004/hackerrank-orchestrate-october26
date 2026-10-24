"""Evidence adapters (DESIGN §3). Each adapter detects its files by structure and writes only to
the fact store. `load_evidence` offers every manifest file to the registry; a file no adapter
claims keeps format NULL and is reported as unclaimed (later tasks add the other adapters)."""
from pathlib import Path

from . import replica_sql, service_export, snapshot_bundle

ADAPTERS = [replica_sql, snapshot_bundle, service_export]


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


def load_evidence(conn, dataset_dir):
    """Run the adapters over every manifest file. Returns {"claimed": [...], "unclaimed": [...]}."""
    claimed, unclaimed = [], []
    for file_id, path in conn.execute("select file_id, path from evidence_file order by file_id"
                                      ).fetchall():
        try:
            text = (Path(dataset_dir) / path).read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            unclaimed.append(file_id)
            continue
        picked = select(text)
        if picked is None:
            unclaimed.append(file_id)
            continue
        picked[0].ingest(conn, file_id, text)
        claimed.append(file_id)
    return {"claimed": claimed, "unclaimed": unclaimed}


def summary(conn):
    """Totals over the parse reports: files, observations, unparsed, plus the unparsed files."""
    files, unparsed = conn.execute(
        "select count(*), coalesce(sum(unparsed), 0) from parse_report").fetchone()
    obs = conn.execute("select count(*) from observation where file_id in "
                       "(select file_id from parse_report)").fetchone()[0]
    bad = [r[0] for r in conn.execute(
        "select file_id from parse_report where unparsed > 0 order by file_id")]
    return {"files": files, "observations": obs, "unparsed": unparsed, "unparsed_files": bad}


def format_summary(summ, clock_choice, unclaimed):
    line = (f"adapters: files={summ['files']} observations={summ['observations']} "
            f"unparsed={summ['unparsed']} clock_choice_matters={clock_choice} "
            f"unclaimed_files={unclaimed}")
    if summ["unparsed"]:
        line += ("\n!!! WARNING: %d unparsed meaningful lines in %s (target is 0)"
                 % (summ["unparsed"], ", ".join(summ["unparsed_files"])))
    return line
