import json
import tempfile
import unittest
from pathlib import Path

from ghost import adapters, dossier, policy
from ghost.adapters import inventory_sql, replica_sql, service_export, snapshot_bundle
from ghost.adapters import logstate
from ghost.adapters import observability_log as olog
from ghost.adapters import trace_log as tlog
from tests import bundle_fixture as bf
from tests import export_fixture as ef
from tests import log_fixture as lf
from tests import sql_fixture as fx
from tests import trace_fixture as tf
from tests.fact_builder import Facts, case, sha

AUDIT = "2026-09-26T11:00:00Z"
T1 = "2026-09-26T07:51:26Z"
T2 = "2026-09-26T07:51:27Z"
T3 = "2026-09-26T08:10:20Z"
T4 = "2026-09-26T08:10:21Z"
AFTER = "2026-09-26T11:30:00Z"
MANIFEST = "2026-09-26T10:38:56Z"
MANIFEST_LATE = "2026-09-27T10:38:56Z"
ANA = "ana@example.com"
OLD = "ana.old@example.com"
BEN = "ben@example.com"
PHONE = "+1 555 0100"
HASH = sha(ANA)


def users():
    return (Facts().user("user_1", ANA, phone=PHONE, name="Ana Lima", old_emails=(OLD,))
            .user("user_2", BEN))


def load(text, file_id="evidence_94", manifest=MANIFEST, facts=None):
    f = facts or users()
    f.file(file_id, manifest)
    return f, tlog.ingest(f.conn, file_id, text)


def obs(conn, rid=None):
    q = ("select obs_id, locator, record_at, system_id, resource_id, category, state, "
         "state_basis, attrs_json from observation")
    args = ()
    if rid is not None:
        q, args = q + " where resource_id = ?", (rid,)
    out = []
    for r in conn.execute(q + " order by obs_id", args):
        d = dict(zip(("obs_id", "locator", "record_at", "system_id", "resource_id", "category",
                      "state", "state_basis", "attrs"), r))
        d["attrs"] = json.loads(d["attrs"])
        out.append(d)
    return out


def one(conn, rid):
    got = obs(conn, rid)
    assert len(got) == 1, got
    return got[0]


def keys_of(conn, oid):
    return sorted(conn.execute("select kind, value from subject_key where obs_id = ?", (oid,)))


def joins_of(conn, oid):
    return sorted(conn.execute("select key_kind, key_value from join_key where obs_id = ?", (oid,)))


def notes(report, kind):
    return [n for n in report["notes"] if n["type"] == kind]


def dossier_for(f, audit=AUDIT, email=ANA):
    return dossier.build_dossier(f.conn, case(email, audit))


def res(d, rid):
    hits = [r for r in d["resources"] if r["resource_id"] == rid]
    return hits[0] if len(hits) == 1 else None


def file_of(*pairs):
    return tf.trace_file(*[l for p in pairs for l in p])


SAMPLE = file_of(tf.profile(T1, "rb-1", "account_1", ANA))


# ---- detection ---------------------------------------------------------------------------------

