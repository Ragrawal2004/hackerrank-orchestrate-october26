"""Synthetic observability log lines for adapter tests, in each of the six syntaxes.
No dataset files are read. `line` renders one logical event; `log` joins lines into file text."""
import json

SYNTAXES = ("logfmt", "json_line", "syslog5424", "bracketed", "pipe", "aws_kv")
PRI = {"DEBUG": 15, "INFO": 14, "WARN": 12, "ERROR": 11}


def _bare(v):
    return "null" if v is None else str(v).lower() if isinstance(v, bool) else str(v)


def _quoted(v):
    s = _bare(v)
    return '"' + s.replace("\\", "\\\\").replace('"', '\\"') + '"'


def _logfmt_value(v):
    s = _bare(v)
    if s == "" or any(c in s for c in ' "@\\='):
        return _quoted(v)
    return s


def line(syntax, ts, service, event, trace=None, level="INFO", instance=None, region="eu-west-1",
         **kv):
    """One line. `trace=None` renders an infrastructure line whose slot repeats the instance.
    `region=None` leaves the region field out."""
    instance = instance or f"{service.split('_')[0]}-1"
    meta = {"instance": instance}
    if region is not None and trace is not None:
        meta.update(pod=f"{instance}-ab12", region=region)
    slot = trace or instance
    if syntax == "logfmt":
        parts = [f"level={level}", f"service={service}"]
        parts += [f"{k}={_logfmt_value(v)}" for k, v in meta.items()]
        if trace:
            parts.append(f"trace_id={trace}")
        parts.append(f"event={event}")
        parts += [f"{k}={_logfmt_value(v)}" for k, v in kv.items()]
        return f"{ts} " + " ".join(parts)
    if syntax == "json_line":
        body = {"severity": level, "logger": service, "service": service, **meta}
        if trace:
            body["trace_id"] = trace
        body["event"] = event
        body.update({k: _bare(v) for k, v in kv.items()})
        return f"{ts} " + json.dumps(body, separators=(",", ":"), ensure_ascii=False)
    quoted = " ".join(f"{k}={_quoted(v)}" for k, v in {**meta, **kv}.items())
    if syntax == "syslog5424":
        tail = " ".join(f"{k}={_quoted(v)}" for k, v in {**meta, "event": event, **kv}.items())
        return f"{ts} <{PRI[level]}>1 node-1 {service} {slot} - - level={level} {tail}"
    if syntax == "bracketed":
        return f"{ts} [{level}] [{service}] [{slot}] {event}: {quoted}"
    if syntax == "pipe":
        return f"{ts} | {level} | {service} | {slot} | {event} | {quoted}"
    if syntax == "aws_kv":
        inner = " ".join(f"{k}={_quoted(v)}" for k, v in {**meta, "event": event, **kv}.items())
        msg = inner.replace("\\", "\\\\").replace('"', '\\"')
        return f'{ts} requestId={slot} level={level} function={service} message="{msg}"'
    raise ValueError(syntax)


def log(*lines):
    return "\n".join(lines) + "\n"


def envelope_start(syntax, ts, expected=10):
    return line(syntax, ts, "log_router", "export_started", level="DEBUG",
                instance="collector-1", sources="platform_services",
                expected_source_events=expected)


def envelope_end(syntax, ts, events=10):
    return line(syntax, ts, "log_router", "export_completed", instance="collector-1",
                source_events=events, auxiliary_events=0)


def noise(syntax, ts):
    return line(syntax, ts, "dns_cache", "cache_refresh", level="DEBUG", instance="resolver-1",
                entries=120)


def lookup_pair(syntax, t_start, t_done, trace, key, rid, rows=0, status=404, svc="account_service"):
    """profile_lookup_started (subject key) + completed (resource) joined by trace."""
    return [line(syntax, t_start, svc, "profile_lookup_started", trace, level="DEBUG",
                 lookup_key=key, attempt=1),
            line(syntax, t_done, svc, "profile_lookup_completed", trace, resource_id=rid,
                 rows=rows, http_status=status, duration_ms=40)]


def obs_file(syntax, *body, start="2026-09-15T08:00:00Z", end="2026-09-15T11:59:00Z"):
    """A whole observability export: envelope, a noise line, the body lines."""
    return log(envelope_start(syntax, start), noise(syntax, start), *body,
               envelope_end(syntax, end))


# ---- other log shapes the observability adapter must not claim -----------------------------

def trace_log(ts="2026-09-26T07:49:50Z"):
    """resource_lookup_trace shape: logfmt, no instance, rb- traces, resource on started lines."""
    return log(
        f"{ts} level=INFO service=readback_collector event=export_started target_count=1 "
        f"mode=point_in_time",
        f"{ts} level=DEBUG service=account_service trace_id=rb-1 event=profile_lookup_started "
        f"resource_id=account_1 lookup_key=ana@example.com",
        f"{ts} level=INFO service=account_service trace_id=rb-1 event=profile_lookup_completed "
        f"resource_id=account_1 rows=0 http_status=404",
        f"{ts} level=INFO service=readback_collector event=export_completed queries=1 dropped=0")


def ledger_log(ts="2026-09-06T07:00:00Z"):
    """record_lookup ledger lines (no level= key)."""
    return log(*[f'{ts} INFO service=search_index_service event=record_lookup seq={i} '
                 f'system="account_service" resource_id="account_{i}" record_type="USER_PROFILE" '
                 f'result="NOT_FOUND" subject_reference="ana{i}@example.com"' for i in (1, 2)])


def support_replica_log(ts="2026-09-17T22:00:00Z"):
    """support_replica shape: instance and pod, but no region and no observability state event."""
    return log(
        f"{ts} level=DEBUG service=support_replica instance=support-eu-03 pod=replica-7c9f "
        f"event=batch_started batch=rp-1 source=legacy_contact_index cursor=1",
        f"{ts} level=DEBUG service=support_replica instance=support-eu-03 pod=replica-7c9f "
        f"trace_id=tr-rp-1 event=contact_query_started lookup_key=ana@example.com",
        f"{ts} level=INFO service=support_replica instance=support-eu-03 pod=replica-7c9f "
        f"trace_id=tr-rp-1 event=contact_query_completed resource_id=support_1 rows=1",
        f"{ts} level=WARN service=support_replica instance=support-eu-03 pod=replica-7c9f "
        f"trace_id=tr-rp-1 event=deletion_marker_check resource_id=support_1 marker_found=false")
