"""Activity-based linkers (SPEC L1, T16): the account linker and the analytics report.

Both look for the one `DELETE_RECORD` / `ANONYMIZE_RECORD` activity row that is the workflow's
claim for this case, using only the row's time against `reported_at` (a learned window), and drop
every candidate that evidence already links to another subject. Pure functions over the fact store
and the already-built dossiers; they write nothing.

- Accounts: M6 only. Candidates in the learned window, minus those another case's dossier links
  by evidence; exactly one left is a low-confidence pick, anything else is `link_unresolved`.
  Elimination is clock-safe (U3): a competing case's link counts only if it existed at THIS case's
  audit_at, so a later case's dossier is rebuilt at this audit_at before it may eliminate.
  M9 rank pairing, M7 nearest offset, window widening and the requested_at bound are not used
  (SPEC L1 records why).
- Analytics (MA): same elimination, but the result is only a diagnostics event; no step is ever
  emitted, in either LINK_FALLBACK mode.
Names never link or eliminate: elimination reads dossier links, which exclude `name` keys.
"""
from . import dossier, loaders, store

MODES = ("guess", "omit")
METHOD = "M6"
US_PER_MIN = 60_000_000
# reported_at - event_time, in minutes, fitted on the 32 samples (LINKING_EXPERIMENT).
ACCOUNT_WINDOW = (-8, 74)
ANALYTICS_WINDOW = (-29, 114)
CENTRE_MIN = 53  # median account offset; picks the row kept when one account has several rows


def _rows(conn, case, event_type, rid_of, window):
    """{resource_id: candidate} for activity rows of one type inside the window and audit_at."""
    store.register_functions(conn)
    reported = store.epoch_us(case["reported_at"])
    lo, hi = window[0] * US_PER_MIN, window[1] * US_PER_MIN
    best = {}
    for aid, event_time, rid_a, target, kind in conn.execute(
            "select activity_id, event_time, resource_id, target_resource, kind from activity "
            "where event_type = ? and ts(event_time) <= ts(?) order by ts(event_time), activity_id",
            (event_type, case["audit_at"])):
        rid = rid_of(rid_a, target, kind)
        if not rid:
            continue
        off = reported - store.epoch_us(event_time)
        if not lo <= off <= hi:
            continue
        key = abs(off - CENTRE_MIN * US_PER_MIN)
        if rid not in best or key < best[rid][0]:
            best[rid] = (key, {"resource_id": rid, "activity_id": aid, "event_time": event_time,
                               "offset_min": round(off / US_PER_MIN, 2)})
    return [best[r][1] for r in sorted(best)]


def _account_rid(rid, target, kind):
    return target if kind in ("primary_profile", "target_resource") and \
        (target or "").startswith("account_") else None


def _analytics_rid(rid, target, kind):
    return rid if (rid or "").startswith("analytics_") else None


def account_candidates(conn, case):
    """Primary-profile DELETE_RECORD rows in the learned window, at most one per account."""
    return _rows(conn, case, "DELETE_RECORD", _account_rid, ACCOUNT_WINDOW)


def analytics_candidates(conn, case):
    return _rows(conn, case, "ANONYMIZE_RECORD", _analytics_rid, ANALYTICS_WINDOW)


def _order(case_id):
    """Numeric case order, with non-numeric ids (fixtures) after them, by name."""
    try:
        return loaders.case_sort_key(case_id)
    except (ValueError, IndexError):
        return (float("inf"), case_id)


