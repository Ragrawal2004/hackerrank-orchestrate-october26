import json
import unittest
from unittest import mock

from ghost import dossier, identity, loaders
from tests.fact_builder import Facts, case, sha
from tests.helpers import DATASET

AUDIT = "2026-09-15T12:00:00Z"
T1 = "2026-09-10T08:00:00Z"
T2 = "2026-09-12T08:00:00Z"
T3 = "2026-09-14T08:00:00Z"
AFTER = "2026-09-16T08:00:00Z"
FAR = "2099-01-01T00:00:00Z"

ANA = "ana@example.com"
ANA_OLD = "ana.old@example.com"
BOB = "bob@example.com"


def facts():
    f = Facts()
    f.user("user_1", ANA, phone="+111", name="Ana Lima", old_emails=[ANA_OLD], old_phones=["+110"])
    f.user("user_2", BOB, phone="+222", name="Bob Stone")
    return f


def build(f, email=ANA, audit=AUDIT):
    return dossier.build_dossier(f.conn, case(email, audit))


def res(d, resource_id):
    hits = [r for r in d["resources"] if r["resource_id"] == resource_id]
    return hits[0] if len(hits) == 1 else (hits or None)


def events(d, kind):
    return [e for e in d["events"] if e["type"] == kind]


class IdentityTests(unittest.TestCase):
    def test_historical_email_resolves_full_chain(self):
        f = facts()
        d = build(f, email=ANA_OLD)
        self.assertEqual((d["user_id"], d["resolved_via"]), ("user_1", "historical"))
        ids = d["identifiers"]
        self.assertEqual(ids["email"], sorted([ANA, ANA_OLD]))
        self.assertEqual(ids["email_hash"], sorted([sha(ANA), sha(ANA_OLD)]))
        self.assertEqual(ids["phone"], ["+110", "+111"])
        self.assertEqual(ids["user_id"], ["user_1"])

    def test_unknown_email_raises(self):
        with self.assertRaises(identity.UnknownIdentity):
            build(facts(), email="nobody@example.com")

    def test_each_identifier_kind_links(self):
        f = facts().file("e1", T3)
        for rid, key in [("r_hash", ("correlation_key", sha(ANA_OLD))), ("r_phone", ("phone", "+110")),
                         ("r_uid", ("subject_reference", "user_1")),
                         ("r_upper", ("lookup_key", "  ANA@EXAMPLE.COM "))]:
            f.obs("e1", T3, "search_service", rid, "PRESENT", keys=[key])
        d = build(f)
        for rid in ("r_hash", "r_phone", "r_uid", "r_upper"):
            self.assertEqual(res(d, rid)["link"]["method"], "direct", rid)

    def test_near_miss_email_does_not_link(self):
        f = facts().file("e1", T3)
        f.obs("e1", T3, "search_service", "search_9", "PRESENT", keys=[("email", "ana.jr@example.com")])
        self.assertIsNone(res(build(f), "search_9"))

    def test_name_alone_does_not_link(self):
        f = facts().file("e1", T3)
        f.obs("e1", T3, "search_service", "search_9", "PRESENT", keys=[("name", "Ana Lima")])
        d = build(f)
        self.assertIsNone(res(d, "search_9"))
        self.assertEqual(len(events(d, "name_only_match")), 1)