class DetectTests(unittest.TestCase):
    def test_a_trace_log_is_claimed_with_its_format(self):
        self.assertEqual(tlog.detect(SAMPLE), "log_trace")

    def test_the_bracket_lines_are_optional(self):
        self.assertEqual(tlog.detect(tf.log(*tf.profile(T1, "rb-1", "account_1", ANA))), "log_trace")

    def test_the_trace_prefix_is_not_the_test(self):
        text = file_of(tf.profile(T1, "xx-77", "account_1", ANA))
        self.assertEqual(tlog.detect(text), "log_trace")

    def test_other_log_shapes_are_not_claimed(self):
        obs_file = lf.obs_file("logfmt", *lf.lookup_pair("logfmt", T1, T2, "tr-1", ANA, "account_1"))
        for name, text in (("observability", obs_file), ("ledger", lf.ledger_log()),
                           ("support_replica", lf.support_replica_log())):
            with self.subTest(name):
                self.assertIsNone(tlog.detect(text))

    def test_other_evidence_is_not_claimed(self):
        sql = fx.j_sql("2026-09-21T10:00:00Z", {"customer_data_primary": [],
                                                  "customer_data_read_replica": []})
        bundle = bf.bundle([bf.snap(T2, "account_service", "customer_profiles",
                                    [bf.profile("account_1", ANA)])])
        export = ef.one("account_service", "profile_lookups", [ef.profile_lookup("account_1", T2)])
        for text in (sql, bundle, export, "", "\n\n", "not a log", "[" * 100000):
            with self.subTest(text[:30]):
                self.assertIsNone(tlog.detect(text))

    def test_a_file_with_only_the_bracket_lines_is_not_claimed(self):
        self.assertIsNone(tlog.detect(tf.log(tf.header(T1, 0), tf.footer(T2, 0))))

    def test_started_lines_without_a_completed_partner_are_not_claimed(self):
        text = tf.log(*[tf.started(T1, "profile_lookup", f"rb-{i}", f"account_{i}", ANA)
                        for i in range(3)])
        self.assertIsNone(tlog.detect(text))

    def test_started_lines_without_a_resource_are_not_the_trace_shape(self):
        started = tf.line(T1, "account_service", "profile_lookup_started", "rb-1", level="DEBUG",
                          lookup_key=ANA)
        done = tf.completed(T2, "profile_lookup", "rb-1", "account_1", rows=0, http_status=404)
        self.assertIsNone(tlog.detect(tf.log(started, done)))

    def test_started_lines_without_a_lookup_key_field_are_not_the_trace_shape(self):
        s = tf.line(T1, "account_service", "profile_lookup_started", "rb-1", level="DEBUG",
                    resource_id="account_1")
        done = tf.completed(T2, "profile_lookup", "rb-1", "account_1", rows=0, http_status=404)
        self.assertIsNone(tlog.detect(tf.log(s, done)))

    def test_lines_with_no_resource_anywhere_are_not_a_pair(self):
        s = tf.line(T1, "account_service", "profile_lookup_started", "rb-1", level="DEBUG",
                    lookup_key=ANA)
        done = tf.line(T2, "account_service", "profile_lookup_completed", "rb-1", rows=0,
                       http_status=404)
        self.assertIsNone(tlog.detect(tf.log(s, done)))

    def test_a_file_in_another_dominant_syntax_is_refused_by_ingest_too(self):
        pipe = [f'{T1} | INFO | account_service | rb-{i} | profile_lookup_started | '
                f'resource_id="account_{i}" lookup_key="{ANA}"' for i in range(5)]
        text = tf.log(*pipe, *tf.profile(T1, "rb-9", "account_9", ANA))
        f = users()
        f.file("evidence_94", MANIFEST)
        with self.assertRaises(ValueError):
            tlog.ingest(f.conn, "evidence_94", text)

    def test_any_instance_region_or_pod_field_excludes_the_file(self):
        for extra in ("instance=account-1", "region=eu-west-1", "pod=replica-7c9f"):
            with self.subTest(extra):
                lines = [l.replace(" event=", f" {extra} event=", 1) for l in SAMPLE.splitlines()]
                self.assertIsNone(tlog.detect(tf.log(*lines)))

    def test_mostly_unparseable_file_is_not_claimed(self):
        good = SAMPLE.splitlines()
        junk = [f"2026-09-26T08:5{i}:00Z ### torn write {i} ###" for i in range(len(good) * 2)]
        self.assertIsNone(tlog.detect(tf.log(*good, *junk)))

    def test_a_few_bad_lines_do_not_stop_detection(self):
        good = []
        for i in range(20):
            good += tf.profile(T1, f"rb-{i}", f"account_{i}", ANA)
        self.assertEqual(tlog.detect(tf.log(*good, "### corrupted ###")), "log_trace")

    def test_a_non_logfmt_syntax_is_not_claimed(self):
        text = lf.log(*lf.lookup_pair("pipe", T1, T2, "rb-1", ANA, "account_1"))
        self.assertIsNone(tlog.detect(text))

    def test_the_observability_adapter_does_not_claim_a_trace_log(self):
        self.assertIsNone(olog.detect(SAMPLE))
        self.assertIsNone(olog.detect(lf.trace_log()))

    def test_existing_adapters_do_not_claim_a_trace_log(self):
        for a in (replica_sql, snapshot_bundle, service_export, inventory_sql, olog):
            with self.subTest(a.name):
                self.assertIsNone(a.detect(SAMPLE))

    def test_registry_lists_the_adapter_without_double_claims(self):
        self.assertIn(tlog, adapters.ADAPTERS)
        self.assertEqual(adapters.select(SAMPLE)[0].name, "resource_trace_log")
        self.assertEqual(adapters.select(lf.trace_log())[0].name, "resource_trace_log")
        obs_file = lf.obs_file("logfmt", *lf.lookup_pair("logfmt", T1, T2, "tr-1", ANA, "account_1"))
        self.assertEqual(adapters.select(obs_file)[0].name, "observability_log")

    def test_file_name_is_irrelevant(self):
        with tempfile.TemporaryDirectory() as d:
            (Path(d) / "evidence").mkdir()
            (Path(d) / "evidence" / "notes.json").write_text(SAMPLE, encoding="utf-8")
            f = users()
            f.conn.execute("insert into evidence_file(file_id,path,origin,format,adapter,manifest_at)"
                           " values ('evidence_07','evidence/notes.json','x',null,null,?)",
                           (MANIFEST,))
            summary = adapters.load_evidence(f.conn, d)
            got = f.conn.execute("select format, adapter from evidence_file").fetchone()
        self.assertEqual(got, ("log_trace", "resource_trace_log"))
        self.assertEqual(summary["claimed"], ["evidence_07"])

    def test_ingest_of_a_non_trace_log_is_an_error(self):
        f = users()
        f.file("evidence_01", MANIFEST)
        for text in (lf.ledger_log(), "nothing", lf.support_replica_log()):
            with self.assertRaises(ValueError):
                tlog.ingest(f.conn, "evidence_01", text)

    def test_ingest_of_an_unknown_file_id_is_an_error(self):
        with self.assertRaises(ValueError):
            tlog.ingest(users().conn, "evidence_99", SAMPLE)


# ---- clock, keys, joins, scope -----------------------------------------------------------------

