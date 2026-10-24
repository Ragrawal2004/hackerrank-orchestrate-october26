"""Synthetic read-back trace lines (logfmt, rb- traces, no instance/region, resource_id on the
started line). No dataset files are read."""

BASES = {  # base event name -> service
    "profile_lookup": "account_service",
    "index_query": "search_service",
    "audit_record_read": "security_service",
    "connector_config_read": "integration_service",
    "analytics_profile_read": "analytics_platform",
    "support_ticket_read": "support_platform",
    "restore_policy_read": "backup_service",
    "export_metadata_read": "export_service",
}


def _v(v):
    s = "null" if v is None else str(v).lower() if isinstance(v, bool) else str(v)
    if s == "" or any(c in s for c in ' "\\='):
        return '"' + s.replace("\\", "\\\\").replace('"', '\\"') + '"'
    return s


def line(ts, service, event, trace=None, level="INFO", **kv):
    parts = [f"level={level}", f"service={service}"]
    if trace:
        parts.append(f"trace_id={trace}")
    parts.append(f"event={event}")
    parts += [f"{k}={_v(v)}" for k, v in kv.items()]
    return f"{ts} " + " ".join(parts)


def started(ts, base, trace, rid, key):
    return line(ts, BASES[base], f"{base}_started", trace, level="DEBUG", resource_id=rid,
                lookup_key=key)


def completed(ts, base, trace, rid, **kv):
    return line(ts, BASES[base], f"{base}_completed", trace, resource_id=rid, **kv)


def failed(ts, base, trace, rid, **kv):
    event = (base + "_failed") if base != "profile_lookup" else "profile_lookup_failed"
    return line(ts, BASES[base], event, trace, level="ERROR", resource_id=rid, **kv)


def header(ts, n):
    return line(ts, "readback_collector", "export_started", target_count=n, mode="point_in_time")


def footer(ts, n, dropped=0):
    return line(ts, "readback_collector", "export_completed", queries=n, dropped=dropped)


def profile(ts, trace, rid, key, found=False, **extra):
    """started + completed profile lookup: 404 when not found, 200 with identifiers when found."""
    kv = {"rows": 1, "http_status": 200, **extra} if found else {"rows": 0, "http_status": 404}
    return [started(ts, "profile_lookup", trace, rid, key),
            completed(ts, "profile_lookup", trace, rid, **kv)]


def search(ts, trace, rid, key, email=None, ref=None, result="hit"):
    return [started(ts, "index_query", trace, rid, key),
            completed(ts, "index_query", trace, rid, result=result, total_hits=1,
                      index="customer_directory_v3", email=email, customer_reference=ref)]


def audit(ts, trace, rid, key, ref, until="2034-09-30T23:59:59Z", rows=1):
    return [started(ts, "audit_record_read", trace, rid, key),
            completed(ts, "audit_record_read", trace, rid, rows=rows, subject_reference=ref,
                      event_code="IDENTITY_CHANGE_RECORDED", retention_until=until)]


def connector(ts, trace, rid, key, enabled, email=None, ref=None):
    return [started(ts, "connector_config_read", trace, rid, key),
            completed(ts, "connector_config_read", trace, rid, enabled=enabled,
                      last_delivery_status="delivered" if enabled else "stopped",
                      connector_name="analytics_identity_bridge", customer_email=email,
                      customer_reference=ref)]


def analytics(ts, trace, rid, key, email=None, phone=None, h=None, ref=None):
    return [started(ts, "analytics_profile_read", trace, rid, key),
            completed(ts, "analytics_profile_read", trace, rid, customer_reference=ref,
                      email=email, phone=phone, email_hash=h, audience_cohort="general_shopper")]


def support(ts, trace, rid, key, name=None, email=None, phone=None, ref=None):
    return [started(ts, "support_ticket_read", trace, rid, key),
            completed(ts, "support_ticket_read", trace, rid, customer_reference=ref,
                      requester_name=name, requester_email=email, requester_phone=phone)]


def restore(ts, trace, rid, key, allowed, present=True, email=None, ref=None):
    return [started(ts, "restore_policy_read", trace, rid, key),
            completed(ts, "restore_policy_read", trace, rid, archive_present=present,
                      restore_allowed=allowed, archive_key=f"archive/{rid}.tar.zst",
                      customer_email=email, customer_reference=ref)]


def export(ts, trace, rid, key, available=True, owner=None):
    return [started(ts, "export_metadata_read", trace, rid, key),
            completed(ts, "export_metadata_read", trace, rid, available=available,
                      owner_email=owner, object_key=f"exports/{rid}.zip")]


def log(*lines):
    return "\n".join(lines) + "\n"


def trace_file(*body, start="2026-09-26T07:49:50Z", end="2026-09-26T10:38:56Z", pairs=None):
    """A whole export: header, the body lines, footer. `pairs` overrides the declared count."""
    n = pairs if pairs is not None else sum(1 for l in body if "_started " in l)
    return log(header(start, n), *body, footer(end, n))
