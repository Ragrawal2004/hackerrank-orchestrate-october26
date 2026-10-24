import json
import tempfile
import unittest
from pathlib import Path

from ghost import adapters, dossier, store
from ghost.adapters import replica_sql
from tests import sql_fixture as fx
from tests.fact_builder import Facts, case, sha

CAP = "2026-09-21T10:00:00Z"
EARLY = "2026-09-21T09:00:00Z"
P, R = "customer_data_primary", "customer_data_read_replica"
JSON_FMT, TYPED_FMT = "sql_replica_json", "sql_replica_typed"


def profile(email="ghost@example.com", user="user_9", name="Ghost Person"):
    return {"customer_reference": user, "full_name": name, "email": email,
            "phone": "+1 555 0100", "account_status": "ACTIVE", "signup_channel": "referral"}


def load(sql, file_id="evidence_01", manifest=CAP, facts=None):
    f = facts or Facts()
    f.file(file_id, manifest)
    report = replica_sql.ingest(f.conn, file_id, sql)
    return f, report


def rows(conn, rid=None):
    q = "select obs_id, file_id, locator, record_at, system_id, resource_id, category, state, " \
        "state_basis, attrs_json from observation"
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


def one_table(*rs, captured=CAP):
    return fx.j_sql(captured, {P: list(rs), R: list(rs)})


def single(conn, rid):
    got = [r for r in rows(conn, rid) if r["attrs"]["table"] == R]
    assert len(got) == 1, got
    return got[0]


class DetectTests(unittest.TestCase):
    def test_record_json_store_and_typed_profiles(self):
        j = fx.j_sql(CAP, {P: [], R: []})
        t = fx.t_sql(CAP, [], [])
        self.assertEqual(replica_sql.detect(j), JSON_FMT)
        self.assertEqual(replica_sql.detect(t), TYPED_FMT)

    def test_inventory_shape_is_not_claimed(self):
        self.assertIsNone(replica_sql.detect(fx.inventory_sql(CAP)))

    def test_structure_not_header_decides(self):
        j = fx.j_sql(CAP, {P: [], R: []}, topology="something_else")
        self.assertEqual(replica_sql.detect(j), JSON_FMT)

    def test_lookalike_table_names_with_wrong_columns_rejected(self):
        sql = fx.header(CAP, "read_replica") + (
            "CREATE TABLE customer_data_primary (resource_id TEXT, payload TEXT);\n"
            "CREATE TABLE customer_data_read_replica (resource_id TEXT, payload TEXT);\n")
        self.assertIsNone(replica_sql.detect(sql))

    def test_primary_without_any_replica_is_not_drift_evidence(self):
        self.assertIsNone(replica_sql.detect(fx.j_sql(CAP, {P: []})))

    def test_garbage_is_not_claimed(self):
        self.assertIsNone(replica_sql.detect("not sql at all {{{"))

    def test_file_name_is_irrelevant(self):
        with tempfile.TemporaryDirectory() as d:
            (Path(d) / "evidence").mkdir()
            (Path(d) / "evidence" / "notes.txt").write_text(one_table(), encoding="utf-8")
            f = Facts()
            f.conn.execute("insert into evidence_file(file_id,path,origin,format,adapter,manifest_at)"
                           " values ('evidence_07','evidence/notes.txt','x',null,null,?)", (CAP,))
            adapters.load_evidence(f.conn, d)
            got = f.conn.execute("select format, adapter from evidence_file").fetchone()
        self.assertEqual(got, (JSON_FMT, "replica_sql"))

    def test_unclaimed_file_keeps_null_format_and_is_counted(self):
        with tempfile.TemporaryDirectory() as d:
            (Path(d) / "a.txt").write_text("hello", encoding="utf-8")
            f = Facts()
            f.conn.execute("insert into evidence_file(file_id,path,origin,format,adapter,manifest_at)"
                           " values ('evidence_08','a.txt','x',null,null,?)", (CAP,))
            summary = adapters.load_evidence(f.conn, d)
            fmt = f.conn.execute("select format from evidence_file").fetchone()[0]
        self.assertIsNone(fmt)
        self.assertEqual(summary["unclaimed"], ["evidence_08"])

    def test_two_claimants_is_an_error(self):
        class A:
            name = "a"
            def detect(self, text): return "x"
        class B(A):
            name = "b"
        with self.assertRaises(adapters.AmbiguousAdapter):
            adapters.select("anything", [A(), B()])