class KeyTests(unittest.TestCase):
    def test_record_at_is_the_line_time_not_the_manifest(self):
        f, _ = load(file_of(tf.profile(T1, "rb-1", "account_1", ANA)))
        o = one(f.conn, "account_1")
        self.assertEqual((o["record_at"], o["state_basis"]), (T1, "explicit"))
        self.assertEqual(o["system_id"], "account_service")

    def test_started_line_is_a_resource_less_link_with_the_trace_join(self):
        f, report = load(file_of(tf.profile(T1, "rb-1", "account_1", ANA.upper())))
        starts = [o for o in obs(f.conn) if o["resource_id"] is None]
        self.assertEqual(len(starts), 1)
        self.assertIsNone(starts[0]["state"])
        self.assertEqual(keys_of(f.conn, starts[0]["obs_id"]), [("email", ANA)])
        self.assertEqual(joins_of(f.conn, starts[0]["obs_id"]), [("trace_id", "rb-1")])
        done = one(f.conn, "account_1")
        self.assertEqual(joins_of(f.conn, done["obs_id"]), [("trace_id", "rb-1")])
        self.assertEqual(report["join_keys"], 2)

    def test_lookup_key_is_classified_by_shape(self):
        for key, want in ((ANA, ("email", ANA)), ("user_1", ("user_id", "user_1")),
                          (HASH, ("email_hash", HASH)), (PHONE, ("phone", PHONE))):
            with self.subTest(key):
                f, _ = load(file_of(tf.profile(T1, "rb-1", "account_1", key)))
                start = [o for o in obs(f.conn) if o["resource_id"] is None][0]
                self.assertEqual(keys_of(f.conn, start["obs_id"]), [want])

    def test_completed_line_identifiers_are_subject_keys(self):
        f, _ = load(file_of(tf.profile(T1, "rb-1", "account_1", ANA, found=True, email=ANA,
                                       phone=PHONE, customer_reference="user_1")))
        got = keys_of(f.conn, one(f.conn, "account_1")["obs_id"])
        self.assertEqual(got, [("email", ANA), ("phone", PHONE), ("user_id", "user_1")])

    def test_names_are_name_keys(self):
        f, _ = load(file_of(tf.support(T1, "rb-1", "support_1", "user_1", name="Ana Lima",
                                       ref="user_1")))
        got = keys_of(f.conn, one(f.conn, "support_1")["obs_id"])
        self.assertIn(("name", "Ana Lima"), got)
        self.assertIn(("user_id", "user_1"), got)

    def test_null_lookup_key_is_a_note_not_unparsed(self):
        f, report = load(file_of(tf.analytics(T1, "rb-1", "analytics_1", None)))
        self.assertEqual(report["unparsed"], 0, report["notes"])
        self.assertEqual([n["locator"] for n in notes(report, "null_lookup_key")], ["L2"])
        start = [o for o in obs(f.conn) if o["resource_id"] is None][0]
        self.assertEqual(keys_of(f.conn, start["obs_id"]), [])
        self.assertEqual(one(f.conn, "analytics_1")["state"], "ANONYMIZED")

    def test_non_identifier_key_is_a_note(self):
        f, report = load(file_of(tf.analytics(T1, "rb-1", "analytics_1", "analytics_257")))
        self.assertEqual(report["unparsed"], 0)
        self.assertEqual([n["value"] for n in notes(report, "non_identifier_key")],
                         ["analytics_257"])

    def test_no_capture_scope_is_ever_written(self):
        f, _ = load(file_of(tf.profile(T1, "rb-1", "account_1", ANA),
                            tf.audit(T3, "rb-2", "audit_record_1", "user_1", "user_1")))
        self.assertEqual(f.conn.execute("select count(*) from capture_scope").fetchone()[0], 0)

    def test_parse_report_and_file_format_are_recorded(self):
        f, report = load(SAMPLE)
        row = f.conn.execute("select adapter, meaningful, parsed, unparsed from parse_report "
                             "where file_id = 'evidence_94'").fetchone()
        self.assertEqual(row, ("resource_trace_log", 4, 4, 0))
        self.assertEqual(f.conn.execute("select format, adapter from evidence_file").fetchone(),
                         ("log_trace", "resource_trace_log"))
        self.assertEqual((report["meaningful"], report["parsed"]), (4, 4))

    def test_reingest_is_idempotent(self):
        f, first = load(SAMPLE)
        again = tlog.ingest(f.conn, "evidence_94", SAMPLE)
        self.assertEqual(len(obs(f.conn)), first["observations"])
        self.assertEqual(f.conn.execute("select count(*) from join_key").fetchone()[0], 2)
        self.assertEqual(again["observations"], first["observations"])

    def test_line_after_manifest_is_noted(self):
        _, report = load(file_of(tf.profile(AFTER, "rb-1", "account_1", ANA)))
        self.assertEqual(len(notes(report, "record_after_manifest")), 2)


# ---- one test per state mapper -----------------------------------------------------------------

