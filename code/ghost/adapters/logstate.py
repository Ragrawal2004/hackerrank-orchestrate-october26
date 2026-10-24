"""State rules shared by the two log adapters (observability_log, trace_log).

A mapper turns the kv of one completed line into (state, attrs). A line that reports a failed
operation raises `Failed`: it is understood but proves no state, so `rows=0 http_status=503`
never becomes ABSENT. `common.Skip` marks a line outside the vocabulary (counted unparsed).
"""
from .. import policy
from . import common

FAILED_RESULTS = frozenset({"error", "timeout"})
KNOWN_SYSTEMS = frozenset(policy.SYSTEM_CATEGORY) | {"support_platform", "analytics_platform"}


class Failed(Exception):
    """The line reports a failed operation: understood, but it proves no state."""


def int_field(kv, field):
    v = kv.get(field)
    if not (isinstance(v, str) and v.isdigit()):
        raise common.Skip(f"{field} is not a non-negative integer: {v!r}")
    return int(v)


def bool_field(kv, field):
    v = kv.get(field)
    if v not in ("true", "false"):
        raise common.Skip(f"{field} is not true/false: {v!r}")
    return v == "true"


def failed_form(event):
    """`x_completed` -> `x_failed`; any other state event `e` -> `e_failed`."""
    return event[:-len("_completed")] + "_failed" if event.endswith("_completed") \
        else event + "_failed"


def profile(kv):
    status = int_field(kv, "http_status")
    if status >= 500:
        raise Failed(f"http_status {status}")
    rows = int_field(kv, "rows")
    if (rows, status) == (1, 200):
        return "PRESENT", {"rows": rows, "http_status": status}
    if (rows, status) == (0, 404):
        return "ABSENT", {"rows": rows, "http_status": status}
    raise common.Skip(f"rows {rows} and http_status {status} disagree")


def index(kv):
    result = kv.get("result")
    if result in FAILED_RESULTS:
        raise Failed(f"result {result}")
    if result not in ("hit", "miss"):
        raise common.Skip(f"unknown result {result!r}")
    return ("PRESENT" if result == "hit" else "ABSENT"), {"result": result,
                                                         "index": kv.get("index")}


def audit_rows(kv):
    if int_field(kv, "rows") != 1:
        raise common.Skip("audit read rows is not 1")


def connector(kv):
    on = bool_field(kv, "enabled")
    return ("ACTIVE" if on else "DISABLED"), {"enabled": on}
