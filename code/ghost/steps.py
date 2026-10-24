"""Step vocabulary, ordering, numbering and serialization (DESIGN §5, SPEC §1.2). Pure, no I/O.

Ordering: phase, then canonical operation order, then system_id, then resource_id as a plain
string. The canonical operation order already puts disable/block before record changes.
"""
import json

PHASES = ("FINDING", "REMEDIATION", "VERIFICATION")

# (operation, phase, state) in canonical order.
_TABLE = (
    ("REPORT_REMAINING_RECORD", "FINDING", "PRESENT"),
    ("REPORT_EXPOSED_PII", "FINDING", "PRESENT"),
    ("REPORT_ACTIVE_DATA_FLOW", "FINDING", "ACTIVE"),
    ("REPORT_RESTORE_RISK", "FINDING", "ACTIVE"),
    ("DISABLE_DATA_FLOW", "REMEDIATION", "DISABLED"),
    ("BLOCK_RESTORE", "REMEDIATION", "BLOCKED"),
    ("DELETE_RECORD", "REMEDIATION", "ABSENT"),
    ("REDACT_RECORD", "REMEDIATION", "REDACTED"),
    ("ANONYMIZE_RECORD", "REMEDIATION", "ANONYMIZED"),
    ("VERIFY_RECORD_ABSENT", "VERIFICATION", "ABSENT"),
    ("VERIFY_RECORD_PRESENT", "VERIFICATION", "PRESENT"),
    ("VERIFY_RECORD_REDACTED", "VERIFICATION", "REDACTED"),
    ("VERIFY_RECORD_ANONYMIZED", "VERIFICATION", "ANONYMIZED"),
    ("VERIFY_DATA_FLOW_DISABLED", "VERIFICATION", "DISABLED"),
    ("VERIFY_RESTORE_BLOCKED", "VERIFICATION", "BLOCKED"),
)

OPS = tuple(op for op, _, _ in _TABLE)
OP_PHASE = {op: phase for op, phase, _ in _TABLE}
OP_STATE = {op: state for op, _, state in _TABLE}

STEP_KEYS = ("step_no", "phase", "system_id", "resource_id", "operation", "state", "evidence_id")

# finding operation -> (allowed remediation operations, allowed verification operations).
# For PII the two tuples are index-aligned: REDACT pairs with REDACTED, ANONYMIZE with ANONYMIZED.
FINDING_TEMPLATES = {
    "REPORT_REMAINING_RECORD": (("DELETE_RECORD",), ("VERIFY_RECORD_ABSENT",)),
    "REPORT_EXPOSED_PII": (("REDACT_RECORD", "ANONYMIZE_RECORD"),
                           ("VERIFY_RECORD_REDACTED", "VERIFY_RECORD_ANONYMIZED")),
    "REPORT_ACTIVE_DATA_FLOW": (("DISABLE_DATA_FLOW",), ("VERIFY_DATA_FLOW_DISABLED",)),
    "REPORT_RESTORE_RISK": (("BLOCK_RESTORE",), ("VERIFY_RESTORE_BLOCKED",)),
}

_OP_INDEX = {op: i for i, op in enumerate(OPS)}
_PHASE_INDEX = {p: i for i, p in enumerate(PHASES)}


def make_step(phase, system_id, resource_id, operation, evidence_id=""):
    """A step without step_no; the state is fixed by the operation."""
    if operation not in OP_PHASE:
        raise ValueError(f"unknown operation {operation!r}")
    if phase not in _PHASE_INDEX:
        raise ValueError(f"unknown phase {phase!r}")
    if OP_PHASE[operation] != phase:
        raise ValueError(f"{operation} belongs to {OP_PHASE[operation]}, not {phase}")
    if not isinstance(evidence_id, str):
        raise ValueError("evidence_id must be a string ('' when there is none)")
    return {"phase": phase, "system_id": system_id, "resource_id": resource_id,
            "operation": operation, "state": OP_STATE[operation], "evidence_id": evidence_id}


def sort_key(step):
    return (_PHASE_INDEX[step["phase"]], _OP_INDEX[step["operation"]],
            step["system_id"], step["resource_id"])


def order_and_number(steps):
    """Return new step dicts (contract key order) sorted by `sort_key`, step_no 1..n."""
    ordered = sorted(steps, key=sort_key)
    return [{"step_no": i, **{k: s[k] for k in STEP_KEYS[1:]}}
            for i, s in enumerate(ordered, start=1)]


def serialize_steps(steps):
    """Deterministic steps_json: fixed key order, compact separators, no ASCII escaping."""
    out = []
    for s in steps:
        if set(s) != set(STEP_KEYS):
            raise ValueError(f"step keys must be exactly {list(STEP_KEYS)}, got {sorted(s)}")
        out.append({k: s[k] for k in STEP_KEYS})
    return json.dumps(out, ensure_ascii=False, separators=(",", ":"))
