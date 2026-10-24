"""Policy engine (DESIGN §4): category, required end state, compliance, status and steps.

A pure function of one dossier and the rule table: no I/O and no LLM. Events are dicts with a
`type`, like the dossier's, and the caller writes them to diagnostics.

Attribute conventions the adapters must follow (in the state source's `attrs`):
- `record_type` (a rule category, e.g. from a ledger) and `collection` / `table` (store names);
- `shared_context` / `subject_only` (bool) for support records;
- `retention_until` (ISO time) for audit records; `hold_active` (bool) or `hold_until` for holds;
- direct identifier fields `name`, `email`, `phone`, `email_hash` (null once removed) and the
  stable `subject_reference` a tombstone keeps.
"""
from . import steps as st
from . import store

RULE_CATEGORIES = (
    "USER_PROFILE", "SECONDARY_IDENTIFIER", "SEARCH_INDEX_ENTRY", "CACHE_ENTRY",
    "SUPPORT_MESSAGE", "SUPPORT_ATTACHMENT", "ANALYTICS_PROFILE", "PRODUCT_TELEMETRY",
    "USER_EXPORT", "MARKETING_PROFILE", "EMAIL_DELIVERY_LOG", "SECURITY_AUDIT_LOG",
    "LEGAL_HOLD_RECORD", "BACKUP_ARCHIVE", "ACCOUNT_TOMBSTONE",
)
DATA_FLOW = "DATA_FLOW"  # a process, not data: no rule row, must be DISABLED
CATEGORIES = frozenset(RULE_CATEGORIES) | {DATA_FLOW}
ACTION_STATE = {"DELETE": "ABSENT", "REDACT": "REDACTED", "ANONYMIZE": "ANONYMIZED",
                "RETAIN": "PRESENT"}
RETAINED = frozenset({"SECURITY_AUDIT_LOG", "LEGAL_HOLD_RECORD", "BACKUP_ARCHIVE"})
NEVER_DELETED = RETAINED | {"ACCOUNT_TOMBSTONE", "SUPPORT_MESSAGE", "EMAIL_DELIVERY_LOG",
                            "ANALYTICS_PROFILE", "PRODUCT_TELEMETRY"}
IDENTIFIER_FIELDS = ("name", "email", "phone", "email_hash")

COLLECTION_CATEGORY = {
    "customer_profiles": "USER_PROFILE",
    "customer_profiles_primary": "USER_PROFILE",
    "customer_search_documents": "SEARCH_INDEX_ENTRY",
    "search_documents_primary": "SEARCH_INDEX_ENTRY",
    "customer_analytics_reports": "ANALYTICS_PROFILE",
    "customer_analytics_profiles": "ANALYTICS_PROFILE",
    "support_conversations": "SUPPORT_MESSAGE",
    "support_records": "SUPPORT_MESSAGE",
    "ticket_attachments": "SUPPORT_ATTACHMENT",
    "customer_export_jobs": "USER_EXPORT",
    "customer_exports": "USER_EXPORT",
    "connector_configurations": DATA_FLOW,
    "connector_subscriptions": DATA_FLOW,
    "backup_catalog": "BACKUP_ARCHIVE",
    "backup_restore_entries": "BACKUP_ARCHIVE",
    "security_audit_events": "SECURITY_AUDIT_LOG",
    "security_audit_records": "SECURITY_AUDIT_LOG",
    "retained_order_records": "LEGAL_HOLD_RECORD",
    "deleted_customer_references": "ACCOUNT_TOMBSTONE",
}
# Only systems that hold a single category; support, analytics and object storage are mixed.
SYSTEM_CATEGORY = {
    "account_service": "USER_PROFILE",
    "search_service": "SEARCH_INDEX_ENTRY",
    "cache_service": "CACHE_ENTRY",
    "export_service": "USER_EXPORT",
    "marketing_platform": "MARKETING_PROFILE",
    "security_service": "SECURITY_AUDIT_LOG",
    "legal_service": "LEGAL_HOLD_RECORD",
    "backup_service": "BACKUP_ARCHIVE",
    "identity_service": "ACCOUNT_TOMBSTONE",
    "integration_service": DATA_FLOW,
}
# Last resort (A7); every use is logged. Longer prefixes first.
PREFIX_CATEGORY = (
    ("email_delivery_", "EMAIL_DELIVERY_LOG"),
    ("audit_record_", "SECURITY_AUDIT_LOG"),
    ("legal_hold_", "LEGAL_HOLD_RECORD"),
    ("data_flow_", DATA_FLOW),
    ("tombstone_", "ACCOUNT_TOMBSTONE"),
    ("marketing_", "MARKETING_PROFILE"),
    ("analytics_", "ANALYTICS_PROFILE"),
    ("telemetry_", "PRODUCT_TELEMETRY"),
    ("account_", "USER_PROFILE"),
    ("search_", "SEARCH_INDEX_ENTRY"),
    ("export_", "USER_EXPORT"),
    ("backup_", "BACKUP_ARCHIVE"),
    ("support_", "SUPPORT_MESSAGE"),
    ("object_", "SUPPORT_ATTACHMENT"),
    ("cache_", "CACHE_ENTRY"),
)

