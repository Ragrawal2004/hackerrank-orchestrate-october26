import json
import tempfile
import unittest
from pathlib import Path

from ghost import adapters, dossier
from ghost.adapters import common, replica_sql, service_export, snapshot_bundle
from tests import bundle_fixture as bf
from tests import export_fixture as ef
from tests import sql_fixture as fx
from tests.fact_builder import Facts, case

FMT = "json_service_export"
AUDIT = "2026-09-15T12:00:00Z"
T1 = "2026-09-15T08:00:00Z"
T2 = "2026-09-15T09:30:00Z"
T3 = "2026-09-15T11:00:00Z"
AFTER = "2026-09-16T08:00:00Z"
MANIFEST = "2026-09-15T11:44:55Z"
MANIFEST_LATE = "2026-09-17T12:00:00Z"
ANA = "ana@example.com"
PHONE = "+1 555 0100"
HASH = "a" * 64


def load(text, file_id="evidence_01", manifest=MANIFEST, facts=None):
    f = facts or Facts()
    f.file(file_id, manifest)
    report = service_export.ingest(f.conn, file_id, text)
    return f, report


def users():
    return Facts().user("user_1", ANA, phone=PHONE, name="Ana Lima")


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


def one(conn, rid):
    got = obs(conn, rid)
    assert len(got) == 1, got
    return got[0]


def single(service, section, row, **kw):
    return ef.one(service, section, [row], **kw)


def dossier_for(f, audit=AUDIT):
    return dossier.build_dossier(f.conn, case(ANA, audit))


def res(d, rid):
    hits = [r for r in d["resources"] if r["resource_id"] == rid]
    return hits[0] if len(hits) == 1 else None


class DetectTests(unittest.TestCase):
    def good(self):
        return single("account_service", "profile_lookups", ef.profile_lookup("account_1", T2, ANA))

    def test_export_is_claimed_by_structure(self):
        self.assertEqual(service_export.detect(self.good()), FMT)

    def test_every_known_section_is_claimed(self):
        rows = {("account_service", "profile_lookups"): ef.profile_lookup("account_1", T2),
                ("analytics_platform", "profiles"): ef.analytics("analytics_1", T2),
                ("backup_service", "restore_entries"): ef.restore("backup_1", T2, False, ref="user_1"),
                ("export_service", "exports"): ef.export_job("export_1", T2, ANA),
                ("integration_service", "connectors"): ef.connector("data_flow_1", T2, True, ANA),
                ("search_service", "index_queries"): ef.search("search_1", T2, ANA),
                ("security_service", "audit_records"): ef.audit("audit_record_1", T2),
                ("support_platform", "tickets"): ef.ticket("support_1", T2, ANA)}
        for (svc, sec), row in rows.items():
            self.assertEqual(service_export.detect(single(svc, sec, row)), FMT, sec)

    def test_other_files_are_not_claimed(self):
        sql = fx.j_sql("2026-09-21T10:00:00Z", {"customer_data_primary": [],
                                                  "customer_data_read_replica": []})
        bundle = bf.bundle([bf.snap(T2, "account_service", "customer_profiles",
                                    [bf.profile("account_1", ANA)])])
        ledger = json.dumps({"exported_at": T2, "origin": "x", "record_count": 1,
                             "records": [{"recorded_at": T2, "system": "account_service",
                                          "resource_id": "account_1", "result": "FOUND"}]})
        for text in (sql, bundle, ledger, "[]", "", "not json {{{", '{"exported_at": "x"}',
                     '{"service_exports": []}', '{"service_exports": {}}',
                     '{"service_exports": "x"}', '{"service_exports": {"a": 1}}',
                     '{"service_exports": {"a": {"s": []}}}',
                     '{"service_exports": {"a": {"s": [1, 2]}}}',
                     '{"service_exports": {"a": {"s": [{"resource_id": "x"}]}}}',
                     '{"service_exports": {"a": {"s": [{"observed_at": "2026-01-01T00:00:00Z"}]}}}',
                     json.dumps({"profile_lookups": [], "observed_at": T2})):
            self.assertIsNone(service_export.detect(text), text)

    def test_other_adapters_do_not_claim_an_export(self):
        self.assertIsNone(snapshot_bundle.detect(self.good()))
        self.assertIsNone(replica_sql.detect(self.good()))

    def test_deeply_nested_json_does_not_raise(self):
        self.assertIsNone(service_export.detect("[" * 200000))

    def test_file_name_is_irrelevant(self):
        with tempfile.TemporaryDirectory() as d:
            (Path(d) / "evidence").mkdir()
            (Path(d) / "evidence" / "notes.txt").write_text(self.good(), encoding="utf-8")
            f = Facts()
            f.conn.execute("insert into evidence_file(file_id,path,origin,format,adapter,manifest_at)"
                           " values ('evidence_07','evidence/notes.txt','x',null,null,?)", (MANIFEST,))
            summary = adapters.load_evidence(f.conn, d)
            got = f.conn.execute("select format, adapter from evidence_file").fetchone()
        self.assertEqual(got, (FMT, "service_export"))
        self.assertEqual(summary["claimed"], ["evidence_07"])

    def test_registry_lists_the_adapter_without_double_claims(self):
        self.assertIn(service_export, adapters.ADAPTERS)
        self.assertEqual(adapters.select(self.good())[0].name, "service_export")
        self.assertEqual(adapters.select(bf.bundle([bf.snap(
            T2, "account_service", "customer_profiles", [bf.profile("account_1", ANA)])]))[0].name,
            "snapshot_bundle")

    def test_ingest_of_a_non_export_is_an_error(self):
        f = Facts().file("evidence_01", MANIFEST)
        for text in ('{"nope": 1}', "[1]", "", '{"service_exports": {}}'):
            with self.assertRaises(ValueError):
                service_export.ingest(f.conn, "evidence_01", text)

    def test_ingest_of_a_file_outside_the_manifest_is_an_error(self):
        with self.assertRaises(ValueError):
            service_export.ingest(Facts().conn, "evidence_99", self.good())