class RolesAndGhostTests(unittest.TestCase):
    def regional(self):
        shared = fx.row("search_1", "search_service", "customer_search_documents",
                        {"index_name": "i", "document": {"display_name": "A B", "email": "a@x.com",
                                                          "customer_reference": "user_1"}}, CAP)
        ghost = fx.row("account_77", "account_service", "customer_profiles", profile(), CAP)
        return fx.j_sql(CAP, {P: [shared], "customer_data_replica_ap_south": [shared],
                              "customer_data_replica_eu_west": [shared, ghost]},
                        topology="regional_replicas")

    def test_ghost_is_replica_only_with_region_and_logical_system(self):
        f, _ = load(self.regional())
        g = rows(f.conn, "account_77")
        self.assertEqual(len(g), 1)
        self.assertEqual(g[0]["system_id"], "account_service")  # A10: logical, never the table
        self.assertEqual(g[0]["attrs"]["role"], "replica")
        self.assertEqual(g[0]["attrs"]["region"], "eu_west")
        self.assertEqual(g[0]["attrs"]["drift"], "replica_only")
        self.assertEqual(g[0]["category"], "USER_PROFILE")
        self.assertEqual(g[0]["state"], "PRESENT")
        self.assertEqual(g[0]["state_basis"], "explicit")

    def test_shared_row_is_seen_in_every_table(self):
        f, _ = load(self.regional())
        got = sorted((r["attrs"]["role"], r["attrs"].get("region")) for r in rows(f.conn, "search_1"))
        self.assertEqual(got, [("primary", None), ("replica", "ap_south"), ("replica", "eu_west")])
        self.assertTrue(all("drift" not in r["attrs"] or r["attrs"]["drift"] is None
                            for r in rows(f.conn, "search_1")))

    def test_read_replica_has_no_region(self):
        f, _ = load(one_table(fx.row("account_5", "account_service", "customer_profiles",
                                      profile(), CAP)))
        r = single(f.conn, "account_5")
        self.assertIsNone(r["attrs"].get("region"))
        self.assertEqual(r["attrs"]["topology"], "read_replica")

    def test_typed_failover_ghost_and_decoys(self):
        prim = [("account_8741", "user_8741", "K E", "k@x.com", "+1", "ACTIVE", "2026-09-02T22:45:00Z")]
        rep = [("account_005", "user_05", "Leila H", "leila@x.com", "+2", "ACTIVE",
                "2026-09-02T22:45:00Z")] + prim
        f, rep_ = load(fx.t_sql("2026-09-02T23:00:00Z", prim, rep), manifest="2026-09-02T23:00:00Z")
        g = rows(f.conn, "account_005")
        self.assertEqual([(r["system_id"], r["attrs"]["drift"], r["category"]) for r in g],
                         [("account_service", "replica_only", "USER_PROFILE")])
        self.assertEqual(len(rows(f.conn, "account_8741")), 2)
        self.assertEqual(f.conn.execute("select format from evidence_file").fetchone()[0], TYPED_FMT)
        self.assertEqual(rep_["unparsed"], 0)

    def test_record_at_is_capture_time_and_row_clock_is_kept(self):
        r = fx.row("account_5", "account_service", "customer_profiles", profile(), EARLY)
        f, _ = load(fx.j_sql(CAP, {P: [], R: [r]}))
        o = single(f.conn, "account_5")
        self.assertEqual(o["record_at"], CAP)
        self.assertEqual(o["attrs"]["row_updated_at"], EARLY)

    def test_row_newer_than_capture_is_reported_not_trusted(self):
        r = fx.row("account_5", "account_service", "customer_profiles", profile(),
                   "2026-09-21T11:00:00Z")
        f, rep = load(fx.j_sql(CAP, {P: [], R: [r]}))
        self.assertEqual(rep["unparsed"], 0)
        self.assertIn("row_after_capture", [n["type"] for n in rep["notes"]])

    def test_header_capture_time_disagreeing_with_manifest_is_noted(self):
        f, rep = load(fx.j_sql("2026-09-21T10:30:00Z", {P: [], R: []}))
        self.assertIn("capture_time_mismatch", [n["type"] for n in rep["notes"]])
        # the manifest stays the clock for the file
        f2, _ = load(fx.j_sql("2026-09-21T10:30:00Z",
                              {P: [], R: [fx.row("account_5", "account_service",
                                                  "customer_profiles", profile(), EARLY)]}))
        self.assertEqual(single(f2.conn, "account_5")["record_at"], CAP)


