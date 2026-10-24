"""Adapter 5: observability log exports in six syntaxes (DESIGN §3, adapter 5; SPEC O1).

Recognised by structure, never by file name: one dominant syntax from `logtok` covers at least
90 % of the non-blank lines, at least 90 % of the parsed lines carry the k8s `instance` field,
some line carries a `region`, and some line is a whitelisted state event. Read-back trace logs (no
`instance`), ledger lines (no `level=`) and the support replica log (no `region`, no state event)
are therefore not claimed.

Clock (SPEC O1, the U3 rule): every observation's record_at is the line's own timestamp, never
the file's manifest time; the dossier's clock gate drops lines after a case's audit_at.
Scope: none. A log is a window of events, not a whole-store capture, so omission proves nothing.
Lines: a `*_started` line carrying a subject key is a resource-less link observation; a state
line carries the resource and its state; both carry the trace id as a join key, so the dossier
joins them. A failed operation (the `_failed` form of a state event, http_status >= 500, result
error/timeout) is understood but sets no state. An unlisted event that carries a resource_id or a
subject key is unparsed; any other unlisted event is noise, counted per event in the notes.
"""
import json

from .. import policy, store
from . import common, logtok

name = "observability_log"
FORMAT_PREFIX = "log_obs_"
DOMINANT = 0.9

KEY_FIELDS = ("lookup_key", "match_key", "subject_ref", "subject_key", "scan_key", "owner_key",
              "correlation_key", "matched_value", "email", "phone")
STARTED = {
    "profile_lookup_started": "lookup_key",
    "index_query_started": "match_key",
    "audit_record_query_started": "subject_ref",
    "message_scan_started": "subject_key",
    "attachment_scan_started": "scan_key",
    "analytics_profile_scan_started": "lookup_key",
    "connector_config_read_started": "subject_key",
    "restore_policy_check_started": "subject_key",
    "export_download_check_started": "owner_key",
    "retained_order_query_started": "subject_ref",
}
FAILED_RESULTS = frozenset({"error", "timeout"})
KNOWN_SYSTEMS = frozenset(policy.SYSTEM_CATEGORY) | {"support_platform", "analytics_platform"}


class _Failed(Exception):
    """The line reports a failed operation: understood, but it proves no state."""


def _int(kv, field):
    v = kv.get(field)
    if not (isinstance(v, str) and v.isdigit()):
        raise common.Skip(f"{field} is not a non-negative integer: {v!r}")
    return int(v)


def _bool(kv, field):
    v = kv.get(field)
    if v not in ("true", "false"):
        raise common.Skip(f"{field} is not true/false: {v!r}")
    return v == "true"


def _identifier(value):
    """attrs for an exposed identifier value: {"email": ...} / {"phone": ...} by shape."""
    c = common.classify(value)
    if c is None:
        return {}
    kind, v = c
    return {kind: v} if kind in ("email", "phone", "email_hash") else {}


def _matched(kv):
    if not kv.get("matched_value"):
        raise common.Skip("matched_value missing")
    return _identifier(kv["matched_value"])


# ---- state mappers: kv -> (state, attrs) -------------------------------------------------------

def _profile(kv):
    status = _int(kv, "http_status")
    if status >= 500:
        raise _Failed(f"http_status {status}")
    rows = _int(kv, "rows")
    if (rows, status) == (1, 200):
        return "PRESENT", {"rows": rows, "http_status": status}
    if (rows, status) == (0, 404):
        return "ABSENT", {"rows": rows, "http_status": status}
    raise common.Skip(f"rows {rows} and http_status {status} disagree")


def _index(kv):
    result = kv.get("result")
    if result in FAILED_RESULTS:
        raise _Failed(f"result {result}")
    if result not in ("hit", "miss"):
        raise common.Skip(f"unknown result {result!r}")
    return ("PRESENT" if result == "hit" else "ABSENT"), {"result": result,
                                                         "index": kv.get("index")}


def _audit(kv):
    if _int(kv, "rows") != 1:
        raise common.Skip("audit read rows is not 1")
    return "PRESENT", {"rows": 1, "integrity_check": kv.get("integrity_check")}


def _message_scan(kv):
    return "IDENTIFIABLE", {"detector": kv.get("detector"), **_matched(kv)}


def _message_read(kv):
    text = kv.get("message_text")
    if not isinstance(text, str):
        raise common.Skip("message_text missing")
    emails = sorted(v for k, v in common.keys(texts=[text]) if k == "email")
    return ("IDENTIFIABLE" if emails else "REDACTED"), {"email": emails[0] if emails else None}


def _attachment_scan(kv):
    return "PRESENT", {"file": kv.get("file"), "detector": kv.get("detector"), **_matched(kv)}


def _identity_scan(kv):
    if _int(kv, "direct_identifiers") < 1:
        raise common.Skip("identity scan without direct identifiers")
    return "IDENTIFIABLE", {"direct_identifiers": int(kv["direct_identifiers"]), **_matched(kv)}


