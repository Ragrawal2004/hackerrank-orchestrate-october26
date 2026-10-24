import json
import unittest

from ghost import invariants, loaders, policy, steps
from tests.helpers import DATASET

AUDIT = "2026-09-15T12:00:00Z"
T = "2026-09-14T08:00:00Z"
BEFORE = "2026-09-01T00:00:00Z"
AFTER = "2027-01-01T00:00:00Z"

RULES = policy.rules_from_rows((r["rule_id"], r["data_category"], r["condition"],
                                r["required_action"]) for r in loaders.load_rules(DATASET))


def res(sys, rid, state, category=None, attrs=None, pick="evidence_1", confidence="high"):
    """A resource entry shaped like dossier._resolve's output."""
    source = None
    if state is not None:
        source = {"obs_id": 1, "file_id": pick, "locator": "L1", "record_at": T,
                  "manifest_at": T, "attrs": dict(attrs or {})}
    return {"system_id": sys, "resource_id": rid,
            "link": {"method": "direct", "via": {}, "confidence": confidence},
            "category": category, "state": state, "state_basis": "explicit" if state else None,
            "state_source": source, "recreated_by": None,
            "citation": {"rule": "E" if pick else None, "pick": pick, "rule_e": [], "rule_o": []}}


def dos(*resources, audit_at=AUDIT, case_id="case_33"):
    return {"case_id": case_id, "audit_at": audit_at, "resources": list(resources), "events": []}


def run(*resources, **kw):
    return policy.evaluate(dos(*resources, **kw), RULES)


def ops(plan):
    return [(s["phase"], s["operation"], s["system_id"], s["resource_id"], s["evidence_id"])
            for s in plan["steps"]]


def events(plan, kind):
    return [e for e in plan["events"] if e["type"] == kind]


def entry(plan, rid):
    hits = [e for e in plan["entries"] if e["resource_id"] == rid]
    assert len(hits) == 1, hits
    return hits[0]


ACCOUNT_OK = res("account_service", "account_1", "ABSENT", "USER_PROFILE")


class RuleLoadingTests(unittest.TestCase):
    def test_all_fifteen_rules_load(self):
        self.assertEqual(len(RULES), 15)
        self.assertEqual(RULES["ACCOUNT_TOMBSTONE"]["action"], "REDACT")
        self.assertEqual(RULES["BACKUP_ARCHIVE"]["action"], "RETAIN")

    def test_missing_rule_category_fails(self):
        rows = [(r["rule_id"], r["data_category"], r["condition"], r["required_action"])
                for r in loaders.load_rules(DATASET) if r["data_category"] != "CACHE_ENTRY"]
        with self.assertRaises(policy.PolicyError):
            policy.rules_from_rows(rows)

    def test_unknown_action_fails(self):
        rows = [(r["rule_id"], r["data_category"], r["condition"],
                 "SHRED" if r["data_category"] == "CACHE_ENTRY" else r["required_action"])
                for r in loaders.load_rules(DATASET)]
        with self.assertRaises(policy.PolicyError):
            policy.rules_from_rows(rows)