VERIFY_OP = {"ABSENT": "VERIFY_RECORD_ABSENT", "PRESENT": "VERIFY_RECORD_PRESENT",
             "REDACTED": "VERIFY_RECORD_REDACTED", "ANONYMIZED": "VERIFY_RECORD_ANONYMIZED",
             "DISABLED": "VERIFY_DATA_FLOW_DISABLED", "BLOCKED": "VERIFY_RESTORE_BLOCKED"}
REMEDIATION_OP = {"REPORT_REMAINING_RECORD": {"ABSENT": "DELETE_RECORD"},
                  "REPORT_EXPOSED_PII": {"REDACTED": "REDACT_RECORD",
                                         "ANONYMIZED": "ANONYMIZE_RECORD"},
                  "REPORT_ACTIVE_DATA_FLOW": {"DISABLED": "DISABLE_DATA_FLOW"},
                  "REPORT_RESTORE_RISK": {"BLOCKED": "BLOCK_RESTORE"}}

# required end state -> (compliant observed states, {violating observed state: finding op},
#                        observed states meaning "the required record is gone")
_JUDGE = {
    "ABSENT": ({"ABSENT"}, dict.fromkeys(("PRESENT", "IDENTIFIABLE", "REDACTED", "ANONYMIZED"),
                                         "REPORT_REMAINING_RECORD"), set()),
    "REDACTED": ({"REDACTED", "ANONYMIZED"}, {"IDENTIFIABLE": "REPORT_EXPOSED_PII"}, {"ABSENT"}),
    "ANONYMIZED": ({"ANONYMIZED", "REDACTED"}, {"IDENTIFIABLE": "REPORT_EXPOSED_PII"},
                   {"ABSENT"}),
    "PRESENT": ({"PRESENT", "IDENTIFIABLE", "REDACTED", "ANONYMIZED"}, {}, {"ABSENT"}),
    "BLOCKED": ({"BLOCKED"}, {"RESTORABLE": "REPORT_RESTORE_RISK"}, {"ABSENT"}),
    "DISABLED": ({"DISABLED"}, {"ACTIVE": "REPORT_ACTIVE_DATA_FLOW"}, set()),
}


class PolicyError(ValueError):
    pass


# ---- rules ----------------------------------------------------------------------------------

def rules_from_rows(rows):
    """{category: {rule_id, condition, action}} from (rule_id, category, condition, action)."""
    rules = {}
    for rule_id, category, condition, action in rows:
        if action not in ACTION_STATE:
            raise PolicyError(f"{rule_id}: unknown action {action!r}")
        if category in rules:
            raise PolicyError(f"{rule_id}: second rule for {category}")
        rules[category] = {"rule_id": rule_id, "condition": condition, "action": action}
    missing = sorted(set(RULE_CATEGORIES) - set(rules))
    if missing:
        raise PolicyError(f"no rule for {missing}")
    return rules


def load_rules(conn):
    return rules_from_rows(conn.execute(
        "select rule_id, category, condition, action from rule order by rule_id"))


# ---- category -------------------------------------------------------------------------------

