import json
import tempfile
import unittest
from pathlib import Path

from ghost import adapters, dossier
from ghost.adapters import replica_sql, snapshot_bundle
from tests import bundle_fixture as bf
from tests import sql_fixture as fx
from tests.fact_builder import Facts, case

FMT = "json_snapshot_bundle"
AUDIT = "2026-09-15T12:00:00Z"
T1 = "2026-09-10T08:00:00Z"
T2 = "2026-09-12T08:00:00Z"
T3 = "2026-09-14T08:00:00Z"
AFTER = "2026-09-16T08:00:00Z"
MANIFEST_LATE = "2026-09-17T12:00:00Z"
ANA = "ana@example.com"


def load(text, file_id="evidence_01", manifest=MANIFEST_LATE, facts=None):
    f = facts or Facts()
    f.file(file_id, manifest)
    report = snapshot_bundle.ingest(f.conn, file_id, text)
    return f, report


def users():
    return Facts().user("user_1", ANA, phone="+111", name="Ana Lima")


def obs(conn, rid=None):
    q = ("select obs_id, file_id, locator, record_at, system_id, resource_id, category, state, "
         "state_basis, attrs_json from observation")
    args = ()
    if rid:
        q, args = q + " where resource_id = ?", (rid,)
    out = []
    for r in conn.execute(q + " order by obs_id", args):
        d = dict(zip(("obs_id", "file_id", "locator", "record_at", "system_id", "resource_id",
                      "category", "state", "state_basis", "attrs"), r))
        d["attrs"] = json.loads(d["attrs"])
        out.append(d)
    return out


def keys(conn, rid):
    return sorted((k, v) for k, v in conn.execute(
        "select sk.kind, sk.value from subject_key sk join observation o on o.obs_id = sk.obs_id "
        "where o.resource_id = ?", (rid,)))


def scopes(conn, file_id="evidence_01"):
    return sorted(conn.execute("select system_id, record_at, complete from capture_scope "
                               "where file_id = ?", (file_id,)))


def one(conn, rid):
    got = obs(conn, rid)
    assert len(got) == 1, got
    return got[0]


def single(collection, system, record, at=T3):
    return bf.bundle([bf.snap(at, system, collection, [record])])


class DetectTests(unittest.TestCase):
    def test_bundle_is_claimed_by_structure(self):
        self.assertEqual(snapshot_bundle.detect(single(
            "customer_profiles", "account_service", bf.profile("account_1", ANA))), FMT)

    def test_empty_records_snapshot_is_still_a_bundle(self):
        self.assertEqual(snapshot_bundle.detect(bf.bundle(
            [bf.snap(T3, "account_service", "customer_profiles", [])])), FMT)

    def test_sql_is_not_claimed_and_bundle_is_not_claimed_by_replica_sql(self):
        sql = fx.j_sql("2026-09-21T10:00:00Z", {"customer_data_primary": [],
                                                  "customer_data_read_replica": []})
        self.assertIsNone(snapshot_bundle.detect(sql))
        self.assertIsNone(replica_sql.detect(single(
            "customer_profiles", "account_service", bf.profile("account_1", ANA))))

    def test_other_json_is_not_claimed(self):
        for text in ('{"snapshots": []}', '{"snapshots": "x"}', '{"exported_at": "x"}', "[]",
                     '[{"snapshots": []}]', '{"snapshots": [1, 2]}', "not json {{{", "",
                     '{"snapshots": [{"captured_at": "x"}]}',
                     '{"snapshots": [{"captured_at": "x", "data_store": "s", "collection": "c"}]}',
                     json.dumps({"profile_lookups": [], "observed_at": "2026-01-01T00:00:00Z"})):
            self.assertIsNone(snapshot_bundle.detect(text), text)

    def test_deeply_nested_json_does_not_raise(self):
        self.assertIsNone(snapshot_bundle.detect("[" * 200000))

    def test_file_name_is_irrelevant(self):
        with tempfile.TemporaryDirectory() as d:
            (Path(d) / "evidence").mkdir()
            (Path(d) / "evidence" / "notes.txt").write_text(single(
                "customer_profiles", "account_service", bf.profile("account_1", ANA)),
                encoding="utf-8")
            f = Facts()
            f.conn.execute("insert into evidence_file(file_id,path,origin,format,adapter,manifest_at)"
                           " values ('evidence_07','evidence/notes.txt','x',null,null,?)",
                           (MANIFEST_LATE,))
            summary = adapters.load_evidence(f.conn, d)
            got = f.conn.execute("select format, adapter from evidence_file").fetchone()
        self.assertEqual(got, (FMT, "snapshot_bundle"))
        self.assertEqual(summary["claimed"], ["evidence_07"])

    def test_registry_lists_both_adapters_without_double_claims(self):
        self.assertIn(snapshot_bundle, adapters.ADAPTERS)
        self.assertEqual(adapters.select(single(
            "customer_profiles", "account_service", bf.profile("account_1", ANA)))[0].name,
            "snapshot_bundle")

    def test_ingest_of_a_non_bundle_is_an_error(self):
        f = Facts().file("evidence_01", MANIFEST_LATE)
        with self.assertRaises(ValueError):
            snapshot_bundle.ingest(f.conn, "evidence_01", '{"nope": 1}')
        with self.assertRaises(ValueError):
            snapshot_bundle.ingest(f.conn, "evidence_01", "[1]")

    def test_ingest_of_a_file_outside_the_manifest_is_an_error(self):
        with self.assertRaises(ValueError):
            snapshot_bundle.ingest(Facts().conn, "evidence_99", single(
                "customer_profiles", "account_service", bf.profile("account_1", ANA)))


