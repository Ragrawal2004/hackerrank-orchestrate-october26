"""T16: account linker (SPEC L1, M6 with clock-safe elimination) and analytics MA (diagnostics only)."""
import unittest

from ghost import dossier, linker
from tests.fact_builder import Facts, case

REPORTED = "2026-09-10T12:00:00Z"
AUDIT = "2026-09-15T12:00:00Z"
LATER_AUDIT = "2026-09-20T12:00:00Z"
T = "2026-09-14T08:00:00Z"


def minutes_before(minutes, at=REPORTED):
    from datetime import datetime, timedelta, timezone
    t = datetime.fromisoformat(at.replace("Z", "+00:00")) - timedelta(minutes=minutes)
    return t.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def primary(f, rid, minutes=50, result="SUCCESS", template="primary"):
    details = (f"Deletion worker submitted primary profile {rid} to account_service."
               if template == "primary" else
               f"Deletion worker reported the requested operation as completed. Target resource: {rid}.")
    return f.activity(minutes_before(minutes), "DELETE_RECORD", "deletion_worker",
                      "account_service", rid, result, details)


def anonymize(f, rid, minutes=50):
    return f.activity(minutes_before(minutes), "ANONYMIZE_RECORD", "deletion_worker",
                      "analytics_platform", rid, "SUCCESS", f"Policy action completed for {rid}.")


def world():
    """Two subjects: u1 (case_a, no account evidence) and u2 (case_b)."""
    f = Facts()
    f.user("user_1", "one@example.com", name="Ana One")
    f.user("user_2", "two@example.com", name="Bo Two")
    f.file("evidence_1", T)
    # a subject record, so each case has some evidence
    f.obs("evidence_1", T, "search_service", "search_1", "PRESENT", keys=[("email", "one@example.com")])
    return f


def ca(audit_at=AUDIT, cid="case_a", email="one@example.com"):
    return case(email, audit_at, case_id=cid, reported_at=REPORTED)


def pool(f, *cases):
    return [(c, dossier.build_dossier(f.conn, c)) for c in cases]


def types(events):
    return [e["type"] for e in events]


class CandidateTests(unittest.TestCase):
    def test_window_edges_and_templates(self):
        f = world()
        primary(f, "account_10", minutes=-8)
        primary(f, "account_11", minutes=74)
        primary(f, "account_12", minutes=-9)
        primary(f, "account_13", minutes=75)
        primary(f, "account_14", minutes=30, template="target")
        got = [c["resource_id"] for c in linker.account_candidates(f.conn, ca())]
        self.assertEqual(got, ["account_10", "account_11", "account_14"])

    def test_only_delete_record_rows_for_accounts(self):
        f = world()
        f.activity(minutes_before(50), "UPDATE_RECORD", "deletion_worker", "account_service",
                   "account_20", "SUCCESS", "Deletion worker submitted primary profile account_20 "
                   "to account_service.")
        primary(f, "search_21")
        self.assertEqual(linker.account_candidates(f.conn, ca()), [])

    def test_rows_after_audit_at_are_never_candidates(self):
        f = world()
        primary(f, "account_10", minutes=50)
        c = case("one@example.com", minutes_before(60), case_id="case_a", reported_at=REPORTED)
        self.assertEqual(linker.account_candidates(f.conn, c), [])

    def test_no_requested_at_lower_bound(self):
        f = world()
        primary(f, "account_10", minutes=50)
        c = case("one@example.com", AUDIT, case_id="case_a", reported_at=REPORTED,
                 requested_at=REPORTED)  # the row is before requested_at, still a candidate
        self.assertEqual([x["resource_id"] for x in linker.account_candidates(f.conn, c)],
                         ["account_10"])

    def test_repeated_rows_for_one_account_give_one_candidate(self):
        f = world()
        primary(f, "account_10", minutes=50)
        primary(f, "account_10", minutes=40, result="FAILED")
        self.assertEqual([x["resource_id"] for x in linker.account_candidates(f.conn, ca())],
                         ["account_10"])