def _attrs(resource):
    src = resource.get("state_source") or {}
    return src.get("attrs") or {}


def categorize(resource, events):
    """(category, basis) by content first (A7); (None, None) when nothing fits."""
    attrs = _attrs(resource)
    rid, sys = resource["resource_id"], resource["system_id"]
    if resource.get("category") in CATEGORIES:
        return resource["category"], "adapter"
    if attrs.get("record_type") in CATEGORIES:
        return attrs["record_type"], "record_type"
    for key in ("collection", "table"):
        if attrs.get(key) in COLLECTION_CATEGORY:
            return COLLECTION_CATEGORY[attrs[key]], "collection"
    if attrs.get("shared_context") is True:
        return "SUPPORT_MESSAGE", "fields"
    if attrs.get("subject_only") is True:
        return "SUPPORT_ATTACHMENT", "fields"
    if sys in SYSTEM_CATEGORY:
        return SYSTEM_CATEGORY[sys], "system"
    for prefix, category in PREFIX_CATEGORY:
        if rid and rid.startswith(prefix):
            events.append({"type": "category_from_prefix", "system_id": sys, "resource_id": rid,
                           "category": category})
            return category, "prefix"
    events.append({"type": "category_unknown", "system_id": sys, "resource_id": rid})
    return None, None


# ---- required end state ---------------------------------------------------------------------

def _after(value, audit_at):
    try:
        return store.epoch_us(value) > store.epoch_us(audit_at)
    except (TypeError, ValueError):
        return None


def _condition(category, attrs, audit_at):
    """True / False when the rule's condition is decided by attrs, None when it cannot be.
    Categories whose condition only describes the violation (attributable, contains an email,
    restore can be blocked, account deleted) always apply and return True."""
    if category == "SUPPORT_MESSAGE":
        return attrs.get("shared_context") if isinstance(attrs.get("shared_context"), bool) else None
    if category == "SUPPORT_ATTACHMENT":
        return attrs.get("subject_only") if isinstance(attrs.get("subject_only"), bool) else None
    if category == "SECURITY_AUDIT_LOG":
        return _after(attrs["retention_until"], audit_at) if attrs.get("retention_until") else None
    if category == "LEGAL_HOLD_RECORD":
        if isinstance(attrs.get("hold_active"), bool):
            return attrs["hold_active"]
        return _after(attrs["hold_until"], audit_at) if attrs.get("hold_until") else None
    return True


def required_state(category, attrs, audit_at, rules, events):
    """Required end state at audit_at, or None when the rule's condition does not hold."""
    if category == DATA_FLOW:
        return "DISABLED"
    rule = rules[category]
    held = _condition(category, attrs, audit_at)
    if held is None:
        events.append({"type": "condition_assumed", "category": category,
                       "rule_id": rule["rule_id"], "condition": rule["condition"]})
    elif held is False:
        events.append({"type": "condition_unmet", "category": category,
                       "rule_id": rule["rule_id"], "condition": rule["condition"]})
        return None
    if category == "BACKUP_ARCHIVE":
        return "BLOCKED"
    return ACTION_STATE[rule["action"]]


def observed_state(resource, required, events):
    """The dossier state, with PRESENT refined by identifier fields where PII must go."""
    state = resource.get("state")
    if state != "PRESENT" or required not in ("REDACTED", "ANONYMIZED"):
        return state
    attrs = _attrs(resource)
    fields = [f for f in IDENTIFIER_FIELDS if f in attrs]
    if not fields:
        events.append({"type": "pii_assumed_exposed", "system_id": resource["system_id"],
                       "resource_id": resource["resource_id"]})
        return "IDENTIFIABLE"
    if any(attrs[f] not in (None, "") for f in fields):
        return "IDENTIFIABLE"
    return required


# ---- evaluation -----------------------------------------------------------------------------

def _judge(required, observed):
    """(verdict, finding op) where verdict is ok | violation | missing | unknown."""
    ok, bad, gone = _JUDGE[required]
    if observed in ok:
        return "ok", None
    if observed in bad:
        return "violation", bad[observed]
    if observed in gone:
        return "missing", None
    return "unknown", None


