"""Adapter 6: read-back trace logs (DESIGN §3, adapter 6; SPEC TR1).

Recognised by structure, never by file name or trace prefix: logfmt is the dominant syntax, no
line carries `instance`, `region` or `pod` (those are observability exports, adapter 5), and some
`<x>_started` line carries a `resource_id` and a `lookup_key` field and has a listed
`<x>_completed` partner with the same trace id and resource.

Clock (SPEC U3): every observation's record_at is the line's own timestamp; the manifest decides
citations only. Scope: none. The file declares itself a list of targeted point-in-time lookups
(`export_started target_count=N`), not a whole-store capture, so omission proves nothing (TR1).
Lines: a started line is a resource-less link observation (the lookup key, by shape) and a
completed line carries the resource, its state and its own identifiers; both carry the trace id as
a join key, which keeps the dossier's trace join and A8 owner check in charge of linking. The
started line's own resource_id is only checked against the completed line. A failed operation sets
no state. Anything unlisted that carries a resource or a subject key is unparsed, never noise.
"""
import json

from .. import policy, store
from . import common, logstate, logtok

name = "resource_trace_log"
FORMAT = "log_trace"
DOMINANT = 0.9
INFRA_FIELDS = ("instance", "region", "pod")
IDENT_FIELDS = ("email", "customer_email", "requester_email", "owner_email", "phone",
                "requester_phone", "email_hash", "customer_reference", "subject_reference")
KEY_FIELDS = ("lookup_key",) + IDENT_FIELDS
IDENT_KINDS = ("email", "phone", "email_hash", "user_id")


def _time(kv, field):
    v = kv.get(field)
    try:
        store.epoch_us(v)
    except (ValueError, TypeError, AttributeError):
        raise common.Skip(f"{field} is not a time: {v!r}")
    return v


def _audit(kv):
    logstate.audit_rows(kv)
    return "PRESENT", {"rows": 1, "retention_until": _time(kv, "retention_until"),
                       "event_code": kv.get("event_code")}


def _analytics(kv):
    fields = {"user_id": kv.get("customer_reference"), "email": common.lower(kv.get("email")),
              "phone": kv.get("phone"), "email_hash": common.lower(kv.get("email_hash"))}
    state = "IDENTIFIABLE" if common.any_set(*fields.values()) else "ANONYMIZED"
    return state, {**fields, "audience_cohort": kv.get("audience_cohort")}


def _support(kv):
    fields = {f: kv.get(f) for f in ("requester_name", "requester_email", "requester_phone")}
    state = "IDENTIFIABLE" if common.any_set(*fields.values()) else "REDACTED"
    return state, {**fields, "requester_email": common.lower(fields["requester_email"])}


def _restore(kv):
    if logstate_flag(kv, "archive_present") is not True:
        raise common.Skip("restore policy read without an archive")
    ok = logstate_flag(kv, "restore_allowed")
    return ("RESTORABLE" if ok else "BLOCKED"), {"restore_allowed": ok}


def _export(kv):
    if logstate_flag(kv, "available") is not True:
        raise common.Skip(f"unknown availability {kv.get('available')!r}")
    return "PRESENT", {"available": True}


def logstate_flag(kv, field):
    return logstate.bool_field(kv, field)


# event base -> (completed event, store collection, mapper)
BASES = {
    "profile_lookup": ("customer_profiles", logstate.profile),
    "index_query": ("customer_search_documents", logstate.index),
    "audit_record_read": ("security_audit_events", _audit),
    "connector_config_read": ("connector_configurations", logstate.connector),
    "analytics_profile_read": ("customer_analytics_reports", _analytics),
    "support_ticket_read": ("support_conversations", _support),
    "restore_policy_read": ("backup_catalog", _restore),
    "export_metadata_read": ("customer_export_jobs", _export),
}
STARTED = {f"{b}_started": b for b in BASES}
STATE_EVENTS = {f"{b}_completed": (c, m) for b, (c, m) in BASES.items()}
FAILED_EVENTS = {logstate.failed_form(e): e for e in STATE_EVENTS}


# ---- detection ---------------------------------------------------------------------------------

def _tokenize(text):
    """(line_no, raw, Line or None) for the non-blank lines when the file is logfmt without
    infrastructure fields, else None. This is all ingest requires; detect asks for more."""
    raw = [(i, t) for i, t in enumerate(text.splitlines(), 1) if t.strip()]
    if logtok.file_syntax(t for _, t in raw) != "logfmt":
        return None
    lines = [(i, t, logtok.parse_line("logfmt", t)) for i, t in raw]
    good = [p for _, _, p in lines if p is not None]
    if not good or any(p.kv.get(f) for p in good for f in INFRA_FIELDS):
        return None
    return lines


def _has_pair(lines):
    starts, dones = {}, set()
    for _, _, p in lines:
        if p is None or not p.trace_id:
            continue
        rid = p.kv.get("resource_id")
        if p.event in STARTED and rid and "lookup_key" in p.kv:
            starts[(p.trace_id, rid)] = p.event
        elif p.event in STATE_EVENTS or p.event in FAILED_EVENTS:
            dones.add((p.trace_id, rid))
    return any(k in dones for k in starts)