def _analytics_read(kv):
    n = _int(kv, "direct_identifiers")
    fields = {"email": common.lower(kv.get("email")), "phone": kv.get("phone")}
    exposed = common.any_set(*fields.values())
    if n == 0 and not exposed:
        return "ANONYMIZED", {"direct_identifiers": 0, **fields}
    if n > 0 and exposed:
        return "IDENTIFIABLE", {"direct_identifiers": n, **fields}
    raise common.Skip("direct_identifiers disagrees with the identifier fields")


def _contact(kv):
    if _int(kv, "rows") != 1:
        raise common.Skip("contact lookup rows is not 1")
    return "PRESENT", {"rows": 1, "email_opt_in": _bool(kv, "email_opt_in")}


def _connector(kv):
    on = _bool(kv, "enabled")
    return ("ACTIVE" if on else "DISABLED"), {"enabled": on}


def _candidate(kv):
    eligible, block = _bool(kv, "eligible"), _bool(kv, "deletion_block")
    ok = eligible and not block
    return ("RESTORABLE" if ok else "BLOCKED"), {"restore_allowed": ok, "eligible": eligible,
                                                 "deletion_block": block}


def _authorization(kv):
    decision = kv.get("decision")
    if decision not in ("allow", "deny"):
        raise common.Skip(f"unknown decision {decision!r}")
    ok = decision == "allow"
    return ("RESTORABLE" if ok else "BLOCKED"), {"restore_allowed": ok, "decision": decision,
                                                 "policy": kv.get("policy")}


def _export(kv):
    if kv.get("available") != "true":
        raise common.Skip(f"unknown availability {kv.get('available')!r}")
    return "PRESENT", {"available": True, "file": kv.get("file")}


def _retained(kv):
    if kv.get("access") != "allowed":
        raise common.Skip(f"unknown access {kv.get('access')!r}")
    return "PRESENT", {"reason": kv.get("reason")}


# event -> (store collection, mapper)
STATE_EVENTS = {
    "profile_lookup_completed": ("customer_profiles", _profile),
    "index_query_completed": ("customer_search_documents", _index),
    "audit_record_read_completed": ("security_audit_events", _audit),
    "message_pii_scan": ("support_conversations", _message_scan),
    "message_read": ("support_conversations", _message_read),
    "attachment_pii_scan": ("ticket_attachments", _attachment_scan),
    "analytics_identity_scan": ("customer_analytics_reports", _identity_scan),
    "analytics_profile_read": ("customer_analytics_reports", _analytics_read),
    "contact_lookup_completed": ("contact_lookups", _contact),
    "connector_heartbeat": ("connector_configurations", _connector),
    "connector_config_loaded": ("connector_configurations", _connector),
    "restore_candidate_evaluated": ("backup_catalog", _candidate),
    "restore_authorization_completed": ("backup_catalog", _authorization),
    "export_download_check_completed": ("customer_export_jobs", _export),
    "retained_order_loaded": ("retained_order_records", _retained),
}


def failed_form(event):
    """`x_completed` -> `x_failed`; any other state event `e` -> `e_failed`."""
    return event[:-len("_completed")] + "_failed" if event.endswith("_completed") \
        else event + "_failed"


FAILED_EVENTS = {failed_form(e): e for e in STATE_EVENTS}


# ---- detection ---------------------------------------------------------------------------------

def _tokenize(text):
    """(syntax, [(line_no, raw, Line or None)] for non-blank lines) or None."""
    raw = [(i, t) for i, t in enumerate(text.splitlines(), 1) if t.strip()]
    syntax = logtok.file_syntax(t for _, t in raw)
    if syntax is None:
        return None
    return syntax, [(i, t, logtok.parse_line(syntax, t)) for i, t in raw]


def _observability_lines(text):
    """The tokenized file when its parsed lines have the observability shape (instance on at
    least 90 % of them), else None. This is all ingest requires; detect asks for more."""
    tok = _tokenize(text)
    if tok is None:
        return None
    good = [p for _, _, p in tok[1] if p is not None]
    if not good or sum(1 for p in good if p.kv.get("instance")) < DOMINANT * len(good):
        return None
    return tok


def _shape(text):
    tok = _observability_lines(text)
    if tok is None:
        return None
    syntax, lines = tok
    good = [p for _, _, p in lines if p is not None]
    if len(good) < DOMINANT * len(lines):
        return None
    if not any(p.kv.get("region") for p in good):
        return None
    if not any(p.event in STATE_EVENTS for p in good):
        return None
    return tok


def detect(text):
    """The format name when the text is an observability log export, else None."""
    tok = _shape(text)
    return FORMAT_PREFIX + tok[0] if tok else None


# ---- ingest ------------------------------------------------------------------------------------

def _subject_keys(kv, extra_texts=()):
    out = set()
    for f in KEY_FIELDS:
        c = common.classify(kv.get(f))
        if c:
            out.add(c)
    return out | common.keys(texts=list(extra_texts))


