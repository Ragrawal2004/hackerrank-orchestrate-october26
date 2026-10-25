"""Screenshot/PDF facts through the dossier and policy: the clock rules decide, T17 adds no rule."""
import unittest

from ghost import dossier, policy
from ghost.adapters import multimodal as mm
from tests.fact_builder import Facts, case
from tests.mm_fixture import PDF, PNG, FakeReader, payload

ANA = "ana@example.com"
PRIYA = "priya@example.com"
OMIT_AT = "2026-09-10T00:00:00Z"     # a complete replica capture that lacks the record
IMG_AT = "2026-09-15T11:00:00Z"      # the screenshot's capture time
LINK_AT = "2026-09-15T11:07:01Z"
AUDIT = "2026-09-16T12:00:00Z"
PDF_FILE = "evidence_58"


def rules():
    return policy.rules_from_rows([
        (f"rule_{i}", c, "Always", "RETAIN" if c in policy.RETAINED else
         "REDACT" if c in ("SUPPORT_MESSAGE", "EMAIL_DELIVERY_LOG", "ACCOUNT_TOMBSTONE") else
         "ANONYMIZE" if c in ("ANALYTICS_PROFILE", "PRODUCT_TELEMETRY") else "DELETE")
        for i, c in enumerate(policy.RULE_CATEGORIES)])


def world(img_manifest=IMG_AT):
    f = Facts().user("user_1", ANA, name="Ana Lima")
    f.file("evidence_05", OMIT_AT)                       # complete capture, omits account_5762
    f.scope("evidence_05", "account_service", OMIT_AT, complete=1)
    f.file("evidence_04", img_manifest)
    f.link_identity(LINK_AT, "account_5762", ANA, "user_1", target="account_service")
    return f


def evaluate(f, audit=AUDIT, email=ANA):
    d = dossier.build_dossier(f.conn, case(email, audit))
    return policy.evaluate(d, rules())


def ops(plan):
    return [(s["phase"], s["operation"], s["resource_id"], s["evidence_id"]) for s in plan["steps"]]


class ClockRuleTests(unittest.TestCase):
    def test_before_the_screenshot_the_case_is_complete_by_omission(self):
        plan = evaluate(world())
        self.assertEqual(plan["status"], "COMPLETE")
        self.assertEqual(ops(plan), [("VERIFICATION", "VERIFY_RECORD_ABSENT", "account_5762",
                                      "evidence_05")])

    def test_a_later_explicit_present_beats_an_earlier_complete_omission(self):
        f = world()
        mm.ingest(f.conn, "evidence_04", PNG,
                  FakeReader(payload("account", "account_5762", emails=[ANA])))
        plan = evaluate(f)
        self.assertEqual(plan["status"], "INCOMPLETE")
        self.assertEqual(ops(plan), [
            ("FINDING", "REPORT_REMAINING_RECORD", "account_5762", "evidence_04"),
            ("REMEDIATION", "DELETE_RECORD", "account_5762", ""),
            ("VERIFICATION", "VERIFY_RECORD_ABSENT", "account_5762", "")])

    def test_a_screenshot_captured_after_audit_at_changes_nothing(self):
        f = world(img_manifest="2026-09-17T09:00:00Z")
        mm.ingest(f.conn, "evidence_04", PNG,
                  FakeReader(payload("account", "account_5762", emails=[ANA])))
        plan = evaluate(f)
        self.assertEqual(plan["status"], "COMPLETE")
        self.assertEqual(ops(plan)[0][3], "evidence_05")

    def test_an_omission_later_than_the_screenshot_wins_again(self):
        f = world()
        f.file("evidence_06", "2026-09-15T20:00:00Z")
        f.scope("evidence_06", "account_service", "2026-09-15T20:00:00Z", complete=1)
        mm.ingest(f.conn, "evidence_04", PNG,
                  FakeReader(payload("account", "account_5762", emails=[ANA])))
        self.assertEqual(evaluate(f)["status"], "COMPLETE")

    def test_two_disagreeing_reads_leave_the_case_exactly_as_it_was(self):
        f = world()
        before = ops(evaluate(f))
        mm.ingest(f.conn, "evidence_04", PNG,
                  FakeReader(payload("account", "account_5762", emails=[ANA]),
                             payload("account", "account_5762", emails=[])))
        self.assertEqual(ops(evaluate(f)), before)