class CategoryTests(unittest.TestCase):
    def cat(self, sys, rid, category=None, attrs=None):
        events = []
        got = policy.categorize(res(sys, rid, "PRESENT", category, attrs), events)
        return got, events

    def test_adapter_category_wins(self):
        (c, basis), ev = self.cat("analytics_platform", "analytics_1", "SECONDARY_IDENTIFIER",
                                  {"record_type": "ANALYTICS_PROFILE"})
        self.assertEqual((c, basis), ("SECONDARY_IDENTIFIER", "adapter"))
        self.assertEqual(ev, [])

    def test_record_type_before_collection(self):
        (c, basis), _ = self.cat("support_platform", "support_1", None,
                                 {"record_type": "SUPPORT_ATTACHMENT",
                                  "collection": "support_conversations"})
        self.assertEqual((c, basis), ("SUPPORT_ATTACHMENT", "record_type"))

    def test_collection_and_table(self):
        for attrs, want in [({"collection": "deleted_customer_references"}, "ACCOUNT_TOMBSTONE"),
                            ({"collection": "ticket_attachments"}, "SUPPORT_ATTACHMENT"),
                            ({"table": "connector_subscriptions"}, "DATA_FLOW"),
                            ({"collection": "retained_order_records"}, "LEGAL_HOLD_RECORD"),
                            ({"table": "backup_restore_entries"}, "BACKUP_ARCHIVE")]:
            (c, basis), _ = self.cat("x_service", "x_1", None, attrs)
            self.assertEqual((c, basis), (want, "collection"), attrs)

    def test_field_signature_for_support_records(self):
        (c, _), _ = self.cat("support_platform", "support_1", None, {"shared_context": True})
        self.assertEqual(c, "SUPPORT_MESSAGE")
        (c, _), _ = self.cat("support_platform", "support_1", None, {"subject_only": True})
        self.assertEqual(c, "SUPPORT_ATTACHMENT")

    def test_single_category_system(self):
        (c, basis), ev = self.cat("security_service", "audit_record_9")
        self.assertEqual((c, basis), ("SECURITY_AUDIT_LOG", "system"))
        self.assertEqual(ev, [])

    def test_mixed_system_falls_to_logged_prefix(self):
        (c, basis), ev = self.cat("analytics_platform", "analytics_9914")
        self.assertEqual((c, basis), ("ANALYTICS_PROFILE", "prefix"))
        self.assertEqual([e["type"] for e in ev], ["category_from_prefix"])

    def test_unknown(self):
        (c, basis), ev = self.cat("mystery_platform", "zzz_1")
        self.assertIsNone(c)
        self.assertEqual([e["type"] for e in ev], ["category_unknown"])

    def test_unknown_adapter_category_is_ignored(self):
        (c, basis), _ = self.cat("search_service", "search_1", "NOT_A_CATEGORY")
        self.assertEqual((c, basis), ("SEARCH_INDEX_ENTRY", "system"))


class RequiredStateTests(unittest.TestCase):
    def req(self, category, attrs=None):
        events = []
        return policy.required_state(category, attrs or {}, AUDIT, RULES, events), events

    def test_each_rule_category(self):
        want = {"USER_PROFILE": "ABSENT", "SECONDARY_IDENTIFIER": "ABSENT",
                "SEARCH_INDEX_ENTRY": "ABSENT", "CACHE_ENTRY": "ABSENT",
                "SUPPORT_MESSAGE": "REDACTED", "SUPPORT_ATTACHMENT": "ABSENT",
                "ANALYTICS_PROFILE": "ANONYMIZED", "PRODUCT_TELEMETRY": "ANONYMIZED",
                "USER_EXPORT": "ABSENT", "MARKETING_PROFILE": "ABSENT",
                "EMAIL_DELIVERY_LOG": "REDACTED", "SECURITY_AUDIT_LOG": "PRESENT",
                "LEGAL_HOLD_RECORD": "PRESENT", "BACKUP_ARCHIVE": "BLOCKED",
                "ACCOUNT_TOMBSTONE": "REDACTED", "DATA_FLOW": "DISABLED"}
        for category, state in want.items():
            self.assertEqual(self.req(category)[0], state, category)

    def test_retention_active_and_expired(self):
        self.assertEqual(self.req("SECURITY_AUDIT_LOG", {"retention_until": AFTER}),
                         ("PRESENT", []))
        state, ev = self.req("SECURITY_AUDIT_LOG", {"retention_until": BEFORE})
        self.assertIsNone(state)
        self.assertEqual([e["type"] for e in ev], ["condition_unmet"])

    def test_legal_hold(self):
        self.assertEqual(self.req("LEGAL_HOLD_RECORD", {"hold_active": True})[0], "PRESENT")
        self.assertIsNone(self.req("LEGAL_HOLD_RECORD", {"hold_active": False})[0])
        self.assertEqual(self.req("LEGAL_HOLD_RECORD", {"hold_until": AFTER})[0], "PRESENT")
        self.assertIsNone(self.req("LEGAL_HOLD_RECORD", {"hold_until": BEFORE})[0])

    def test_support_conditions(self):
        self.assertIsNone(self.req("SUPPORT_MESSAGE", {"shared_context": False})[0])
        self.assertIsNone(self.req("SUPPORT_ATTACHMENT", {"subject_only": False})[0])

    def test_missing_condition_attr_is_assumed_and_logged(self):
        state, ev = self.req("SECURITY_AUDIT_LOG")
        self.assertEqual(state, "PRESENT")
        self.assertEqual([e["type"] for e in ev], ["condition_assumed"])

    def test_unconditional_rule_logs_nothing(self):
        self.assertEqual(self.req("USER_PROFILE"), ("ABSENT", []))


