"""The custom evaluation cases CC1-CC6: synthetic users, evidence, activity and hand-written
expected answers. Nothing here is copied from dataset/ and nothing is produced by the pipeline.

Expected steps are literals derived from AGENTS.md section 6 (operation->state table 6.4, phase
order, citation rules 6.6) and the ordering that all 32 sample answers follow: phase, then the
canonical operation order, then system_id, then resource_id. They were written before the first
run; any later change is recorded in evaluation_report.md with its reason.

`controls` names scenarios whose hazard is removed (a negative control): the same data with the
decisive line changed must give the other answer, which proves the case tests what it claims.
"""
import hashlib
from dataclasses import dataclass, field

from . import render as r

# AGENTS.md 6.4: the fixed operation -> (phase, state) table.
OPS = {
    "REPORT_REMAINING_RECORD": ("FINDING", "PRESENT"),
    "REPORT_EXPOSED_PII": ("FINDING", "PRESENT"),
    "REPORT_ACTIVE_DATA_FLOW": ("FINDING", "ACTIVE"),
    "REPORT_RESTORE_RISK": ("FINDING", "ACTIVE"),
    "DISABLE_DATA_FLOW": ("REMEDIATION", "DISABLED"),
    "BLOCK_RESTORE": ("REMEDIATION", "BLOCKED"),
    "DELETE_RECORD": ("REMEDIATION", "ABSENT"),
    "REDACT_RECORD": ("REMEDIATION", "REDACTED"),
    "ANONYMIZE_RECORD": ("REMEDIATION", "ANONYMIZED"),
    "VERIFY_RECORD_ABSENT": ("VERIFICATION", "ABSENT"),
    "VERIFY_RECORD_PRESENT": ("VERIFICATION", "PRESENT"),
    "VERIFY_RECORD_REDACTED": ("VERIFICATION", "REDACTED"),
    "VERIFY_RECORD_ANONYMIZED": ("VERIFICATION", "ANONYMIZED"),
    "VERIFY_DATA_FLOW_DISABLED": ("VERIFICATION", "DISABLED"),
    "VERIFY_RESTORE_BLOCKED": ("VERIFICATION", "BLOCKED"),
}

# 15 categories and their required action, written from the problem statement's policy list.
RULES = [
    ("USER_PROFILE", "Always", "DELETE"),
    ("SECONDARY_IDENTIFIER", "Always", "DELETE"),
    ("SEARCH_INDEX_ENTRY", "Always", "DELETE"),
    ("CACHE_ENTRY", "Always", "DELETE"),
    ("SUPPORT_MESSAGE", "Thread mixes customer, agent or order context kept for the business",
     "REDACT"),
    ("SUPPORT_ATTACHMENT", "Attachment is owned by the subject alone", "DELETE"),
    ("ANALYTICS_PROFILE", "Record can still be tied to a person", "ANONYMIZE"),
    ("PRODUCT_TELEMETRY", "Record can still be tied to a person", "ANONYMIZE"),
    ("USER_EXPORT", "Always", "DELETE"),
    ("MARKETING_PROFILE", "Always", "DELETE"),
    ("EMAIL_DELIVERY_LOG", "Entry holds an email address", "REDACT"),
    ("SECURITY_AUDIT_LOG", "Entry is inside its stated retention window", "RETAIN"),
    ("LEGAL_HOLD_RECORD", "A stated legal hold is in force", "RETAIN"),
    ("BACKUP_ARCHIVE", "Restore access can be switched off", "RETAIN"),
    ("ACCOUNT_TOMBSTONE", "Only the stable internal reference must be kept", "REDACT"),
]


def step(op, system, resource, evidence=""):
    phase, state = OPS[op]
    return {"phase": phase, "system_id": system, "resource_id": resource, "operation": op,
            "state": state, "evidence_id": evidence}


def numbered(steps):
    return [{"step_no": i, **s} for i, s in enumerate(steps, 1)]