class AccountLinkTests(unittest.TestCase):
    def test_unique_candidate_is_a_low_confidence_m6_pick(self):
        f = world()
        aid = primary(f, "account_10")
        out = linker.link_accounts(f.conn, pool(f, ca()), "guess")
        pick = out["case_a"]["pick"]
        self.assertEqual((pick["resource_id"], pick["method"], pick["confidence"]),
                         ("account_10", "M6", "low"))
        self.assertEqual(pick["activity_id"], aid)
        ev = [e for e in out["case_a"]["events"] if e["type"] == "account_guess"]
        self.assertEqual(len(ev), 1)
        self.assertEqual((ev[0]["resource_id"], ev[0]["confidence"]), ("account_10", "low"))

    def test_tie_is_unresolved_never_ranked(self):
        f = world()
        primary(f, "account_10", minutes=50)
        primary(f, "account_11", minutes=52)
        out = linker.link_accounts(f.conn, pool(f, ca()), "guess")
        self.assertIsNone(out["case_a"]["pick"])
        ev = out["case_a"]["events"]
        self.assertEqual(types(ev), ["link_unresolved"])
        self.assertEqual(ev[0]["reason"], "tie")
        self.assertEqual(ev[0]["candidates"], ["account_10", "account_11"])

    def test_no_candidate_is_unresolved(self):
        f = world()
        out = linker.link_accounts(f.conn, pool(f, ca()), "guess")
        self.assertIsNone(out["case_a"]["pick"])
        self.assertEqual(out["case_a"]["events"][0]["reason"], "no_candidates")

    def test_case_with_an_evidence_account_is_left_alone(self):
        f = world()
        f.obs("evidence_1", T, "account_service", "account_9", "ABSENT",
              keys=[("email", "one@example.com")])
        primary(f, "account_10")
        out = linker.link_accounts(f.conn, pool(f, ca()), "guess")
        self.assertNotIn("case_a", out)

    def test_another_cases_evidence_link_eliminates_a_candidate(self):
        f = world()
        primary(f, "account_10", minutes=50)
        primary(f, "account_11", minutes=52)
        f.obs("evidence_1", T, "account_service", "account_11", "ABSENT",
              keys=[("email", "two@example.com")])
        out = linker.link_accounts(
            f.conn, pool(f, ca(), ca(cid="case_b", email="two@example.com")), "guess")
        self.assertEqual(out["case_a"]["pick"]["resource_id"], "account_10")
        self.assertEqual(out["case_a"]["pick"]["eliminated"],
                         [{"resource_id": "account_11", "case_id": "case_b", "user_id": "user_2"}])

    def test_elimination_is_clock_safe(self):
        """case_b links account_11 only through a record after case_a's audit_at: no elimination."""
        f = world()
        primary(f, "account_10", minutes=50)
        primary(f, "account_11", minutes=52)
        f.file("evidence_2", "2026-09-18T00:00:00Z")
        f.obs("evidence_2", "2026-09-18T00:00:00Z", "account_service", "account_11", "ABSENT",
              keys=[("email", "two@example.com")])
        b = ca(LATER_AUDIT, cid="case_b", email="two@example.com")
        out = linker.link_accounts(f.conn, pool(f, ca(), b), "guess")
        self.assertIsNone(out["case_a"]["pick"])
        self.assertEqual(out["case_a"]["events"][0]["reason"], "tie")

    def test_a_later_case_link_that_existed_before_audit_at_still_eliminates(self):
        f = world()
        primary(f, "account_10", minutes=50)
        primary(f, "account_11", minutes=52)
        f.obs("evidence_1", T, "account_service", "account_11", "ABSENT",
              keys=[("email", "two@example.com")])
        b = ca(LATER_AUDIT, cid="case_b", email="two@example.com")
        out = linker.link_accounts(f.conn, pool(f, ca(), b), "guess")
        self.assertEqual(out["case_a"]["pick"]["resource_id"], "account_10")

    def test_a_name_never_links_or_eliminates(self):
        f = world()
        primary(f, "account_10", minutes=50)
        primary(f, "account_11", minutes=52)
        f.obs("evidence_1", T, "account_service", "account_11", "ABSENT", keys=[("name", "bo two")])
        out = linker.link_accounts(
            f.conn, pool(f, ca(), ca(cid="case_b", email="two@example.com")), "guess")
        self.assertIsNone(out["case_a"]["pick"])

    def test_omit_never_guesses(self):
        f = world()
        primary(f, "account_10")
        out = linker.link_accounts(f.conn, pool(f, ca()), "omit")
        self.assertIsNone(out["case_a"]["pick"])
        self.assertEqual(types(out["case_a"]["events"]), ["link_unresolved"])
        self.assertEqual(out["case_a"]["events"][0]["reason"], "omit")

    def test_unknown_mode_is_refused(self):
        f = world()
        with self.assertRaises(ValueError):
            linker.link_accounts(f.conn, pool(f, ca()), "rank")

    def test_targets_restrict_output_but_not_the_pool(self):
        f = world()
        primary(f, "account_10", minutes=50)
        primary(f, "account_11", minutes=52)
        f.obs("evidence_1", T, "account_service", "account_11", "ABSENT",
              keys=[("email", "two@example.com")])
        p = pool(f, ca(), ca(cid="case_b", email="two@example.com"))
        out = linker.link_accounts(f.conn, p, "guess", targets={"case_a"})
        self.assertEqual(sorted(out), ["case_a"])
        self.assertEqual(out["case_a"]["pick"]["resource_id"], "account_10")

    def test_pool_order_does_not_matter(self):
        f = world()
        primary(f, "account_10", minutes=50)
        primary(f, "account_11", minutes=52)
        f.obs("evidence_1", T, "account_service", "account_11", "ABSENT",
              keys=[("email", "two@example.com")])
        p = pool(f, ca(), ca(cid="case_b", email="two@example.com"))
        self.assertEqual(linker.link_accounts(f.conn, p, "guess"),
                         linker.link_accounts(f.conn, list(reversed(p)), "guess"))