class TwoClockTests(unittest.TestCase):
    def test_late_companion_record_links_and_sets_state_but_is_not_cited(self):
        # U3: file captured after audit_at, record inside it stamped before audit_at.
        f = facts().file("e_late", AFTER)
        f.obs("e_late", T3, "security_service", "audit_record_1", "PRESENT",
              keys=[("subject_reference", "user_1")])
        d = build(f)
        r = res(d, "audit_record_1")
        self.assertEqual(r["state"], "PRESENT")
        self.assertEqual(r["link"]["method"], "direct")
        self.assertEqual(r["citation"]["rule_e"], [])
        self.assertIsNone(r["citation"]["pick"])
        self.assertEqual(len(events(d, "unciteable")), 1)
        self.assertEqual(d["clock"]["late_manifest_files_used"], ["e_late"])

    def test_late_record_in_companion_is_never_used(self):
        # Trap (F-B4/F-D4): the later file also holds a record stamped after audit_at.
        f = facts().file("e1", T3).file("e_late", AFTER)
        early = f.obs("e1", T3, "account_service", "account_1", "ABSENT", keys=[("email", ANA)])
        late = f.obs("e_late", AFTER, "account_service", "account_1", "PRESENT",
                     keys=[("email", ANA)])
        d = build(f)
        r = res(d, "account_1")
        self.assertEqual(r["state"], "ABSENT")
        self.assertEqual(r["state_source"]["obs_id"], early)
        self.assertEqual(r["citation"]["pick"], "e1")
        self.assertNotIn(f"L{late}", json.dumps(d))
        self.assertNotIn("e_late", json.dumps(d))
        self.assertEqual(d["clock"]["late_records_excluded"], 1)

    def test_record_after_audit_cannot_link(self):
        f = facts().file("e_late", AFTER)
        f.obs("e_late", AFTER, "search_service", "search_1", "PRESENT", keys=[("email", ANA)])
        self.assertIsNone(res(build(f), "search_1"))

    def test_boundaries_are_inclusive(self):
        later = "2026-09-15T12:00:01Z"
        f = facts().file("e_eq", AUDIT).file("e_after", later)
        f.obs("e_eq", AUDIT, "search_service", "search_eq", "PRESENT", keys=[("email", ANA)])
        f.obs("e_after", later, "search_service", "search_after", "PRESENT", keys=[("email", ANA)])
        f.link_identity(AUDIT, "data_flow_eq", ANA, "user_1", target="integration_service")
        f.link_identity(later, "data_flow_after", ANA, "user_1", target="integration_service")
        d = build(f)
        self.assertEqual(res(d, "search_eq")["citation"]["pick"], "e_eq")
        self.assertIsNotNone(res(d, "data_flow_eq"))
        self.assertIsNone(res(d, "search_after"))
        self.assertIsNone(res(d, "data_flow_after"))

    def test_link_activity_after_audit_is_ignored(self):
        f = facts()
        f.link_identity(AFTER, "export_1842", ANA, "user_1", target="export_service")
        self.assertIsNone(res(build(f), "export_1842"))

    def test_assertion_fires_if_a_late_observation_is_read(self):
        f = facts().file("e_late", AFTER)
        f.obs("e_late", AFTER, "search_service", "search_1", "PRESENT", keys=[("email", ANA)])
        real = dossier._query_observations
        with mock.patch.object(dossier, "_query_observations", lambda c, a: real(c, FAR)):
            with self.assertRaises(dossier.LateRecordRead):
                build(f)

    def test_assertion_fires_if_a_late_activity_is_read(self):
        f = facts()
        f.link_identity(AFTER, "data_flow_1", ANA, "user_1", target="integration_service")
        real = dossier._query_activities
        with mock.patch.object(dossier, "_query_activities", lambda c, a: real(c, FAR)):
            with self.assertRaises(dossier.LateRecordRead):
                build(f)

    def test_final_check_rejects_a_late_citation(self):
        f = facts().file("e1", T3).file("e_late", AFTER)
        f.obs("e1", T3, "search_service", "search_1", "PRESENT", keys=[("email", ANA)])
        d = build(f)
        res(d, "search_1")["citation"]["pick"] = "e_late"
        with self.assertRaises(dossier.LateCitation):
            dossier.assert_clock(f.conn, d)