class MappingTests(unittest.TestCase):
    def check(self, collection, system, record, category, state):
        f, rep = load(single(collection, system, record))
        o = one(f.conn, record["resource_id"])
        self.assertEqual((o["system_id"], o["category"], o["state"], o["state_basis"]),
                         (system, category, state, "explicit"))
        self.assertEqual(o["attrs"]["collection"], collection)
        self.assertEqual(rep["unparsed"], 0)
        return f, o

    def test_account_profile(self):
        f, o = self.check("customer_profiles", "account_service", bf.profile("account_1", "ANA@Example.com"),
                          "USER_PROFILE", "PRESENT")
        self.assertEqual(o["attrs"]["email"], ANA)
        got = keys(f.conn, "account_1")
        self.assertIn(("email", ANA), got)
        self.assertIn(("user_id", "user_1"), got)
        self.assertIn(("phone", "+1 555 0100"), got)
        self.assertIn(("name", "Ana Lima"), got)

    def test_search_document(self):
        f, _ = self.check("customer_search_documents", "search_service",
                          bf.search_doc("search_1", ANA), "SEARCH_INDEX_ENTRY", "PRESENT")
        self.assertIn(("email", ANA), keys(f.conn, "search_1"))
        self.assertIn(("user_id", "user_1"), keys(f.conn, "search_1"))

    def test_analytics_identifiable_and_anonymized(self):
        f, _ = self.check("customer_analytics_reports", "analytics_platform",
                          bf.analytics("analytics_1", email=ANA), "ANALYTICS_PROFILE", "IDENTIFIABLE")
        self.assertIn(("email", ANA), keys(f.conn, "analytics_1"))
        self.check("customer_analytics_reports", "analytics_platform",
                   bf.analytics("analytics_2", user=None), "ANALYTICS_PROFILE", "ANONYMIZED")

    def test_support_conversation_redacted_and_identifiable(self):
        self.check("support_conversations", "support_platform",
                   bf.support("support_1", text="No contact details here."),
                   "SUPPORT_MESSAGE", "REDACTED")
        f, _ = self.check("support_conversations", "support_platform",
                          bf.support("support_2", text=f"Reach me at {ANA.upper()} soon."),
                          "SUPPORT_MESSAGE", "IDENTIFIABLE")
        self.assertIn(("email", ANA), keys(f.conn, "support_2"))

    def test_attachment_export_connector_backup_audit_hold(self):
        f, _ = self.check("ticket_attachments", "support_platform", bf.attachment("support_3", ANA),
                          "SUPPORT_ATTACHMENT", "PRESENT")
        self.assertIn(("email", ANA), keys(f.conn, "support_3"))
        self.check("customer_export_jobs", "export_service", bf.export_job("export_1", ANA),
                   "USER_EXPORT", "PRESENT")
        self.check("connector_configurations", "integration_service",
                   bf.connector("data_flow_1", ANA, True), "DATA_FLOW", "ACTIVE")
        self.check("connector_configurations", "integration_service",
                   bf.connector("data_flow_2", ANA, False), "DATA_FLOW", "DISABLED")
        self.check("backup_catalog", "backup_service", bf.backup("backup_1", ANA, True),
                   "BACKUP_ARCHIVE", "RESTORABLE")
        self.check("backup_catalog", "backup_service", bf.backup("backup_2", ANA, False),
                   "BACKUP_ARCHIVE", "BLOCKED")
        self.check("security_audit_events", "security_service", bf.audit("audit_record_1"),
                   "SECURITY_AUDIT_LOG", "PRESENT")
        _, o = self.check("retained_order_records", "legal_service", bf.legal_hold("legal_hold_1"),
                          "LEGAL_HOLD_RECORD", "PRESENT")
        self.assertEqual(o["attrs"]["hold_until"], "2033-12-31T23:59:59Z")

    def test_tombstone_keeps_subject_reference(self):
        f, o = self.check("deleted_customer_references", "identity_service",
                          bf.tombstone("tombstone_1"), "ACCOUNT_TOMBSTONE", "REDACTED")
        self.assertEqual(o["attrs"]["subject_reference"], "user_1")
        self.assertIn(("user_id", "user_1"), keys(f.conn, "tombstone_1"))
        f, o = self.check("deleted_customer_references", "identity_service",
                          bf.tombstone("tombstone_2", phone="+111", email=ANA.upper()),
                          "ACCOUNT_TOMBSTONE", "IDENTIFIABLE")
        self.assertEqual(o["attrs"]["subject_reference"], "user_1")
        self.assertIn(("email", ANA), keys(f.conn, "tombstone_2"))

    def test_locator_names_the_snapshot_and_record(self):
        text = bf.bundle([bf.snap(T1, "account_service", "customer_profiles", []),
                          bf.snap(T3, "account_service", "customer_profiles",
                                  [bf.profile("account_1", ANA), bf.profile("account_2", "b@x.com")])])
        f, _ = load(text)
        self.assertEqual(one(f.conn, "account_2")["locator"], "snapshots[1].records[1]")

    def test_no_join_keys_and_format_is_written_back(self):
        f, rep = load(single("customer_profiles", "account_service", bf.profile("account_1", ANA)))
        self.assertEqual(f.conn.execute("select count(*) from join_key").fetchone()[0], 0)
        self.assertEqual(rep["join_keys"], 0)
        self.assertEqual(f.conn.execute("select format, adapter from evidence_file").fetchone(),
                         (FMT, "snapshot_bundle"))