class _Pool:
    """The dossiers of every case in the request file, with clock-safe elimination."""

    def __init__(self, conn, pool):
        self.conn = conn
        self.entries = sorted(((c, d) for c, d in pool if d is not None),
                              key=lambda cd: _order(cd[0]["case_id"]))
        self.ids = {c["case_id"]: {r["resource_id"] for r in d["resources"]}
                    for c, d in self.entries}
        self._rebuilt = {}

    def _at(self, other, audit_at):
        key = (other["case_id"], audit_at)
        if key not in self._rebuilt:
            d = dossier.build_dossier(self.conn, dict(other, audit_at=audit_at))
            self._rebuilt[key] = {r["resource_id"] for r in d["resources"]}
        return self._rebuilt[key]

    def eliminated_by(self, case, d, rid):
        """The first other subject's case that links rid by evidence at this case's audit_at."""
        limit = store.epoch_us(case["audit_at"])
        for other, od in self.entries:
            if other["case_id"] == case["case_id"] or od["user_id"] == d["user_id"]:
                continue
            if rid not in self.ids[other["case_id"]]:
                continue
            if store.epoch_us(other["audit_at"]) > limit and \
                    rid not in self._at(other, case["audit_at"]):
                continue
            return {"resource_id": rid, "case_id": other["case_id"], "user_id": od["user_id"]}
        return None

    def split(self, case, d, cands):
        kept, gone = [], []
        for c in cands:
            e = self.eliminated_by(case, d, c["resource_id"])
            if e:
                gone.append(e)
            else:
                kept.append(c)
        return kept, gone


def _wanted(case, d, targets, linked):
    return not (targets is not None and case["case_id"] not in targets) and not linked(d)


def link_accounts(conn, pool, mode, targets=None):
    """{case_id: {"pick": pick or None, "events": [...]}} for each case with no evidence account.

    pool: [(case, dossier)] for every request in the file (never just the targets, so a competing
    case's links are always known). targets: case_ids to report, or None for all."""
    if mode not in MODES:
        raise ValueError(f"mode must be one of {MODES}, got {mode!r}")
    p = _Pool(conn, pool)
    out = {}
    for case, d in p.entries:
        if not _wanted(case, d, targets, lambda x: x["account_linked"]):
            continue
        if mode == "omit":
            out[case["case_id"]] = {"pick": None, "events": [
                {"type": "link_unresolved", "reason": "omit"}]}
            continue
        cands = account_candidates(conn, case)
        kept, gone = p.split(case, d, cands)
        window = [c["resource_id"] for c in cands]
        if len(kept) == 1:
            c = kept[0]
            pick = {"resource_id": c["resource_id"], "method": METHOD, "confidence": "low",
                    "activity_id": c["activity_id"], "candidates": window, "eliminated": gone}
            out[case["case_id"]] = {"pick": pick, "events": [
                {"type": "account_guess", **pick}]}
            continue
        reason = "tie" if kept else "all_eliminated" if cands else "no_candidates"
        out[case["case_id"]] = {"pick": None, "events": [
            {"type": "link_unresolved", "reason": reason,
             "candidates": [c["resource_id"] for c in kept], "window_candidates": window,
             "eliminated": gone}]}
    return out


def link_analytics(conn, pool, targets=None):
    """{case_id: [event]} for each case with no evidence analytics record. Diagnostics only:
    a unique candidate is reported as withheld, never linked; a tie or none is unresolved."""
    p = _Pool(conn, pool)
    out = {}
    for case, d in p.entries:
        if not _wanted(case, d, targets,
                       lambda x: any((r["resource_id"] or "").startswith("analytics_")
                                     for r in x["resources"])):
            continue
        cands = analytics_candidates(conn, case)
        kept, gone = p.split(case, d, cands)
        if len(kept) == 1:
            c = kept[0]
            out[case["case_id"]] = [{
                "type": "analytics_candidate", "resource_id": c["resource_id"],
                "activity_id": c["activity_id"], "confidence": "low", "withheld": True,
                "candidates": [x["resource_id"] for x in cands], "eliminated": gone}]
            continue
        reason = "tie" if kept else "all_eliminated" if cands else "no_candidates"
        out[case["case_id"]] = [{
            "type": "analytics_unresolved", "reason": reason,
            "candidates": [c["resource_id"] for c in kept],
            "window_candidates": [c["resource_id"] for c in cands], "eliminated": gone}]
    return out
