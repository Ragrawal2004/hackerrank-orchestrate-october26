import json
import tempfile
import unittest
from pathlib import Path

from ghost import adapters, dossier, policy
from ghost.adapters import inventory_sql, logtok, replica_sql, service_export, snapshot_bundle
from ghost.adapters import ledger_json, ledger_log, ledger_text
from ghost.adapters import observability_log as olog
from ghost.adapters import trace_log as tlog
from tests import bundle_fixture as bf
from tests import export_fixture as ef
from tests import ledger_fixture as lg
from tests import log_fixture as lf
from tests import sql_fixture as fx
from tests import trace_fixture as tf
from tests.fact_builder import Facts, case, sha

MODULE = {"json": ledger_json, "log": ledger_log, "text": ledger_text}
FILE = "evidence_30"
T0 = "2026-09-20T08:00:00Z"
T1 = "2026-09-20T09:00:00Z"
T_EARLY = "2026-09-18T09:00:00Z"
MANIFEST = "2026-09-20T09:00:00Z"
MANIFEST_LATE = "2026-09-25T09:00:00Z"
AUDIT = "2026-09-20T12:00:00Z"
AFTER = "2026-09-22T09:00:00Z"
ANA = "ana@example.com"
OLD = "ana.old@example.com"
BEN = "ben@example.com"
HASH = sha(ANA)


def users():
    return Facts().user("user_1", ANA, phone="+1 555 0100", name="Ana Lima",
                        old_emails=(OLD,)).user("user_2", BEN)


def load(container, rows, manifest=MANIFEST, facts=None, **kw):
    f = facts or users()
    f.file(FILE, manifest)
    return f, MODULE[container].ingest(f.conn, FILE, lg.render(container, rows, **kw))


def ingest_text(container, text, manifest=MANIFEST):
    f = users()
    f.file(FILE, manifest)
    return f, MODULE[container].ingest(f.conn, FILE, text)


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


def notes(report, kind):
    return [n for n in report["notes"] if n["type"] == kind]


def dossier_for(f, audit=AUDIT, email=ANA):
    return dossier.build_dossier(f.conn, case(email, audit))


def res(d, rid):
    hits = [r for r in d["resources"] if r["resource_id"] == rid]
    return hits[0] if len(hits) == 1 else None


def rules():
    return policy.rules_from_rows([
        (f"rule_{i}", c, "Always", "RETAIN" if c in policy.RETAINED else
         "REDACT" if c in ("SUPPORT_MESSAGE", "EMAIL_DELIVERY_LOG", "ACCOUNT_TOMBSTONE") else
         "ANONYMIZE" if c in ("ANALYTICS_PROFILE", "PRODUCT_TELEMETRY") else "DELETE")
        for i, c in enumerate(policy.RULE_CATEGORIES)])


BASE = [lg.account(T1, "account_1", "NOT_FOUND", ANA)]


# ---- row rules shared by the three containers --------------------------------------------------