def sha(text):
    return hashlib.sha256(text.encode()).hexdigest()


@dataclass
class User:
    user_id: str
    name: str
    email: str
    phone: str
    updated_at: str = "2026-04-01T08:00:00Z"
    old_emails: tuple = ()  # (old email, changed_at)


@dataclass
class Request:
    case_id: str
    email: str
    requested_at: str
    reported_status: str
    reported_at: str
    audit_at: str
    text: str


@dataclass
class Evidence:
    file_id: str
    path: str
    origin: str
    captured_at: str
    text: str


@dataclass
class Spec:
    users: list = field(default_factory=list)
    requests: list = field(default_factory=list)
    activities: list = field(default_factory=list)  # (id, time, type, source, target, rid, result, details)
    evidence: list = field(default_factory=list)
    expected: dict = field(default_factory=dict)  # case_id -> (status, [steps])
    labels: dict = field(default_factory=dict)  # case_id -> scenario label
    control_status: dict = field(default_factory=dict)  # case_id -> status when the hazard is removed
    diagnostics: dict = field(default_factory=dict)  # case_id -> [(event type, {field: value})]


# ---- users --------------------------------------------------------------------------------------

U1 = User("user_9001", "Marta Okonkwo-Lind", "marta.lind@example.test", "+44 7700 900101",
          "2026-03-02T10:00:00Z", (("marta.okonkwo@example.test", "2026-03-02T10:00:00Z"),))
U2 = User("user_9002", "Ibrahim Vasquez", "ibrahim.vasquez@example.test", "+44 7700 900102")
U3 = User("user_9003", "Ibrahim Vasquez-Rao", "ibrahim.vr@example.test", "+44 7700 900103")
U4 = User("user_9004", "Sunniva Haugen", "sunniva.haugen@example.test", "+44 7700 900104")
U5 = User("user_9005", "Dagmar Eze", "dagmar.eze@example.test", "+44 7700 900105")
U6 = User("user_9006", "Pavel Nakagawa", "pavel.nakagawa@example.test", "+44 7700 900106")
U7 = User("user_9007", "Lucia Fontaine", "lucia.fontaine@example.test", "+44 7700 900107")
DECOYS = [  # near-miss names and emails that must never link by similarity
    User("user_9008", "Marta Okonkwo", "marta.okonkwo.jr@example.test", "+44 7700 900108"),
    User("user_9009", "Sunniva Haugen", "sunniva.h@example.test", "+44 7700 900109"),
]
ALL_USERS = [U1, U2, U3, U4, U5, U6, U7, *DECOYS]

LOG_ORIGIN = "service_readback_logs"
BUNDLE_ORIGIN = "records_assurance_snapshot_bundle"


def _log(spec, n, name, captured_at, lookups, begin, end):
    spec.evidence.append(Evidence(f"evidence_{n}", f"evidence/logs/{name}.log", LOG_ORIGIN,
                                  captured_at, r.trace_file(lookups, begin, end)))


# ---- CC1: a failed read that looks like success, requested under an old email ------------------