class StateTests(unittest.TestCase):
    def one(self, collection, system, record, rid="x_1"):
        f, rep = load(one_table(fx.row(rid, system, collection, record, CAP)))
        self.assertEqual(rep["unparsed"], 0, rep)
        return single(f.conn, rid)

    def test_connector_enabled_and_disabled(self):
        base = {"connector_name": "c", "destination": "d", "subject_selector": "user_1",
                "mode": "continuous_sync"}
        on = self.one("connector_configurations", "integration_service", {**base, "enabled": True})
        off = self.one("connector_configurations", "integration_service", {**base, "enabled": False})
        self.assertEqual((on["state"], on["category"]), ("ACTIVE", "DATA_FLOW"))
        self.assertEqual(off["state"], "DISABLED")
        self.assertIs(on["attrs"]["enabled"], True)

    def test_backup_restore_allowed(self):
        base = {"archive_path": "s3://b/x", "subject_reference": "user_1", "encryption": "AES"}
        yes = self.one("backup_catalog", "backup_service", {**base, "restore_allowed": True})
        no = self.one("backup_catalog", "backup_service", {**base, "restore_allowed": False})
        self.assertEqual((yes["state"], yes["category"]), ("RESTORABLE", "BACKUP_ARCHIVE"))
        self.assertEqual(no["state"], "BLOCKED")

    def test_analytics_all_null_is_anonymized_partial_is_not(self):
        nulls = {"user_id": None, "email": None, "phone": None, "email_hash": None}
        a = self.one("customer_analytics_reports", "analytics_platform", nulls)
        b = self.one("customer_analytics_reports", "analytics_platform",
                     {**nulls, "phone": "+1 555 0101"})
        c = self.one("customer_analytics_reports", "analytics_platform",
                     {**nulls, "user_id": "user_4"})
        self.assertEqual((a["state"], a["category"]), ("ANONYMIZED", "ANALYTICS_PROFILE"))
        self.assertEqual(b["state"], "IDENTIFIABLE")
        self.assertEqual(c["state"], "IDENTIFIABLE")
        self.assertIsNone(a["attrs"]["email"])

    def test_support_redacted_vs_identifiable(self):
        red = self.one("support_conversations", "support_platform",
                       {"ticket_id": "CS-1", "subject_reference": "user_3", "channel": "email",
                        "message_text": "[REDACTED]"})
        live = self.one("support_conversations", "support_platform",
                        {"ticket_id": "CS-1", "subject_reference": "user_3", "channel": "email",
                         "message_text": "Please write to Mansour.Y@Gmail.com about order 4."})
        self.assertEqual((red["state"], red["category"]), ("REDACTED", "SUPPORT_MESSAGE"))
        self.assertEqual(live["state"], "IDENTIFIABLE")
        self.assertEqual(live["attrs"]["email"], "mansour.y@gmail.com")
        self.assertIsNone(red["attrs"]["email"])
        self.assertNotIn("shared_context", live["attrs"])  # the data does not state it

    def test_tombstone_any_identifier_left_is_exposed(self):
        full_null = {"subject_reference": "user_7", "name": None, "email": None, "phone": None,
                     "email_hash": None, "account_status": "DELETED"}
        ok = self.one("deleted_customer_references", "identity_service", full_null, "tombstone_1")
        phone_null = self.one("deleted_customer_references", "identity_service",
                              {**full_null, "name": "C M", "email": "c@x.com",
                               "email_hash": "a" * 64}, "tombstone_2")
        email_null = self.one("deleted_customer_references", "identity_service",
                              {**full_null, "name": "M C", "phone": "+55 61 1",
                               "email_hash": "b" * 64}, "tombstone_3")
        self.assertEqual((ok["state"], ok["category"]), ("REDACTED", "ACCOUNT_TOMBSTONE"))
        self.assertEqual(ok["attrs"]["subject_reference"], "user_7")
        self.assertEqual(phone_null["state"], "IDENTIFIABLE")
        self.assertEqual(email_null["state"], "IDENTIFIABLE")

    def test_legal_audit_export_search_attachment_profile(self):
        legal = self.one("retained_order_records", "legal_service",
                         {"subject_reference": "user_2", "order_reference": "ORD-1",
                          "retention_reason": "tax", "retained_until": "2033-12-31T23:59:59Z"})
        audit = self.one("security_audit_events", "security_service",
                         {"subject_reference": "user_2", "event_name": "e", "actor_type": "a",
                          "integrity_seal": "sha256:0"})
        export = self.one("customer_export_jobs", "export_service",
                          {"owner_email": "o@x.com", "file_name": "f.zip", "created_at": CAP,
                           "expires_at": CAP, "download_available": True,
                           "included_sections": ["profile"]})
        search = self.one("customer_search_documents", "search_service",
                          {"index_name": "i", "document": {"display_name": "N N", "email": "n@x.com",
                                                            "customer_reference": "user_2"}})
        att = self.one("ticket_attachments", "support_platform",
                       {"ticket_id": "CS-2", "file_name": "f.pdf", "uploaded_by": "u@x.com",
                        "storage_class": "ticket_attachment"})
        prof = self.one("customer_profiles", "account_service", profile())
        self.assertEqual((legal["state"], legal["category"]), ("PRESENT", "LEGAL_HOLD_RECORD"))
        self.assertEqual(legal["attrs"]["hold_until"], "2033-12-31T23:59:59Z")
        self.assertEqual((audit["state"], audit["category"]), ("PRESENT", "SECURITY_AUDIT_LOG"))
        self.assertNotIn("retention_until", audit["attrs"])
        self.assertEqual((export["state"], export["category"]), ("PRESENT", "USER_EXPORT"))
        self.assertEqual((search["state"], search["category"]), ("PRESENT", "SEARCH_INDEX_ENTRY"))
        self.assertEqual((att["state"], att["category"]), ("PRESENT", "SUPPORT_ATTACHMENT"))
        self.assertNotIn("subject_only", att["attrs"])
        self.assertEqual(prof["attrs"]["name"], "Ghost Person")
        self.assertEqual(prof["attrs"]["email"], "ghost@example.com")