class RowRules:
    container = None

    def load(self, rows, **kw):
        return load(self.container, rows, **kw)

    def test_record_type_is_the_category_and_the_system_is_kept(self):
        f, report = self.load([lg.account(T1, "account_1", "NOT_FOUND", ANA),
                               lg.search(T1, "search_1", "FOUND", ANA)])
        self.assertEqual(report["unparsed"], 0, report["notes"])
        a, s = one(f.conn, "account_1"), one(f.conn, "search_1")
        self.assertEqual((a["system_id"], a["category"], a["state"], a["state_basis"]),
                         ("account_service", "USER_PROFILE", "ABSENT", "explicit"))
        self.assertEqual((s["system_id"], s["category"], s["state"]),
                         ("search_service", "SEARCH_INDEX_ENTRY", "PRESENT"))
        self.assertEqual(a["attrs"]["record_type"], "USER_PROFILE")

    def test_result_words_map_to_states_for_every_category(self):
        cases = [("USER_PROFILE", "account_service", "FOUND", "PRESENT"),
                 ("USER_PROFILE", "account_service", "NOT_FOUND", "ABSENT"),
                 ("SEARCH_INDEX_ENTRY", "search_service", "FOUND", "PRESENT"),
                 ("SEARCH_INDEX_ENTRY", "search_service", "NOT_FOUND", "ABSENT"),
                 ("SECURITY_AUDIT_LOG", "security_service", "FOUND", "PRESENT"),
                 ("LEGAL_HOLD_RECORD", "legal_service", "FOUND", "PRESENT"),
                 ("USER_EXPORT", "export_service", "FOUND", "PRESENT"),
                 ("SUPPORT_ATTACHMENT", "support_platform", "FOUND", "PRESENT"),
                 ("SUPPORT_MESSAGE", "support_platform", "IDENTIFIABLE", "IDENTIFIABLE"),
                 ("SUPPORT_MESSAGE", "support_platform", "REDACTED", "REDACTED"),
                 ("ANALYTICS_PROFILE", "analytics_platform", "IDENTIFIABLE", "IDENTIFIABLE"),
                 ("ANALYTICS_PROFILE", "analytics_platform", "ANONYMIZED", "ANONYMIZED"),
                 ("DATA_FLOW", "integration_service", "ACTIVE", "ACTIVE"),
                 ("DATA_FLOW", "integration_service", "DISABLED", "DISABLED"),
                 ("BACKUP_ARCHIVE", "backup_service", "ACTIVE", "RESTORABLE"),
                 ("BACKUP_ARCHIVE", "backup_service", "BLOCKED", "BLOCKED"),
                 ("ACCOUNT_TOMBSTONE", "identity_service", "IDENTIFIABLE", "IDENTIFIABLE"),
                 ("ACCOUNT_TOMBSTONE", "identity_service", "REDACTED", "REDACTED")]
        for rtype, system, result, state in cases:
            with self.subTest(rtype=rtype, result=result):
                f, report = self.load([lg.row(T1, system, "r_1", rtype, result, ANA)])
                self.assertEqual(report["unparsed"], 0, report["notes"])
                o = one(f.conn, "r_1")
                self.assertEqual((o["category"], o["state"]), (rtype, state))

    def test_a_result_that_does_not_fit_the_category_is_unparsed(self):
        for rtype, system, result in (("USER_PROFILE", "account_service", "ACTIVE"),
                                      ("SEARCH_INDEX_ENTRY", "search_service", "IDENTIFIABLE"),
                                      ("DATA_FLOW", "integration_service", "FOUND"),
                                      ("BACKUP_ARCHIVE", "backup_service", "FOUND"),
                                      ("ANALYTICS_PROFILE", "analytics_platform", "NOT_FOUND"),
                                      ("USER_PROFILE", "account_service", "MAYBE")):
            with self.subTest(rtype=rtype, result=result):
                f, report = self.load([lg.row(T1, system, "r_1", rtype, result, ANA)])
                self.assertEqual(report["unparsed"], 1)
                self.assertEqual(obs(f.conn, "r_1"), [])

    def test_an_unknown_record_type_is_unparsed(self):
        f, report = self.load([lg.row(T1, "account_service", "r_1", "MYSTERY_TABLE", "FOUND", ANA)])
        self.assertEqual(report["unparsed"], 1)
        self.assertEqual(obs(f.conn), [])

    def test_clock_is_the_row_time_not_the_manifest(self):
        f, _ = self.load([lg.account(T_EARLY, "account_1", "NOT_FOUND", ANA)])
        self.assertEqual(one(f.conn, "account_1")["record_at"], T_EARLY)

    def test_subject_reference_is_classified_by_shape(self):
        for subject, want in ((ANA.upper(), ("email", ANA)), ("user_1", ("user_id", "user_1")),
                              (HASH, ("email_hash", HASH))):
            with self.subTest(subject=subject):
                f, _ = self.load([lg.account(T1, "account_1", "NOT_FOUND", subject)])
                self.assertEqual(keys_of(f.conn, one(f.conn, "account_1")["obs_id"]), [want])

    def test_a_non_identifier_subject_is_a_name_key_and_a_note(self):
        f, report = self.load([lg.account(T1, "account_1", "NOT_FOUND", "Ana Lima")])
        self.assertEqual(keys_of(f.conn, one(f.conn, "account_1")["obs_id"]),
                         [("name", "Ana Lima")])
        self.assertEqual(report["unparsed"], 0)
        self.assertEqual(len(notes(report, "non_identifier_subject")), 1)

    def test_a_blank_subject_is_allowed_only_for_an_anonymized_record(self):
        f, report = self.load([lg.row(T1, "analytics_platform", "analytics_1", "ANALYTICS_PROFILE",
                                      "ANONYMIZED", "")])
        self.assertEqual(report["unparsed"], 0, report["notes"])
        self.assertEqual(keys_of(f.conn, one(f.conn, "analytics_1")["obs_id"]), [])
        self.assertEqual(len(notes(report, "blank_subject")), 1)
        f, report = self.load([lg.account(T1, "account_1", "NOT_FOUND", "")])
        self.assertEqual(report["unparsed"], 1)
        self.assertEqual(obs(f.conn), [])

    def test_no_capture_scope_and_no_join_key_is_ever_written(self):
        f, _ = self.load(BASE + [lg.search(T1, "search_1", "FOUND", ANA)])
        self.assertEqual(f.conn.execute("select count(*) from capture_scope").fetchone()[0], 0)
        self.assertEqual(f.conn.execute("select count(*) from join_key").fetchone()[0], 0)

    def test_row_after_the_manifest_is_noted_and_kept(self):
        f, report = self.load(BASE + [lg.account(AFTER, "account_2", "FOUND", ANA)],
                              manifest=MANIFEST)
        self.assertEqual([n["resource_id"] for n in notes(report, "record_after_manifest")],
                         ["account_2"])
        self.assertEqual(one(f.conn, "account_2")["record_at"], AFTER)

    def test_two_rows_for_one_resource_are_both_kept(self):
        rows = [lg.account(T_EARLY, "account_1", "FOUND", ANA),
                lg.account(T1, "account_1", "NOT_FOUND", ANA)]
        f, report = self.load(rows)
        self.assertEqual([(o["record_at"], o["state"]) for o in obs(f.conn, "account_1")],
                         [(T_EARLY, "PRESENT"), (T1, "ABSENT")])
        self.assertEqual([n["resource_id"] for n in notes(report, "repeated_resource")],
                         ["account_1"])
        self.assertEqual(report["unparsed"], 0)

    def test_the_later_row_wins_in_the_dossier(self):
        f, _ = self.load([lg.account(T_EARLY, "account_1", "FOUND", ANA),
                          lg.account(T1, "account_1", "NOT_FOUND", ANA)])
        self.assertEqual(res(dossier_for(f), "account_1")["state"], "ABSENT")

    def test_a_row_recreated_after_the_manifest_only_counts_after_its_time(self):
        rows = [lg.account(T1, "account_1", "NOT_FOUND", ANA),
                lg.account(AFTER, "account_1", "FOUND", ANA)]
        f, _ = self.load(rows, manifest=MANIFEST)
        self.assertEqual(res(dossier_for(f, audit=AUDIT), "account_1")["state"], "ABSENT")
        d = dossier_for(f, audit="2026-09-23T00:00:00Z")
        self.assertEqual(res(d, "account_1")["state"], "PRESENT")

    def test_a_row_after_the_audit_neither_links_nor_sets_state(self):
        f, _ = self.load([lg.account(T1, "account_1", "NOT_FOUND", ANA),
                          lg.search(AFTER, "search_late", "FOUND", ANA)], manifest=MANIFEST_LATE)
        d = dossier_for(f)
        self.assertIsNone(res(d, "search_late"))
        self.assertEqual(d["clock"]["late_records_excluded"], 1)

    def test_early_rows_of_a_late_file_link_but_are_not_cited(self):
        f, _ = self.load(BASE, manifest=MANIFEST_LATE)
        d = dossier_for(f)
        self.assertEqual(res(d, "account_1")["state"], "ABSENT")
        self.assertEqual(d["anchor_candidates"], [])
        self.assertEqual(d["clock"]["late_manifest_files_used"], [FILE])

    def test_current_historical_and_user_id_subjects_link_and_decoys_do_not(self):
        rows = [lg.account(T1, "account_1", "NOT_FOUND", OLD),
                lg.search(T1, "search_1", "FOUND", ANA.upper()),
                lg.audit(T1, "audit_record_1", "FOUND", "user_1"),
                lg.account(T1, "account_2", "NOT_FOUND", BEN),
                lg.account(T1, "account_3", "NOT_FOUND", "ana@example.org"),
                lg.account(T1, "account_4", "NOT_FOUND", "ana.lima@example.com")]
        f, _ = self.load(rows)
        d = dossier_for(f)
        self.assertEqual({r["resource_id"] for r in d["resources"]},
                         {"account_1", "search_1", "audit_record_1"})
        self.assertEqual(res(dossier_for(f, email=BEN), "account_2")["state"], "ABSENT")

    def test_an_anonymized_record_with_a_blank_subject_is_not_linked(self):
        f, _ = self.load(BASE + [lg.row(T1, "analytics_platform", "analytics_1",
                                        "ANALYTICS_PROFILE", "ANONYMIZED", "")])
        self.assertIsNone(res(dossier_for(f), "analytics_1"))

    def test_the_policy_sees_findings_and_verifications_with_the_ledger_cited(self):
        rows = [lg.account(T1, "account_1", "NOT_FOUND", ANA),
                lg.search(T1, "search_1", "FOUND", ANA),
                lg.audit(T1, "audit_record_1", "FOUND", "user_1"),
                lg.row(T1, "support_platform", "support_1", "SUPPORT_MESSAGE", "IDENTIFIABLE", ANA),
                lg.row(T1, "analytics_platform", "analytics_1", "ANALYTICS_PROFILE",
                       "IDENTIFIABLE", ANA),
                lg.row(T1, "integration_service", "data_flow_1", "DATA_FLOW", "ACTIVE", ANA),
                lg.row(T1, "backup_service", "backup_1", "BACKUP_ARCHIVE", "ACTIVE", ANA),
                lg.row(T1, "legal_service", "legal_hold_1", "LEGAL_HOLD_RECORD", "FOUND", ANA)]
        f, report = self.load(rows)
        self.assertEqual(report["unparsed"], 0, report["notes"])
        plan = policy.evaluate(dossier_for(f), rules())
        self.assertEqual(plan["status"], "INCOMPLETE")
        ops = {(s["operation"], s["resource_id"], s["evidence_id"]) for s in plan["steps"]}
        for want in (("REPORT_REMAINING_RECORD", "search_1", FILE),
                     ("REPORT_EXPOSED_PII", "support_1", FILE),
                     ("REPORT_EXPOSED_PII", "analytics_1", FILE),
                     ("REPORT_ACTIVE_DATA_FLOW", "data_flow_1", FILE),
                     ("REPORT_RESTORE_RISK", "backup_1", FILE),
                     ("VERIFY_RECORD_ABSENT", "account_1", ""),
                     ("VERIFY_RECORD_PRESENT", "audit_record_1", ""),
                     ("VERIFY_RECORD_PRESENT", "legal_hold_1", "")):
            self.assertIn(want, ops)

    def test_a_clean_ledger_is_complete_and_cites_itself(self):
        rows = [lg.account(T1, "account_1", "NOT_FOUND", ANA),
                lg.audit(T1, "audit_record_1", "FOUND", "user_1"),
                lg.row(T1, "support_platform", "support_1", "SUPPORT_MESSAGE", "REDACTED", ANA),
                lg.row(T1, "integration_service", "data_flow_1", "DATA_FLOW", "DISABLED", ANA),
                lg.row(T1, "backup_service", "backup_1", "BACKUP_ARCHIVE", "BLOCKED", ANA)]
        f, _ = self.load(rows)
        plan = policy.evaluate(dossier_for(f), rules())
        self.assertEqual(plan["status"], "COMPLETE")
        self.assertEqual({s["evidence_id"] for s in plan["steps"]}, {FILE})

    def test_parse_report_format_and_reingest(self):
        f, first = self.load(BASE + [lg.search(T1, "search_1", "FOUND", ANA)])
        mod = MODULE[self.container]
        self.assertEqual(f.conn.execute("select format, adapter from evidence_file").fetchone(),
                         (mod.FORMAT, mod.name))
        row = f.conn.execute("select adapter, unparsed from parse_report").fetchone()
        self.assertEqual(row, (mod.name, 0))
        again = mod.ingest(f.conn, FILE, lg.render(self.container, BASE + [
            lg.search(T1, "search_1", "FOUND", ANA)]))
        self.assertEqual(len(obs(f.conn)), first["observations"])
        self.assertEqual(again["observations"], first["observations"])

    def test_ingest_refuses_an_unknown_file_id_and_foreign_text(self):
        with self.assertRaises(ValueError):
            MODULE[self.container].ingest(users().conn, "evidence_99",
                                          lg.render(self.container, BASE))
        f = users()
        f.file(FILE, MANIFEST)
        for text in ("", "not a ledger", lf.trace_log()):
            with self.assertRaises(ValueError):
                MODULE[self.container].ingest(f.conn, FILE, text)

    def test_detection_and_registry(self):
        mod = MODULE[self.container]
        text = lg.render(self.container, BASE)
        self.assertEqual(mod.detect(text), mod.FORMAT)
        self.assertIn(mod, adapters.ADAPTERS)
        self.assertEqual(adapters.select(text)[0].name, mod.name)
        for other in lg.CONTAINERS:
            if other != self.container:
                self.assertIsNone(mod.detect(lg.render(other, BASE)), other)

    def test_other_evidence_is_not_claimed(self):
        mod = MODULE[self.container]
        sql = fx.j_sql("2026-09-21T10:00:00Z", {"customer_data_primary": [],
                                                  "customer_data_read_replica": []})
        bundle = bf.bundle([bf.snap(T1, "account_service", "customer_profiles",
                                    [bf.profile("account_1", ANA)])])
        export = ef.one("account_service", "profile_lookups", [ef.profile_lookup("account_1", T1)])
        obs_log = lf.obs_file("logfmt", *lf.lookup_pair("logfmt", T0, T1, "tr-1", ANA, "account_1"))
        for text in (sql, bundle, export, obs_log, lf.trace_log(), lf.support_replica_log(),
                     tf.trace_file(*tf.profile(T0, "rb-1", "account_1", ANA)), "", "\n\n",
                     "not a ledger", "[" * 100000, "{}", "[]"):
            with self.subTest(text[:30]):
                self.assertIsNone(mod.detect(text))

    def test_existing_adapters_do_not_claim_a_ledger(self):
        text = lg.render(self.container, BASE)
        for a in (replica_sql, snapshot_bundle, service_export, inventory_sql, olog, tlog):
            with self.subTest(a.name):
                self.assertIsNone(a.detect(text))

    def test_file_name_is_irrelevant(self):
        mod = MODULE[self.container]
        with tempfile.TemporaryDirectory() as d:
            (Path(d) / "evidence").mkdir()
            (Path(d) / "evidence" / "notes.bin").write_text(lg.render(self.container, BASE),
                                                             encoding="utf-8")
            f = users()
            f.conn.execute("insert into evidence_file(file_id,path,origin,format,adapter,manifest_at)"
                           " values ('evidence_07','evidence/notes.bin','x',null,null,?)",
                           (MANIFEST,))
            summary = adapters.load_evidence(f.conn, d)
            got = f.conn.execute("select format, adapter from evidence_file").fetchone()
        self.assertEqual(got, (mod.FORMAT, mod.name))
        self.assertEqual(summary["claimed"], ["evidence_07"])