def cc1(spec, controls):
    old, new = U1.old_emails[0][0], U1.email
    unavailable = r.profile_unavailable if "CC1" not in controls else r.profile_absent
    _log(spec, 901, "lookup_trace_cc1", "2026-08-06T10:00:00Z", [
        r.profile_present("2026-08-06T08:10:01Z", "2026-08-06T08:10:02Z", "rb-c101", "account_9101", old),
        unavailable("2026-08-06T09:20:01Z", "2026-08-06T09:20:02Z", "rb-c102", "account_9101", old),
        r.profile_errored("2026-08-06T09:50:01Z", "2026-08-06T09:50:02Z", "rb-c103", "account_9101", new),
    ], "2026-08-06T08:00:00Z", "2026-08-06T10:00:00Z")
    spec.activities.append(("activity_9001", "2026-08-04T10:00:00Z", "DELETE_RECORD", "deletion_worker",
                            "account_service", "account_9101", "SUCCESS",
                            "Deletion worker submitted primary profile account_9101 to account_service."))
    spec.requests.append(Request(
        "case_901", old, "2026-08-03T09:00:00Z", "COMPLETE", "2026-08-05T02:00:00Z",
        "2026-08-06T12:00:00Z",
        "Hello, please permanently erase my customer account and all personal data linked to it. "
        "I registered with a different email address than the one on file today."))
    spec.labels["case_901"] = "CC1"
    spec.expected["case_901"] = ("INCOMPLETE", [
        step("REPORT_REMAINING_RECORD", "account_service", "account_9101", "evidence_901"),
        step("DELETE_RECORD", "account_service", "account_9101"),
        step("VERIFY_RECORD_ABSENT", "account_service", "account_9101"),
    ])
    spec.control_status["case_901"] = "COMPLETE"  # the 09:20 read is a real 404: the account is gone


# ---- CC2: one trace names two different subjects (A8) -----------------------------------------

def cc2(spec, controls):
    owner = U2 if "CC2" in controls else U3
    _log(spec, 902, "lookup_trace_cc2", "2026-08-12T10:00:00Z", [
        r.profile_present("2026-08-12T08:00:01Z", "2026-08-12T08:00:02Z", "rb-c201", "account_9201",
                          U2.email, email=owner.email, customer_reference=owner.user_id),
        r.profile_absent("2026-08-12T08:30:01Z", "2026-08-12T08:30:02Z", "rb-c202", "account_9202",
                         U2.email),
    ], "2026-08-12T07:50:00Z", "2026-08-12T10:00:00Z")
    base = dict(requested_at="2026-08-10T09:00:00Z", reported_status="PARTIALLY_COMPLETE",
                reported_at="2026-08-11T02:00:00Z", audit_at="2026-08-12T12:00:00Z")
    spec.requests.append(Request(
        "case_902", U2.email, text="Please remove my account and every personal record you keep about me.",
        **base))
    spec.requests.append(Request(
        "case_903", U3.email, text="I want my profile and all the data attached to it deleted from "
                                "your systems.", **base))
    spec.labels.update(case_902="CC2a", case_903="CC2b")
    spec.expected["case_902"] = ("COMPLETE", [
        step("VERIFY_RECORD_ABSENT", "account_service", "account_9202", "evidence_902"),
    ])
    spec.expected["case_903"] = ("INCOMPLETE", [
        step("REPORT_REMAINING_RECORD", "account_service", "account_9201", "evidence_902"),
        step("DELETE_RECORD", "account_service", "account_9201"),
        step("VERIFY_RECORD_ABSENT", "account_service", "account_9201"),
    ])
    # control: the completed line names subject A, so the record is A's and case_902 turns INCOMPLETE;
    # case_903 then has nothing linked and is INCOMPLETE with no steps.
    spec.control_status.update(case_902="INCOMPLETE")
    spec.diagnostics["case_902"] = [("a8_conflict", {})]


# ---- CC3: evidence captured after audit_at (the clock gate), same user at two audit times -----