class LinkingTests(unittest.TestCase):
    def test_trace_join_within_one_file_only(self):
        f = facts().file("log1", T3).file("log2", T3)
        f.obs("log1", T2, keys=[("lookup_key", ANA)], joins=[("trace_id", "tr-1")])
        f.obs("log1", T2, "account_service", "account_1", "ABSENT", joins=[("trace_id", "tr-1")])
        f.obs("log2", T2, "search_service", "search_x", "PRESENT", joins=[("trace_id", "tr-1")])
        d = build(f)
        self.assertEqual(res(d, "account_1")["link"]["method"], "trace")
        self.assertIsNone(res(d, "search_x"))

    def test_closure_runs_to_a_fixpoint(self):
        # activity -> repopulate -> reference -> trace: each hop needs the one before it.
        f = facts().file("log1", T3).file("e_search", T3)
        f.obs("log1", T2, "support_platform", "support_7", "PRESENT", joins=[("trace_id", "tr-7")])
        f.obs("log1", T2, "account_service", "account_7", "PRESENT", joins=[("trace_id", "tr-7")])
        f.obs("e_search", T2, "search_service", "search_7", "PRESENT",
              joins=[("source_resource_id", "account_7")])
        f.repopulate(T2, "data_flow_7", "search_7")
        f.link_identity(T1, "data_flow_7", ANA, "user_1", target="integration_service")
        d = build(f)
        got = {r["resource_id"]: r["link"]["method"] for r in d["resources"]}
        self.assertEqual(got, {"data_flow_7": "activity", "search_7": "activity",
                               "account_7": "reference", "support_7": "trace"})

    def test_reference_join_both_directions(self):
        f = facts().file("e1", T3)
        f.obs("e1", T3, "search_service", "search_1", "PRESENT", keys=[("email", ANA)],
              joins=[("source_resource_id", "account_1")])
        f.obs("e1", T3, "export_service", "export_1", "PRESENT",
              joins=[("source_resource_id", "search_1")])
        d = build(f)
        self.assertEqual(res(d, "account_1")["link"]["method"], "reference")
        self.assertEqual(res(d, "export_1")["link"]["method"], "reference")

    def test_reference_holding_an_identifier_is_not_a_resource(self):
        f = facts().file("e1", T3)
        f.obs("e1", T3, "security_service", "audit_record_1", "PRESENT",
              keys=[("customer_reference", "user_1")], joins=[("customer_reference", "user_1")])
        d = build(f)
        self.assertEqual([r["resource_id"] for r in d["resources"]], ["audit_record_1"])

    def test_unknown_identifier_does_not_block_a_reference(self):
        f = facts().file("e1", T3)
        f.obs("e1", T3, "search_service", "search_1", "PRESENT", keys=[("email", ANA)],
              joins=[("source_resource_id", "account_9")])
        f.obs("e1", T3, "account_service", "account_9", "ABSENT",
              keys=[("email", "nobody@example.com")])
        self.assertEqual(res(build(f), "account_9")["link"]["method"], "reference")