class MappingTests(unittest.TestCase):
    def check(self, service, section, row, collection, category, state):
        f, rep = load(single(service, section, row))
        o = one(f.conn, row["resource_id"])
        self.assertEqual((o["system_id"], o["category"], o["state"], o["state_basis"]),
                         (service, category, state, "explicit"))
        self.assertEqual(o["attrs"]["collection"], collection)
        self.assertEqual(o["attrs"]["section"], section)
        self.assertEqual(rep["unparsed"], 0)
        return f, o

    def test_account_present_and_absent(self):
        f, o = self.check("account_service", "profile_lookups",
                          ef.profile_lookup("account_1", T2, "ANA@Example.com ", customer_reference="user_1",
                                            phone=PHONE, full_name="Ana Lima", email_hash=HASH.upper()),
                          "customer_profiles", "USER_PROFILE", "PRESENT")
        self.assertEqual(o["attrs"]["email"], ANA)
        self.assertEqual(keys(f.conn, "account_1"),
                         sorted([("email", ANA), ("user_id", "user_1"), ("phone", PHONE),
                                 ("name", "Ana Lima"), ("email_hash", HASH)]))
        f, o = self.check("account_service", "profile_lookups", ef.profile_lookup("account_2", T2),
                          "customer_profiles", "USER_PROFILE", "ABSENT")
        self.assertEqual(keys(f.conn, "account_2"), [])

    def test_analytics_identifiable_and_anonymized(self):
        for field, value, kind in (("email", "ANA@example.com", "email"), ("phone", PHONE, "phone"),
                                   ("email_hash", HASH, "email_hash"),
                                   ("customer_reference", "user_1", "user_id")):
            f, _ = self.check("analytics_platform", "profiles",
                              ef.analytics("analytics_1", T2, **{field: value}),
                              "customer_analytics_reports", "ANALYTICS_PROFILE", "IDENTIFIABLE")
            self.assertEqual(len(keys(f.conn, "analytics_1")), 1, field)
            self.assertEqual(keys(f.conn, "analytics_1")[0][0], kind)
        f, o = self.check("analytics_platform", "profiles", ef.analytics("analytics_2", T2),
                          "customer_analytics_reports", "ANALYTICS_PROFILE", "ANONYMIZED")
        self.assertEqual(keys(f.conn, "analytics_2"), [])
        self.assertEqual(o["attrs"]["audience_cohort"], "general_shopper")

    def test_backup_restorable_and_blocked(self):
        f, o = self.check("backup_service", "restore_entries", ef.restore("backup_1", T2, True, email="ANA@example.com"),
                          "backup_catalog", "BACKUP_ARCHIVE", "RESTORABLE")
        self.assertEqual(o["attrs"]["restore_allowed"], True)
        self.assertEqual(keys(f.conn, "backup_1"), [("email", ANA)])
        f, o = self.check("backup_service", "restore_entries", ef.restore("backup_2", T2, False, ref="user_1"),
                          "backup_catalog", "BACKUP_ARCHIVE", "BLOCKED")
        self.assertEqual(keys(f.conn, "backup_2"), [("user_id", "user_1")])

    def test_export_is_present(self):
        f, _ = self.check("export_service", "exports",
                          ef.export_job("export_1", T2, "ANA@example.com", PHONE, "user_1"),
                          "customer_export_jobs", "USER_EXPORT", "PRESENT")
        self.assertEqual(keys(f.conn, "export_1"),
                         sorted([("email", ANA), ("phone", PHONE), ("user_id", "user_1")]))

    def test_connector_active_and_disabled(self):
        f, o = self.check("integration_service", "connectors", ef.connector("data_flow_1", T2, True, ANA),
                          "connector_configurations", "DATA_FLOW", "ACTIVE")
        self.assertEqual(o["attrs"]["enabled"], True)
        self.assertEqual(keys(f.conn, "data_flow_1"), [("email", ANA)])
        f, _ = self.check("integration_service", "connectors",
                          ef.connector("data_flow_2", T2, False, ref="user_1"),
                          "connector_configurations", "DATA_FLOW", "DISABLED")
        self.assertEqual(keys(f.conn, "data_flow_2"), [("user_id", "user_1")])

    def test_audit_record_keeps_its_retention(self):
        f, o = self.check("security_service", "audit_records",
                          ef.audit("audit_record_1", T2, "user_1", "2034-09-30T23:59:59Z"),
                          "security_audit_events", "SECURITY_AUDIT_LOG", "PRESENT")
        self.assertEqual(o["attrs"]["retention_until"], "2034-09-30T23:59:59Z")
        self.assertEqual(keys(f.conn, "audit_record_1"), [("user_id", "user_1")])

    def test_ticket_identifiable_and_redacted(self):
        f, _ = self.check("support_platform", "tickets",
                          ef.ticket("support_1", T2, email="ANA@example.com", name="Ana Lima", phone=PHONE),
                          "support_conversations", "SUPPORT_MESSAGE", "IDENTIFIABLE")
        self.assertEqual(keys(f.conn, "support_1"),
                         sorted([("email", ANA), ("name", "Ana Lima"), ("phone", PHONE)]))
        f, _ = self.check("support_platform", "tickets", ef.ticket("support_2", T2, ref="user_1"),
                          "support_conversations", "SUPPORT_MESSAGE", "REDACTED")
        self.assertEqual(keys(f.conn, "support_2"), [("user_id", "user_1")])

    def test_ticket_with_only_a_name_or_phone_is_identifiable(self):
        for kw in ({"name": "Ana Lima"}, {"phone": PHONE}):
            f, _ = load(single("support_platform", "tickets", ef.ticket("support_1", T2, ref="user_1", **kw)))
            self.assertEqual(one(f.conn, "support_1")["state"], "IDENTIFIABLE", kw)

    def test_states_use_the_same_words_as_the_shared_record_mapper(self):
        pairs = [("backup_catalog", {"restore_allowed": True, "subject_reference": "user_1"},
                  ("backup_service", "restore_entries", ef.restore("b1", T2, True, ref="user_1"))),
                 ("backup_catalog", {"restore_allowed": False, "subject_reference": "user_1"},
                  ("backup_service", "restore_entries", ef.restore("b2", T2, False, ref="user_1"))),
                 ("connector_configurations", {"enabled": True, "subject_selector": ANA},
                  ("integration_service", "connectors", ef.connector("d1", T2, True, ANA))),
                 ("connector_configurations", {"enabled": False, "subject_selector": ANA},
                  ("integration_service", "connectors", ef.connector("d2", T2, False, ANA))),
                 ("customer_analytics_reports", {"email": ANA},
                  ("analytics_platform", "profiles", ef.analytics("a1", T2, email=ANA))),
                 ("customer_analytics_reports", {},
                  ("analytics_platform", "profiles", ef.analytics("a2", T2))),
                 ("support_conversations", {"message_text": f"mail {ANA}"},
                  ("support_platform", "tickets", ef.ticket("s1", T2, email=ANA))),
                 ("support_conversations", {"message_text": "none"},
                  ("support_platform", "tickets", ef.ticket("s2", T2, ref="user_1")))]
        for collection, record, (svc, sec, row) in pairs:
            want = common.record(collection, record)[0]
            f, _ = load(single(svc, sec, row))
            self.assertEqual(one(f.conn, row["resource_id"])["state"], want, row["resource_id"])

    def test_locator_names_the_service_section_and_row(self):
        text = ef.one("account_service", "profile_lookups",
                      [ef.profile_lookup("account_1", T2, ANA), ef.profile_lookup("account_2", T2)])
        f, _ = load(text)
        self.assertEqual(one(f.conn, "account_2")["locator"],
                         "service_exports.account_service.profile_lookups[1]")

    def test_envelope_facts_are_kept_in_attrs(self):
        f, _ = load(single("account_service", "profile_lookups", ef.profile_lookup("account_1", T2),
                           region="eu", source="gw"))
        a = one(f.conn, "account_1")["attrs"]
        self.assertEqual((a["region"], a["source"], a["exported_at"], a["observed_at"]),
                         ("eu", "gw", MANIFEST, T2))

    def test_no_join_keys_no_scope_and_format_is_written_back(self):
        f, rep = load(ef.envelope({"account_service": {"profile_lookups": [
            ef.profile_lookup("account_1", T2, ANA), ef.profile_lookup("account_2", T2)]},
            "search_service": {"index_queries": [ef.search("search_1", T2, ANA, docs=[], total=0)]}}))
        self.assertEqual(f.conn.execute("select count(*) from join_key").fetchone()[0], 0)
        self.assertEqual(f.conn.execute("select count(*) from capture_scope").fetchone()[0], 0)
        self.assertEqual(rep["join_keys"], 0)
        self.assertEqual(f.conn.execute("select format, adapter from evidence_file").fetchone(),
                         (FMT, "service_export"))