class StateTests(unittest.TestCase):
    def state(self, pairs):
        f, report = load(file_of(pairs))
        return f, report

    def assert_state(self, pairs, rid, state, category, system):
        f, report = self.state(pairs)
        self.assertEqual(report["unparsed"], 0, report["notes"])
        o = one(f.conn, rid)
        self.assertEqual((o["state"], o["category"], o["system_id"]), (state, category, system))
        return o

    def test_profile_found_and_absent(self):
        self.assert_state(tf.profile(T1, "rb-1", "account_1", ANA, found=True, email=ANA),
                          "account_1", "PRESENT", "USER_PROFILE", "account_service")
        self.assert_state(tf.profile(T1, "rb-1", "account_1", ANA), "account_1", "ABSENT",
                          "USER_PROFILE", "account_service")

    def test_profile_rows_and_status_that_disagree_are_unparsed(self):
        for rows, status in ((1, 404), (0, 200), (2, 200)):
            with self.subTest(rows=rows):
                pairs = [tf.started(T1, "profile_lookup", "rb-1", "account_1", ANA),
                         tf.completed(T2, "profile_lookup", "rb-1", "account_1", rows=rows,
                                      http_status=status)]
                f, report = self.state(pairs)
                self.assertEqual(report["unparsed"], 1)
                self.assertEqual(obs(f.conn, "account_1"), [])

    def test_index_hit_and_miss(self):
        o = self.assert_state(tf.search(T1, "rb-1", "search_1", ANA, email=ANA), "search_1",
                              "PRESENT", "SEARCH_INDEX_ENTRY", "search_service")
        self.assertEqual(o["attrs"]["result"], "hit")
        self.assert_state(tf.search(T1, "rb-1", "search_1", ANA, result="miss"), "search_1",
                          "ABSENT", "SEARCH_INDEX_ENTRY", "search_service")

    def test_unknown_index_result_is_unparsed(self):
        f, report = self.state(tf.search(T1, "rb-1", "search_1", ANA, result="partial"))
        self.assertEqual(report["unparsed"], 1)
        self.assertEqual(obs(f.conn, "search_1"), [])

    def test_audit_record_present_with_retention(self):
        o = self.assert_state(tf.audit(T1, "rb-1", "audit_record_1", "user_1", "user_1"),
                              "audit_record_1", "PRESENT", "SECURITY_AUDIT_LOG",
                              "security_service")
        self.assertEqual(o["attrs"]["retention_until"], "2034-09-30T23:59:59Z")

    def test_audit_with_bad_retention_or_rows_is_unparsed(self):
        for pairs in (tf.audit(T1, "rb-1", "audit_record_1", "user_1", "user_1", until="soon"),
                      tf.audit(T1, "rb-1", "audit_record_1", "user_1", "user_1", rows=0)):
            f, report = self.state(pairs)
            self.assertEqual(report["unparsed"], 1)
            self.assertEqual(obs(f.conn, "audit_record_1"), [])

    def test_connector_active_and_disabled(self):
        self.assert_state(tf.connector(T1, "rb-1", "data_flow_1", ANA, True, email=ANA),
                          "data_flow_1", "ACTIVE", policy.DATA_FLOW, "integration_service")
        self.assert_state(tf.connector(T1, "rb-1", "data_flow_1", "user_1", False, ref="user_1"),
                          "data_flow_1", "DISABLED", policy.DATA_FLOW, "integration_service")

    def test_connector_flag_that_is_not_boolean_is_unparsed(self):
        pairs = [tf.started(T1, "connector_config_read", "rb-1", "data_flow_1", ANA),
                 tf.completed(T2, "connector_config_read", "rb-1", "data_flow_1", enabled="maybe")]
        _, report = self.state(pairs)
        self.assertEqual(report["unparsed"], 1)

    def test_analytics_identifiable_or_anonymized(self):
        for kw in ({"email": ANA}, {"phone": PHONE}, {"h": HASH}, {"ref": "user_1"}):
            with self.subTest(kw):
                self.assert_state(tf.analytics(T1, "rb-1", "analytics_1", ANA, **kw),
                                  "analytics_1", "IDENTIFIABLE", "ANALYTICS_PROFILE",
                                  "analytics_platform")
        self.assert_state(tf.analytics(T1, "rb-1", "analytics_1", None), "analytics_1",
                          "ANONYMIZED", "ANALYTICS_PROFILE", "analytics_platform")

    def test_support_ticket_identifiable_or_redacted(self):
        for kw in ({"email": ANA}, {"name": "Ana Lima"}, {"phone": PHONE}):
            with self.subTest(kw):
                self.assert_state(tf.support(T1, "rb-1", "support_1", ANA, **kw), "support_1",
                                  "IDENTIFIABLE", "SUPPORT_MESSAGE", "support_platform")
        self.assert_state(tf.support(T1, "rb-1", "support_1", "user_1", ref="user_1"),
                          "support_1", "REDACTED", "SUPPORT_MESSAGE", "support_platform")

    def test_restore_policy_restorable_or_blocked(self):
        self.assert_state(tf.restore(T1, "rb-1", "backup_1", ANA, True, email=ANA), "backup_1",
                          "RESTORABLE", "BACKUP_ARCHIVE", "backup_service")
        self.assert_state(tf.restore(T1, "rb-1", "backup_1", "user_1", False, ref="user_1"),
                          "backup_1", "BLOCKED", "BACKUP_ARCHIVE", "backup_service")

    def test_restore_policy_without_an_archive_is_unparsed_not_guessed(self):
        f, report = self.state(tf.restore(T1, "rb-1", "backup_1", ANA, False, present=False))
        self.assertEqual(report["unparsed"], 1)
        self.assertEqual(obs(f.conn, "backup_1"), [])

    def test_export_metadata_available(self):
        self.assert_state(tf.export(T1, "rb-1", "export_1", ANA, owner=ANA), "export_1",
                          "PRESENT", "USER_EXPORT", "export_service")

    def test_export_metadata_not_available_is_unparsed_not_guessed(self):
        f, report = self.state(tf.export(T1, "rb-1", "export_1", ANA, available=False))
        self.assertEqual(report["unparsed"], 1)
        self.assertEqual(obs(f.conn, "export_1"), [])

    def test_all_eight_services_in_one_file_parse_with_no_unparsed(self):
        f, report = load(file_of(
            tf.profile(T1, "rb-1", "account_1", ANA), tf.search(T1, "rb-2", "search_1", ANA),
            tf.audit(T1, "rb-3", "audit_record_1", "user_1", "user_1"),
            tf.connector(T1, "rb-4", "data_flow_1", ANA, True),
            tf.analytics(T1, "rb-5", "analytics_1", ANA, email=ANA),
            tf.support(T1, "rb-6", "support_1", ANA, email=ANA),
            tf.restore(T1, "rb-7", "backup_1", ANA, True),
            tf.export(T1, "rb-8", "export_1", ANA)))
        self.assertEqual(report["unparsed"], 0, report["notes"])
        self.assertEqual(report["meaningful"], 8 * 2 + 2)
        self.assertEqual(len([o for o in obs(f.conn) if o["state"]]), 8)