class JsonRowRules(RowRules, unittest.TestCase):
    container = "json"


class LogRowRules(RowRules, unittest.TestCase):
    container = "log"


class TextRowRules(RowRules, unittest.TestCase):
    container = "text"


# ---- family 1: JSON lists ----------------------------------------------------------------------

class JsonFamilyTests(unittest.TestCase):
    def doc(self, records, **over):
        d = {"exported_at": MANIFEST, "origin": "integration_management_service",
             "record_count": len(records), "records": records}
        d.update(over)
        return json.dumps(d, indent=2)

    def test_record_count_must_match_the_records(self):
        _, report = ingest_text("json", self.doc(BASE + BASE[:0], record_count=2))
        self.assertEqual(report["unparsed"], 1)
        self.assertEqual(len(notes(report, "record_count_disagrees")), 1)

    def test_a_record_that_is_not_an_object_or_misses_a_field_is_unparsed(self):
        bad = dict(BASE[0])
        del bad["result"]
        text = self.doc([BASE[0], "oops", bad, 7])
        f, report = ingest_text("json", text)
        self.assertEqual(report["unparsed"], 3)
        self.assertEqual(report["meaningful"], 4)
        self.assertEqual(one(f.conn, "account_1")["state"], "ABSENT")

    def test_an_unexpected_extra_field_is_unparsed(self):
        _, report = ingest_text("json", self.doc([{**BASE[0], "surprise": 1}]))
        self.assertEqual(report["unparsed"], 1)

    def test_wrong_types_and_bad_times_are_unparsed(self):
        for rec in ({**BASE[0], "resource_id": 7}, {**BASE[0], "recorded_at": "yesterday"},
                    {**BASE[0], "result": None}, {**BASE[0], "resource_id": " "}):
            with self.subTest(rec):
                f, report = ingest_text("json", self.doc([rec]))
                self.assertEqual(report["unparsed"], 1)
                self.assertEqual(obs(f.conn), [])

    def test_exported_at_is_checked_against_the_manifest(self):
        _, report = ingest_text("json", self.doc(BASE, exported_at="2026-09-19T09:00:00Z"))
        self.assertEqual(len(notes(report, "capture_time_mismatch")), 1)
        self.assertEqual(report["unparsed"], 0)

    def test_a_byte_identical_repeated_record_is_written_once(self):
        f, report = ingest_text("json", self.doc([BASE[0], BASE[0]]))
        self.assertEqual(len(obs(f.conn, "account_1")), 1)
        self.assertEqual(len(notes(report, "duplicate_record")), 1)
        self.assertEqual(report["unparsed"], 0)

    def test_locator_is_the_json_path(self):
        f, _ = ingest_text("json", self.doc([BASE[0], lg.search(T1, "search_1", "FOUND", ANA)]))
        self.assertEqual(one(f.conn, "search_1")["locator"], "records[1]")

    def test_detect_needs_a_ledger_shaped_records_list(self):
        for text in (self.doc([]), self.doc([{"a": 1}]), json.dumps({"records": [1, 2]}),
                     json.dumps({"snapshots": [], "records": BASE}) + "x", "[1,2]",
                     json.dumps(BASE)):
            with self.subTest(text[:40]):
                self.assertIsNone(ledger_json.detect(text))

    def test_detect_tolerates_a_few_bad_records(self):
        recs = [lg.account(T1, f"account_{i}", "NOT_FOUND", ANA) for i in range(20)] + [{"x": 1}]
        self.assertEqual(ledger_json.detect(self.doc(recs)), "ledger_json")

    def test_detect_refuses_a_list_that_is_mostly_not_ledger_rows(self):
        good = [lg.account(T1, f"account_{i}", "NOT_FOUND", ANA) for i in range(5)]
        self.assertIsNone(ledger_json.detect(self.doc(good + [{"x": i} for i in range(5)])))

    def test_a_mixed_system_file_keeps_each_rows_own_system(self):
        recs = [lg.account(T1, "account_1", "NOT_FOUND", ANA), lg.search(T1, "search_1", "FOUND", ANA),
                lg.audit(T1, "audit_record_1", "FOUND", "user_1")]
        f, _ = ingest_text("json", self.doc(recs))
        self.assertEqual({o["system_id"] for o in obs(f.conn)},
                         {"account_service", "search_service", "security_service"})