class SearchQueryKeyTests(unittest.TestCase):
    """The query is classified by shape (condition 1): email and phone keep their kind, plain text
    becomes a name key, which never links on its own."""

    def keys_of(self, query, **doc):
        f, _ = load(single("search_service", "index_queries", ef.search(
            "search_1", T2, query, docs=[{"customer_reference": None, "display_name": None,
                                          "email": None, "email_hash": None, "phone": None,
                                          "resource_id": "search_1", **doc}])))
        return f, keys(f.conn, "search_1")

    def test_email_query_is_an_email_key(self):
        _, got = self.keys_of(" ANA@Example.com ")
        self.assertEqual(got, [("email", ANA)])

    def test_phone_query_is_a_phone_key(self):
        _, got = self.keys_of(PHONE)
        self.assertEqual(got, [("phone", PHONE)])

    def test_plain_text_query_is_a_name_key(self):
        _, got = self.keys_of("Ana Lima")
        self.assertEqual(got, [("name", "Ana Lima")])

    def test_hash_and_user_queries_keep_their_kind(self):
        self.assertEqual(self.keys_of(HASH.upper())[1], [("email_hash", HASH)])
        self.assertEqual(self.keys_of("user_1")[1], [("user_id", "user_1")])

    def test_hit_document_fields_add_keys(self):
        _, got = self.keys_of("Ana Lima", email="ANA@example.com", phone=PHONE,
                              customer_reference="user_1", display_name="A. Lima", email_hash=HASH)
        self.assertEqual(got, sorted([("name", "Ana Lima"), ("name", "A. Lima"), ("email", ANA),
                                      ("phone", PHONE), ("user_id", "user_1"),
                                      ("email_hash", HASH)]))

    def test_email_query_links_the_search_entry(self):
        f = users()
        load(single("search_service", "index_queries", ef.search("search_1", T2, ANA)), facts=f)
        self.assertEqual(res(dossier_for(f), "search_1")["link"]["method"], "direct")

    def test_phone_query_links_the_search_entry(self):
        f = users()
        load(single("search_service", "index_queries", ef.search(
            "search_1", T2, PHONE, docs=[{"customer_reference": None, "display_name": None, "email": None,
                                          "email_hash": None, "phone": None,
                                          "resource_id": "search_1"}])), facts=f)
        self.assertEqual(res(dossier_for(f), "search_1")["link"]["method"], "direct")

    def test_plain_text_query_never_links_alone(self):
        f = users()
        load(single("search_service", "index_queries", ef.search(
            "search_1", T2, "Ana Lima", docs=[{"customer_reference": None, "display_name": None,
                                               "email": None, "email_hash": None, "phone": None,
                                               "resource_id": "search_1"}])), facts=f)
        d = dossier_for(f)
        self.assertIsNone(res(d, "search_1"))
        self.assertIn("name_only_match", [e["type"] for e in d["events"]])

    def test_zero_hits_is_an_explicit_absence_tied_to_the_query(self):
        f = users()
        load(single("search_service", "index_queries", ef.search("search_1", T2, ANA, docs=[], total=0)),
             facts=f)
        o = one(f.conn, "search_1")
        self.assertEqual((o["state"], o["state_basis"]), ("ABSENT", "explicit"))
        self.assertEqual(keys(f.conn, "search_1"), [("email", ANA)])
        self.assertEqual(res(dossier_for(f), "search_1")["state"], "ABSENT")

    def test_hits_with_documents_are_present(self):
        f, _ = load(single("search_service", "index_queries", ef.search("search_1", T2, ANA)))
        self.assertEqual(one(f.conn, "search_1")["state"], "PRESENT")


