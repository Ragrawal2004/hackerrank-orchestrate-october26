"""Render synthetic evidence text from structured values. Fresh code: it imports nothing from
tests/ and reads no dataset file, so the custom cases run from the shipped code alone."""
import json

# base event name -> logical service that logs it (the system_id the pipeline reports)
SERVICES = {
    "profile_lookup": "account_service",
    "index_query": "search_service",
    "audit_record_read": "security_service",
    "connector_config_read": "integration_service",
}


def _value(v):
    if v is None:
        text = "null"
    elif isinstance(v, bool):
        text = "true" if v else "false"
    else:
        text = str(v)
    if text == "" or any(c in text for c in ' "\\='):
        return '"' + text.replace("\\", "\\\\").replace('"', '\\"') + '"'
    return text


def logfmt(ts, service, event, trace=None, level="INFO", **fields):
    parts = [f"level={level}", f"service={service}"]
    if trace:
        parts.append(f"trace_id={trace}")
    parts.append(f"event={event}")
    parts += [f"{k}={_value(v)}" for k, v in fields.items()]
    return f"{ts} " + " ".join(parts)


def started(ts, base, trace, resource_id, key):
    return logfmt(ts, SERVICES[base], f"{base}_started", trace, level="DEBUG",
                  resource_id=resource_id, lookup_key=key)


def completed(ts, base, trace, resource_id, **fields):
    return logfmt(ts, SERVICES[base], f"{base}_completed", trace, resource_id=resource_id, **fields)


def failed_event(ts, base, trace, resource_id, **fields):
    """The `_failed` form of a lookup: the operation errored, nothing was read."""
    return logfmt(ts, SERVICES[base], f"{base}_failed", trace, level="ERROR",
                  resource_id=resource_id, **fields)


def profile_present(ts_start, ts_done, trace, resource_id, key, **who):
    return [started(ts_start, "profile_lookup", trace, resource_id, key),
            completed(ts_done, "profile_lookup", trace, resource_id, rows=1, http_status=200, **who)]


def profile_absent(ts_start, ts_done, trace, resource_id, key):
    return [started(ts_start, "profile_lookup", trace, resource_id, key),
            completed(ts_done, "profile_lookup", trace, resource_id, rows=0, http_status=404)]


def profile_unavailable(ts_start, ts_done, trace, resource_id, key):
    """A read that found nothing only because the service answered 503."""
    return [started(ts_start, "profile_lookup", trace, resource_id, key),
            completed(ts_done, "profile_lookup", trace, resource_id, rows=0, http_status=503)]


def profile_errored(ts_start, ts_done, trace, resource_id, key):
    return [started(ts_start, "profile_lookup", trace, resource_id, key),
            failed_event(ts_done, "profile_lookup", trace, resource_id, http_status=500)]


def search_hit(ts_start, ts_done, trace, resource_id, key, email, user):
    return [started(ts_start, "index_query", trace, resource_id, key),
            completed(ts_done, "index_query", trace, resource_id, result="hit", total_hits=1,
                      index="customer_directory_v3", email=email, customer_reference=user)]


def audit_record(ts_start, ts_done, trace, resource_id, key, user, retention_until):
    return [started(ts_start, "audit_record_read", trace, resource_id, key),
            completed(ts_done, "audit_record_read", trace, resource_id, rows=1,
                      subject_reference=user, event_code="ACCESS_REVIEW_RECORDED",
                      retention_until=retention_until)]


def connector(ts_start, ts_done, trace, resource_id, key, enabled, email, user):
    return [started(ts_start, "connector_config_read", trace, resource_id, key),
            completed(ts_done, "connector_config_read", trace, resource_id, enabled=enabled,
                      connector_name="crm_sync_bridge", customer_email=email,
                      customer_reference=user)]


def trace_file(lookups, begin, end):
    """A whole read-back export: a bracket header, the lookup lines in time order, a footer.
    `lookups` is a list of line groups; the declared count is the number of started lines."""
    lines = [line for group in lookups for line in group]
    lines.sort(key=lambda l: l.split(" ", 1)[0])  # stable, so a started line precedes its partner
    n = sum(1 for l in lines if "_started " in l)
    head = logfmt(begin, "readback_collector", "export_started", target_count=n,
                  mode="point_in_time")
    tail = logfmt(end, "readback_collector", "export_completed", queries=n, dropped=0)
    return "\n".join([head, *lines, tail]) + "\n"


def profile_record(resource_id, email, user, name, phone):
    return {"resource_id": resource_id, "customer_reference": user, "full_name": name,
            "email": email, "phone": phone, "account_status": "ACTIVE"}


def bundle(snapshots, exported_at):
    """JSON text of a snapshot bundle. snapshots: [(captured_at, data_store, collection, records)]."""
    body = [{"captured_at": at, "data_store": store, "collection": coll, "records": recs}
            for at, store, coll, recs in snapshots]
    return json.dumps({"exported_at": exported_at, "source": "records_assurance_service",
                       "snapshots": body}, indent=2) + "\n"