# ---- family 2: log-line ledgers ----------------------------------------------------------------

class LogFamilyTests(unittest.TestCase):
    def test_leveled_line_tokenizer(self):
        line = lg.log_line(3, BASE[0], level="WARN")
        got = logtok.parse_leveled_line(line)
        self.assertEqual((got.ts, got.level, got.service, got.event), (T1, "WARN",
                         "search_index_service", "record_lookup"))
        self.assertIsNone(got.trace_id)
        self.assertEqual((got.kv["seq"], got.kv["resource_id"], got.kv["result"]),
                         ("3", "account_1", "NOT_FOUND"))
        for bad in ("", "garbage", line[:40] + '"', line.replace(" WARN ", " "),
                    line.replace("service=", "svc=")):
            self.assertIsNone(logtok.parse_leveled_line(bad), bad)

    def test_the_leveled_form_is_not_a_logfmt_line(self):
        line = lg.log_line(1, BASE[0])
        self.assertEqual(logtok.parses(line), [])

    def test_level_does_not_change_the_state(self):
        f, _ = ingest_text("log", lg.render_log([lg.search(T1, "search_1", "FOUND", ANA)],
                                                level="WARN"))
        o = one(f.conn, "search_1")
        self.assertEqual((o["state"], o["attrs"]["level"]), ("PRESENT", "WARN"))

    def test_an_unlisted_event_with_a_resource_is_unparsed_not_noise(self):
        text = lg.render_log(BASE) + lg.log_line(2, BASE[0], event="record_deleted") + "\n"
        f, report = ingest_text("log", text)
        self.assertEqual(report["unparsed"], 1)
        self.assertEqual([n["event"] for n in notes(report, "unlisted_event")], ["record_deleted"])
        self.assertEqual(len(obs(f.conn, "account_1")), 1)

    def test_an_unlisted_event_without_a_resource_or_subject_is_noise(self):
        noise = f"{T1} INFO service=search_index_service event=export_flushed batch=7\n"
        _, report = ingest_text("log", lg.render_log(BASE) + noise + noise.replace("batch=7", "batch=8"))
        self.assertEqual(report["unparsed"], 0)
        self.assertEqual(notes(report, "noise_events")[0]["counts"], {"export_flushed": 2})

    def test_an_unparseable_or_incomplete_line_is_unparsed(self):
        text = (lg.render_log(BASE) + "2026-09-20T09:00:00Z ### torn write ###\n"
                + f'{T1} INFO service=s event=record_lookup seq=9 system="a" resource_id="r_9"\n')
        f, report = ingest_text("log", text)
        self.assertEqual(report["unparsed"], 2)
        self.assertEqual(len(obs(f.conn)), 1)

    def test_a_byte_identical_repeated_line_is_written_once(self):
        line = lg.log_line(1, BASE[0])
        f, report = ingest_text("log", f"{line}\n{line}\n")
        self.assertEqual(len(obs(f.conn, "account_1")), 1)
        self.assertEqual([n["locator"] for n in notes(report, "duplicate_line")], ["L2"])
        self.assertEqual(report["unparsed"], 0)

    def test_a_gap_in_seq_is_noted_not_unparsed(self):
        text = "\n".join([lg.log_line(1, BASE[0]), lg.log_line(4, lg.search(T1, "search_1", "FOUND", ANA))]) + "\n"
        _, report = ingest_text("log", text)
        self.assertEqual(report["unparsed"], 0)
        self.assertEqual(len(notes(report, "seq_gap")), 1)

    def test_locator_is_the_line_number(self):
        f, _ = ingest_text("log", lg.render_log(BASE + [lg.search(T1, "search_1", "FOUND", ANA)]))
        self.assertEqual(one(f.conn, "search_1")["locator"], "L2")

    def test_detect_needs_mostly_ledger_lines(self):
        junk = "\n".join(f"{T1} INFO ### torn {i}" for i in range(5)) + "\n"
        self.assertIsNone(ledger_log.detect(junk + lg.render_log(BASE)))
        self.assertEqual(ledger_log.detect(lg.render_log(BASE * 20) + "### one bad line\n"),
                         "ledger_log")

    def test_the_ledger_lines_belong_to_no_other_log_adapter(self):
        text = lg.render_log(BASE)
        for a in (olog, tlog):
            self.assertIsNone(a.detect(text))


