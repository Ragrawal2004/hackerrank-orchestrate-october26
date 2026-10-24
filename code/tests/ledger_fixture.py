"""Synthetic ledger rows rendered in the three containers: a JSON list, `record_lookup` log lines
and a shift-handover text. No dataset files are read."""
import json

CONTAINERS = ("json", "log", "text")


def row(ts, system, rid, rtype, result, subject):
    return {"recorded_at": ts, "system": system, "resource_id": rid, "record_type": rtype,
            "result": result, "subject_reference": subject}


def account(ts, rid, result, subject):
    return row(ts, "account_service", rid, "USER_PROFILE", result, subject)


def search(ts, rid, result, subject):
    return row(ts, "search_service", rid, "SEARCH_INDEX_ENTRY", result, subject)


def audit(ts, rid, result, subject):
    return row(ts, "security_service", rid, "SECURITY_AUDIT_LOG", result, subject)


def render_json(rows, exported_at=None, count=None, origin="integration_management_service"):
    return json.dumps({"exported_at": exported_at or max(r["recorded_at"] for r in rows),
                       "origin": origin,
                       "record_count": len(rows) if count is None else count,
                       "records": rows}, indent=2)


def log_line(i, r, level="INFO", service="search_index_service", event="record_lookup"):
    return (f'{r["recorded_at"]} {level} service={service} event={event} seq={i} '
            f'system="{r["system"]}" resource_id="{r["resource_id"]}" '
            f'record_type="{r["record_type"]}" result="{r["result"]}" '
            f'subject_reference="{r["subject_reference"]}"')


def render_log(rows, **kw):
    return "\n".join(log_line(i, r, **kw) for i, r in enumerate(rows, 1)) + "\n"


def text_line(r):
    return (f'- {r["recorded_at"]} | {r["system"]} | {r["resource_id"]} | {r["record_type"]} | '
            f'{r["result"]} | {r["subject_reference"]}')


def render_text(rows, snapshot=None):
    snap = snapshot or max(r["recorded_at"] for r in rows)
    return ("CUSTOMER PLATFORM - SHIFT HANDOVER\n"
            f"Snapshot: {snap}\n\n"
            "Account and downstream checks completed during the shift:\n\n"
            + "\n".join(text_line(r) for r in rows) + "\n\n")


def render(container, rows, **kw):
    return {"json": render_json, "log": render_log, "text": render_text}[container](rows, **kw)