class ClockTests(unittest.TestCase):
    def test_record_at_is_observed_at_not_the_manifest(self):
        f, _ = load(ef.one("account_service", "profile_lookups",
                           [ef.profile_lookup("account_1", T1, ANA), ef.profile_lookup("account_2", T3)]))
        self.assertEqual(one(f.conn, "account_1")["record_at"], T1)
        self.assertEqual(one(f.conn, "account_2")["record_at"], T3)

    def test_offsets_compare_as_instants(self):
        f, rep = load(single("account_service", "profile_lookups",
                             ef.profile_lookup("account_1", "2026-09-15T13:00:00+02:00", ANA)))
        self.assertEqual(rep["notes"], [])

    def test_late_record_is_stored_with_its_true_time_and_noted(self):
        f, rep = load(single("account_service", "profile_lookups", ef.profile_lookup("account_1", AFTER, ANA),
                             exported_at="2026-09-20T00:00:00Z"), manifest="2026-09-15T00:00:00Z")
        types = {n["type"] for n in rep["notes"]}
        self.assertIn("capture_time_mismatch", types)
        self.assertIn("record_after_manifest", types)
        self.assertEqual(rep["unparsed"], 0)
        self.assertEqual(one(f.conn, "account_1")["record_at"], AFTER)

    def test_matching_header_gives_no_notes(self):
        _, rep = load(single("account_service", "profile_lookups", ef.profile_lookup("account_1", T2, ANA)))
        self.assertEqual(rep["notes"], [])

    def test_the_same_resource_in_two_files_keeps_both(self):
        f = users()
        f.file("evidence_02", MANIFEST)
        service_export.ingest(f.conn, "evidence_02", single(
            "account_service", "profile_lookups", ef.profile_lookup("account_1", T3, ANA)))
        load(single("account_service", "profile_lookups", ef.profile_lookup("account_1", T1, ANA)), facts=f)
        self.assertEqual([o["record_at"] for o in obs(f.conn, "account_1")], [T3, T1])