class SubjectKeyTests(unittest.TestCase):
    def setUp(self):
        self.h = sha("a@x.com")

    def test_search_document_keys(self):
        f, _ = load(one_table(fx.row("search_1", "search_service", "customer_search_documents",
                                      {"index_name": "i", "document": {
                                          "display_name": "Ana B", "email": "A@X.com",
                                          "customer_reference": "user_1"}}, CAP)))
        self.assertEqual(keys(f.conn, "search_1"),
                         sorted([("email", "a@x.com"), ("name", "Ana B"), ("user_id", "user_1")] * 2))  # primary + replica copy

    def test_uppercase_email_in_subject_reference(self):
        f, _ = load(one_table(fx.row("backup_1", "backup_service", "backup_catalog",
                                      {"archive_path": "s3://x", "subject_reference": "K.KOW@GMAIL.COM",
                                       "restore_allowed": True, "encryption": "AES"}, CAP)))
        self.assertIn(("email", "k.kow@gmail.com"), keys(f.conn, "backup_1"))

    def test_connector_selector_email_or_user(self):
        a = fx.row("data_flow_1", "integration_service", "connector_configurations",
                   {"connector_name": "c", "destination": "d", "subject_selector": "Linn@X.com",
                    "enabled": True, "mode": "m"}, CAP)
        b = fx.row("data_flow_2", "integration_service", "connector_configurations",
                   {"connector_name": "c", "destination": "d", "subject_selector": "user_5",
                    "enabled": False, "mode": "m"}, CAP)
        f, _ = load(one_table(a, b))
        self.assertIn(("email", "linn@x.com"), keys(f.conn, "data_flow_1"))
        self.assertIn(("user_id", "user_5"), keys(f.conn, "data_flow_2"))

    def test_analytics_and_tombstone_identifiers(self):
        an = fx.row("analytics_1", "analytics_platform", "customer_analytics_reports",
                    {"user_id": "user_4", "email": "a@x.com", "phone": "+1 555 0100",
                     "email_hash": self.h.upper()}, CAP)
        tb = fx.row("tombstone_1", "identity_service", "deleted_customer_references",
                    {"subject_reference": "user_7", "name": "C M", "email": None, "phone": None,
                     "email_hash": self.h, "account_status": "DELETED"}, CAP)
        f, _ = load(one_table(an, tb))
        got = keys(f.conn, "analytics_1")
        for want in (("user_id", "user_4"), ("email", "a@x.com"), ("phone", "+1 555 0100"),
                     ("email_hash", self.h)):
            self.assertIn(want, got)
        t = keys(f.conn, "tombstone_1")
        self.assertIn(("user_id", "user_7"), t)
        self.assertIn(("email_hash", self.h), t)
        self.assertIn(("name", "C M"), t)

    def test_email_inside_message_text_and_uploaded_by(self):
        s = fx.row("support_1", "support_platform", "support_conversations",
                   {"ticket_id": "CS-1", "subject_reference": "user_3", "channel": "e",
                    "message_text": "Send it to Mansour.Y@Gmail.com please."}, CAP)
        a = fx.row("support_2", "support_platform", "ticket_attachments",
                   {"ticket_id": "CS-1", "file_name": "f", "uploaded_by": "Ana@Proton.me",
                    "storage_class": "t"}, CAP)
        f, _ = load(one_table(s, a))
        self.assertIn(("email", "mansour.y@gmail.com"), keys(f.conn, "support_1"))
        self.assertIn(("user_id", "user_3"), keys(f.conn, "support_1"))
        self.assertIn(("email", "ana@proton.me"), keys(f.conn, "support_2"))

    def test_redacted_message_has_no_email_key(self):
        s = fx.row("support_1", "support_platform", "support_conversations",
                   {"ticket_id": "CS-1", "subject_reference": "user_3", "channel": "e",
                    "message_text": "[REDACTED]"}, CAP)
        f, _ = load(one_table(s))
        self.assertEqual({k for k, _ in keys(f.conn, "support_1")}, {"user_id"})

    def test_no_join_keys_exist_in_replica_sql(self):
        f, rep = load(one_table(fx.row("account_5", "account_service", "customer_profiles",
                                        profile(), CAP)))
        self.assertEqual(f.conn.execute("select count(*) from join_key").fetchone()[0], 0)
        self.assertEqual(rep["join_keys"], 0)