class GuessedAccountDossierTests(unittest.TestCase):
    def guessed(self, f, c=None):
        c = c or ca()
        out = linker.link_accounts(f.conn, pool(f, c), "guess")
        d = dossier.build_dossier(f.conn, c)
        return dossier.add_guessed_account(f.conn, d, out[c["case_id"]]["pick"])

    def acct(self, d):
        hits = [r for r in d["resources"] if r["resource_id"] == "account_10"]
        self.assertEqual(len(hits), 1)
        return hits[0]

    def set_adapter(self, f, fid, adapter):
        f.conn.execute("update evidence_file set adapter=? where file_id=?", (adapter, fid))

    def test_resource_is_low_confidence_absent_by_required_end_state(self):
        f = world()
        primary(f, "account_10")
        r = self.acct(self.guessed(f))
        self.assertEqual(r["system_id"], "account_service")
        self.assertEqual(r["link"]["confidence"], "low")
        self.assertEqual(r["link"]["method"], "M6")
        self.assertEqual(r["category"], "USER_PROFILE")
        self.assertEqual((r["state"], r["state_basis"]), ("ABSENT", "l1_guess"))

    def test_citation_is_rule_o_dump_before_inventory_then_latest(self):
        f = world()
        primary(f, "account_10")
        f.file("evidence_3", "2026-09-12T00:00:00Z")
        f.file("evidence_4", "2026-09-13T00:00:00Z")
        f.file("evidence_5", "2026-09-14T09:00:00Z")
        self.set_adapter(f, "evidence_3", "replica_sql")
        self.set_adapter(f, "evidence_4", "replica_sql")
        self.set_adapter(f, "evidence_5", "inventory_sql")
        f.scope("evidence_3", "account_service", "2026-09-12T00:00:00Z", complete=1)
        f.scope("evidence_4", "account_service", "2026-09-13T00:00:00Z", complete=1)
        f.scope("evidence_5", "account_service", "2026-09-14T09:00:00Z", complete=0)
        c = self.acct(self.guessed(f))["citation"]
        self.assertEqual((c["rule"], c["pick"]), ("O", "evidence_4"))

    def test_a_partial_capture_may_be_cited_but_proves_nothing(self):
        f = world()
        primary(f, "account_10")
        f.file("evidence_6", "2026-09-14T09:00:00Z")
        self.set_adapter(f, "evidence_6", "snapshot_bundle")
        f.scope("evidence_6", "account_service", "2026-09-14T09:00:00Z", complete=0)
        r = self.acct(self.guessed(f))
        self.assertEqual((r["citation"]["rule"], r["citation"]["pick"]), ("O", "evidence_6"))
        self.assertEqual(r["state_basis"], "l1_guess")

    def test_never_rule_e_even_when_a_file_names_the_account(self):
        f = world()
        primary(f, "account_10")
        f.file("evidence_7", "2026-09-14T10:00:00Z")
        f.obs("evidence_7", "2026-09-14T10:00:00Z", "account_service", "account_10", "ABSENT")
        r = self.acct(self.guessed(f))
        self.assertEqual(r["state"], "ABSENT")
        self.assertNotEqual(r["citation"]["pick"], "evidence_7")
        self.assertIsNone(r["citation"]["pick"])

    def test_capture_after_audit_at_is_not_cited(self):
        f = world()
        primary(f, "account_10")
        f.file("evidence_8", "2026-09-16T00:00:00Z")
        f.scope("evidence_8", "account_service", "2026-09-14T09:00:00Z", complete=1)
        self.assertIsNone(self.acct(self.guessed(f))["citation"]["pick"])

    def test_capture_that_holds_the_account_is_not_an_omission(self):
        f = world()
        primary(f, "account_10")
        f.file("evidence_9", "2026-09-14T09:00:00Z")
        f.scope("evidence_9", "account_service", "2026-09-14T09:00:00Z", complete=1)
        f.obs("evidence_9", "2026-09-14T09:00:00Z", "account_service", "account_10", "PRESENT")
        r = self.acct(self.guessed(f))
        self.assertEqual(r["state"], "PRESENT")
        self.assertIsNone(r["citation"]["pick"])

    def test_clock_is_rechecked(self):
        f = world()
        primary(f, "account_10")
        d = self.guessed(f)
        dossier.assert_clock(f.conn, d)

    def test_none_pick_leaves_the_dossier_unchanged(self):
        f = world()
        d = dossier.build_dossier(f.conn, ca())
        self.assertEqual(dossier.add_guessed_account(f.conn, d, None), d)


