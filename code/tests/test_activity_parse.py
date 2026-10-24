import unittest

from ghost import loaders
from ghost.activity_parse import parse_details
from tests.helpers import DATASET


class ParseDetailsTests(unittest.TestCase):
    def test_primary_profile(self):
        p = parse_details("DELETE_RECORD", "account_1482",
                          "Deletion worker submitted primary profile account_1482 to account_service.")
        self.assertEqual(p["kind"], "primary_profile")
        self.assertEqual(p["target_resource"], "account_1482")

    def test_target_resource(self):
        p = parse_details("DELETE_RECORD", "account_001",
                          "Deletion worker reported the requested operation as completed. Target resource: account_001.")
        self.assertEqual(p["kind"], "target_resource")
        self.assertEqual(p["target_resource"], "account_001")

    def test_repopulated_sync(self):
        p = parse_details("SYNC_RECORD", "data_flow_8165",
                          "Active connector repopulated downstream resource search_2818.")
        self.assertEqual(p["kind"], "repopulate")
        self.assertEqual(p["downstream_resource"], "search_2818")

    def test_repopulated_restore(self):
        p = parse_details("RESTORE_BACKUP", "backup_9287",
                          "Restore job repopulated downstream resource search_4882.")
        self.assertEqual(p["kind"], "repopulate")
        self.assertEqual(p["downstream_resource"], "search_4882")

    def test_link_identity(self):
        p = parse_details("LINK_RESOURCE_IDENTITY", "account_5762",
                          "Data catalog linked account_5762 (account_5762) to haruka.s1838@zoho.com and user_11.")
        self.assertEqual(p["kind"], "link_identity")
        self.assertEqual(p["target_resource"], "account_5762")
        self.assertEqual(p["linked_email"], "haruka.s1838@zoho.com")
        self.assertEqual(p["linked_user_id"], "user_11")

    def test_link_identity_attachment_phrasing(self):
        p = parse_details("LINK_RESOURCE_IDENTITY", "object_19",
                          "Attachment catalog confirmed object_19 (delivery_confirmation_19.pdf) belongs only to priya.raman@gmail.com and user_07; no other subject is linked.")
        self.assertEqual(p["kind"], "link_identity")
        self.assertEqual(p["target_resource"], "object_19")
        self.assertEqual(p["linked_email"], "priya.raman@gmail.com")
        self.assertEqual(p["linked_user_id"], "user_07")

    def test_link_identity_lowercases_email(self):
        p = parse_details("LINK_RESOURCE_IDENTITY", "search_1",
                          "Data catalog linked search_1 (search_1) to Foo.Bar@Zoho.com and user_2.")
        self.assertEqual(p["linked_email"], "foo.bar@zoho.com")

    def test_classify_non_deletion(self):
        p = parse_details("CLASSIFY_REQUEST", "account_2461",
                          "Privacy intake classified request for account_2461 as non-deletion; no destructive action was authorized.")
        self.assertEqual(p["kind"], "classify_non_deletion")
        self.assertEqual(p["target_resource"], "account_2461")

    def test_unrecognized_is_other(self):
        p = parse_details("SEND_EMAIL", "email_delivery_1", "Something unrelated happened.")
        self.assertEqual(p["kind"], "other")
        self.assertIsNone(p["target_resource"])
        self.assertIsNone(p["downstream_resource"])
        self.assertIsNone(p["linked_email"])
        self.assertIsNone(p["linked_user_id"])


class ParseRealDatasetTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        rows = loaders.load_activity(DATASET)
        cls.parsed = [(r, parse_details(r["event_type"], r["resource_id"], r["details"])) for r in rows]

    def kinds(self, kind):
        return [(r, p) for r, p in self.parsed if p["kind"] == kind]

    def test_classify_count(self):
        self.assertEqual(len(self.kinds("classify_non_deletion")), 10)

    def test_link_identity_count(self):
        k = self.kinds("link_identity")
        self.assertEqual(len(k), 12)
        for r, p in k:
            self.assertEqual(r["event_type"], "LINK_RESOURCE_IDENTITY")
            self.assertTrue(p["linked_email"] and p["linked_user_id"])

    def test_observed_phrase_counts(self):
        self.assertEqual(len(self.kinds("primary_profile")), 532)
        self.assertEqual(len(self.kinds("target_resource")), 12)
        self.assertEqual(len(self.kinds("repopulate")), 32)

    def test_every_link_event_type_parsed(self):
        for r, p in self.parsed:
            if r["event_type"] == "LINK_RESOURCE_IDENTITY":
                self.assertEqual(p["kind"], "link_identity", r["activity_id"])
            if r["event_type"] == "CLASSIFY_REQUEST":
                self.assertEqual(p["kind"], "classify_non_deletion", r["activity_id"])


if __name__ == "__main__":
    unittest.main()