class TrapTests(unittest.TestCase):
    def test_later_absence_beats_earlier_presence_and_a_post_audit_row_is_ignored(self):
        f = users()
        load(ef.one("account_service", "profile_lookups", [
            ef.profile_lookup("account_1", T1, ANA), ef.profile_lookup("account_1", T3),
            ef.profile_lookup("account_1", AFTER, ANA)], exported_at=MANIFEST_LATE),
            manifest=MANIFEST_LATE, facts=f)
        # the 404 carries no identifier, so link the account through the present rows
        d = dossier_for(f)
        r = res(d, "account_1")
        self.assertEqual(r["state"], "ABSENT")
        self.assertGreaterEqual(d["clock"]["late_records_excluded"], 1)

    def test_account_present_only_after_audit_is_not_linked(self):
        f = users()
        load(single("account_service", "profile_lookups", ef.profile_lookup("account_1", AFTER, ANA),
                    exported_at=MANIFEST_LATE), manifest=MANIFEST_LATE, facts=f)
        d = dossier_for(f)
        self.assertIsNone(res(d, "account_1"))
        self.assertEqual(d["clock"]["late_records_excluded"], 1)

    def test_a_404_without_an_identifier_does_not_link_on_its_own(self):
        f = users()
        load(single("account_service", "profile_lookups", ef.profile_lookup("account_1", T2)), facts=f)
        self.assertIsNone(res(dossier_for(f), "account_1"))

    def test_early_record_in_a_late_manifest_file_is_usable_but_not_citable(self):
        f = users()
        load(single("security_service", "audit_records", ef.audit("audit_record_1", T2),
                    exported_at=MANIFEST_LATE), manifest=MANIFEST_LATE, facts=f)
        d = dossier_for(f)
        r = res(d, "audit_record_1")
        self.assertEqual((r["state"], r["link"]["method"]), ("PRESENT", "direct"))
        self.assertEqual(r["citation"]["rule_e"], [])
        self.assertIsNone(r["citation"]["pick"])
        self.assertEqual(d["clock"]["late_manifest_files_used"], ["evidence_01"])

    def test_export_is_cited_when_its_manifest_is_in_time(self):
        f = users()
        load(single("security_service", "audit_records", ef.audit("audit_record_1", T2)), facts=f)
        self.assertEqual(res(dossier_for(f), "audit_record_1")["citation"]["pick"], "evidence_01")

    def test_boundary_observation_at_audit_at_counts(self):
        f = users()
        load(single("search_service", "index_queries", ef.search("search_1", AUDIT, ANA)),
             manifest=AUDIT, facts=f)
        self.assertIsNotNone(res(dossier_for(f), "search_1"))

    def test_every_gold_style_state_reaches_the_dossier(self):
        f = users()
        load(ef.envelope({
            "analytics_platform": {"profiles": [ef.analytics("analytics_1", T2, email=ANA)]},
            "backup_service": {"restore_entries": [ef.restore("backup_1", T2, True, ref="user_1")]},
            "integration_service": {"connectors": [ef.connector("data_flow_1", T2, True, ANA)]},
            "support_platform": {"tickets": [ef.ticket("support_1", T2, email=ANA)]}}), facts=f)
        d = dossier_for(f)
        got = {r["resource_id"]: r["state"] for r in d["resources"]}
        self.assertEqual(got, {"analytics_1": "IDENTIFIABLE", "backup_1": "RESTORABLE",
                               "data_flow_1": "ACTIVE", "support_1": "IDENTIFIABLE"})