class ConflictTests(unittest.TestCase):
    """A8: direct evidence of another subject beats any indirect link."""

    def test_forward_reference_cannot_pull_in_another_subjects_resource(self):
        f = facts().file("e1", T3)
        f.obs("e1", T3, "search_service", "search_1", "PRESENT", keys=[("email", ANA)],
              joins=[("source_resource_id", "account_9")])
        f.obs("e1", T3, "account_service", "account_9", "PRESENT", keys=[("email", BOB)])
        d = build(f)
        self.assertIsNone(res(d, "account_9"))
        [e] = events(d, "a8_conflict")
        self.assertEqual((e["resource_id"], e["method"], e["owners"]), ("account_9", "reference", ["user_2"]))

    def test_reverse_reference_cannot_pull_in_another_subjects_resource(self):
        f = facts().file("e1", T3)
        f.obs("e1", T3, "account_service", "account_1", "ABSENT", keys=[("email", ANA)])
        f.obs("e1", T3, "search_service", "search_9", "PRESENT", keys=[("email", BOB)],
              joins=[("source_resource_id", "account_1")])
        d = build(f)
        self.assertIsNone(res(d, "search_9"))
        self.assertEqual([e["resource_id"] for e in events(d, "a8_conflict")], ["search_9"])

    def test_blocked_resource_does_not_spread(self):
        f = facts().file("e1", T3)
        f.obs("e1", T3, "search_service", "search_1", "PRESENT", keys=[("email", ANA)],
              joins=[("source_resource_id", "account_9")])
        f.obs("e1", T3, "account_service", "account_9", "PRESENT", keys=[("email", BOB)],
              joins=[("source_resource_id", "export_9")], )
        f.obs("e1", T3, "export_service", "export_9", "PRESENT")
        f.obs("e1", T3, "support_platform", "support_9", "PRESENT",
              joins=[("source_resource_id", "account_9")])
        d = build(f)
        self.assertIsNone(res(d, "export_9"))
        self.assertIsNone(res(d, "support_9"))

    def test_trace_cannot_pull_in_another_subjects_line(self):
        f = facts().file("log1", T3)
        f.obs("log1", T2, keys=[("lookup_key", ANA)], joins=[("trace_id", "tr-1")])
        f.obs("log1", T2, "search_service", "search_9", "PRESENT", keys=[("email", BOB)],
              joins=[("trace_id", "tr-1")])
        d = build(f)
        self.assertIsNone(res(d, "search_9"))
        self.assertEqual(len(events(d, "a8_conflict")), 1)

    def test_activity_link_loses_to_direct_evidence(self):
        f = facts().file("e1", T3)
        f.obs("e1", T3, "account_service", "account_8", "PRESENT", keys=[("email", BOB)])
        f.link_identity(T1, "account_8", ANA, "user_1")
        d = build(f)
        self.assertIsNone(res(d, "account_8"))
        self.assertEqual(events(d, "a8_conflict")[0]["method"], "activity")

    def test_shared_record_with_both_subjects_links(self):
        f = facts().file("e1", T3)
        f.obs("e1", T3, "support_platform", "support_5", "IDENTIFIABLE",
              keys=[("requester_email", ANA), ("participant_email", BOB)])
        d = build(f)
        self.assertEqual(res(d, "support_5")["link"]["method"], "direct")
        self.assertEqual(events(d, "a8_conflict"), [])