class ClockTests(unittest.TestCase):
    def test_record_at_is_the_snapshot_time_not_the_manifest(self):
        text = bf.bundle([bf.snap(T1, "account_service", "customer_profiles", [bf.profile("account_1", ANA)]),
                          bf.snap(T3, "search_service", "customer_search_documents",
                                  [bf.search_doc("search_1", ANA)])], exported_at=MANIFEST_LATE)
        f, _ = load(text)
        self.assertEqual(one(f.conn, "account_1")["record_at"], T1)
        self.assertEqual(one(f.conn, "search_1")["record_at"], T3)
        self.assertEqual(f.conn.execute("select manifest_at from evidence_file").fetchone()[0],
                         MANIFEST_LATE)

    def test_one_store_at_two_times_keeps_both(self):
        text = bf.bundle([bf.snap(T1, "account_service", "customer_profiles", [bf.profile("account_1", ANA)]),
                          bf.snap(T3, "account_service", "customer_profiles", [])])
        f, _ = load(text)
        self.assertEqual([o["record_at"] for o in obs(f.conn, "account_1")], [T1])
        self.assertEqual(scopes(f.conn), [("account_service", T1, 1), ("account_service", T3, 1)])

    def test_late_snapshot_is_stored_with_its_true_time(self):
        # The store holds no case; the dossier clock gate excludes it (see TrapTests).
        text = bf.bundle([bf.snap(AFTER, "account_service", "customer_profiles", [bf.profile("account_1", ANA)])])
        f, rep = load(text, manifest=MANIFEST_LATE)
        self.assertEqual(one(f.conn, "account_1")["record_at"], AFTER)
        self.assertEqual(rep["unparsed"], 0)

    def test_history_row_clock_is_recorded_at(self):
        text = bf.bundle([bf.snap(MANIFEST_LATE, "backup_restore_service", "operational_record_history", [
            bf.history(T2, "account_service", "account_1", "USER_PROFILE", "NOT_FOUND", ANA)])])
        f, _ = load(text)
        self.assertEqual(one(f.conn, "account_1")["record_at"], T2)

    def test_offsets_compare_as_instants(self):
        text = bf.bundle([bf.snap("2026-09-14T10:00:00+02:00", "account_service", "customer_profiles",
                                  [bf.profile("account_1", ANA)])])
        f, rep = load(text)
        self.assertEqual(rep["unparsed"], 0)
        self.assertEqual(one(f.conn, "account_1")["record_at"], "2026-09-14T10:00:00+02:00")

    def test_notes_for_clock_oddities(self):
        text = bf.bundle([bf.snap(AFTER, "account_service", "customer_profiles", [bf.profile("account_1", ANA)])],
                         exported_at="2026-09-20T00:00:00Z")
        f, rep = load(text, manifest="2026-09-15T00:00:00Z")
        types = {n["type"] for n in rep["notes"]}
        self.assertIn("capture_time_mismatch", types)
        self.assertIn("record_after_manifest", types)
        self.assertEqual(rep["unparsed"], 0)  # flagged, never dropped (A-3)
        self.assertEqual(one(f.conn, "account_1")["record_at"], AFTER)


