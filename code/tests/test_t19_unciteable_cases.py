"""T19 item 1: cases whose violation is proven only by a file captured after audit_at.

case_126 shape: the account's PRESENT record is stamped before audit_at, but the only file that
names it has a manifest after audit_at (two clocks, SPEC U3). The state is usable, the citation is
not, so the finding is unciteable and the case is INCOMPLETE with no steps.
case_179 shape: a citable file names the resource, but with a different state word, so Rule E
does not pick it (documented in docs/ASSUMPTIONS.md, T19).
"""
import unittest

from ghost import dossier, loaders, policy
from tests.fact_builder import Facts, case
from tests.helpers import DATASET

AUDIT = "2026-08-17T10:00:00Z"
RECORD_AT = "2026-08-11T10:00:00Z"
MANIFEST_LATE = "2026-08-19T10:00:00Z"
MANIFEST_OK = "2026-08-17T09:42:50Z"
EMAIL = "subject@example.com"

RULES = policy.rules_from_rows((r["rule_id"], r["data_category"], r["condition"],
                                r["required_action"]) for r in loaders.load_rules(DATASET))


def facts():
    return Facts().user("user_1", EMAIL, name="Subject Person")


def plan_for(f):
    d = dossier.build_dossier(f.conn, case(EMAIL, AUDIT))
    return d, policy.evaluate(d, RULES)


def events(p, kind):
    return [e for e in p["events"] if e["type"] == kind]


class LateOnlyViolationTests(unittest.TestCase):
    def late_only(self):
        f = facts().file("e_late", MANIFEST_LATE).file("e_ok", MANIFEST_OK)
        f.obs("e_late", RECORD_AT, "account_service", "account_1", "PRESENT",
              category="USER_PROFILE", keys=[("email", EMAIL)])
        # a retained record that a citable file does prove
        f.obs("e_ok", "2026-08-17T07:18:17Z", "security_service", "audit_record_1", "PRESENT",
              category="SECURITY_AUDIT_LOG", keys=[("subject_reference", "user_1")],
              attrs={"retention_until": "2032-06-30T23:59:59Z"})
        return f

    def test_state_is_used_but_not_cited(self):
        d, _ = plan_for(self.late_only())
        r = [x for x in d["resources"] if x["resource_id"] == "account_1"][0]
        self.assertEqual(r["state"], "PRESENT")
        self.assertIsNone(r["citation"]["pick"])
        self.assertEqual(d["clock"]["late_manifest_files_used"], ["e_late"])

    def test_case_is_incomplete_with_no_steps_and_no_late_citation(self):
        _, p = plan_for(self.late_only())
        self.assertEqual(p["status"], "INCOMPLETE")
        self.assertEqual(p["steps"], [])
        self.assertEqual(len(events(p, "unciteable_finding")), 1)
        self.assertEqual(events(p, "no_steps")[0]["reason"], "unverified_deletion")
        self.assertNotIn("e_late", repr(p["steps"]))

    def test_failed_delete_activity_is_not_proof_of_presence(self):
        f = self.late_only()
        f.activity("2026-08-05T09:15:59Z", "DELETE_RECORD", "deletion_worker", "account_service",
                   "account_1", "FAILED",
                   "Deletion worker submitted primary profile account_1 to account_service.")
        _, p = plan_for(f)
        self.assertEqual(p["steps"], [])
        self.assertEqual(p["status"], "INCOMPLETE")


class StateMismatchCitationTests(unittest.TestCase):
    def test_citable_file_with_another_state_word_is_not_cited(self):
        # case_179 shape: e_ok names the record as IDENTIFIABLE, the late file says PRESENT.
        f = facts().file("e_late", "2026-07-25T15:00:00Z").file("e_ok", "2026-07-23T11:43:30Z")
        f.obs("e_ok", "2026-07-23T09:25:20Z", "support_platform", "support_1", "IDENTIFIABLE",
              category="SUPPORT_MESSAGE", keys=[("email", EMAIL)])
        f.obs("e_late", "2026-07-23T12:00:00Z", "support_platform", "support_1", "PRESENT",
              category="SUPPORT_ATTACHMENT", keys=[("email", EMAIL)])
        d = dossier.build_dossier(f.conn, case(EMAIL, "2026-07-23T12:00:00Z"))
        r = [x for x in d["resources"] if x["resource_id"] == "support_1"][0]
        self.assertEqual(r["state"], "PRESENT")
        self.assertEqual([e["file_id"] for e in r["citation"]["rule_e"]], ["e_ok"])
        self.assertIsNone(r["citation"]["pick"])  # current behaviour; see ASSUMPTIONS T19 item 1


if __name__ == "__main__":
    unittest.main()