class StateTests(unittest.TestCase):
    def _two(self, first, second):
        f = facts().file("e1", T1).file("e2", T2)
        f.obs("e1", T1, "account_service", "account_1", first, keys=[("email", ANA)])
        f.obs("e2", T2, "account_service", "account_1", second, keys=[("email", ANA)])
        return res(build(f), "account_1")

    def test_latest_observation_wins(self):
        r = self._two("PRESENT", "ABSENT")
        self.assertEqual((r["state"], r["citation"]["pick"]), ("ABSENT", "e2"))
        r = self._two("ABSENT", "PRESENT")
        self.assertEqual((r["state"], r["citation"]["pick"]), ("PRESENT", "e2"))

    def test_latest_wins_regardless_of_insert_order(self):
        f = facts().file("e1", T1).file("e2", T2)
        f.obs("e2", T2, "account_service", "account_1", "ABSENT", keys=[("email", ANA)])
        f.obs("e1", T1, "account_service", "account_1", "PRESENT", keys=[("email", ANA)])
        self.assertEqual(res(build(f), "account_1")["state"], "ABSENT")

    def test_explicit_beats_omission_on_a_tie(self):
        f = facts().file("replica", T2).file("primary", T2)
        f.obs("replica", T2, "account_service", "account_5", "PRESENT", keys=[("email", ANA)])
        f.scope("primary", "account_service", T2)
        r = res(build(f), "account_5")
        self.assertEqual((r["state"], r["state_basis"]), ("PRESENT", "explicit"))

    def test_later_omission_beats_earlier_explicit(self):
        f = facts().file("e1", T1).file("dump", T2)
        f.obs("e1", T1, "account_service", "account_1", "PRESENT", keys=[("email", ANA)])
        f.scope("dump", "account_service", T2)
        r = res(build(f), "account_1")
        self.assertEqual((r["state"], r["state_basis"]), ("ABSENT", "omission"))
        self.assertEqual((r["citation"]["rule"], r["citation"]["pick"]), ("O", "dump"))
        self.assertEqual(r["citation"]["rule_e"], [{"file_id": "e1", "manifest_at": T1, "state": "PRESENT"}])

    def test_later_snapshot_in_the_same_file_omits(self):
        f = facts().file("bundle", T3)
        f.obs("bundle", T1, "account_service", "account_1", "PRESENT", keys=[("email", ANA)])
        f.scope("bundle", "account_service", T1).scope("bundle", "account_service", T2)
        r = res(build(f), "account_1")
        self.assertEqual((r["state"], r["state_basis"]), ("ABSENT", "omission"))

    def test_scope_without_proof_gives_no_omission(self):
        f = facts().file("e1", T1).file("partial", T2).file("other", T2).file("late", AFTER)
        f.obs("e1", T1, "account_service", "account_1", "PRESENT", keys=[("email", ANA)])
        f.scope("partial", "account_service", T2, complete=0)
        f.scope("other", "search_service", T2)
        f.scope("late", "account_service", AFTER)
        r = res(build(f), "account_1")
        self.assertEqual((r["state"], r["state_basis"]), ("PRESENT", "explicit"))
        self.assertEqual(r["citation"]["rule_o"], [])

    def test_system_from_activity_when_no_observation(self):
        # case_06 pattern: linked by reference only, absence proven by a whole-store dump.
        f = facts().file("e_search", T3).file("dump", T2)
        f.obs("e_search", T3, "search_service", "search_1", "PRESENT", keys=[("email", ANA)],
              joins=[("source_resource_id", "account_6")])
        f.activity(T1, "DELETE_RECORD", "deletion_worker", "account_service", "account_6",
                   "SUCCESS", "Deletion worker submitted primary profile account_6 to account_service.")
        f.scope("dump", "account_service", T2)
        d = build(f)
        r = res(d, "account_6")
        self.assertEqual((r["system_id"], r["state"], r["citation"]["pick"]),
                         ("account_service", "ABSENT", "dump"))
        self.assertTrue(d["account_linked"])

    def test_system_unknown_is_flagged(self):
        f = facts().file("e1", T3)
        f.obs("e1", T3, "search_service", "search_1", "PRESENT", keys=[("email", ANA)],
              joins=[("source_resource_id", "thing_1")])
        d = build(f)
        r = res(d, "thing_1")
        self.assertEqual((r["system_id"], r["state"]), (None, None))
        self.assertEqual([e["resource_id"] for e in events(d, "system_unknown")], ["thing_1"])