class ComplianceTests(unittest.TestCase):
    def test_delete_category_present_is_a_finding(self):
        p = run(res("search_service", "search_1", "PRESENT", "SEARCH_INDEX_ENTRY",
                    pick="evidence_7"), ACCOUNT_OK)
        self.assertEqual(p["status"], "INCOMPLETE")
        self.assertEqual(ops(p), [
            ("FINDING", "REPORT_REMAINING_RECORD", "search_service", "search_1", "evidence_7"),
            ("REMEDIATION", "DELETE_RECORD", "search_service", "search_1", ""),
            ("VERIFICATION", "VERIFY_RECORD_ABSENT", "account_service", "account_1", ""),
            ("VERIFICATION", "VERIFY_RECORD_ABSENT", "search_service", "search_1", ""),
        ])

    def test_delete_category_redacted_still_remains(self):
        p = run(res("export_service", "export_1", "REDACTED", "USER_EXPORT"))
        self.assertEqual(entry(p, "export_1")["finding"], "REPORT_REMAINING_RECORD")

    def test_identifiable_analytics_is_exposed_pii_anonymize(self):
        p = run(res("analytics_platform", "analytics_1", "IDENTIFIABLE", "ANALYTICS_PROFILE"))
        self.assertEqual([o[1] for o in ops(p)],
                         ["REPORT_EXPOSED_PII", "ANONYMIZE_RECORD", "VERIFY_RECORD_ANONYMIZED"])

    def test_identifiable_support_message_is_redacted(self):
        p = run(res("support_platform", "support_1", "IDENTIFIABLE", "SUPPORT_MESSAGE"))
        self.assertEqual([o[1] for o in ops(p)],
                         ["REPORT_EXPOSED_PII", "REDACT_RECORD", "VERIFY_RECORD_REDACTED"])

    def test_active_flow_and_restorable_backup(self):
        p = run(res("integration_service", "data_flow_1", "ACTIVE", "DATA_FLOW"),
                res("backup_service", "backup_1", "RESTORABLE", "BACKUP_ARCHIVE"),
                res("account_service", "account_1", "PRESENT", "USER_PROFILE"))
        self.assertEqual([(o[0], o[1]) for o in ops(p)], [
            ("FINDING", "REPORT_REMAINING_RECORD"),
            ("FINDING", "REPORT_ACTIVE_DATA_FLOW"),
            ("FINDING", "REPORT_RESTORE_RISK"),
            ("REMEDIATION", "DISABLE_DATA_FLOW"),
            ("REMEDIATION", "BLOCK_RESTORE"),
            ("REMEDIATION", "DELETE_RECORD"),
            ("VERIFICATION", "VERIFY_RECORD_ABSENT"),
            ("VERIFICATION", "VERIFY_DATA_FLOW_DISABLED"),
            ("VERIFICATION", "VERIFY_RESTORE_BLOCKED"),
        ])

    def test_compliant_states(self):
        for r in [res("analytics_platform", "a", "ANONYMIZED", "ANALYTICS_PROFILE"),
                  res("support_platform", "s", "REDACTED", "SUPPORT_MESSAGE"),
                  res("security_service", "r", "PRESENT", "SECURITY_AUDIT_LOG",
                      {"retention_until": AFTER}),
                  res("security_service", "r2", "IDENTIFIABLE", "SECURITY_AUDIT_LOG",
                      {"retention_until": AFTER}),
                  res("backup_service", "b", "BLOCKED", "BACKUP_ARCHIVE"),
                  res("integration_service", "f", "DISABLED", "DATA_FLOW"),
                  res("cache_service", "c", "ABSENT", "CACHE_ENTRY")]:
            self.assertEqual(entry(run(r), r["resource_id"])["verdict"], "ok", r["resource_id"])

    def test_retained_record_missing_is_counted_with_no_step(self):
        p = run(res("security_service", "audit_record_1", "ABSENT", "SECURITY_AUDIT_LOG",
                    {"retention_until": AFTER}),
                res("backup_service", "backup_1", "ABSENT", "BACKUP_ARCHIVE"), ACCOUNT_OK)
        self.assertEqual(p["retained_missing"], [
            {"system_id": "backup_service", "resource_id": "backup_1",
             "category": "BACKUP_ARCHIVE"},
            {"system_id": "security_service", "resource_id": "audit_record_1",
             "category": "SECURITY_AUDIT_LOG"}])
        self.assertEqual([o[3] for o in ops(p)], ["account_1"])
        self.assertEqual(p["status"], "COMPLETE")
        self.assertEqual(len(events(p, "retained_missing")), 2)

    def test_redact_target_absent_gives_no_step(self):
        p = run(res("support_platform", "support_1", "ABSENT", "SUPPORT_MESSAGE"), ACCOUNT_OK)
        self.assertEqual(entry(p, "support_1")["verdict"], "missing")
        self.assertEqual(events(p, "required_record_absent")[0]["resource_id"], "support_1")
        self.assertEqual([o[3] for o in ops(p)], ["account_1"])

    def test_unknown_state_gives_no_step(self):
        p = run(res("search_service", "search_1", None, "SEARCH_INDEX_ENTRY", pick=None),
                ACCOUNT_OK)
        self.assertEqual(entry(p, "search_1")["verdict"], "unknown")
        self.assertEqual(events(p, "state_unknown")[0]["resource_id"], "search_1")

    def test_backup_present_without_restore_state_is_unknown(self):
        p = run(res("backup_service", "backup_1", "PRESENT", "BACKUP_ARCHIVE"), ACCOUNT_OK)
        self.assertEqual(entry(p, "backup_1")["verdict"], "unknown")

    def test_retained_category_is_never_deleted(self):
        for cat in ("SECURITY_AUDIT_LOG", "LEGAL_HOLD_RECORD", "BACKUP_ARCHIVE",
                    "ACCOUNT_TOMBSTONE", "SUPPORT_MESSAGE"):
            for state in ("PRESENT", "IDENTIFIABLE", "RESTORABLE", "ABSENT"):
                p = run(res("x_service", "x_1", state, cat), ACCOUNT_OK)
                self.assertNotIn("DELETE_RECORD", [o[1] for o in ops(p) if o[3] == "x_1"],
                                 (cat, state))

    def test_safety_assert_on_retained_delete(self):
        bad = {"system_id": "security_service", "resource_id": "audit_record_1",
               "category": "SECURITY_AUDIT_LOG", "required": "ABSENT", "observed": "PRESENT",
               "verdict": "violation", "finding": "REPORT_REMAINING_RECORD",
               "evidence": "evidence_1", "confidence": "high"}
        with self.assertRaises(policy.PolicyError):
            policy.to_steps([bad], "INCOMPLETE")