class HistoryTests(unittest.TestCase):
    def rows(self, *items, store="privacy_treatment_service"):
        return bf.bundle([bf.snap(MANIFEST_LATE, store, "operational_record_history", list(items))])

    def test_results_map_to_states(self):
        cases = [("USER_PROFILE", "account_service", "account_1", "FOUND", "PRESENT"),
                 ("USER_PROFILE", "account_service", "account_2", "NOT_FOUND", "ABSENT"),
                 ("SUPPORT_MESSAGE", "support_platform", "support_1", "IDENTIFIABLE", "IDENTIFIABLE"),
                 ("SUPPORT_MESSAGE", "support_platform", "support_2", "REDACTED", "REDACTED"),
                 ("ANALYTICS_PROFILE", "analytics_platform", "analytics_1", "ANONYMIZED", "ANONYMIZED"),
                 ("DATA_FLOW", "integration_service", "data_flow_1", "ACTIVE", "ACTIVE"),
                 ("DATA_FLOW", "integration_service", "data_flow_2", "DISABLED", "DISABLED"),
                 ("BACKUP_ARCHIVE", "backup_service", "backup_1", "ACTIVE", "RESTORABLE"),
                 ("BACKUP_ARCHIVE", "backup_service", "backup_2", "BLOCKED", "BLOCKED"),
                 ("SECURITY_AUDIT_LOG", "security_service", "audit_record_1", "FOUND", "PRESENT")]
        f, rep = load(self.rows(*[bf.history(T2, sys, rid, cat, res, ANA)
                                  for cat, sys, rid, res, _ in cases]))
        for cat, sys, rid, res, state in cases:
            o = one(f.conn, rid)
            self.assertEqual((o["system_id"], o["category"], o["state"], o["state_basis"]),
                             (sys, cat, state, "explicit"), rid)
            self.assertEqual(o["attrs"]["record_type"], cat)
            self.assertEqual(o["attrs"]["result"], res)
        self.assertEqual(rep["unparsed"], 0)

    def test_lookup_value_is_a_subject_key(self):
        f, _ = load(self.rows(bf.history(T2, "account_service", "account_1", "USER_PROFILE",
                                         "FOUND", ANA.upper()),
                              bf.history(T2, "security_service", "audit_record_1",
                                         "SECURITY_AUDIT_LOG", "FOUND", "user_1")))
        self.assertEqual(keys(f.conn, "account_1"), [("email", ANA)])
        self.assertEqual(keys(f.conn, "audit_record_1"), [("user_id", "user_1")])

    def test_history_adds_no_capture_scope(self):
        f, _ = load(self.rows(bf.history(T2, "account_service", "account_1", "USER_PROFILE",
                                         "NOT_FOUND", ANA)))
        self.assertEqual(scopes(f.conn), [])

    def test_bad_rows_are_unparsed(self):
        good = bf.history(T2, "account_service", "account_1", "USER_PROFILE", "FOUND", ANA)
        for bad in ({**good, "result": "MAYBE"}, {**good, "record_type": "NOT_A_CATEGORY"},
                    {**good, "recorded_at": "yesterday"}, {**good, "resource_id": ""},
                    {k: v for k, v in good.items() if k != "system"}, "not a dict", 7):
            f, rep = load(self.rows(good, bad))
            self.assertEqual((rep["meaningful"], rep["unparsed"]), (3, 1), bad)
            self.assertEqual(len(obs(f.conn)), 1, bad)