class RecreationTests(unittest.TestCase):
    def _absent_then(self, *acts):
        f = facts().file("e1", T1)
        f.obs("e1", T1, "account_service", "account_1", "ABSENT", keys=[("email", ANA)])
        ids = [fn(f) for fn in acts]
        return f, ids

    def test_recreation_results_are_success_and_partial(self):
        # U7 (open assumption): PARTIAL final checkpoints reopen.
        self.assertEqual(dossier.RECREATION_RESULTS, frozenset({"SUCCESS", "PARTIAL"}))

    def test_success_checkpoint_after_absent_reopens(self):
        f, [aid] = self._absent_then(lambda f: f.checkpoint(T2, "account_1"))
        d = build(f)
        r = res(d, "account_1")
        self.assertEqual((r["state"], r["state_basis"]), ("PRESENT", "recreation"))
        self.assertEqual(r["recreated_by"], {"activity_id": aid, "event_time": T2, "result": "SUCCESS"})
        self.assertEqual((r["citation"]["rule"], r["citation"]["pick"]), ("activity", aid))
        self.assertEqual(len(events(d, "recreation_reopen")), 1)
        self.assertEqual(events(d, "recreation_partial"), [])
        self.assertIn(aid, d["activities_used"])

    def test_partial_checkpoint_reopens_and_is_flagged(self):
        f, [aid] = self._absent_then(lambda f: f.checkpoint(T2, "account_1", result="PARTIAL"))
        d = build(f)
        self.assertEqual(res(d, "account_1")["state"], "PRESENT")
        self.assertEqual([e["activity_id"] for e in events(d, "recreation_partial")], [aid])

    def test_failed_checkpoint_does_not_reopen(self):
        f, _ = self._absent_then(lambda f: f.checkpoint(T2, "account_1", result="FAILED"))
        self.assertEqual(res(build(f), "account_1")["state"], "ABSENT")

    def test_recreation_before_the_absent_observation_does_not_reopen(self):
        f, _ = self._absent_then(lambda f: f.checkpoint("2026-09-01T00:00:00Z", "account_1"))
        self.assertEqual(res(build(f), "account_1")["state"], "ABSENT")

    def test_recreation_after_audit_is_ignored(self):
        f, _ = self._absent_then(lambda f: f.checkpoint(AFTER, "account_1"))
        d = build(f)
        self.assertEqual(res(d, "account_1")["state"], "ABSENT")
        self.assertEqual(d["activities_used"], [])

    def test_later_absent_observation_closes_it_again(self):
        f, _ = self._absent_then(lambda f: f.checkpoint(T2, "account_1"))
        f.file("e3", T3)
        f.obs("e3", T3, "account_service", "account_1", "ABSENT", keys=[("email", ANA)])
        r = res(build(f), "account_1")
        self.assertEqual((r["state"], r["citation"]["pick"]), ("ABSENT", "e3"))

    def test_checkpoint_on_another_system_does_not_reopen(self):
        f, _ = self._absent_then(lambda f: f.checkpoint(T2, "account_1", system="billing_service"))
        self.assertEqual(res(build(f), "account_1")["state"], "ABSENT")

    def test_repopulated_downstream_resource_reopens(self):
        f = facts().file("e1", T1)
        f.obs("e1", T1, "search_service", "search_1", "ABSENT", keys=[("email", ANA)])
        aid = f.repopulate(T2, "data_flow_1", "search_1")
        r = res(build(f), "search_1")
        self.assertEqual((r["state"], r["citation"]["pick"]), ("PRESENT", aid))

    def test_restore_backup_reopens(self):
        f = facts().file("e1", T1)
        f.obs("e1", T1, "search_service", "search_1", "ABSENT", keys=[("email", ANA)])
        aid = f.activity(T2, "RESTORE_BACKUP", "backup_service", "search_service", "backup_1",
                         "SUCCESS", "Restore job repopulated downstream resource search_1.")
        self.assertEqual(res(build(f), "search_1")["recreated_by"]["activity_id"], aid)