class TombstoneAndPiiTests(unittest.TestCase):
    def test_tombstone_with_phone_left_is_exposed_never_deleted(self):
        t = res("identity_service", "tombstone_1", "PRESENT", "ACCOUNT_TOMBSTONE",
                {"subject_reference": "user_1", "name": None, "email": None,
                 "phone": "+111", "email_hash": None})
        p = run(t, ACCOUNT_OK)
        self.assertEqual([o[1] for o in ops(p) if o[3] == "tombstone_1"],
                         ["REPORT_EXPOSED_PII", "REDACT_RECORD", "VERIFY_RECORD_REDACTED"])

    def test_clean_tombstone_is_redacted(self):
        t = res("identity_service", "tombstone_1", "PRESENT", "ACCOUNT_TOMBSTONE",
                {"subject_reference": "user_1", "name": None, "email": None,
                 "phone": None, "email_hash": None}, pick="evidence_5")
        p = run(t, ACCOUNT_OK)
        self.assertEqual(p["status"], "COMPLETE")
        self.assertIn(("VERIFICATION", "VERIFY_RECORD_REDACTED", "identity_service",
                       "tombstone_1", "evidence_5"), ops(p))

    def test_present_without_identifier_fields_is_assumed_exposed(self):
        p = run(res("support_platform", "support_1", "PRESENT", "SUPPORT_MESSAGE"), ACCOUNT_OK)
        self.assertEqual(entry(p, "support_1")["finding"], "REPORT_EXPOSED_PII")
        self.assertEqual(len(events(p, "pii_assumed_exposed")), 1)

    def test_present_analytics_with_null_identifiers_is_anonymized(self):
        a = res("analytics_platform", "analytics_1", "PRESENT", "ANALYTICS_PROFILE",
                {"email": None, "email_hash": None, "phone": None, "name": None})
        self.assertEqual(entry(run(a), "analytics_1")["verdict"], "ok")