class ScopeTests(unittest.TestCase):
    def test_empty_snapshot_gives_scope_and_omission(self):
        f, _ = load(bf.bundle([bf.snap(T3, "account_service", "customer_profiles", [])]))
        self.assertEqual(scopes(f.conn), [("account_service", T3, 1)])
        self.assertEqual(obs(f.conn), [])

    def test_every_store_snapshot_has_a_scope_row(self):
        f, _ = load(bf.bundle([
            bf.snap(T3, "account_service", "customer_profiles", [bf.profile("account_1", ANA)]),
            bf.snap(T3, "search_service", "customer_search_documents", []),
            bf.snap(T3, "security_service", "security_audit_events", [bf.audit("audit_record_1")])]))
        self.assertEqual(scopes(f.conn), [("account_service", T3, 1), ("search_service", T3, 1),
                                          ("security_service", T3, 1)])

    def test_mixed_store_needs_all_its_collections_at_that_time(self):
        conv, att = "support_conversations", "ticket_attachments"
        f, _ = load(bf.bundle([
            bf.snap(T1, "support_platform", att, [bf.attachment("support_9", ANA)]),
            bf.snap(T1, "support_platform", conv, [bf.support("support_1")]),
            bf.snap(T3, "support_platform", conv, [])]))
        # T3 has only conversations, so it cannot prove an attachment is gone.
        self.assertEqual(scopes(f.conn), [("support_platform", T1, 1)])

    def test_mixed_store_with_both_collections_everywhere_is_scoped_at_each_time(self):
        conv, att = "support_conversations", "ticket_attachments"
        f, _ = load(bf.bundle([
            bf.snap(T1, "support_platform", conv, []), bf.snap(T1, "support_platform", att, []),
            bf.snap(T3, "support_platform", conv, []), bf.snap(T3, "support_platform", att, [])]))
        self.assertEqual(scopes(f.conn), [("support_platform", T1, 1), ("support_platform", T3, 1)])

    def test_single_collection_store_is_unaffected_by_other_stores(self):
        f, _ = load(bf.bundle([
            bf.snap(T1, "support_platform", "support_conversations", [bf.support("support_1")]),
            bf.snap(T3, "support_platform", "support_conversations", [])]))
        self.assertEqual(scopes(f.conn), [("support_platform", T1, 1), ("support_platform", T3, 1)])

    def test_duplicate_snapshot_times_give_one_scope_row(self):
        f, _ = load(bf.bundle([bf.snap(T3, "search_service", "customer_search_documents", []),
                               bf.snap(T3, "search_service", "customer_search_documents", [])]))
        self.assertEqual(scopes(f.conn), [("search_service", T3, 1)])


def dossier_for(f, audit=AUDIT):
    return dossier.build_dossier(f.conn, case(ANA, audit))


def res(d, rid):
    hits = [r for r in d["resources"] if r["resource_id"] == rid]
    return hits[0] if len(hits) == 1 else None