def cc3(spec, controls):
    early_bundle = "CC3" in controls  # control: the bundle is captured before case_904's audit_at
    day = "19" if early_bundle else "21"
    _log(spec, 904, "lookup_trace_cc3", "2026-08-18T09:30:00Z", [
        r.profile_absent("2026-08-18T08:40:01Z", "2026-08-18T08:40:02Z", "rb-c301", "account_9301",
                         U4.email),
        r.audit_record("2026-08-18T08:45:01Z", "2026-08-18T08:45:02Z", "rb-c302", "audit_record_9302",
                       U4.email, U4.user_id, "2027-06-30T23:59:59Z"),
    ], "2026-08-18T08:30:00Z", "2026-08-18T09:30:00Z")
    spec.evidence.append(Evidence(
        "evidence_905", "evidence/records/profile_bundle_cc3.json", BUNDLE_ORIGIN,
        f"2026-08-{int(day) + 1}T12:00:00Z",
        r.bundle([(f"2026-08-{day}T09:00:00Z", "account_service", "customer_profiles", [
            r.profile_record("account_9301", U4.email, U4.user_id, U4.name, U4.phone)])],
            f"2026-08-{int(day) + 1}T12:00:00Z")))
    common = dict(email=U4.email, requested_at="2026-08-16T09:00:00Z", reported_status="COMPLETE",
                  reported_at="2026-08-17T02:00:00Z")
    spec.requests.append(Request(
        "case_904", audit_at="2026-08-20T12:00:00Z",
        text="Kindly delete my account and the personal data that goes with it.", **common))
    spec.requests.append(Request(
        "case_905", audit_at="2026-08-23T12:00:00Z",
        text="Following my earlier message, please make sure everything tied to my account is "
             "deleted.", **{**common, "requested_at": "2026-08-22T09:00:00Z",
                            "reported_at": "2026-08-22T20:00:00Z"}))
    spec.labels.update(case_904="CC3a", case_905="CC3b")
    spec.expected["case_904"] = ("COMPLETE", [
        step("VERIFY_RECORD_ABSENT", "account_service", "account_9301", "evidence_904"),
        step("VERIFY_RECORD_PRESENT", "security_service", "audit_record_9302", "evidence_904"),
    ])
    spec.expected["case_905"] = ("INCOMPLETE", [
        step("REPORT_REMAINING_RECORD", "account_service", "account_9301", "evidence_905"),
        step("DELETE_RECORD", "account_service", "account_9301"),
        step("VERIFY_RECORD_ABSENT", "account_service", "account_9301"),
        step("VERIFY_RECORD_PRESENT", "security_service", "audit_record_9302"),
    ])
    spec.control_status["case_904"] = "INCOMPLETE"
    spec.diagnostics["case_904"] = [("dossier", {"clock.late_records_excluded": 1})]


# ---- CC4: a resource recreated after it was seen deleted (later record, earlier-looking file) --

def cc4(spec, controls):
    # evidence_906 is the later FILE (manifest 10:00) but its only line is the older read (07:30);
    # evidence_907 is the earlier file (manifest 08:55) with the newer read (08:50) of the account.
    _log(spec, 906, "lookup_trace_cc4_a", "2026-08-25T10:00:00Z", [
        r.profile_absent("2026-08-25T07:30:01Z", "2026-08-25T07:30:02Z", "rb-c401", "account_9401",
                         U5.email),
    ], "2026-08-25T07:00:00Z", "2026-08-25T10:00:00Z")
    again = r.profile_absent if "CC4" in controls else r.profile_present
    _log(spec, 907, "lookup_trace_cc4_b", "2026-08-25T08:55:00Z", [
        again("2026-08-25T08:50:01Z", "2026-08-25T08:50:02Z", "rb-c402", "account_9401", U5.email),
    ], "2026-08-25T08:45:00Z", "2026-08-25T08:55:00Z")
    spec.activities.append(("activity_9002", "2026-08-24T09:00:00Z", "DELETE_RECORD", "deletion_worker",
                            "account_service", "account_9401", "SUCCESS",
                            "Deletion worker submitted primary profile account_9401 to account_service."))
    spec.activities.append(("activity_9003", "2026-08-25T08:45:00Z", "SYNC_RECORD", "account_service",
                            "account_replication_cluster", "account_9401", "SUCCESS",
                            "Replication worker reported its final checkpoint for account_9401."))
    spec.requests.append(Request(
        "case_906", U5.email, "2026-08-23T09:00:00Z", "COMPLETE", "2026-08-24T02:00:00Z",
        "2026-08-25T12:00:00Z",
        "Please erase my account together with any personal information you hold about me."))
    spec.labels["case_906"] = "CC4"
    spec.expected["case_906"] = ("INCOMPLETE", [
        step("REPORT_REMAINING_RECORD", "account_service", "account_9401", "evidence_907"),
        step("DELETE_RECORD", "account_service", "account_9401"),
        step("VERIFY_RECORD_ABSENT", "account_service", "account_9401"),
    ])
    spec.control_status["case_906"] = "COMPLETE"