class DossierIntegrationTests(unittest.TestCase):
    def test_ghost_keyed_by_historical_email_links_to_the_right_user(self):
        f = Facts().user("user_5", "new.name@x.com", old_emails=["old.name@x.com"])
        ghost = fx.row("account_77", "account_service", "customer_profiles",
                       profile("OLD.NAME@x.com", "user_5"), CAP)
        load(one_table(ghost), facts=f)
        d = dossier.build_dossier(f.conn, case("old.name@x.com", CAP))
        got = {(r["system_id"], r["resource_id"]): r for r in d["resources"]}
        r = got[("account_service", "account_77")]
        self.assertEqual((r["state"], r["state_basis"]), ("PRESENT", "explicit"))
        self.assertEqual((r["citation"]["rule"], r["citation"]["pick"]), ("E", "evidence_01"))

    def test_near_miss_email_does_not_link(self):
        f = Facts().user("user_5", "silva76@x.com")
        near = fx.row("account_78", "account_service", "customer_profiles",
                      profile("silva76.jr@x.com", "user_6"), CAP)
        load(one_table(near), facts=f)
        d = dossier.build_dossier(f.conn, case("silva76@x.com", CAP))
        self.assertEqual(d["resources"], [])

    def test_audit_record_links_by_user_id(self):
        f = Facts().user("user_53", "diya@x.com")
        a = fx.row("audit_record_9528", "security_service", "security_audit_events",
                   {"subject_reference": "user_53", "event_name": "e", "actor_type": "a",
                    "integrity_seal": "sha256:0"}, CAP)
        load(one_table(a), facts=f)
        d = dossier.build_dossier(f.conn, case("diya@x.com", CAP))
        self.assertEqual([r["resource_id"] for r in d["resources"]], ["audit_record_9528"])

    def test_audit_before_capture_sees_nothing(self):
        f = Facts().user("user_5", "g@x.com")
        load(one_table(fx.row("account_77", "account_service", "customer_profiles",
                               profile("g@x.com", "user_5"), CAP)), facts=f)
        d = dossier.build_dossier(f.conn, case("g@x.com", "2026-09-21T09:59:59Z"))
        self.assertEqual(d["resources"], [])
        self.assertEqual(d["observations_used"], [])