def detect(text):
    """The format name when the text is a read-back trace log, else None."""
    lines = _tokenize(text)
    if lines is None or sum(1 for _, _, p in lines if p is not None) < DOMINANT * len(lines):
        return None
    return FORMAT if _has_pair(lines) else None


# ---- ingest ------------------------------------------------------------------------------------

def _own_keys(kv):
    out = set()
    for f in IDENT_FIELDS:
        c = common.classify(kv.get(f))
        if c:
            out.add(c)
    return out | common.keys(names=[kv.get("requester_name")])


def _count(kv, field):
    v = kv.get(field)
    if not (isinstance(v, str) and v.isdigit()):
        raise common.Skip(f"{field} is not a non-negative integer: {v!r}")
    return int(v)


def _analyse(entries):
    """Per trace id: its start lines and completion lines (listed or failed), duplicates removed.
    Returns (conflicts, starts, dones)."""
    starts, dones = {}, {}
    for loc, p in entries:
        if p is None or not p.trace_id:
            continue
        if p.event in STARTED:
            starts.setdefault(p.trace_id, []).append((loc, p))
        elif p.event in STATE_EVENTS or p.event in FAILED_EVENTS:
            dones.setdefault(p.trace_id, []).append((loc, p))
    conflicts = set()
    for t, ss in starts.items():
        rids = {p.kv.get("resource_id") for _, p in ss}
        # a completion with no resource is already unparsed on its own line
        done_rids = {p.kv.get("resource_id") for _, p in dones.get(t, [])} - {None}
        if len(ss) > 1 or (done_rids and rids != done_rids):
            conflicts.add(t)
    return conflicts, starts, dones