# ---- CC5: retained audit record, an active data flow and the remediation order ----------------

def cc5(spec, controls):
    until = "2026-01-31T23:59:59Z" if "CC5" in controls else "2028-01-31T23:59:59Z"
    e = U6.email
    _log(spec, 908, "lookup_trace_cc5", "2026-09-02T10:00:00Z", [
        r.profile_present("2026-09-02T08:00:01Z", "2026-09-02T08:00:02Z", "rb-c501", "account_9501", e),
        r.search_hit("2026-09-02T08:10:01Z", "2026-09-02T08:10:02Z", "rb-c502", "search_9502", e, e,
                     U6.user_id),
        r.audit_record("2026-09-02T08:20:01Z", "2026-09-02T08:20:02Z", "rb-c503", "audit_record_9503",
                       e, U6.user_id, until),
        r.connector("2026-09-02T08:30:01Z", "2026-09-02T08:30:02Z", "rb-c504", "data_flow_9504", e,
                    True, e, U6.user_id),
    ], "2026-09-02T07:50:00Z", "2026-09-02T10:00:00Z")
    spec.requests.append(Request(
        "case_907", e, "2026-08-31T09:00:00Z", "NOT_STARTED", "2026-09-01T02:00:00Z",
        "2026-09-02T12:00:00Z",
        "I would like my account closed and all of my personal data deleted, please."))
    spec.labels["case_907"] = "CC5"
    spec.expected["case_907"] = ("INCOMPLETE", [
        step("REPORT_REMAINING_RECORD", "account_service", "account_9501", "evidence_908"),
        step("REPORT_REMAINING_RECORD", "search_service", "search_9502", "evidence_908"),
        step("REPORT_ACTIVE_DATA_FLOW", "integration_service", "data_flow_9504", "evidence_908"),
        step("DISABLE_DATA_FLOW", "integration_service", "data_flow_9504"),
        step("DELETE_RECORD", "account_service", "account_9501"),
        step("DELETE_RECORD", "search_service", "search_9502"),
        step("VERIFY_RECORD_ABSENT", "account_service", "account_9501"),
        step("VERIFY_RECORD_ABSENT", "search_service", "search_9502"),
        step("VERIFY_RECORD_PRESENT", "security_service", "audit_record_9503"),
        step("VERIFY_DATA_FLOW_DISABLED", "integration_service", "data_flow_9504"),
    ])
    # control: retention already over, so the audit record is no longer a required retained record
    spec.control_status["case_907"] = "INCOMPLETE"


# ---- CC6: deletion words in a request that is not a deletion ----------------------------------

def cc6(spec, controls):
    spec.requests.append(Request(
        "case_908", U7.email, "2026-09-05T09:00:00Z", "NOT_STARTED", "2026-09-06T02:00:00Z",
        "2026-09-07T12:00:00Z",
        "Please keep my account exactly as it is. The only thing I want is to stop the "
        "promotional newsletters, so do not delete anything."))
    spec.labels["case_908"] = "CC6"
    spec.expected["case_908"] = ("NOT_APPLICABLE", [])


SCENARIOS = {"CC1": cc1, "CC2": cc2, "CC3": cc3, "CC4": cc4, "CC5": cc5, "CC6": cc6}


def build_spec(controls=frozenset()):
    """The whole synthetic dataset as data. `controls` removes the hazard of the named scenarios."""
    spec = Spec(users=list(ALL_USERS))
    for build in SCENARIOS.values():
        build(spec, frozenset(controls))
    spec.requests.sort(key=lambda q: q.case_id)
    return spec