# ---- family 3: handover text -------------------------------------------------------------------

class TextFamilyTests(unittest.TestCase):
    def test_the_snapshot_time_is_checked_against_the_manifest(self):
        _, report = ingest_text("text", lg.render_text(BASE, snapshot="2026-09-19T09:00:00Z"))
        self.assertEqual(len(notes(report, "capture_time_mismatch")), 1)
        self.assertEqual(report["unparsed"], 0)

    def test_prose_lines_are_not_rows(self):
        f, report = ingest_text("text", lg.render_text(BASE))
        self.assertEqual(report["meaningful"], 1)
        self.assertEqual(len(obs(f.conn)), 1)

    def test_a_pipe_row_with_the_wrong_field_count_is_unparsed(self):
        text = lg.render_text(BASE) + f"- {T1} | account_service | account_9 | USER_PROFILE | FOUND\n"
        f, report = ingest_text("text", text)
        self.assertEqual(report["unparsed"], 1)
        self.assertEqual(obs(f.conn, "account_9"), [])

    def test_a_row_with_a_bad_time_is_unparsed(self):
        text = lg.render_text(BASE) + "- yesterday | a | r | USER_PROFILE | FOUND | x@y.com\n"
        _, report = ingest_text("text", text)
        self.assertEqual(report["unparsed"], 1)

    def test_a_dash_line_without_pipes_is_prose(self):
        text = lg.render_text(BASE) + "- remember to lock the door\n"
        _, report = ingest_text("text", text)
        self.assertEqual(report["unparsed"], 0)

    def test_a_byte_identical_repeated_row_is_written_once(self):
        r = lg.text_line(BASE[0])
        f, report = ingest_text("text", lg.render_text(BASE) + r + "\n")
        self.assertEqual(len(obs(f.conn, "account_1")), 1)
        self.assertEqual(len(notes(report, "duplicate_line")), 1)

    def test_uppercase_emails_in_the_text_link(self):
        f, _ = ingest_text("text", lg.render_text([lg.account(T1, "account_1", "NOT_FOUND",
                                                               ANA.upper())]))
        self.assertEqual(res(dossier_for(f), "account_1")["state"], "ABSENT")

    def test_locator_is_the_line_number(self):
        f, _ = ingest_text("text", lg.render_text(BASE))
        self.assertEqual(one(f.conn, "account_1")["locator"], "L6")

    def test_detect_refuses_a_note_that_is_mostly_damaged_rows(self):
        bad = "".join(f"- {T1} | account_service | account_{i} | USER_PROFILE | FOUND\n"
                      for i in range(5))
        self.assertIsNone(ledger_text.detect(lg.render_text(BASE * 5) + bad))
        self.assertEqual(ledger_text.detect(lg.render_text(BASE * 20) + bad[:90] + "\n"),
                         "ledger_text")

    def test_detect_needs_rows(self):
        self.assertIsNone(ledger_text.detect("CUSTOMER PLATFORM - SHIFT HANDOVER\nSnapshot: x\n"))
        self.assertIsNone(ledger_text.detect("a | b | c | d | e | f\n"))
        self.assertEqual(ledger_text.detect(lg.render_text(BASE)), "ledger_text")


if __name__ == "__main__":
    unittest.main()