class AccountingTests(unittest.TestCase):
    def full(self):
        return ef.envelope({
            "account_service": {"profile_lookups": [ef.profile_lookup("account_1", T2, ANA),
                                                    ef.profile_lookup("account_2", T2)]},
            "analytics_platform": {"profiles": [ef.analytics("analytics_1", T2)]},
            "backup_service": {"restore_entries": [ef.restore("backup_1", T2, False, ref="user_1")]},
            "export_service": {"exports": [ef.export_job("export_1", T2, ANA)]},
            "integration_service": {"connectors": [ef.connector("data_flow_1", T2, False, ref="user_1")]},
            "search_service": {"index_queries": [ef.search("search_1", T2, ANA)]},
            "security_service": {"audit_records": [ef.audit("audit_record_1", T2)]},
            "support_platform": {"tickets": [ef.ticket("support_1", T2, ref="user_1")]}})

    def test_clean_export_has_everything_parsed(self):
        f, rep = load(self.full())
        self.assertEqual((rep["meaningful"], rep["parsed"], rep["unparsed"]), (9, 9, 0))
        self.assertEqual(rep["observations"], 9)
        row = f.conn.execute("select adapter, meaningful, parsed, unparsed from parse_report").fetchone()
        self.assertEqual(row, ("service_export", 9, 9, 0))
        self.assertEqual(adapters.summary(f.conn)["unparsed"], 0)

    def test_empty_known_section_is_nothing_to_parse(self):
        _, rep = load(ef.envelope({"account_service": {"profile_lookups": []},
                                   "search_service": {"index_queries": [ef.search("search_1", T2, ANA)]}}))
        self.assertEqual((rep["meaningful"], rep["unparsed"]), (1, 0))

    def test_unknown_service_or_section_counts_its_rows(self):
        good = ef.search("search_1", T2, ANA)
        text = ef.envelope({"search_service": {"index_queries": [good],
                                               "mystery": [{"resource_id": "x"}, {"resource_id": "y"}]},
                            "unknown_service": {"profile_lookups": [ef.profile_lookup("account_9", T2, ANA)]}})
        f, rep = load(text)
        self.assertEqual((rep["meaningful"], rep["unparsed"]), (4, 3))
        self.assertEqual([o["resource_id"] for o in obs(f.conn)], ["search_1"])
        self.assertEqual({n["type"] for n in rep["notes"]}, {"section_unparsed"})

    def test_malformed_containers_count_one_each(self):
        good = ef.search("search_1", T2, ANA)
        for bad_services in ({"search_service": {"index_queries": [good], "mystery": {"a": 1}}},
                             {"search_service": {"index_queries": [good]}, "account_service": [1]},
                             {"search_service": {"index_queries": [good]}, "account_service": None},
                             {"search_service": {"index_queries": [good]}, "other": "text"},
                             {"search_service": {"index_queries": [good], "mystery": None}}):
            _, rep = load(ef.envelope(bad_services))
            self.assertEqual((rep["meaningful"], rep["unparsed"]), (2, 1), bad_services)

    def test_bad_rows_count_one_each(self):
        good = ef.profile_lookup("account_9", T2, ANA)
        bads = ["a string", 7, None, ["x"], {"resource_id": 5, "observed_at": T2, "status_code": 404, "profile": None},
                {"observed_at": T2, "status_code": 404, "profile": None},
                {"resource_id": "", "observed_at": T2, "status_code": 404, "profile": None},
                {"resource_id": "account_1", "status_code": 404, "profile": None},
                {"resource_id": "account_1", "observed_at": "not a time", "status_code": 404, "profile": None},
                {"resource_id": "account_1", "observed_at": None, "status_code": 404, "profile": None},
                {"resource_id": "account_1", "observed_at": 5, "status_code": 404, "profile": None},
                {"resource_id": "account_1", "observed_at": T2, "status_code": 404,
                 "profile": {"email": ANA}},  # a 404 that still carries a profile
                {"resource_id": "account_1", "observed_at": T2, "status_code": 200, "profile": None},
                {"resource_id": "account_1", "observed_at": T2, "status_code": 500, "profile": None},
                {"resource_id": "account_1", "observed_at": T2, "status_code": "404", "profile": None},
                {"resource_id": "account_1", "observed_at": T2, "status_code": True, "profile": None}]
        for bad in bads:
            f, rep = load(ef.one("account_service", "profile_lookups", [good, bad]))
            self.assertEqual((rep["meaningful"], rep["unparsed"]), (2, 1), bad)
            self.assertEqual(len(obs(f.conn)), 1, bad)

    def test_bad_values_in_other_sections_are_unparsed(self):
        cases = [("backup_service", "restore_entries", {**ef.restore("b", T2, True, ref="user_1"), "restore_allowed": "yes"}),
                 ("backup_service", "restore_entries", {**ef.restore("b", T2, True, ref="user_1"), "restore_allowed": None}),
                 ("integration_service", "connectors", {**ef.connector("d", T2, True, ANA), "enabled": 1}),
                 ("integration_service", "connectors", {**ef.connector("d", T2, True, ANA), "enabled": None}),
                 ("search_service", "index_queries", {**ef.search("s", T2, ANA), "hits": "x"}),
                 ("search_service", "index_queries", {**ef.search("s", T2, ANA), "hits": {"documents": [], "total": 2}}),
                 ("search_service", "index_queries", ef.search("s", T2, ANA, total=3)),
                 ("search_service", "index_queries", ef.search("s", T2, ANA, doc_rid="search_other")),
                 ("search_service", "index_queries", {**ef.search("s", T2, ANA), "hits": {"documents": "x", "total": 1}}),
                 ("search_service", "index_queries", {**ef.search("s", T2, ANA), "hits": {"documents": ["x"], "total": 1}}),
                 ("search_service", "index_queries", {**ef.search("s", T2, ANA), "hits": {"documents": [], "total": "0"}}),
                 ("search_service", "index_queries", {**ef.search("s", T2, ANA), "hits": {"documents": [], "total": -1}}),
                 ("security_service", "audit_records", {**ef.audit("a", T2), "retention_until": None}),
                 ("security_service", "audit_records", {**ef.audit("a", T2), "retention_until": "soon"}),
                 ("security_service", "audit_records", {**ef.audit("a", T2), "subject_reference": None})]
        for svc, sec, bad in cases:
            _, rep = load(ef.one(svc, sec, [bad]))
            self.assertEqual((rep["meaningful"], rep["unparsed"]), (1, 1), bad)

    def test_hostile_values_never_raise(self):
        weird = [("account_service", "profile_lookups", {"resource_id": "account_5", "observed_at": T2,
                                                          "status_code": 200,
                                                          "profile": {"email": 123, "full_name": {"a": 1},
                                                                      "phone": ["x"]}}),
                 ("analytics_platform", "profiles", ef.analytics("analytics_5", T2, email={"x": 1})),
                 ("support_platform", "tickets", ef.ticket("support_5", T2, email=12)),
                 ("export_service", "exports", ef.export_job("export_5", T2, owner=["x@y.com"])),
                 ("search_service", "index_queries", {**ef.search("search_5", T2, 5), "query": 5})]
        for svc, sec, row in weird:
            f, rep = load(ef.one(svc, sec, [row]))
            self.assertEqual(rep["meaningful"], 1, row)
            self.assertEqual(rep["parsed"] + rep["unparsed"], rep["meaningful"], row)

    def test_reingest_is_idempotent(self):
        text = self.full()
        f, _ = load(text)
        counts = lambda: [f.conn.execute(f"select count(*) from {t}").fetchone()[0]
                          for t in ("observation", "subject_key", "capture_scope", "parse_report")]
        before = counts()
        service_export.ingest(f.conn, "evidence_01", text)
        self.assertEqual(counts(), before)

    def test_other_files_are_left_alone_on_reingest(self):
        f, _ = load(single("account_service", "profile_lookups", ef.profile_lookup("account_1", T2, ANA)))
        f.file("evidence_02", MANIFEST)
        text2 = single("account_service", "profile_lookups", ef.profile_lookup("account_2", T2, ANA))
        service_export.ingest(f.conn, "evidence_02", text2)
        service_export.ingest(f.conn, "evidence_02", text2)
        self.assertEqual(sorted(o["resource_id"] for o in obs(f.conn)), ["account_1", "account_2"])

    def test_summary_flags_unparsed_files(self):
        f, _ = load(ef.envelope({"search_service": {"index_queries": [ef.search("search_1", T2, ANA)],
                                                    "mystery": [{"resource_id": "x"}]}}))
        summ = adapters.summary(f.conn)
        self.assertEqual((summ["unparsed"], summ["unparsed_files"]), (1, ["evidence_01"]))
        self.assertIn("WARNING", adapters.format_summary(summ, 0, 0))


if __name__ == "__main__":
    unittest.main()