def ingest(conn, file_id, text):
    """Parse one observability log into the store; returns the parse report dict."""
    row = conn.execute("select manifest_at from evidence_file where file_id = ?",
                       (file_id,)).fetchone()
    if row is None:
        raise ValueError(f"{file_id}: not in evidence_file")
    manifest_at = row[0]
    tok = _observability_lines(text)
    if tok is None:
        raise ValueError(f"{file_id}: not an observability log")
    syntax, lines = tok
    limit = store.epoch_us(manifest_at)
    notes, noise = [], {}
    meaningful = unparsed = n_obs = n_joins = 0
    seen_lines, seen_state, unmapped = {}, set(), set()

    common.clear(conn, file_id)

    def bad(kind, loc, **extra):
        nonlocal meaningful, unparsed
        meaningful += 1
        unparsed += 1
        notes.append({"type": kind, "locator": loc, **extra})

    def write(p, loc, rid, collection, state, attrs, keys):
        nonlocal n_obs, n_joins
        attrs = {**attrs, "collection": collection, "event": p.event, "syntax": syntax,
                 "line_ts": p.ts, "trace_id": p.trace_id, "level": p.level}
        cur = conn.execute(
            "insert into observation(file_id, locator, record_at, system_id, resource_id, "
            "category, state, state_basis, attrs_json) values (?,?,?,?,?,?,?,?,?)",
            (file_id, loc, p.ts, p.service, rid,
             policy.COLLECTION_CATEGORY[collection] if collection else None, state, "explicit",
             json.dumps(attrs, sort_keys=True)))
        conn.executemany("insert into subject_key values (?,?,?)",
                         [(cur.lastrowid, k, v) for k, v in sorted(keys)])
        if p.trace_id:
            conn.execute("insert into join_key values (?,?,?,?,?)",
                         (file_id, "trace_id", p.trace_id, cur.lastrowid, p.ts))
            n_joins += 1
        n_obs += 1
        if p.service not in KNOWN_SYSTEMS:
            unmapped.add(p.service)
        if store.epoch_us(p.ts) > limit:
            notes.append({"type": "record_after_manifest", "locator": loc, "resource_id": rid})

    last_end = None
    for line_no, raw, p in lines:
        loc = f"L{line_no}"
        if raw in seen_lines:
            notes.append({"type": "duplicate_line", "locator": loc, "first": seen_lines[raw]})
            continue
        seen_lines[raw] = loc
        if p is None:
            others = [s for s in logtok.parses(raw) if s != syntax]
            if others:
                bad("syntax_mismatch", loc, syntaxes=others, head=raw[:60])
            else:
                bad("line_unparsed", loc, head=raw[:60])
            continue
        kv, event, rid = p.kv, p.event, p.kv.get("resource_id")
        if event in STARTED:
            meaningful += 1
            value = kv.get(STARTED[event])
            if not value:
                unparsed += 1
                notes.append({"type": "line_unparsed", "locator": loc,
                              "reason": f"{STARTED[event]} missing"})
                continue
            keys = _subject_keys(kv)
            if common.classify(value) is None:
                notes.append({"type": "non_identifier_key", "locator": loc,
                              "field": STARTED[event], "value": value})
            write(p, loc, None, None, None, {"lookup_field": STARTED[event]}, keys)
            continue
        if event in STATE_EVENTS or event in FAILED_EVENTS:
            meaningful += 1
            collection, mapper = STATE_EVENTS[FAILED_EVENTS.get(event, event)]
            try:
                if not (isinstance(rid, str) and rid.strip()):
                    raise common.Skip("resource_id missing")
                if event in FAILED_EVENTS:
                    raise _Failed(f"event {event}")
                state, attrs = mapper(kv)
            except _Failed as e:
                notes.append({"type": "operation_failed", "locator": loc, "event": event,
                              "resource_id": rid, "reason": str(e)[:80]})
                continue
            except (common.Skip, ValueError, TypeError) as e:
                unparsed += 1
                notes.append({"type": "line_unparsed", "locator": loc, "event": event,
                              "reason": str(e)[:80]})
                continue
            texts = [kv["message_text"]] if event == "message_read" and kv.get("message_text") \
                else []
            if (p.trace_id, rid) in seen_state:
                notes.append({"type": "retried_completion", "locator": loc, "resource_id": rid,
                              "trace_id": p.trace_id})
            seen_state.add((p.trace_id, rid))
            write(p, loc, rid, collection, state, attrs, _subject_keys(kv, texts))
            continue
        if rid or any(kv.get(f) for f in KEY_FIELDS):
            bad("unlisted_event", loc, event=event, resource_id=rid)
            continue
        noise[event] = noise.get(event, 0) + 1
        if event == "export_completed":
            last_end = p.ts

    if last_end and store.epoch_us(last_end) != limit:
        notes.append({"type": "capture_time_mismatch", "header": last_end, "manifest": manifest_at})
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
                 (FORMAT_PREFIX + syntax, name, file_id))
    return report
