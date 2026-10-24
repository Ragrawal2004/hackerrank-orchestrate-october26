import hashlib
import unittest

from ghost import identity, loaders
from tests.helpers import DATASET


def sha(s):
    return hashlib.sha256(s.encode()).hexdigest()


class IdentityTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.conn = loaders.load_store(DATASET)
        cls.requests = loaders.load_requests(DATASET)
        cls.samples = loaders.load_requests(DATASET, sample=True)

    def test_all_request_emails_resolve_uniquely(self):
        for r in self.requests + self.samples:
            uid = identity.resolve_email(self.conn, r["email"])
            self.assertRegex(uid, r"^user_\d+$", r["case_id"])

    def test_resolution_split(self):
        via = {"current": 0, "historical": 0}
        for r in self.requests:
            via[identity.resolve_email_detailed(self.conn, r["email"])[1]] += 1
        # SPEC §3 quotes 417 current + 100 historical + 1 current email that also
        # appears as an audit new_value; that one is a current match, so 418.
        self.assertEqual(via, {"current": 418, "historical": 100})

    def test_case_insensitive_and_trimmed(self):
        base = self.requests[0]["email"]
        self.assertEqual(
            identity.resolve_email(self.conn, base),
            identity.resolve_email(self.conn, "  " + base.upper() + " "),
        )

    def test_decoys_do_not_resolve(self):
        for e in ("g.silva76.jr@gmail.com", "nadia.popescu@yahoo.com", "customer4104@yahoo.com"):
            with self.assertRaises(identity.UnknownIdentity, msg=e):
                identity.resolve_email(self.conn, e)

    def test_near_miss_is_exact_only(self):
        self.assertEqual(identity.resolve_email(self.conn, "g.silva76@gmail.com"), "user_13")

    def test_unknown_email_raises(self):
        with self.assertRaises(identity.UnknownIdentity):
            identity.resolve_email(self.conn, "nobody@example.invalid")

    def test_ambiguous_raises(self):
        self.conn.execute(
            "insert into identity(user_id,kind,value,valid_from,valid_to,source) "
            "values('user_a','email','dup@example.invalid',null,null,'test')")
        self.conn.execute(
            "insert into identity(user_id,kind,value,valid_from,valid_to,source) "
            "values('user_b','email','dup@example.invalid',null,null,'test')")
        try:
            with self.assertRaises(identity.AmbiguousIdentity):
                identity.resolve_email(self.conn, "dup@example.invalid")
        finally:
            self.conn.execute("delete from identity where value='dup@example.invalid'")

    def test_user10_chain(self):
        ids = identity.identifiers(self.conn, "user_10")
        for e in ("nadia.ionescu@yahoo.com", "nadia.petrescu@yahoo.com", "n.petrescu@yahoo.com"):
            self.assertIn(e, ids["email"])
            self.assertIn(sha(e), ids["email_hash"])
        self.assertIn("user_10", ids["user_id"])
        # audit hashes for the first two links are present too
        self.assertIn("708ef5640eec7c7aa89a6dc39793c5df6f9bca6968397f46a8f44ff1bdde75f5", ids["email_hash"])
        self.assertIn("6385b5b5e70720c197abedd379f714bb84330a9c7bc86bbb4bfbc4405bdba326", ids["email_hash"])
        self.assertEqual(len(ids["email"]), 3)
        self.assertEqual(len(ids["name"]), 1)

    def test_user35_chain(self):
        ids = identity.identifiers(self.conn, "user_35")
        for e in ("abena.o189491@zoho.com", "abena.o189466@zoho.com", "abena.o1894@zoho.com"):
            self.assertIn(e, ids["email"])
            self.assertIn(sha(e), ids["email_hash"])
        self.assertEqual(len(ids["email"]), 3)
        self.assertEqual(identity.resolve_email(self.conn, "abena.o189491@zoho.com"), "user_35")

    def test_historical_email_resolves_to_same_user_as_current(self):
        self.assertEqual(identity.resolve_email(self.conn, "nadia.ionescu@yahoo.com"), "user_10")
        self.assertEqual(identity.resolve_email(self.conn, "n.petrescu@yahoo.com"), "user_10")

    def test_phones_include_history(self):
        ids = identity.identifiers(self.conn, "user_485")
        self.assertIn("+33 6 24 34 80 16", ids["phone"])
        self.assertIn("+33 6 63 29 68 28", ids["phone"])

    def test_multi_case_user(self):
        by_case = {r["case_id"]: r for r in self.requests}
        u33 = identity.resolve_email(self.conn, by_case["case_33"]["email"])
        u52 = identity.resolve_email(self.conn, by_case["case_52"]["email"])
        self.assertEqual(u33, u52)
        self.assertEqual(u33, "user_18")

    def test_as_of_guard_drops_later_rows(self):
        self.conn.execute(
            "insert into identity(user_id,kind,value,valid_from,valid_to,source) "
            "values('user_18','email','late@example.invalid','2030-01-01T00:00:00Z',null,'test')")
        try:
            now = identity.identifiers(self.conn, "user_18")
            cut = identity.identifiers(self.conn, "user_18", as_of="2026-10-01T12:00:00Z")
            self.assertIn("late@example.invalid", now["email"])
            self.assertNotIn("late@example.invalid", cut["email"])
            self.assertNotIn(sha("late@example.invalid"), cut["email_hash"])
        finally:
            self.conn.execute("delete from identity where value='late@example.invalid'")

    def test_as_of_guard_noop_on_real_data(self):
        for r in self.requests[:50]:
            uid = identity.resolve_email(self.conn, r["email"])
            self.assertEqual(
                identity.identifiers(self.conn, uid),
                identity.identifiers(self.conn, uid, as_of=r["audit_at"]),
            )


if __name__ == "__main__":
    unittest.main()