# ---- misleading lines --------------------------------------------------------------------------

class MisleadingLineTests(unittest.TestCase):
    def test_a_failed_lookup_that_looks_like_success_sets_no_state(self):
        for status in (500, 503):
            with self.subTest(status):
                pairs = [tf.started(T1, "profile_lookup", "rb-1", "account_1", ANA),
                         tf.completed(T2, "profile_lookup", "rb-1", "account_1", rows=0,
                                      http_status=status)]
                f, report = load(file_of(pairs))
                self.assertEqual(obs(f.conn, "account_1"), [])
                self.assertEqual(report["unparsed"], 0, report["notes"])
                self.assertEqual([n["resource_id"] for n in notes(report, "operation_failed")],
                                 ["account_1"])
                self.assertIsNone(res(dossier_for(f), "account_1"))

    def test_failed_events_and_failed_results_set_no_state(self):
        pairs = [tf.started(T1, "profile_lookup", "rb-1", "account_1", ANA),
                 tf.failed(T2, "profile_lookup", "rb-1", "account_1", http_status=500),
                 tf.started(T1, "index_query", "rb-2", "search_1", ANA),
                 tf.completed(T2, "index_query", "rb-2", "search_1", result="timeout",
                              total_hits=0, index="v3", email=None, customer_reference=None),
                 tf.started(T1, "index_query", "rb-3", "search_2", ANA),
                 tf.completed(T2, "index_query", "rb-3", "search_2", result="error",
                              total_hits=0, index="v3", email=None, customer_reference=None),
                 tf.started(T1, "support_ticket_read", "rb-4", "support_1", ANA),
                 tf.failed(T2, "support_ticket_read", "rb-4", "support_1")]
        f, report = load(tf.trace_file(*pairs))
        self.assertEqual([o for o in obs(f.conn) if o["resource_id"]], [])
        self.assertEqual(report["unparsed"], 0, report["notes"])
        self.assertEqual(len(notes(report, "operation_failed")), 4)

    def test_a_failed_attempt_then_a_good_retry_keeps_only_the_retry(self):
        pairs = [tf.started(T1, "profile_lookup", "rb-1", "account_1", ANA),
                 tf.completed(T1, "profile_lookup", "rb-1", "account_1", rows=0, http_status=503),
                 tf.started(T3, "profile_lookup", "rb-2", "account_1", ANA),
                 tf.completed(T4, "profile_lookup", "rb-2", "account_1", rows=1, http_status=200,
                              email=ANA)]
        f, report = load(tf.trace_file(*pairs))
        self.assertEqual([(o["state"], o["record_at"]) for o in obs(f.conn, "account_1")],
                         [("PRESENT", T4)])
        self.assertEqual(len(notes(report, "operation_failed")), 1)
        self.assertEqual(report["unparsed"], 0)
        self.assertEqual(res(dossier_for(f), "account_1")["state"], "PRESENT")

    def test_a_good_lookup_then_a_failed_retry_keeps_the_good_state(self):
        pairs = [*tf.profile(T1, "rb-1", "account_1", ANA, found=True, email=ANA),
                 tf.started(T3, "profile_lookup", "rb-2", "account_1", ANA),
                 tf.completed(T4, "profile_lookup", "rb-2", "account_1", rows=0, http_status=503)]
        f, _ = load(tf.trace_file(*pairs))
        self.assertEqual(res(dossier_for(f), "account_1")["state"], "PRESENT")

    def test_two_real_completions_keep_both_and_the_later_wins(self):
        pairs = [*tf.profile(T1, "rb-1", "account_1", ANA, found=True, email=ANA),
                 *tf.profile(T3, "rb-2", "account_1", ANA)]
        f, report = load(tf.trace_file(*pairs))
        self.assertEqual([o["state"] for o in obs(f.conn, "account_1")], ["PRESENT", "ABSENT"])
        self.assertEqual(report["unparsed"], 0)
        self.assertEqual(res(dossier_for(f), "account_1")["state"], "ABSENT")

    def test_two_completions_on_one_trace_are_noted(self):
        pairs = [*tf.profile(T1, "rb-1", "account_1", ANA, found=True, email=ANA),
                 tf.completed(T3, "profile_lookup", "rb-1", "account_1", rows=0, http_status=404)]
        _, report = load(tf.trace_file(*pairs, pairs=1))
        self.assertEqual([n["resource_id"] for n in notes(report, "retried_completion")],
                         ["account_1"])

    def test_line_after_audit_neither_links_nor_sets_state(self):
        pairs = [*tf.profile(T1, "rb-1", "account_1", ANA, found=True, email=ANA),
                 *tf.profile(AFTER, "rb-2", "account_1", ANA),
                 *tf.search(AFTER, "rb-3", "search_late", ANA, email=ANA)]
        f, _ = load(tf.trace_file(*pairs, end=AFTER), manifest=MANIFEST_LATE)
        d = dossier_for(f)
        self.assertEqual(res(d, "account_1")["state"], "PRESENT")
        self.assertIsNone(res(d, "search_late"))
        self.assertEqual(d["clock"]["late_records_excluded"], 3)  # the keyed lines after audit_at
        self.assertEqual(len([o for o in obs(f.conn, "account_1") if o["record_at"] == AFTER]), 1)

    def test_early_lines_of_a_late_file_link_but_are_not_cited(self):
        f, _ = load(file_of(tf.profile(T1, "rb-1", "account_1", ANA)), manifest=MANIFEST_LATE)
        d = dossier_for(f)
        self.assertEqual(res(d, "account_1")["state"], "ABSENT")
        self.assertEqual(d["anchor_candidates"], [])
        self.assertEqual(d["clock"]["late_manifest_files_used"], ["evidence_94"])

    def test_duplicate_line_writes_one_observation(self):
        a, b = tf.profile(T1, "rb-1", "account_1", ANA)
        f, report = load(tf.log(tf.header(T1, 1), a, b, b, tf.footer(T2, 1)))
        self.assertEqual(len(obs(f.conn, "account_1")), 1)
        self.assertEqual([n["locator"] for n in notes(report, "duplicate_line")], ["L4"])
        self.assertEqual(report["unparsed"], 0)
        self.assertEqual(notes(report, "retried_completion"), [])

    def test_unparseable_line_is_counted_and_the_rest_still_ingested(self):
        a, b = tf.profile(T1, "rb-1", "account_1", ANA)
        f, report = load(tf.log(tf.header(T1, 1), a, "2026-09-26T07:51:26Z ### torn write ###", b,
                                tf.footer(T2, 1)))
        self.assertEqual(report["unparsed"], 1)
        self.assertEqual([n["locator"] for n in notes(report, "line_unparsed")], ["L3"])
        self.assertEqual(one(f.conn, "account_1")["state"], "ABSENT")
        self.assertIn("WARNING", adapters.format_summary(adapters.summary(f.conn), 0, 0))

    def test_a_line_in_another_syntax_is_unparsed(self):
        a, b = tf.profile(T1, "rb-1", "account_1", ANA)
        pipe = lf.line("pipe", T1, "account_service", "profile_lookup_started", "rb-9",
                       lookup_key=ANA)
        _, report = load(tf.log(tf.header(T1, 1), a, pipe, b, tf.footer(T2, 1)))
        self.assertEqual([n["type"] for n in report["notes"] if n["type"] == "syntax_mismatch"],
                         ["syntax_mismatch"])
        self.assertEqual(report["unparsed"], 1)

    def test_unlisted_event_with_a_resource_id_is_unparsed_not_noise(self):
        extra = tf.line(T3, "account_service", "profile_cache_checked", "rb-9",
                        resource_id="account_7", hit=True)
        f, report = load(tf.log(*tf.profile(T1, "rb-1", "account_1", ANA), extra))
        self.assertEqual(report["unparsed"], 1)
        self.assertEqual([(n["event"], n["resource_id"]) for n in notes(report, "unlisted_event")],
                         [("profile_cache_checked", "account_7")])
        self.assertNotIn("profile_cache_checked", notes(report, "noise_events")[0]["counts"])
        self.assertEqual(obs(f.conn, "account_7"), [])

    def test_unlisted_event_with_a_subject_key_is_unparsed(self):
        extra = tf.line(T3, "account_service", "profile_scan_started", "rb-9", lookup_key=ANA)
        _, report = load(tf.log(*tf.profile(T1, "rb-1", "account_1", ANA), extra))
        self.assertEqual(report["unparsed"], 1)
        self.assertEqual(len(notes(report, "unlisted_event")), 1)

    def test_unlisted_event_with_neither_is_counted_noise(self):
        noise = [tf.line(T3, "dns_cache", "cache_refresh", level="DEBUG", entries=120)] * 1 + [
            tf.line(T4, "dns_cache", "cache_refresh", level="DEBUG", entries=121),
            tf.line(T4, "disk", "disk_latency", ms=3)]
        _, report = load(tf.log(*tf.profile(T1, "rb-1", "account_1", ANA), *noise))
        self.assertEqual(report["unparsed"], 0)
        self.assertEqual(notes(report, "noise_events")[0]["counts"],
                         {"cache_refresh": 2, "disk_latency": 1})

    def test_state_line_without_a_resource_is_unparsed(self):
        done = tf.line(T2, "account_service", "profile_lookup_completed", "rb-1", rows=0,
                       http_status=404)
        f, report = load(tf.log(tf.started(T1, "profile_lookup", "rb-1", "account_1", ANA), done))
        self.assertEqual(report["unparsed"], 1)
        self.assertEqual(obs(f.conn, "account_1"), [])

    def test_started_line_without_a_lookup_key_field_is_unparsed(self):
        s = tf.line(T1, "account_service", "profile_lookup_started", "rb-1", level="DEBUG",
                    resource_id="account_1")
        done = tf.completed(T2, "profile_lookup", "rb-1", "account_1", rows=0, http_status=404)
        _, report = load(tf.log(s, done))
        self.assertEqual(report["unparsed"], 1)

    def test_orphan_started_and_orphan_completed_are_noted(self):
        pairs = [tf.started(T1, "profile_lookup", "rb-1", "account_1", ANA),
                 tf.completed(T3, "index_query", "rb-2", "search_1", result="hit", total_hits=1,
                              index="v3", email=ANA, customer_reference=None)]
        f, report = load(tf.trace_file(*pairs, pairs=1))
        self.assertEqual([n["trace_id"] for n in notes(report, "started_without_completion")],
                         ["rb-1"])
        self.assertEqual([n["trace_id"] for n in notes(report, "completed_without_start")],
                         ["rb-2"])
        self.assertEqual(report["unparsed"], 0, report["notes"])
        self.assertEqual(res(dossier_for(f), "search_1")["link"]["method"], "direct")
        self.assertIsNone(res(dossier_for(f), "account_1"))

    def test_a_trace_id_reused_for_two_subjects_links_nothing_through_the_trace(self):
        pairs = [tf.started(T1, "profile_lookup", "rb-1", "account_1", ANA),
                 tf.started(T1, "profile_lookup", "rb-1", "account_2", BEN),
                 tf.completed(T2, "profile_lookup", "rb-1", "account_1", rows=0, http_status=404),
                 tf.completed(T2, "profile_lookup", "rb-1", "account_2", rows=0, http_status=404)]
        f, report = load(tf.trace_file(*pairs, pairs=2))
        self.assertTrue(notes(report, "trace_conflict"))
        self.assertGreaterEqual(report["unparsed"], 1)
        self.assertEqual(f.conn.execute("select count(*) from join_key").fetchone()[0], 0)
        self.assertIsNone(res(dossier_for(f), "account_1"))
        self.assertIsNone(res(dossier_for(f, email=BEN), "account_2"))

    def test_started_and_completed_naming_different_resources_is_a_conflict(self):
        pairs = [tf.started(T1, "profile_lookup", "rb-1", "account_1", ANA),
                 tf.completed(T2, "profile_lookup", "rb-1", "account_2", rows=0, http_status=404)]
        f, report = load(tf.trace_file(*pairs, pairs=1))
        self.assertTrue(notes(report, "trace_conflict"))
        self.assertGreaterEqual(report["unparsed"], 1)
        self.assertEqual(f.conn.execute("select count(*) from join_key").fetchone()[0], 0)
        self.assertIsNone(res(dossier_for(f), "account_2"))

    def test_export_bracket_counts_must_agree_with_the_pairs(self):
        body = [*tf.profile(T1, "rb-1", "account_1", ANA), *tf.profile(T3, "rb-2", "account_2", ANA)]
        for label, text in (
                ("target_count", tf.log(tf.header(T1, 3), *body, tf.footer(T4, 2))),
                ("queries", tf.log(tf.header(T1, 2), *body, tf.footer(T4, 1))),
                ("dropped", tf.log(tf.header(T1, 2), *body, tf.footer(T4, 2, dropped=1)))):
            with self.subTest(label):
                _, report = load(text)
                self.assertEqual(report["unparsed"], 1, report["notes"])
                self.assertEqual(len(notes(report, "export_counts_disagree")), 1)

    def test_export_bracket_with_a_non_numeric_count_is_unparsed(self):
        body = tf.profile(T1, "rb-1", "account_1", ANA)
        bad = tf.line(T1, "readback_collector", "export_started", target_count="many",
                      mode="point_in_time")
        _, report = load(tf.log(bad, *body, tf.footer(T4, 1)))
        self.assertEqual([(n["locator"], n["event"]) for n in notes(report, "line_unparsed")],
                         [("L1", "export_started")])
        self.assertEqual(report["unparsed"], 1)

    def test_a_subject_mismatch_on_one_trace_is_noted_and_left_to_the_dossier(self):
        pairs = tf.profile(T1, "rb-1", "account_9", ANA, found=True, email=BEN)
        f, report = load(file_of(pairs))
        self.assertEqual(len(notes(report, "subject_mismatch")), 1)
        self.assertEqual(report["unparsed"], 0)

    def test_current_and_historical_email_of_one_user_are_noted_but_still_link(self):
        # The adapter cannot know that OLD and ANA are one person, so it writes a
        # subject_mismatch note (never unparsed) and leaves the link to the dossier, which
        # resolves both strings to user_1 and to nobody else.
        pairs = tf.profile(T1, "rb-1", "account_9", OLD, found=True, email=ANA)
        f, report = load(file_of(pairs))
        self.assertEqual(report["unparsed"], 0, report["notes"])
        self.assertEqual([(n["lookup_key"], n["found"]) for n in notes(report, "subject_mismatch")],
                         [(OLD, [ANA])])
        self.assertEqual(res(dossier_for(f), "account_9")["state"], "PRESENT")
        self.assertIsNone(res(dossier_for(f, email=BEN), "account_9"))

    def test_a_lookup_and_result_of_different_kinds_are_not_a_mismatch(self):
        pairs = tf.profile(T1, "rb-1", "account_9", "user_1", found=True, email=ANA)
        _, report = load(file_of(pairs))
        self.assertEqual(notes(report, "subject_mismatch"), [])