def _entry(resource, audit_at, rules, events):
    sys, rid = resource["system_id"], resource["resource_id"]
    where = {"system_id": sys, "resource_id": rid}
    e = {**where, "category": None, "category_basis": None, "required": None,
         "observed": resource.get("state"), "verdict": "uncategorized", "finding": None,
         "evidence": (resource.get("citation") or {}).get("pick"),
         "confidence": (resource.get("link") or {}).get("confidence", "high")}
    e["category"], e["category_basis"] = categorize(resource, events)
    if e["category"] is None:
        return e
    local = []
    e["required"] = required_state(e["category"], _attrs(resource), audit_at, rules, local)
    events.extend({**ev, **where} for ev in local)
    if e["required"] is None:
        e["verdict"] = "unmet"
        return e
    e["observed"] = observed_state(resource, e["required"], events)
    e["verdict"], finding = _judge(e["required"], e["observed"])
    if e["verdict"] == "missing":
        kind = "retained_missing" if e["category"] in RETAINED else "required_record_absent"
        events.append({"type": kind, **where, "category": e["category"]})
    elif e["verdict"] == "unknown":
        events.append({"type": "state_unknown", **where, "state": e["observed"],
                       "required": e["required"]})
    elif e["verdict"] == "violation":
        if e["confidence"] == "low":
            events.append({"type": "low_confidence_violation", **where, "finding": finding})
        elif not e["evidence"]:
            events.append({"type": "unciteable_finding", **where, "finding": finding})
        else:
            e["finding"] = finding
    return e


def _status(entries):
    """(status, no_steps reason or None) from the judged entries (DESIGN §4 step 4)."""
    if any(e["finding"] for e in entries):
        return "INCOMPLETE", None
    high = [e for e in entries if e["confidence"] != "low"]
    deletions = [e for e in high if e["required"] == "ABSENT"]
    unverified = [e for e in deletions if not (e["verdict"] == "ok" and e["evidence"])]
    unverified += [e for e in high if e["verdict"] == "violation"]
    if unverified:
        return "INCOMPLETE", "unverified_deletion"
    if not any(e["verdict"] == "ok" and e["evidence"] for e in entries):
        return "INCOMPLETE", "no_resources" if not entries else "nothing_verifiable"
    return "COMPLETE", None


def to_steps(entries, status):
    """Numbered steps for the judged entries; [] when nothing can be stated."""
    out = []
    for e in entries:
        sys, rid, f = e["system_id"], e["resource_id"], e.get("finding")
        if status == "INCOMPLETE":
            if f:
                rem = REMEDIATION_OP[f][e["required"]]
                if rem == "DELETE_RECORD" and e["category"] in NEVER_DELETED:
                    raise PolicyError(f"{e['category']} {rid} must never be deleted")
                out.append(st.make_step("FINDING", sys, rid, f, e["evidence"]))
                out.append(st.make_step("REMEDIATION", sys, rid, rem))
            if e["verdict"] in ("ok", "violation"):
                out.append(st.make_step("VERIFICATION", sys, rid, VERIFY_OP[e["required"]]))
        elif status == "COMPLETE" and e["verdict"] == "ok" and e["evidence"]:
            out.append(st.make_step("VERIFICATION", sys, rid, VERIFY_OP[e["required"]],
                                    e["evidence"]))
    return st.order_and_number(out)


def evaluate(dossier, rules):
    """{status, steps, entries, events, retained_missing} for one deletion case."""
    events = []
    resources = sorted(dossier["resources"],
                       key=lambda r: (r["system_id"] or "", r["resource_id"] or ""))
    entries = [_entry(r, dossier["audit_at"], rules, events) for r in resources]
    status, reason = _status(entries)
    result = to_steps(entries, status) if reason is None else []
    if reason is not None:
        events.append({"type": "no_steps", "reason": reason})
    missing = [{"system_id": e["system_id"], "resource_id": e["resource_id"],
                "category": e["category"]}
               for e in entries if e["verdict"] == "missing" and e["category"] in RETAINED]
    return {"status": status, "steps": result, "entries": entries, "events": events,
            "retained_missing": missing}