class GuardTests(unittest.TestCase):
    def test_low_confidence_violation_is_verification_only(self):
        acct = res("account_service", "account_9", "PRESENT", "USER_PROFILE", confidence="low")
        p = run(acct, res("search_service", "search_1", "PRESENT", "SEARCH_INDEX_ENTRY"))
        self.assertNotIn(("FINDING", "REPORT_REMAINING_RECORD", "account_service", "account_9",
                          "evidence_1"), ops(p))
        self.assertIn(("VERIFICATION", "VERIFY_RECORD_ABSENT", "account_service", "account_9", ""),
                      ops(p))
        self.assertNotIn("account_9", [o[3] for o in ops(p) if o[0] == "REMEDIATION"])
        self.assertEqual(len(events(p, "low_confidence_violation")), 1)

    def test_low_confidence_violation_never_changes_status(self):
        acct = res("account_service", "account_9", "PRESENT", "USER_PROFILE", confidence="low")
        p = run(acct, res("security_service", "audit_record_1", "PRESENT", "SECURITY_AUDIT_LOG",
                          {"retention_until": AFTER}))
        self.assertEqual(p["status"], "COMPLETE")
        self.assertEqual(ops(p), [("VERIFICATION", "VERIFY_RECORD_PRESENT", "security_service",
                                   "audit_record_1", "evidence_1")])

    def test_unciteable_violation_is_not_a_finding(self):
        p = run(res("search_service", "search_1", "PRESENT", "SEARCH_INDEX_ENTRY", pick=None),
                ACCOUNT_OK)
        self.assertEqual([o for o in ops(p) if o[0] == "FINDING"], [])
        self.assertEqual(len(events(p, "unciteable_finding")), 1)
        self.assertEqual(p["status"], "INCOMPLETE")
        self.assertEqual(p["steps"], [])
        self.assertEqual(events(p, "no_steps")[0]["reason"], "unverified_deletion")


class StatusTests(unittest.TestCase):
    def test_empty_dossier_is_incomplete_with_no_steps(self):
        p = run()
        self.assertEqual((p["status"], p["steps"]), ("INCOMPLETE", []))
        self.assertEqual(events(p, "no_steps"), [{"type": "no_steps", "reason": "no_resources"}])

    def test_only_unknown_resources_is_no_steps(self):
        p = run(res("account_service", "account_1", None, "USER_PROFILE", pick=None))
        self.assertEqual((p["status"], p["steps"]), ("INCOMPLETE", []))
        self.assertEqual(events(p, "no_steps")[0]["reason"], "unverified_deletion")

    def test_complete_cites_every_verification(self):
        p = run(res("account_service", "account_1", "ABSENT", "USER_PROFILE", pick="evidence_3"),
                res("analytics_platform", "analytics_1", "ANONYMIZED", "ANALYTICS_PROFILE",
                    pick="evidence_4"),
                res("integration_service", "data_flow_1", "DISABLED", "DATA_FLOW",
                    pick="evidence_4"))
        self.assertEqual(p["status"], "COMPLETE")
        self.assertEqual(ops(p), [
            ("VERIFICATION", "VERIFY_RECORD_ABSENT", "account_service", "account_1", "evidence_3"),
            ("VERIFICATION", "VERIFY_RECORD_ANONYMIZED", "analytics_platform", "analytics_1",
             "evidence_4"),
            ("VERIFICATION", "VERIFY_DATA_FLOW_DISABLED", "integration_service", "data_flow_1",
             "evidence_4"),
        ])

    def test_complete_drops_uncited_compliant_record(self):
        p = run(ACCOUNT_OK, res("security_service", "audit_record_1", "PRESENT",
                                "SECURITY_AUDIT_LOG", {"retention_until": AFTER}, pick=None))
        self.assertEqual(p["status"], "COMPLETE")
        self.assertEqual([o[3] for o in ops(p)], ["account_1"])

    def test_uncited_absent_account_blocks_complete(self):
        p = run(res("account_service", "account_1", "ABSENT", "USER_PROFILE", pick=None),
                res("security_service", "audit_record_1", "PRESENT", "SECURITY_AUDIT_LOG",
                    {"retention_until": AFTER}))
        self.assertEqual((p["status"], p["steps"]), ("INCOMPLETE", []))

    def test_incomplete_verifies_full_end_state_uncited(self):
        p = run(res("search_service", "search_1", "PRESENT", "SEARCH_INDEX_ENTRY"),
                ACCOUNT_OK,
                res("security_service", "audit_record_1", "PRESENT", "SECURITY_AUDIT_LOG",
                    {"retention_until": AFTER}),
                res("backup_service", "backup_1", "BLOCKED", "BACKUP_ARCHIVE"))
        ver = [o for o in ops(p) if o[0] == "VERIFICATION"]
        self.assertEqual(ver, [
            ("VERIFICATION", "VERIFY_RECORD_ABSENT", "account_service", "account_1", ""),
            ("VERIFICATION", "VERIFY_RECORD_ABSENT", "search_service", "search_1", ""),
            ("VERIFICATION", "VERIFY_RECORD_PRESENT", "security_service", "audit_record_1", ""),
            ("VERIFICATION", "VERIFY_RESTORE_BLOCKED", "backup_service", "backup_1", ""),
        ])

    def test_condition_unmet_resource_gets_no_step(self):
        p = run(ACCOUNT_OK, res("security_service", "audit_record_1", "PRESENT",
                                "SECURITY_AUDIT_LOG", {"retention_until": BEFORE}))
        self.assertEqual([o[3] for o in ops(p)], ["account_1"])
        self.assertEqual(entry(p, "audit_record_1")["verdict"], "unmet")

    def test_recreation_finding_cites_activity(self):
        r = res("account_service", "account_1", "PRESENT", "USER_PROFILE", pick="activity_42")
        r["recreated_by"] = {"activity_id": "activity_42"}
        p = run(r)
        self.assertEqual(ops(p)[0], ("FINDING", "REPORT_REMAINING_RECORD", "account_service",
                                     "account_1", "activity_42"))

    def test_same_resource_in_two_systems_is_two_entries(self):
        p = run(res("account_service", "account_1", "PRESENT", "USER_PROFILE"),
                res("search_service", "account_1", "ABSENT", "USER_PROFILE"))
        self.assertEqual(len(p["entries"]), 2)