# ---- end to end through the dossier ------------------------------------------------------------

class DossierTests(unittest.TestCase):
    def scenario(self):
        return file_of(
            tf.profile(T1, "rb-1", "account_1", ANA),
            tf.audit(T1, "rb-2", "audit_record_1", "user_1", "user_1"),
            tf.connector(T1, "rb-3", "data_flow_1", ANA, True, email=ANA),
            tf.restore(T1, "rb-4", "backup_1", "user_1", True, ref="user_1"),
            tf.search(T1, "rb-5", "search_1", OLD, email=OLD),
            tf.support(T1, "rb-6", "support_1", ANA, email=ANA),
            tf.analytics(T1, "rb-7", "analytics_1", ANA, email=ANA),
            tf.analytics(T1, "rb-8", "analytics_2", None),
            tf.profile(T1, "rb-9", "account_2", BEN),
            tf.profile(T1, "rb-10", "account_3", "ana@example.org"))

    def test_trace_joins_link_resources(self):
        f, report = load(self.scenario())
        self.assertEqual(report["unparsed"], 0, report["notes"])
        d = dossier_for(f)
        got = {r["resource_id"]: (r["system_id"], r["state"], r["link"]["method"])
               for r in d["resources"]}
        self.assertEqual(got, {
            # a 404 profile names nobody, so only the trace joins it to the lookup key; every
            # other completed line names the subject itself and links directly
            "account_1": ("account_service", "ABSENT", "trace"),
            "audit_record_1": ("security_service", "PRESENT", "direct"),
            "data_flow_1": ("integration_service", "ACTIVE", "direct"),
            "backup_1": ("backup_service", "RESTORABLE", "direct"),
            "search_1": ("search_service", "PRESENT", "direct"),
            "support_1": ("support_platform", "IDENTIFIABLE", "direct"),
            "analytics_1": ("analytics_platform", "IDENTIFIABLE", "direct")})
        self.assertEqual(d["anchor_candidates"], ["evidence_94"])

    def test_another_user_decoys_and_the_null_key_analytics_are_not_linked(self):
        f, _ = load(self.scenario())
        d = dossier_for(f)
        for rid in ("account_2", "account_3", "analytics_2"):
            self.assertIsNone(res(d, rid), rid)
        self.assertEqual(res(dossier_for(f, email=BEN), "account_2")["state"], "ABSENT")

    def test_a_trace_that_names_two_subjects_is_blocked_for_the_lookup_subject(self):
        pairs = tf.profile(T1, "rb-1", "account_9", ANA, found=True, email=BEN,
                           customer_reference="user_2")
        f, _ = load(file_of(pairs))
        d = dossier_for(f)
        self.assertIsNone(res(d, "account_9"))
        self.assertTrue(any(e["type"] == "a8_conflict" for e in d["events"]))
        other = dossier_for(f, email=BEN)
        self.assertEqual(res(other, "account_9")["state"], "PRESENT")
        self.assertEqual(res(other, "account_9")["link"]["method"], "direct")

    def test_historical_email_links(self):
        f, _ = load(file_of(tf.profile(T1, "rb-1", "account_1", OLD)))
        self.assertEqual(res(dossier_for(f), "account_1")["state"], "ABSENT")

    def test_the_policy_sees_the_findings(self):
        f, _ = load(self.scenario())
        plan = policy.evaluate(dossier_for(f), policy.rules_from_rows([
            (f"rule_{i}", c, "Always", "RETAIN" if c in policy.RETAINED else
             "REDACT" if c in ("SUPPORT_MESSAGE", "EMAIL_DELIVERY_LOG", "ACCOUNT_TOMBSTONE") else
             "ANONYMIZE" if c in ("ANALYTICS_PROFILE", "PRODUCT_TELEMETRY") else "DELETE")
            for i, c in enumerate(policy.RULE_CATEGORIES)]))
        self.assertEqual(plan["status"], "INCOMPLETE")
        ops = {(s["operation"], s["resource_id"], s["evidence_id"]) for s in plan["steps"]}
        for want in (("REPORT_ACTIVE_DATA_FLOW", "data_flow_1", "evidence_94"),
                     ("REPORT_RESTORE_RISK", "backup_1", "evidence_94"),
                     ("REPORT_REMAINING_RECORD", "search_1", "evidence_94"),
                     ("REPORT_EXPOSED_PII", "support_1", "evidence_94"),
                     ("REPORT_EXPOSED_PII", "analytics_1", "evidence_94"),
                     ("VERIFY_RECORD_ABSENT", "account_1", ""),  # INCOMPLETE verification is uncited
                     ("VERIFY_RECORD_PRESENT", "audit_record_1", "")):
            self.assertIn(want, ops)

    def test_clean_case_is_complete(self):
        f, _ = load(file_of(
            tf.profile(T1, "rb-1", "account_1", ANA),
            tf.audit(T1, "rb-2", "audit_record_1", "user_1", "user_1"),
            tf.connector(T1, "rb-3", "data_flow_1", ANA, False, email=ANA),
            tf.restore(T1, "rb-4", "backup_1", "user_1", False, ref="user_1"),
            tf.support(T1, "rb-6", "support_1", ANA, ref="user_1")))
        plan = policy.evaluate(dossier_for(f), policy.rules_from_rows([
            (f"rule_{i}", c, "Always", "RETAIN" if c in policy.RETAINED else
             "REDACT" if c in ("SUPPORT_MESSAGE", "EMAIL_DELIVERY_LOG", "ACCOUNT_TOMBSTONE") else
             "ANONYMIZE" if c in ("ANALYTICS_PROFILE", "PRODUCT_TELEMETRY") else "DELETE")
            for i, c in enumerate(policy.RULE_CATEGORIES)]))
        self.assertEqual(plan["status"], "COMPLETE")


class SharedStateRuleTests(unittest.TestCase):
    """The failure rule both log adapters share (logstate)."""

    def test_failed_form_of_completed_and_of_other_state_events(self):
        self.assertEqual(logstate.failed_form("profile_lookup_completed"), "profile_lookup_failed")
        self.assertEqual(logstate.failed_form("connector_heartbeat"), "connector_heartbeat_failed")
        self.assertEqual(logstate.failed_form("message_pii_scan"), "message_pii_scan_failed")


if __name__ == "__main__":
    unittest.main()