class ScopeAndOmissionTests(unittest.TestCase):
    def linked(self, sql):
        f = Facts().user("user_5", "g@x.com")
        f.file("evidence_09", "2026-09-01T00:00:00Z")
        f.link_identity("2026-09-01T00:00:00Z", "account_5", "g@x.com", "user_5")
        load(sql, facts=f)
        d = dossier.build_dossier(f.conn, case("g@x.com", CAP))
        return f, {(r["system_id"], r["resource_id"]): r for r in d["resources"]}

    def other_ghost(self):
        return fx.row("account_99", "account_service", "customer_profiles",
                      profile("other@x.com", "user_99"), CAP)

    def test_system_in_the_file_proves_absence_of_a_missing_resource(self):
        f, got = self.linked(one_table(self.other_ghost()))
        r = got[("account_service", "account_5")]
        self.assertEqual((r["state"], r["state_basis"]), ("ABSENT", "omission"))
        self.assertEqual((r["citation"]["rule"], r["citation"]["pick"]), ("O", "evidence_01"))

    def test_system_not_in_the_file_proves_nothing(self):
        other = fx.row("search_9", "search_service", "customer_search_documents",
                       {"index_name": "i", "document": {"display_name": "X", "email": "x@x.com",
                                                         "customer_reference": "user_9"}}, CAP)
        f, got = self.linked(one_table(other))
        self.assertIsNone(got[("account_service", "account_5")]["state"])

    def test_explicit_row_is_never_out_clocked_by_its_own_scope(self):
        # the row is older than the capture; the scope row must not beat the explicit PRESENT
        old = fx.row("account_5", "account_service", "customer_profiles",
                     profile("g@x.com", "user_5"), EARLY)
        f, got = self.linked(fx.j_sql(CAP, {P: [], R: [old]}))
        r = got[("account_service", "account_5")]
        self.assertEqual((r["state"], r["state_basis"]), ("PRESENT", "explicit"))

    def test_scope_rows_are_complete_and_per_logical_system(self):
        f, _ = load(one_table(self.other_ghost(),
                              fx.row("search_9", "search_service", "customer_search_documents",
                                     {"index_name": "i", "document": {"display_name": "X",
                                      "email": "x@x.com", "customer_reference": "user_9"}}, CAP)))
        got = f.conn.execute("select file_id, system_id, record_at, complete from capture_scope "
                             "order by system_id").fetchall()
        self.assertEqual(got, [("evidence_01", "account_service", CAP, 1),
                               ("evidence_01", "search_service", CAP, 1)])