def ingest(conn, file_id, text):
    """Parse one read-back trace log into the store; returns the parse report dict."""
    row = conn.execute("select manifest_at from evidence_file where file_id = ?",
                       (file_id,)).fetchone()
    if row is None:
        raise ValueError(f"{file_id}: not in evidence_file")
    manifest_at = row[0]
    lines = _tokenize(text)
    if lines is None:
        raise ValueError(f"{file_id}: not a read-back trace log")
    limit = store.epoch_us(manifest_at)
    notes, noise = [], {}
    meaningful = unparsed = n_obs = n_joins = 0
    seen_lines, seen_state, unmapped = {}, set(), set()

    common.clear(conn, file_id)

    deduped = []
    for line_no, raw, p in lines:
        if raw not in seen_lines:
            seen_lines[raw] = f"L{line_no}"
            deduped.append((f"L{line_no}", p))
    conflicts, starts, dones = _analyse(deduped)
    start_key = {t: common.classify(ss[0][1].kv.get("lookup_key"))
                 for t, ss in starts.items() if t not in conflicts}
    n_started = sum(len(ss) for ss in starts.values())
    for t in sorted(conflicts):
        notes.append({"type": "trace_conflict", "trace_id": t,
                      "starts": [loc for loc, _ in starts[t]],
                      "completions": [loc for loc, _ in dones.get(t, [])]})
    for t in sorted(set(starts) - set(dones) - conflicts):
        notes.append({"type": "started_without_completion", "trace_id": t,
                      "locator": starts[t][0][0]})
    for t in sorted(set(dones) - set(starts)):
        notes.append({"type": "completed_without_start", "trace_id": t,
                      "locator": dones[t][0][0]})

    def bad(kind, loc, **extra):
        nonlocal meaningful, unparsed
        meaningful += 1
        unparsed += 1
        notes.append({"type": kind, "locator": loc, **extra})

    def write(p, loc, rid, collection, state, attrs, keys):
        nonlocal n_obs, n_joins
        attrs = {**attrs, "collection": collection, "event": p.event, "syntax": "logfmt",
                 "line_ts": p.ts, "trace_id": p.trace_id, "level": p.level}
        cur = conn.execute(
            "insert into observation(file_id, locator, record_at, system_id, resource_id, "
            "category, state, state_basis, attrs_json) values (?,?,?,?,?,?,?,?,?)",
            (file_id, loc, p.ts, p.service, rid,
             policy.COLLECTION_CATEGORY[collection] if collection else None, state, "explicit",
             json.dumps(attrs, sort_keys=True)))
        conn.executemany("insert into subject_key values (?,?,?)",
                         [(cur.lastrowid, k, v) for k, v in sorted(keys)])
        if p.trace_id and p.trace_id not in conflicts:
            conn.execute("insert into join_key values (?,?,?,?,?)",
                         (file_id, "trace_id", p.trace_id, cur.lastrowid, p.ts))
            n_joins += 1
        n_obs += 1
        if p.service not in logstate.KNOWN_SYSTEMS:
            unmapped.add(p.service)
        if store.epoch_us(p.ts) > limit:
            notes.append({"type": "record_after_manifest", "locator": loc, "resource_id": rid})

    header = footer = None  # (locator, counts) of the export bracket lines
    last_end = None
    for line_no, raw, p in lines:
        loc = f"L{line_no}"
        if seen_lines[raw] != loc:
            notes.append({"type": "duplicate_line", "locator": loc, "first": seen_lines[raw]})
            continue
        if p is None:
            others = [s for s in logtok.parses(raw) if s != "logfmt"]
            if others:
                bad("syntax_mismatch", loc, syntaxes=others, head=raw[:60])
            else:
                bad("line_unparsed", loc, head=raw[:60])
            continue
        kv, event, rid = p.kv, p.event, p.kv.get("resource_id")
        if event in ("export_started", "export_completed"):
            meaningful += 1
            try:
                counts = ({"target_count": _count(kv, "target_count")} if event == "export_started"
                          else {"queries": _count(kv, "queries"), "dropped": _count(kv, "dropped")})
            except common.Skip as e:
                unparsed += 1
                notes.append({"type": "line_unparsed", "locator": loc, "event": event,
                              "reason": str(e)[:80]})
                continue
            if event == "export_started":
                header = (loc, counts)
            else:
                footer = (loc, counts)
                last_end = p.ts
            continue
        if event in STARTED:
            meaningful += 1
            if not (isinstance(rid, str) and rid.strip()) or not p.trace_id \
                    or "lookup_key" not in kv:
                unparsed += 1
                notes.append({"type": "line_unparsed", "locator": loc, "event": event,
                              "reason": "resource_id, trace_id or lookup_key missing"})
                continue
            if p.trace_id in conflicts:
                unparsed += 1
            value = kv["lookup_key"]
            if value is None:
                notes.append({"type": "null_lookup_key", "locator": loc, "resource_id": rid})
            elif common.classify(value) is None:
                notes.append({"type": "non_identifier_key", "locator": loc,
                              "field": "lookup_key", "value": value})
            keys = {c} if (c := common.classify(value)) else set()
            write(p, loc, None, None, None,
                  {"lookup_field": "lookup_key", "start_resource_id": rid}, keys)
            continue
        if event in STATE_EVENTS or event in FAILED_EVENTS:
            meaningful += 1
            collection, mapper = STATE_EVENTS[FAILED_EVENTS.get(event, event)]
            try:
                if not (isinstance(rid, str) and rid.strip()):
                    raise common.Skip("resource_id missing")
                if event in FAILED_EVENTS:
                    raise logstate.Failed(f"event {event}")
                state, attrs = mapper(kv)
            except logstate.Failed as e:
                notes.append({"type": "operation_failed", "locator": loc, "event": event,
                              "resource_id": rid, "reason": str(e)[:80]})
                continue
            except (common.Skip, ValueError, TypeError) as e:
                unparsed += 1
                notes.append({"type": "line_unparsed", "locator": loc, "event": event,
                              "reason": str(e)[:80]})
                continue
            keys = _own_keys(kv)
            looked_up = start_key.get(p.trace_id)
            if looked_up and any(k == looked_up[0] and v != looked_up[1] for k, v in keys
                                 if k in IDENT_KINDS):
                notes.append({"type": "subject_mismatch", "locator": loc, "resource_id": rid,
                              "trace_id": p.trace_id, "lookup_key": looked_up[1],
                              "found": sorted(v for k, v in keys if k == looked_up[0])})
            if (p.trace_id, rid) in seen_state:
                notes.append({"type": "retried_completion", "locator": loc, "resource_id": rid,
                              "trace_id": p.trace_id})
            seen_state.add((p.trace_id, rid))
            write(p, loc, rid, collection, state, attrs, keys)
            continue
        if rid or any(kv.get(f) for f in KEY_FIELDS):
            bad("unlisted_event", loc, event=event, resource_id=rid)
            continue
        noise[event] = noise.get(event, 0) + 1

    declared = [(k, v) for part in (header, footer) if part for k, v in part[1].items()]
    if declared and any((k in ("target_count", "queries") and v != n_started)
                        or (k == "dropped" and v != 0) for k, v in declared):
        unparsed += 1
        notes.append({"type": "export_counts_disagree", "lookups": n_started,
                      "declared": dict(declared)})
    if last_end and store.epoch_us(last_end) != limit:
        notes.append({"type": "capture_time_mismatch", "footer": last_end, "manifest": manifest_at})
    for svc in sorted(unmapped):
        notes.append({"type": "unmapped_service", "service": svc})
    notes.append({"type": "noise_events", "counts": dict(sorted(noise.items()))})

    parsed = meaningful - unparsed
    report = {"meaningful": meaningful, "parsed": parsed, "unparsed": unparsed,
              "observations": n_obs, "join_keys": n_joins, "noise": sum(noise.values()),
              "notes": notes}
    conn.execute("insert into parse_report values (?,?,?,?,?,?)",
                 (file_id, name, meaningful, parsed, unparsed, json.dumps(notes, sort_keys=True)))
    conn.execute("update evidence_file set format = ?, adapter = ? where file_id = ?",
                 (FORMAT, name, file_id))
    return report