class CitationTests(unittest.TestCase):
    def test_rule_e_latest_file_at_or_before_audit(self):
        f = facts().file("e1", T1).file("e2", T2).file("e_late", AFTER)
        for fid in ("e1", "e2"):
            f.obs(fid, T1 if fid == "e1" else T2, "search_service", "search_1", "PRESENT",
                  keys=[("email", ANA)])
        f.obs("e_late", T3, "search_service", "search_1", "PRESENT", keys=[("email", ANA)])
        c = res(build(f), "search_1")["citation"]
        self.assertEqual((c["rule"], c["pick"]), ("E", "e2"))
        self.assertEqual([x["file_id"] for x in c["rule_e"]], ["e2", "e1"])

    def test_rule_o_latest_and_ambiguity_logged(self):
        # case_04 pattern: two whole-store captures omit the account; we pick the latest.
        f = facts().file("dump", T1).file("inventory", T2).file("late_dump", AFTER)
        f.link_identity(T1, "account_4", ANA, "user_1")
        f.scope("dump", "account_service", T1).scope("inventory", "account_service", T2)
        f.scope("late_dump", "account_service", T3)
        d = build(f)
        c = res(d, "account_4")["citation"]
        self.assertEqual((c["rule"], c["pick"]), ("O", "inventory"))
        self.assertEqual([x["file_id"] for x in c["rule_o"]], ["inventory", "dump"])
        [e] = events(d, "rule_o_ambiguous")
        self.assertEqual(e["candidates"], ["inventory", "dump"])

    def test_rule_e_only_from_files_showing_the_state(self):
        f = facts().file("e1", T1).file("e2", T2)
        f.obs("e1", T1, "account_service", "account_1", "ABSENT", keys=[("email", ANA)])
        f.obs("e2", T2, "account_service", "account_1", "ABSENT", keys=[("email", ANA)])
        f.file("dump", T3).scope("dump", "account_service", T3)
        c = res(build(f), "account_1")["citation"]
        self.assertEqual((c["rule"], c["pick"]), ("E", "e2"))

    def test_unciteable_is_logged(self):
        f = facts()
        f.link_identity(T1, "account_4", ANA, "user_1")
        d = build(f)
        self.assertIsNone(res(d, "account_4")["citation"]["pick"])
        self.assertEqual([e["resource_id"] for e in events(d, "unciteable")], ["account_4"])


class OutputTests(unittest.TestCase):
    def _rich(self):
        f = facts().file("e1", T1).file("log1", T2).file("e_late", AFTER)
        f.obs("e1", T1, "account_service", "account_1", "ABSENT", keys=[("email", ANA)],
              attrs={"http_status": 404})
        f.obs("log1", T2, keys=[("lookup_key", ANA)], joins=[("trace_id", "tr-1")])
        f.obs("log1", T2, "search_service", "search_1", "PRESENT", joins=[("trace_id", "tr-1")])
        f.obs("e_late", T3, "security_service", "audit_record_1", "PRESENT",
              keys=[("subject_reference", "user_1")])
        f.checkpoint(T2, "account_1", result="PARTIAL")
        return f

    def test_json_serializable_and_deterministic(self):
        f = self._rich()
        a, b = build(f), build(f)
        self.assertEqual(json.loads(json.dumps(a)), a)
        self.assertEqual(json.dumps(a, sort_keys=True), json.dumps(b, sort_keys=True))
        self.assertEqual([r["resource_id"] for r in a["resources"]],
                         ["account_1", "audit_record_1", "search_1"])

    def test_store_is_not_modified(self):
        f = self._rich()
        before = f.conn.total_changes
        build(f)
        self.assertEqual(f.conn.total_changes, before)

    def test_anchor_candidates_are_citable_files_with_a_direct_match(self):
        d = build(self._rich())
        self.assertEqual(d["anchor_candidates"], ["e1", "log1"])

    def test_attrs_and_category_come_from_the_state_observation(self):
        r = res(build(self._rich()), "account_1")
        self.assertEqual(r["state_source"]["attrs"], {"http_status": 404})


class RealStoreSmokeTests(unittest.TestCase):
    """No adapters yet: only identities and activities are in the store."""

    def test_every_case_builds(self):
        conn = loaders.load_store(DATASET)
        rows = loaders.load_requests(DATASET) + loaders.load_requests(DATASET, sample=True)
        before = conn.total_changes
        for r in rows:
            d = dossier.build_dossier(conn, r)
            json.dumps(d)
            self.assertEqual(d["case_id"], r["case_id"])
        self.assertEqual(conn.total_changes, before)
        self.assertEqual(len(rows), 550)


if __name__ == "__main__":
    unittest.main()