class TrapTests(unittest.TestCase):
    """F-B4 / F-D4: a companion bundle whose manifest and later records follow audit_at (U3)."""

    def test_late_snapshot_cannot_reopen_an_absent_account(self):
        f = users()
        load(bf.bundle([
            bf.snap(T1, "account_service", "customer_profiles", [bf.profile("account_1", ANA)]),
            bf.snap(T3, "account_service", "customer_profiles", []),
            bf.snap(AFTER, "account_service", "customer_profiles", [bf.profile("account_1", ANA)])],
            exported_at=MANIFEST_LATE), facts=f)
        d = dossier_for(f)
        r = res(d, "account_1")
        self.assertEqual(r["state"], "ABSENT")  # omission at T3 beats the explicit T1 record
        self.assertGreaterEqual(d["clock"]["late_records_excluded"], 1)
        self.assertIsNone(r["citation"]["pick"])  # manifest after audit_at: never cited

    def test_account_present_only_after_audit_is_not_linked(self):
        f = users()
        load(bf.bundle([bf.snap(AFTER, "account_service", "customer_profiles",
                                [bf.profile("account_1", ANA)])]), facts=f)
        d = dossier_for(f)
        self.assertIsNone(res(d, "account_1"))
        self.assertEqual(d["clock"]["late_records_excluded"], 1)

    def test_history_row_after_audit_is_ignored_and_an_earlier_one_is_used(self):
        f = users()
        load(bf.bundle([bf.snap(MANIFEST_LATE, "backup_restore_service", "operational_record_history", [
            bf.history(T2, "account_service", "account_1", "USER_PROFILE", "NOT_FOUND", ANA),
            bf.history(T2, "search_service", "search_1", "SEARCH_INDEX_ENTRY", "FOUND", ANA),
            bf.history(AFTER, "account_service", "account_1", "USER_PROFILE", "FOUND", ANA),
            bf.history(AFTER, "search_service", "search_2", "SEARCH_INDEX_ENTRY", "FOUND", ANA)])]),
            facts=f)
        d = dossier_for(f)
        self.assertEqual(res(d, "account_1")["state"], "ABSENT")
        self.assertEqual(res(d, "search_1")["state"], "PRESENT")
        self.assertIsNone(res(d, "search_2"))
        self.assertEqual(d["clock"]["late_records_excluded"], 2)

    def test_early_record_in_a_late_manifest_file_is_usable_but_not_citable(self):
        f = users()
        load(bf.bundle([bf.snap(T3, "security_service", "security_audit_events", [bf.audit("audit_record_1")])],
                       exported_at=MANIFEST_LATE), manifest=MANIFEST_LATE, facts=f)
        d = dossier_for(f)
        r = res(d, "audit_record_1")
        self.assertEqual((r["state"], r["link"]["method"]), ("PRESENT", "direct"))
        self.assertEqual(r["citation"]["rule_e"], [])
        self.assertIsNone(r["citation"]["pick"])
        self.assertEqual(d["clock"]["late_manifest_files_used"], ["evidence_01"])

    def test_bundle_that_is_cited_when_its_manifest_is_in_time(self):
        f = users()
        load(bf.bundle([bf.snap(AUDIT, "security_service", "security_audit_events", [bf.audit("audit_record_1")])]),
             manifest=AUDIT, facts=f)
        self.assertEqual(res(dossier_for(f), "audit_record_1")["citation"]["pick"], "evidence_01")

    def test_boundary_snapshot_at_audit_at_counts(self):
        f = users()
        load(bf.bundle([bf.snap(AUDIT, "search_service", "customer_search_documents",
                                [bf.search_doc("search_1", ANA)])]), facts=f)
        self.assertIsNotNone(res(dossier_for(f), "search_1"))