class ClockChoiceTests(unittest.TestCase):
    """audit_at in [row updated_at, capture time): the other clock would have used this row."""

    def setup(self, updated="2026-09-21T09:00:00Z"):
        f = Facts().user("user_5", "g@x.com")
        r = fx.row("account_77", "account_service", "customer_profiles",
                   profile("g@x.com", "user_5"), updated)
        load(fx.j_sql(CAP, {P: [], R: [r]}), facts=f)
        return f

    def events(self, f, audit_at):
        return replica_sql.clock_choice_events(f.conn, case("g@x.com", audit_at))

    def test_fires_strictly_inside_the_window(self):
        f = self.setup()
        ev = self.events(f, "2026-09-21T09:30:00Z")
        self.assertEqual([e["type"] for e in ev], ["clock_choice_matters"])
        self.assertEqual((ev[0]["file_id"], ev[0]["resource_id"]), ("evidence_01", "account_77"))
        self.assertEqual((ev[0]["row_updated_at"], ev[0]["record_at"]),
                         ("2026-09-21T09:00:00Z", CAP))

    def test_boundaries(self):
        f = self.setup()
        n = lambda t: len(self.events(f, t))
        self.assertEqual(n("2026-09-21T08:59:59Z"), 0)   # before the row existed
        self.assertEqual(n("2026-09-21T09:00:00Z"), 1)   # updated_at inclusive
        self.assertEqual(n("2026-09-21T09:59:59Z"), 1)
        self.assertEqual(n("2026-09-21T10:00:00Z"), 0)   # capture time exclusive
        self.assertEqual(n("2026-09-22T00:00:00Z"), 0)

    def test_rows_stamped_at_capture_never_fire(self):
        f = Facts().user("user_5", "g@x.com")
        load(fx.j_sql(CAP, {P: [], R: [fx.row("account_77", "account_service", "customer_profiles",
                                              profile("g@x.com", "user_5"), CAP)]}), facts=f)
        self.assertEqual(self.events(f, "2026-09-21T09:59:59Z"), [])

    def test_equal_instants_in_other_spellings_do_not_fire(self):
        f = Facts().user("user_5", "g@x.com")
        load(fx.j_sql(CAP, {P: [], R: [fx.row("account_77", "account_service", "customer_profiles",
                                              profile("g@x.com", "user_5"),
                                              "2026-09-21T10:00:00+00:00")]}), facts=f)
        self.assertEqual(self.events(f, "2026-09-21T09:59:59Z"), [])

    def test_summary_counts_events_over_cases(self):
        f = self.setup()
        cases = [case("g@x.com", "2026-09-21T09:30:00Z", "case_1"),
                 case("g@x.com", "2026-09-21T10:00:00Z", "case_2"),
                 case("g@x.com", "2026-09-21T09:00:00Z", "case_3")]
        self.assertEqual(replica_sql.count_clock_choice(f.conn, cases), 2)


