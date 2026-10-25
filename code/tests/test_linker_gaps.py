"""T16 gap tests, added after the first mutation run left these behaviours unchecked."""
import unittest

from ghost import dossier, linker
from tests.test_linker import T, ca, pool, primary, world
from tests.test_policy import AUDIT_OK, events, ops, res, run


class EliminationGapTests(unittest.TestCase):
    def test_targets_limit_which_cases_are_reported(self):
        f = world()
        primary(f, "account_10", minutes=50)
        p = pool(f, ca(), ca(cid="case_b", email="two@example.com"))
        both = linker.link_accounts(f.conn, p, "guess")
        only_b = linker.link_accounts(f.conn, p, "guess", targets={"case_b"})
        self.assertEqual(sorted(both), ["case_a", "case_b"])
        self.assertEqual(sorted(only_b), ["case_b"])
        self.assertEqual(sorted(linker.link_analytics(f.conn, p, targets={"case_a"})), ["case_a"])

    def test_all_candidates_eliminated_is_its_own_reason(self):
        f = world()
        primary(f, "account_11", minutes=52)
        f.obs("evidence_1", T, "account_service", "account_11", "ABSENT",
              keys=[("email", "two@example.com")])
        out = linker.link_accounts(
            f.conn, pool(f, ca(), ca(cid="case_b", email="two@example.com")), "guess")
        ev = out["case_a"]["events"][0]
        self.assertEqual((ev["reason"], ev["candidates"], ev["window_candidates"]),
                         ("all_eliminated", [], ["account_11"]))

    def test_account_with_several_rows_keeps_the_row_closest_to_the_median_offset(self):
        f = world()
        far = primary(f, "account_10", minutes=10)
        near = primary(f, "account_10", minutes=55)
        c = linker.account_candidates(f.conn, ca())
        self.assertEqual([(x["resource_id"], x["activity_id"]) for x in c], [("account_10", near)])
        self.assertNotEqual(far, near)


class GuessedCitationGapTests(unittest.TestCase):
    def guessed(self, f):
        c = ca()
        pick = linker.link_accounts(f.conn, pool(f, c), "guess")["case_a"]["pick"]
        return dossier.add_guessed_account(f.conn, dossier.build_dossier(f.conn, c), pick)

    def acct(self, d):
        return [r for r in d["resources"] if r["resource_id"] == "account_10"][0]

    def test_a_capture_that_names_the_account_absent_is_not_an_omission(self):
        f = world()
        primary(f, "account_10")
        f.file("evidence_9", "2026-09-14T09:00:00Z")
        f.scope("evidence_9", "account_service", "2026-09-14T09:00:00Z", complete=1)
        f.obs("evidence_9", "2026-09-14T09:00:00Z", "account_service", "account_10", "ABSENT")
        r = self.acct(self.guessed(f))
        self.assertEqual(r["state"], "ABSENT")
        self.assertIsNone(r["citation"]["pick"])

    def test_no_citation_when_the_account_was_last_seen_present(self):
        f = world()
        primary(f, "account_10")
        f.file("evidence_9", "2026-09-13T09:00:00Z")
        f.obs("evidence_9", "2026-09-13T09:00:00Z", "account_service", "account_10", "PRESENT")
        f.file("evidence_10", "2026-09-14T09:00:00Z")
        f.scope("evidence_10", "account_service", "2026-09-14T09:00:00Z", complete=1)
        r = self.acct(self.guessed(f))
        self.assertEqual(r["state"], "PRESENT")
        self.assertEqual((r["citation"]["rule"], r["citation"]["pick"], r["citation"]["rule_o"]),
                         (None, None, []))

    def test_ambiguous_rule_o_is_logged(self):
        f = world()
        primary(f, "account_10")
        for i, day in ((3, "12"), (4, "13")):
            f.file(f"evidence_{i}", f"2026-09-{day}T00:00:00Z")
            f.scope(f"evidence_{i}", "account_service", f"2026-09-{day}T00:00:00Z", complete=1)
        d = self.guessed(f)
        ev = [e for e in d["events"] if e["type"] == "rule_o_ambiguous"]
        self.assertEqual([e["candidates"] for e in ev], [["evidence_4", "evidence_3"]])

    def test_a_pick_from_after_audit_at_is_refused(self):
        f = world()
        late = f.activity("2026-09-16T00:00:00Z", "DELETE_RECORD", "deletion_worker",
                          "account_service", "account_10", "SUCCESS",
                          "Deletion worker submitted primary profile account_10 to account_service.")
        d = dossier.build_dossier(f.conn, ca())
        pick = {"resource_id": "account_10", "method": "M6", "confidence": "low",
                "activity_id": late}
        with self.assertRaises(dossier.LateRecordRead):
            dossier.add_guessed_account(f.conn, d, pick)


class PolicyGapTests(unittest.TestCase):
    def test_only_an_l1_guess_may_be_uncited_in_a_complete_case(self):
        """A low-confidence link that is not the L1 guess (basis explicit) needs its evidence."""
        other = res("account_service", "account_8", "ABSENT", "USER_PROFILE", pick=None,
                    confidence="low")
        p = run(AUDIT_OK, other)
        self.assertEqual(p["status"], "COMPLETE")
        self.assertNotIn("account_8", [o[3] for o in ops(p)])
        self.assertEqual(events(p, "guessed_account_uncited"), [])


if __name__ == "__main__":
    unittest.main()