class AnalyticsTests(unittest.TestCase):
    def test_unique_candidate_is_reported_but_never_linked(self):
        f = world()
        anonymize(f, "analytics_10")
        out = linker.link_analytics(f.conn, pool(f, ca()))
        ev = out["case_a"]
        self.assertEqual(types(ev), ["analytics_candidate"])
        self.assertEqual((ev[0]["resource_id"], ev[0]["withheld"], ev[0]["confidence"]),
                         ("analytics_10", True, "low"))

    def test_tie_is_unresolved(self):
        f = world()
        anonymize(f, "analytics_10", minutes=50)
        anonymize(f, "analytics_11", minutes=60)
        ev = linker.link_analytics(f.conn, pool(f, ca()))["case_a"]
        self.assertEqual(types(ev), ["analytics_unresolved"])
        self.assertEqual((ev[0]["reason"], ev[0]["candidates"]),
                         ("tie", ["analytics_10", "analytics_11"]))

    def test_window_is_minus_29_to_plus_114(self):
        f = world()
        anonymize(f, "analytics_10", minutes=-29)
        anonymize(f, "analytics_11", minutes=114)
        anonymize(f, "analytics_12", minutes=-30)
        anonymize(f, "analytics_13", minutes=115)
        ev = linker.link_analytics(f.conn, pool(f, ca()))["case_a"]
        self.assertEqual(ev[0]["candidates"], ["analytics_10", "analytics_11"])

    def test_evidence_linked_elsewhere_is_eliminated(self):
        f = world()
        anonymize(f, "analytics_10", minutes=50)
        anonymize(f, "analytics_11", minutes=60)
        f.obs("evidence_1", T, "analytics_platform", "analytics_11", "IDENTIFIABLE",
              keys=[("email", "two@example.com")])
        out = linker.link_analytics(
            f.conn, pool(f, ca(), ca(cid="case_b", email="two@example.com")))
        self.assertEqual(types(out["case_a"]), ["analytics_candidate"])
        self.assertEqual(out["case_a"][0]["resource_id"], "analytics_10")

    def test_case_with_evidence_analytics_is_left_alone(self):
        f = world()
        f.obs("evidence_1", T, "analytics_platform", "analytics_9", "IDENTIFIABLE",
              keys=[("email", "one@example.com")])
        anonymize(f, "analytics_10")
        self.assertNotIn("case_a", linker.link_analytics(f.conn, pool(f, ca())))


if __name__ == "__main__":
    unittest.main()