class UnparsedAccountingTests(unittest.TestCase):
    def test_clean_file_reports_zero_and_is_stored(self):
        f, rep = load(one_table(fx.row("account_5", "account_service", "customer_profiles",
                                        profile(), CAP)))
        self.assertEqual(rep["unparsed"], 0)
        self.assertGreater(rep["meaningful"], 0)
        self.assertEqual(rep["meaningful"], rep["parsed"])
        got = f.conn.execute("select file_id, adapter, meaningful, parsed, unparsed "
                             "from parse_report").fetchall()
        self.assertEqual(got, [("evidence_01", "replica_sql", rep["meaningful"], rep["parsed"], 0)])

    def test_unknown_collection_is_unparsed(self):
        f, rep = load(one_table(fx.row("mystery_1", "x_service", "mystery_collection", {}, CAP)))
        self.assertEqual(rep["unparsed"], 2)  # primary and replica copy
        self.assertEqual(len(rows(f.conn, "mystery_1")), 0)

    def test_broken_json_is_unparsed(self):
        sql = fx.j_sql(CAP, {P: [], R: []}).replace("COMMIT;", (
            "INSERT INTO \"customer_data_read_replica\" VALUES('account_1','account_service',"
            "'customer_profiles','{broken','" + CAP + "');\nCOMMIT;"))
        # json_valid CHECK rejects the row at load time; the file still must be accounted for
        f, rep = load(sql)
        self.assertGreaterEqual(rep["unparsed"], 1)

    def test_missing_resource_id_in_json_is_unparsed(self):
        r = ("account_1", "account_service", "customer_profiles",
             json.dumps({"email": "a@x.com"}), CAP)
        f, rep = load(fx.j_sql(CAP, {P: [], R: [r]}))
        self.assertEqual(rep["unparsed"], 1)

    def test_json_resource_id_disagreeing_with_column_is_unparsed(self):
        r = ("account_1", "account_service", "customer_profiles",
             json.dumps({"resource_id": "account_2", "email": "a@x.com"}), CAP)
        f, rep = load(fx.j_sql(CAP, {P: [], R: [r]}))
        self.assertEqual(rep["unparsed"], 1)

    def test_stray_statements_are_unparsed(self):
        sql = fx.j_sql(CAP, {P: [], R: []},
                       extra="UPDATE customer_data_primary SET updated_at='x';\nDROP TABLE zzz;\n")
        f, rep = load(sql)
        self.assertEqual(rep["unparsed"], 2)

    def test_hostile_sql_is_denied_and_reported_not_executed(self):
        with tempfile.TemporaryDirectory() as d:
            target = Path(d) / "pwn.db"
            sql = fx.j_sql(CAP, {P: [], R: []},
                           extra=f"ATTACH DATABASE '{target}' AS pwn;\n"
                                 "SELECT load_extension('/nonexistent');\n")
            f, rep = load(sql)
            self.assertFalse(target.exists())
        self.assertEqual(rep["unparsed"], 2)
        # never executed at all (not merely refused): ATTACH creates its file lazily, so the
        # file check alone cannot tell the two apart
        self.assertEqual(sorted((n["type"], n["kind"]) for n in rep["notes"]
                                if n["type"] in ("unexecuted_statement", "statement_error")),
                         [("unexecuted_statement", "OTHER"), ("unexecuted_statement", "OTHER")])

    def test_summary_totals(self):
        f = Facts()
        f.file("evidence_01", CAP).file("evidence_02", CAP)
        replica_sql.ingest(f.conn, "evidence_01", one_table(
            fx.row("account_5", "account_service", "customer_profiles", profile(), CAP)))
        replica_sql.ingest(f.conn, "evidence_02", one_table(
            fx.row("m_1", "x", "nope", {}, CAP)))
        s = adapters.summary(f.conn)
        self.assertEqual((s["files"], s["unparsed"]), (2, 2))
        self.assertGreater(s["observations"], 0)


class DeterminismTests(unittest.TestCase):
    def test_two_loads_are_identical(self):
        sql = one_table(fx.row("account_5", "account_service", "customer_profiles", profile(), CAP),
                        fx.row("audit_1", "security_service", "security_audit_events",
                               {"subject_reference": "user_9", "event_name": "e",
                                "actor_type": "a", "integrity_seal": "s"}, CAP))
        dumps = []
        for _ in range(2):
            f, _ = load(sql)
            dumps.append([f.conn.execute(f"select * from {t} order by 1,2,3").fetchall()
                          for t in ("observation", "subject_key", "capture_scope", "parse_report")])
        self.assertEqual(dumps[0], dumps[1])

    def test_ingest_twice_does_not_duplicate(self):
        f, _ = load(one_table(fx.row("account_5", "account_service", "customer_profiles",
                                      profile(), CAP)))
        n = f.conn.execute("select count(*) from observation").fetchone()[0]
        replica_sql.ingest(f.conn, "evidence_01", one_table(
            fx.row("account_5", "account_service", "customer_profiles", profile(), CAP)))
        self.assertEqual(f.conn.execute("select count(*) from observation").fetchone()[0], n)


if __name__ == "__main__":
    unittest.main()