class AccountingTests(unittest.TestCase):
    def test_clean_bundle_has_everything_parsed(self):
        f, rep = load(bf.bundle([
            bf.snap(T3, "account_service", "customer_profiles", [bf.profile("account_1", ANA)]),
            bf.snap(T3, "search_service", "customer_search_documents", []),
            bf.snap(T3, "backup_restore_service", "operational_record_history", [
                bf.history(T2, "account_service", "account_2", "USER_PROFILE", "NOT_FOUND", ANA)])]))
        self.assertEqual((rep["meaningful"], rep["parsed"], rep["unparsed"]), (5, 5, 0))
        self.assertEqual(rep["observations"], 2)
        row = f.conn.execute("select adapter, meaningful, parsed, unparsed from parse_report").fetchone()
        self.assertEqual(row, ("snapshot_bundle", 5, 5, 0))
        self.assertEqual(adapters.summary(f.conn)["unparsed"], 0)

    def test_bad_snapshot_counts_itself_and_its_records(self):
        rec = [bf.profile("account_1", ANA), bf.profile("account_2", "b@x.com")]
        good = bf.snap(T3, "account_service", "customer_profiles", [bf.profile("account_9", ANA)])
        bads = [bf.snap(T3, "account_service", "mystery_collection", rec),
                bf.snap("not a time", "account_service", "customer_profiles", rec),
                bf.snap(T3, "", "customer_profiles", rec),
                bf.snap(T3, None, "customer_profiles", rec),
                bf.snap(T3, "account_service", "", rec)]
        for bad in bads:
            f, rep = load(bf.bundle([good, bad]))
            self.assertEqual((rep["meaningful"], rep["unparsed"]), (5, 3), bad)
            self.assertEqual([o["resource_id"] for o in obs(f.conn)], ["account_9"], bad)

    def test_non_list_records_and_missing_keys(self):
        good = bf.snap(T3, "account_service", "customer_profiles", [bf.profile("account_9", ANA)])
        for bad in ({"captured_at": T3, "data_store": "s", "collection": "customer_profiles",
                     "records": {"a": 1}},
                    {"captured_at": T3, "data_store": "s", "collection": "customer_profiles",
                     "records": None},
                    {"captured_at": T3, "data_store": "s", "collection": "customer_profiles"},
                    "not a snapshot", 7, None):
            _, rep = load(bf.bundle([good, bad]))
            self.assertEqual((rep["meaningful"], rep["unparsed"]), (3, 1), bad)

    def test_bad_records_count_one_each(self):
        good = bf.profile("account_9", ANA)
        for bad in ("a string", 7, None, ["x"], {"resource_id": 5, "email": ANA}, {"email": ANA},
                    {"resource_id": ""}, {"resource_id": None}):
            f, rep = load(bf.bundle([bf.snap(T3, "account_service", "customer_profiles", [good, bad])]))
            self.assertEqual((rep["meaningful"], rep["unparsed"]), (3, 1), bad)
            self.assertEqual(len(obs(f.conn)), 1, bad)

    def test_hostile_values_never_raise(self):
        weird = {"resource_id": "account_5", "customer_reference": ["user_1"], "full_name": {"a": 1},
                 "email": 123, "phone": None}
        deep = {"resource_id": "search_5", "document": {"email": ["x@y.com"], "display_name": 5}}
        for coll, system, rec in (("customer_profiles", "account_service", weird),
                                  ("customer_search_documents", "search_service", deep),
                                  ("customer_analytics_reports", "analytics_platform",
                                   {"resource_id": "analytics_5", "email": {"x": 1}}),
                                  ("support_conversations", "support_platform",
                                   {"resource_id": "support_5", "message_text": 12}),
                                  ("customer_search_documents", "search_service",
                                   {"resource_id": "search_6", "document": "string"})):
            f, rep = load(bf.bundle([bf.snap(T3, system, coll, [rec])]))
            self.assertEqual(rep["meaningful"], 2, rec)
            self.assertEqual(rep["parsed"] + rep["unparsed"], rep["meaningful"], rec)

    def test_reingest_is_idempotent(self):
        text = bf.bundle([bf.snap(T3, "account_service", "customer_profiles", [bf.profile("account_1", ANA)]),
                          bf.snap(T3, "search_service", "customer_search_documents", [])])
        f, first = load(text)
        counts = lambda: [f.conn.execute(f"select count(*) from {t}").fetchone()[0]
                          for t in ("observation", "subject_key", "capture_scope", "parse_report")]
        before = counts()
        snapshot_bundle.ingest(f.conn, "evidence_01", text)
        self.assertEqual(counts(), before)

    def test_other_files_are_left_alone_on_reingest(self):
        f, _ = load(single("customer_profiles", "account_service", bf.profile("account_1", ANA)))
        f.file("evidence_02", MANIFEST_LATE)
        snapshot_bundle.ingest(f.conn, "evidence_02", single(
            "customer_profiles", "account_service", bf.profile("account_2", ANA)))
        snapshot_bundle.ingest(f.conn, "evidence_02", single(
            "customer_profiles", "account_service", bf.profile("account_2", ANA)))
        self.assertEqual(sorted(o["resource_id"] for o in obs(f.conn)), ["account_1", "account_2"])

    def test_summary_flags_unparsed_files(self):
        f, _ = load(bf.bundle([bf.snap(T3, "account_service", "mystery", [{"resource_id": "x"}])]))
        summ = adapters.summary(f.conn)
        self.assertEqual((summ["unparsed"], summ["unparsed_files"]), (2, ["evidence_01"]))
        self.assertIn("WARNING", adapters.format_summary(summ, 0, 0))


if __name__ == "__main__":
    unittest.main()