class ScreenToStepsTests(unittest.TestCase):
    def test_analytics_screen_with_identifiers_is_exposed_pii(self):
        f = Facts().user("user_1", ANA)
        f.file("evidence_19", IMG_AT)
        f.link_identity(LINK_AT, "analytics_7438", ANA, "user_1", target="analytics_platform")
        mm.ingest(f.conn, "evidence_19", PNG,
                  FakeReader(payload("analytics", "analytics_7438", emails=[ANA],
                                     user_refs=["user_1"])))
        plan = evaluate(f)
        self.assertEqual(plan["status"], "INCOMPLETE")
        self.assertEqual([o[1] for o in ops(plan)],
                         ["REPORT_EXPOSED_PII", "ANONYMIZE_RECORD", "VERIFY_RECORD_ANONYMIZED"])
        self.assertEqual(ops(plan)[0][3], "evidence_19")

    def test_sample_07_shape_a_pdf_attachment_alone_gives_the_finding(self):
        f = Facts().user("user_7", PRIYA, name="Priya Raman", phone="+91 98765 73142")
        f.file(PDF_FILE, "2026-09-12T07:30:00Z")
        f.activity("2026-08-31T12:00:00Z", "DELETE_RECORD", "support_platform", "support_platform",
                   "support_001", "SUCCESS",
                   "Deletion workflow reported success. Target resource: support_001.")
        mm.ingest(f.conn, PDF_FILE, PDF,
                  FakeReader(payload("document_attachment", "support_001", emails=[PRIYA],
                                     names=["Priya Raman"], phones=["+91 98765 73142"])))
        plan = evaluate(f, audit="2026-09-12T08:00:00Z", email=PRIYA)
        self.assertEqual(plan["status"], "INCOMPLETE")
        self.assertEqual(ops(plan), [
            ("FINDING", "REPORT_REMAINING_RECORD", "support_001", PDF_FILE),
            ("REMEDIATION", "DELETE_RECORD", "support_001", ""),
            ("VERIFICATION", "VERIFY_RECORD_ABSENT", "support_001", "")])
        step = [s for s in plan["steps"] if s["operation"] == "REPORT_REMAINING_RECORD"][0]
        self.assertEqual(step["system_id"], "support_platform")

    def test_a_screen_with_no_state_field_leaves_the_case_without_steps(self):
        f = Facts().user("user_1", ANA)
        f.file("evidence_32", IMG_AT)
        f.link_identity(LINK_AT, "backup_9287", ANA, "user_1", target="backup_service")
        mm.ingest(f.conn, "evidence_32", PNG,
                  FakeReader(payload("backup", "backup_9287", emails=[ANA])))  # no restore flag
        plan = evaluate(f)
        self.assertEqual(plan["steps"], [])
        self.assertTrue(any(e["type"] == "no_steps" for e in plan["events"]))

    def test_corroborating_screen_for_a_resource_with_its_own_anchor_adds_no_step(self):
        # sample 15 shape: the connector is already ACTIVE from a log; the screen has no flag
        f = Facts().user("user_1", ANA)
        f.file("evidence_01", "2026-09-15T11:59:00Z")
        f.obs("evidence_01", "2026-09-15T11:59:00Z", "integration_service", "data_flow_9846",
              "ACTIVE", policy.DATA_FLOW, keys=[("email", ANA)])
        f.file("evidence_14", "2026-08-07T08:00:00Z")
        f.link_identity("2026-08-07T08:07:01Z", "data_flow_9846", ANA, "user_1",
                        target="integration_service")
        before = ops(evaluate(f, audit="2026-09-15T12:00:00Z"))
        mm.ingest(f.conn, "evidence_14", PNG,
                  FakeReader(payload("connector", "data_flow_9846", emails=[ANA])))
        self.assertEqual(ops(evaluate(f, audit="2026-09-15T12:00:00Z")), before)


if __name__ == "__main__":
    unittest.main()