class ContractTests(unittest.TestCase):
    def check(self, plan):
        rows = [{"case_id": "case_33", "deletion_status": plan["status"],
                 "steps_json": steps.serialize_steps(plan["steps"])}]
        r = invariants.check_table(invariants.EXPECTED_HEADER, rows)
        self.assertEqual(r.violations, [])

    def test_mixed_incomplete_passes_invariants(self):
        self.check(run(res("integration_service", "data_flow_1", "ACTIVE", "DATA_FLOW"),
                       res("backup_service", "backup_1", "RESTORABLE", "BACKUP_ARCHIVE"),
                       res("search_service", "search_2", "PRESENT", "SEARCH_INDEX_ENTRY"),
                       res("analytics_platform", "analytics_1", "IDENTIFIABLE",
                           "ANALYTICS_PROFILE"),
                       res("identity_service", "tombstone_1", "IDENTIFIABLE",
                           "ACCOUNT_TOMBSTONE"),
                       ACCOUNT_OK,
                       res("security_service", "audit_record_1", "PRESENT",
                           "SECURITY_AUDIT_LOG", {"retention_until": AFTER})))

    def test_complete_passes_invariants(self):
        self.check(run(ACCOUNT_OK, res("backup_service", "backup_1", "BLOCKED",
                                       "BACKUP_ARCHIVE")))

    def test_steps_are_numbered_and_serializable(self):
        p = run(res("search_service", "search_1", "PRESENT", "SEARCH_INDEX_ENTRY"), ACCOUNT_OK)
        self.assertEqual([s["step_no"] for s in p["steps"]], list(range(1, len(p["steps"]) + 1)))
        json.loads(steps.serialize_steps(p["steps"]))

    def test_evaluate_is_deterministic_and_order_independent(self):
        a = res("search_service", "search_1", "PRESENT", "SEARCH_INDEX_ENTRY")
        b = res("integration_service", "data_flow_1", "ACTIVE", "DATA_FLOW")
        self.assertEqual(run(a, b, ACCOUNT_OK)["steps"], run(ACCOUNT_OK, b, a)["steps"])


if __name__ == "__main__":
    unittest.main()
