import hashlib
import unittest

from ghost import loaders, store
from tests.helpers import DATASET, tree_fingerprint

TABLES = {
    "identity", "evidence_file", "observation", "subject_key", "join_key",
    "capture_scope", "activity", "rule", "extraction",
}


def count(conn, sql, *args):
    return conn.execute(sql, args).fetchone()[0]


class LoaderCountTests(unittest.TestCase):
    def test_requests(self):
        rows = loaders.load_requests(DATASET)
        self.assertEqual(len(rows), 518)
        ids = [r["case_id"] for r in rows]
        self.assertEqual(len(set(ids)), 518)
        self.assertEqual(min(loaders.case_number(i) for i in ids), 33)
        self.assertEqual(max(loaders.case_number(i) for i in ids), 550)
        self.assertEqual(
            set(rows[0]),
            {"case_id", "email", "requested_at", "reported_status", "reported_at",
             "audit_at", "request_text"},
        )

    def test_sample_requests(self):
        rows = loaders.load_requests(DATASET, sample=True)
        self.assertEqual(len(rows), 32)
        self.assertEqual(rows[0]["case_id"], "case_01")

    def test_reported_status_counts(self):
        from collections import Counter
        c = Counter(r["reported_status"] for r in loaders.load_requests(DATASET))
        self.assertEqual(c, {"COMPLETE": 254, "PARTIALLY_COMPLETE": 152, "NOT_STARTED": 112})

    def test_case_sort_key_numeric(self):
        self.assertLess(loaders.case_sort_key("case_99"), loaders.case_sort_key("case_100"))
        self.assertEqual(
            sorted(["case_100", "case_33", "case_99"], key=loaders.case_sort_key),
            ["case_33", "case_99", "case_100"],
        )

    def test_other_files(self):
        self.assertEqual(len(loaders.load_identities(DATASET)), 850)
        self.assertEqual(len(loaders.load_audit_log(DATASET)), 306)
        self.assertEqual(len(loaders.load_activity(DATASET)), 4011)
        self.assertEqual(len(loaders.load_rules(DATASET)), 15)
        self.assertEqual(len(loaders.load_evidence_manifest(DATASET)), 94)

    def test_audit_fields(self):
        from collections import Counter
        c = Counter(r["field_name"] for r in loaders.load_audit_log(DATASET))
        self.assertEqual(c, {"email": 107, "email_hash": 107, "phone": 92})

    def test_email_hash_is_sha256_for_all(self):
        rows = loaders.load_identities(DATASET)
        for r in rows:
            self.assertEqual(r["email"], r["email"].strip().lower())
            self.assertEqual(hashlib.sha256(r["email"].encode()).hexdigest(), r["email_hash"])


class StoreTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.before = tree_fingerprint(DATASET)
        cls.conn = loaders.load_store(DATASET)

    def test_schema_has_all_tables(self):
        names = {r[0] for r in self.conn.execute(
            "select name from sqlite_master where type='table'")}
        self.assertTrue(TABLES <= names, TABLES - names)

    def test_activity_stores_reported_result_claim(self):
        cols = [r[1] for r in self.conn.execute("pragma table_info(activity)")]
        self.assertIn("reported_result", cols)
        self.assertNotIn("result", cols)
        self.assertEqual(
            self.conn.execute(
                "select reported_result from activity where activity_id='activity_01'"
            ).fetchone()[0],
            "SUCCESS",
        )

    def test_populated_tables(self):
        self.assertEqual(count(self.conn, "select count(*) from activity"), 4011)
        self.assertEqual(count(self.conn, "select count(*) from rule"), 15)
        self.assertEqual(count(self.conn, "select count(*) from evidence_file"), 94)
        self.assertEqual(count(self.conn, "select count(*) from identity where kind='user_id'"), 850)

    def test_tables_no_adapter_writes_yet_are_empty(self):
        self.assertEqual(count(self.conn, "select count(*) from extraction"), 0)

    def test_only_the_log_adapters_write_join_keys(self):
        self.assertGreater(count(self.conn, "select count(*) from join_key"), 0)
        self.assertEqual(count(self.conn,
                               "select count(*) from join_key where file_id not in (select file_id "
                               "from evidence_file where adapter in ('observability_log', 'resource_trace_log'))"), 0)

    def test_replica_files_are_claimed_and_clean(self):
        self.assertEqual(count(self.conn, "select count(*) from evidence_file where adapter='replica_sql'"), 12)
        self.assertEqual(count(self.conn, "select coalesce(sum(unparsed),0) from parse_report"), 0)

    def test_evidence_manifest_row(self):
        row = self.conn.execute(
            "select path, origin, format, manifest_at from evidence_file where file_id='evidence_01'"
        ).fetchone()
        self.assertEqual(row[0], "evidence/logs/service_events_ap_south_20260915.log")
        self.assertEqual(row[1], "central_observability_export")
        self.assertEqual(row[2], "log_obs_logfmt")
        self.assertEqual(row[3], "2026-09-15T11:59:39Z")

    def test_emails_lowercase_in_identity(self):
        bad = count(self.conn,
                    "select count(*) from identity where kind='email' and value != lower(trim(value))")
        self.assertEqual(bad, 0)

    def test_deterministic(self):
        other = loaders.load_store(DATASET)
        q = "select * from identity order by user_id, kind, value, source"
        self.assertEqual(self.conn.execute(q).fetchall(), other.execute(q).fetchall())

    def test_dump(self):
        import tempfile, sqlite3
        from pathlib import Path
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "facts.sqlite"
            store.dump(self.conn, p)
            c2 = sqlite3.connect(p)
            self.assertEqual(count(c2, "select count(*) from activity"), 4011)
            c2.close()

    def test_dataset_untouched(self):
        self.assertEqual(tree_fingerprint(DATASET), self.before)


if __name__ == "__main__":
    unittest.main()
