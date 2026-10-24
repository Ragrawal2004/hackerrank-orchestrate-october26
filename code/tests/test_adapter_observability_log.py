import json
import tempfile
import unittest
from pathlib import Path

from ghost import adapters, dossier, policy
from ghost.adapters import logtok, observability_log as olog
from ghost.adapters import inventory_sql, replica_sql, service_export, snapshot_bundle
from tests import bundle_fixture as bf
from tests import export_fixture as ef
from tests import log_fixture as lf
from tests import sql_fixture as fx
from tests.fact_builder import Facts, case, sha

AUDIT = "2026-09-15T12:00:00Z"
T0 = "2026-09-15T08:00:00Z"
T1 = "2026-09-15T08:51:00Z"
T2 = "2026-09-15T08:51:05Z"
T3 = "2026-09-15T08:51:09Z"
T4 = "2026-09-15T08:51:20Z"
AFTER = "2026-09-15T12:30:00Z"
MANIFEST = "2026-09-15T11:59:00Z"
MANIFEST_LATE = "2026-09-17T12:00:00Z"
ANA = "ana@example.com"
BEN = "ben@example.com"
PHONE = "+1 555 0100"
HASH = sha(ANA)


def users():
    return Facts().user("user_1", ANA, phone=PHONE, name="Ana Lima").user("user_2", BEN)


def load(text, file_id="evidence_01", manifest=MANIFEST, facts=None):
    f = facts or users()
    f.file(file_id, manifest)
    report = olog.ingest(f.conn, file_id, text)
    return f, report


def obs(conn, rid=None, file_id=None):
    q = ("select obs_id, file_id, locator, record_at, system_id, resource_id, category, state, "
         "state_basis, attrs_json from observation where 1=1")
    args = []
    if rid is not None:
        q += " and resource_id = ?"
        args.append(rid)
    if file_id is not None:
        q += " and file_id = ?"
        args.append(file_id)
    out = []
    for r in conn.execute(q + " order by obs_id", args):
        d = dict(zip(("obs_id", "file_id", "locator", "record_at", "system_id", "resource_id",
                      "category", "state", "state_basis", "attrs"), r))
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


def body(syntax, *pairs):
    return lf.obs_file(syntax, *pairs)


def state_line(syntax, ts, svc, event, rid, trace="tr-1", **kv):
    return lf.line(syntax, ts, svc, event, trace, resource_id=rid, **kv)


# ---- tokenizer ---------------------------------------------------------------------------------

class TokenizerTests(unittest.TestCase):
    def test_every_syntax_round_trips_one_line(self):
        for syn in lf.SYNTAXES:
            with self.subTest(syn):
                text = lf.line(syn, T2, "account_service", "profile_lookup_completed", "tr-1234567",
                               level="INFO", resource_id="account_1", rows=0, http_status=404)
                got = logtok.parse_line(syn, text)
                self.assertIsNotNone(got)
                self.assertEqual((got.ts, got.level, got.service, got.trace_id, got.event),
                                 (T2, "INFO", "account_service", "tr-1234567",
                                  "profile_lookup_completed"))
                self.assertEqual(got.kv["resource_id"], "account_1")
                self.assertEqual(got.kv["rows"], "0")
                self.assertEqual(got.kv["http_status"], "404")
                self.assertEqual(got.kv["instance"], "account-1")

    def test_a_line_only_parses_under_its_own_syntax(self):
        for syn in lf.SYNTAXES:
            text = lf.line(syn, T2, "search_service", "index_query_started", "tr-1",
                           match_key=ANA)
            for other in lf.SYNTAXES:
                with self.subTest(syn=syn, other=other):
                    parsed = logtok.parse_line(other, text)
                    if other == syn:
                        self.assertIsNotNone(parsed)
                    else:
                        self.assertIsNone(parsed)

    def test_null_becomes_none(self):
        for syn in lf.SYNTAXES:
            with self.subTest(syn):
                got = logtok.parse_line(syn, lf.line(
                    syn, T2, "analytics_platform", "analytics_profile_read", "tr-1",
                    resource_id="analytics_1", direct_identifiers=0, email=None, phone=None))
                self.assertIsNone(got.kv["email"])
                self.assertIsNone(got.kv["phone"])

    def test_json_real_null_and_bool_are_normalised(self):
        text = (T2 + ' {"severity":"INFO","service":"integration_service","instance":"i-1",'
                '"trace_id":"tr-1","event":"connector_heartbeat","resource_id":"data_flow_1",'
                '"enabled":true,"scheduler_registered":true,"correlation_key":null}')
        got = logtok.parse_line("json_line", text)
        self.assertEqual(got.kv["enabled"], "true")
        self.assertIsNone(got.kv["correlation_key"])

    def test_quotes_backslashes_and_spaces_survive_every_syntax(self):
        tricky = 'He said "hi" \\ then left = ok'
        for syn in lf.SYNTAXES:
            with self.subTest(syn):
                got = logtok.parse_line(syn, lf.line(
                    syn, T2, "support_platform", "message_read", "tr-1", resource_id="support_1",
                    subject_ref="user_1", message_text=tricky))
                self.assertEqual(got.kv["message_text"], tricky)
                self.assertEqual(got.kv["subject_ref"], "user_1")

    def test_aws_message_is_unescaped_before_splitting(self):
        text = (T2 + ' requestId=tr-9 level=INFO function=search_service message="instance=\\"s-1\\"'
                ' event=\\"index_query_completed\\" resource_id=\\"search_1\\" result=\\"hit\\"'
                ' correlation_key=\\"ana@example.com\\""')
        got = logtok.parse_line("aws_kv", text)
        self.assertEqual((got.trace_id, got.service, got.event), ("tr-9", "search_service",
                                                                  "index_query_completed"))
        self.assertEqual(got.kv["correlation_key"], ANA)
        self.assertEqual(got.kv["result"], "hit")

    def test_slot_equal_to_instance_is_not_a_trace(self):
        for syn in ("syslog5424", "bracketed", "pipe", "aws_kv"):
            with self.subTest(syn):
                got = logtok.parse_line(syn, lf.noise(syn, T2))
                self.assertIsNone(got.trace_id)
                self.assertEqual(got.event, "cache_refresh")

    def test_trace_field_is_used_when_present(self):
        for syn in ("logfmt", "json_line"):
            with self.subTest(syn):
                self.assertIsNone(logtok.parse_line(syn, lf.noise(syn, T2)).trace_id)
                got = logtok.parse_line(syn, lf.line(syn, T2, "account_service", "cache_lookup",
                                                     "tr-5", result="miss"))
                self.assertEqual(got.trace_id, "tr-5")

    def test_malformed_lines_are_none(self):
        good = {syn: lf.line(syn, T2, "account_service", "profile_lookup_started", "tr-1",
                             lookup_key=ANA) for syn in lf.SYNTAXES}
        for syn, text in good.items():
            for bad in (text[:len(text) // 2] + '"', text[21:], "garbage " + text, "", "   ",
                        text.replace("2026-09-15T08:51:05Z", "15/09/2026 08:51")):
                with self.subTest(syn=syn, bad=bad[:40]):
                    self.assertIsNone(logtok.parse_line(syn, bad))

    def test_a_line_without_an_event_is_none(self):
        self.assertIsNone(logtok.parse_line("logfmt", f"{T2} level=INFO service=x instance=i-1"))
        self.assertIsNone(logtok.parse_line(
            "json_line", T2 + ' {"severity":"INFO","service":"x","instance":"i-1"}'))
        self.assertIsNone(logtok.parse_line("json_line", T2 + ' ["not", "an", "object"]'))

    def test_file_syntax_is_the_dominant_one(self):
        lines = [lf.noise("pipe", T2)] * 5 + [lf.noise("logfmt", T2)] + ["", "  "]
        self.assertEqual(logtok.file_syntax(lines), "pipe")

    def test_file_syntax_tie_or_nothing_is_none(self):
        self.assertIsNone(logtok.file_syntax([lf.noise("pipe", T2), lf.noise("logfmt", T2)]))
        self.assertIsNone(logtok.file_syntax(["nothing here", ""]))
        self.assertIsNone(logtok.file_syntax([]))


# ---- detection ---------------------------------------------------------------------------------

def sample_file(syn):
    return body(syn, *lf.lookup_pair(syn, T1, T2, "tr-1", ANA, "account_1"))


class DetectTests(unittest.TestCase):
    def test_every_syntax_is_claimed_with_its_format(self):
        for syn in lf.SYNTAXES:
            with self.subTest(syn):
                self.assertEqual(olog.detect(sample_file(syn)), "log_obs_" + syn)

    def test_other_log_shapes_are_not_claimed(self):
        for name, text in (("trace", lf.trace_log()), ("ledger", lf.ledger_log()),
                           ("support_replica", lf.support_replica_log())):
            with self.subTest(name):
                self.assertIsNone(olog.detect(text))

    def test_other_evidence_is_not_claimed(self):
        sql = fx.j_sql("2026-09-21T10:00:00Z", {"customer_data_primary": [],
                                                  "customer_data_read_replica": []})
        bundle = bf.bundle([bf.snap(T2, "account_service", "customer_profiles",
                                    [bf.profile("account_1", ANA)])])
        export = ef.one("account_service", "profile_lookups", [ef.profile_lookup("account_1", T2)])
        for text in (sql, bundle, export, "", "\n\n", "not a log", "[" * 100000):
            with self.subTest(text[:30]):
                self.assertIsNone(olog.detect(text))

    def test_a_file_of_only_noise_is_not_claimed(self):
        for syn in lf.SYNTAXES:
            with self.subTest(syn):
                self.assertIsNone(olog.detect(lf.log(lf.envelope_start(syn, T0), lf.noise(syn, T1),
                                                     lf.envelope_end(syn, T4))))

    def test_a_file_without_any_state_event_is_not_claimed(self):
        for syn in lf.SYNTAXES:
            with self.subTest(syn):
                started = [lf.line(syn, T1, "account_service", "profile_lookup_started", f"tr-{i}",
                                   level="DEBUG", lookup_key=ANA, attempt=1) for i in range(3)]
                self.assertIsNone(olog.detect(lf.log(*started)))

    def test_mostly_unparseable_file_is_not_claimed(self):
        good = sample_file("pipe").splitlines()
        junk = [f"2026-09-15T08:5{i}:00Z ### torn write {i} ###" for i in range(len(good))]
        self.assertIsNone(olog.detect(lf.log(*good, *junk)))

    def test_lines_without_instance_are_not_observability(self):
        text = lf.log(*[l.replace("instance=account-1 ", "") for l in
                        lf.lookup_pair("logfmt", T1, T2, "tr-1", ANA, "account_1")])
        self.assertIsNone(olog.detect(text))

    def test_without_any_region_it_is_not_claimed(self):
        text = lf.log(*[lf.line("logfmt", T2, "account_service", "profile_lookup_completed",
                                "tr-1", region=None, resource_id="account_1", rows=0,
                                http_status=404)] * 3)
        self.assertIsNone(olog.detect(text))

    def test_a_tied_mix_of_syntaxes_is_not_claimed(self):
        text = lf.log(*lf.lookup_pair("pipe", T1, T2, "tr-1", ANA, "account_1"),
                      *lf.lookup_pair("bracketed", T1, T2, "tr-2", BEN, "account_2"))
        self.assertIsNone(olog.detect(text))

    def test_a_few_bad_lines_do_not_stop_detection(self):
        good = [l for l in sample_file("pipe").splitlines()] * 4
        self.assertEqual(olog.detect(lf.log(*good, "### corrupted ###")), "log_obs_pipe")

    def test_existing_adapters_do_not_claim_a_log(self):
        for syn in lf.SYNTAXES:
            text = sample_file(syn)
            for a in (replica_sql, snapshot_bundle, service_export, inventory_sql):
                with self.subTest(syn=syn, adapter=a.name):
                    self.assertIsNone(a.detect(text))

    def test_registry_lists_the_adapter_without_double_claims(self):
        self.assertIn(olog, adapters.ADAPTERS)
        for syn in lf.SYNTAXES:
            with self.subTest(syn):
                self.assertEqual(adapters.select(sample_file(syn))[0].name, "observability_log")

    def test_file_name_is_irrelevant(self):
        with tempfile.TemporaryDirectory() as d:
            (Path(d) / "evidence").mkdir()
            (Path(d) / "evidence" / "notes.json").write_text(sample_file("aws_kv"),
                                                              encoding="utf-8")
            f = users()
            f.conn.execute("insert into evidence_file(file_id,path,origin,format,adapter,manifest_at)"
                           " values ('evidence_07','evidence/notes.json','x',null,null,?)",
                           (MANIFEST,))
            summary = adapters.load_evidence(f.conn, d)
            got = f.conn.execute("select format, adapter from evidence_file").fetchone()
        self.assertEqual(got, ("log_obs_aws_kv", "observability_log"))
        self.assertEqual(summary["claimed"], ["evidence_07"])

    def test_ingest_of_a_non_log_is_an_error(self):
        f = users()
        f.file("evidence_01", MANIFEST)
        with self.assertRaises(ValueError):
            olog.ingest(f.conn, "evidence_01", lf.trace_log())
        with self.assertRaises(ValueError):
            olog.ingest(f.conn, "evidence_99", sample_file("logfmt"))


# ---- ingest: events -> observations ------------------------------------------------------------

# (service, event, kv, collection, state)
STATE_EVENTS = [
    ("account_service", "profile_lookup_completed", {"rows": 1, "http_status": 200},
     "customer_profiles", "PRESENT"),
    ("account_service", "profile_lookup_completed", {"rows": 0, "http_status": 404},
     "customer_profiles", "ABSENT"),
    ("search_service", "index_query_completed", {"index": "customer_directory_v3", "result": "hit"},
     "customer_search_documents", "PRESENT"),
    ("search_service", "index_query_completed", {"index": "customer_directory_v3", "result": "miss"},
     "customer_search_documents", "ABSENT"),
    ("security_service", "audit_record_read_completed", {"rows": 1, "integrity_check": "passed"},
     "security_audit_events", "PRESENT"),
    ("support_platform", "message_pii_scan", {"matched_value": ANA, "detector": "direct_identifier"},
     "support_conversations", "IDENTIFIABLE"),
    ("support_platform", "message_read", {"subject_ref": "user_1", "message_text": "[REDACTED]"},
     "support_conversations", "REDACTED"),
    ("support_platform", "message_read", {"subject_ref": "user_1",
                                          "message_text": f"Please reply to {ANA} today"},
     "support_conversations", "IDENTIFIABLE"),
    ("support_platform", "attachment_pii_scan", {"matched_value": ANA, "file": "invoice_1.pdf",
                                                 "detector": "direct_identifier"},
     "ticket_attachments", "PRESENT"),
    ("analytics_platform", "analytics_identity_scan", {"matched_value": ANA,
                                                       "direct_identifiers": 1,
                                                       "detector": "direct_identifier"},
     "customer_analytics_reports", "IDENTIFIABLE"),
    ("analytics_platform", "analytics_profile_read", {"direct_identifiers": 0, "email": None,
                                                      "phone": None},
     "customer_analytics_reports", "ANONYMIZED"),
    ("analytics_platform", "contact_lookup_completed", {"rows": 1, "email_opt_in": True,
                                                        "correlation_key": HASH},
     "contact_lookups", "PRESENT"),
    ("integration_service", "connector_heartbeat", {"enabled": True, "scheduler_registered": True,
                                                    "last_run": T1},
     "connector_configurations", "ACTIVE"),
    ("integration_service", "connector_config_loaded", {"enabled": False,
                                                        "scheduler_registered": False},
     "connector_configurations", "DISABLED"),
    ("backup_service", "restore_candidate_evaluated", {"eligible": True, "deletion_block": False},
     "backup_catalog", "RESTORABLE"),
    ("backup_service", "restore_candidate_evaluated", {"eligible": False, "deletion_block": True},
     "backup_catalog", "BLOCKED"),
    ("backup_service", "restore_candidate_evaluated", {"eligible": True, "deletion_block": True},
     "backup_catalog", "BLOCKED"),
    ("backup_service", "restore_authorization_completed", {"decision": "deny",
                                                           "policy": "privacy_deletion"},
     "backup_catalog", "BLOCKED"),
    ("backup_service", "restore_authorization_completed", {"decision": "allow",
                                                           "policy": "privacy_deletion"},
     "backup_catalog", "RESTORABLE"),
    ("export_service", "export_download_check_completed", {"available": True,
                                                           "file": "customer_data.zip"},
     "customer_export_jobs", "PRESENT"),
    ("legal_service", "retained_order_loaded", {"reason": "payment_dispute_and_tax_record",
                                                "access": "allowed"},
     "retained_order_records", "PRESENT"),
]

STARTED = [("account_service", "profile_lookup_started", "lookup_key"),
           ("search_service", "index_query_started", "match_key"),
           ("security_service", "audit_record_query_started", "subject_ref"),
           ("support_platform", "message_scan_started", "subject_key"),
           ("support_platform", "attachment_scan_started", "scan_key"),
           ("analytics_platform", "analytics_profile_scan_started", "lookup_key"),
           ("integration_service", "connector_config_read_started", "subject_key"),
           ("backup_service", "restore_policy_check_started", "subject_key"),
           ("export_service", "export_download_check_started", "owner_key"),
           ("legal_service", "retained_order_query_started", "subject_ref")]


class EventMappingTests(unittest.TestCase):
    def test_every_state_event_maps_to_collection_category_and_state(self):
        for syn in ("logfmt", "aws_kv"):
            for svc, event, kv, collection, state in STATE_EVENTS:
                with self.subTest(syn=syn, event=event, state=state):
                    f, report = load(body(syn, state_line(syn, T2, svc, event, "res_1", **kv)))
                    o = one(f.conn, "res_1")
                    self.assertEqual(o["system_id"], svc)
                    self.assertEqual(o["state"], state)
                    self.assertEqual(o["category"], policy.COLLECTION_CATEGORY[collection])
                    self.assertEqual(o["attrs"]["collection"], collection)
                    self.assertEqual(o["attrs"]["event"], event)
                    self.assertEqual(o["state_basis"], "explicit")
                    self.assertEqual(report["unparsed"], 0, report["notes"])

    def test_contact_lookup_is_a_secondary_identifier(self):
        self.assertEqual(policy.COLLECTION_CATEGORY["contact_lookups"], "SECONDARY_IDENTIFIER")

    def test_started_lines_are_resource_less_link_observations(self):
        for svc, event, field in STARTED:
            with self.subTest(event):
                f, report = load(body("pipe", lf.line("pipe", T1, svc, event, "tr-7", level="DEBUG",
                                                      attempt=1, **{field: ANA})))
                rows = [o for o in obs(f.conn) if o["attrs"].get("event") == event]
                self.assertEqual(len(rows), 1)
                o = rows[0]
                self.assertIsNone(o["resource_id"])
                self.assertIsNone(o["state"])
                self.assertEqual(o["system_id"], svc)
                self.assertEqual(keys_of(f.conn, o["obs_id"]), [("email", ANA)])
                self.assertEqual(joins_of(f.conn, o["obs_id"]), [("trace_id", "tr-7")])
                self.assertEqual(report["unparsed"], 0, report["notes"])

    def test_record_at_is_the_line_time_and_locator_the_line_number(self):
        text = body("bracketed", *lf.lookup_pair("bracketed", T1, T2, "tr-1", ANA, "account_1"))
        f, _ = load(text)
        o = one(f.conn, "account_1")
        self.assertEqual(o["record_at"], T2)
        n = text.splitlines().index(next(l for l in text.splitlines() if "account_1" in l)) + 1
        self.assertEqual(o["locator"], f"L{n}")
        self.assertEqual(o["attrs"]["syntax"], "bracketed")
        self.assertEqual(o["attrs"]["trace_id"], "tr-1")

    def test_completed_lines_carry_the_trace_join_key(self):
        f, report = load(body("syslog5424", *lf.lookup_pair("syslog5424", T1, T2, "tr-3", ANA,
                                                            "account_1")))
        self.assertEqual(joins_of(f.conn, one(f.conn, "account_1")["obs_id"]), [("trace_id", "tr-3")])
        self.assertEqual(report["join_keys"], 2)

    def test_subject_keys_are_classified_by_shape(self):
        lines = [state_line("logfmt", T2, "security_service", "audit_record_read_completed",
                            "audit_record_1", rows=1, integrity_check="passed",
                            correlation_key="user_1"),
                 state_line("logfmt", T2, "analytics_platform", "contact_lookup_completed",
                            "analytics_1", rows=1, email_opt_in=True, correlation_key=HASH.upper()),
                 state_line("logfmt", T2, "search_service", "index_query_completed", "search_1",
                            index="v3", result="hit", correlation_key=ANA.upper()),
                 state_line("logfmt", T2, "support_platform", "message_pii_scan", "support_1",
                            matched_value=PHONE, detector="direct_identifier")]
        f, _ = load(body("logfmt", *lines))
        self.assertEqual(keys_of(f.conn, one(f.conn, "audit_record_1")["obs_id"]), [("user_id", "user_1")])
        self.assertEqual(keys_of(f.conn, one(f.conn, "analytics_1")["obs_id"]), [("email_hash", HASH)])
        self.assertEqual(keys_of(f.conn, one(f.conn, "search_1")["obs_id"]), [("email", ANA)])
        self.assertEqual(keys_of(f.conn, one(f.conn, "support_1")["obs_id"]), [("phone", PHONE)])

    def test_matched_value_is_kept_as_the_exposed_identifier(self):
        f, _ = load(body("json_line", state_line("json_line", T2, "support_platform",
                                                 "message_pii_scan", "support_1",
                                                 matched_value=ANA.upper(),
                                                 detector="direct_identifier")))
        self.assertEqual(one(f.conn, "support_1")["attrs"]["email"], ANA)

    def test_a_non_identifier_lookup_key_gives_no_subject_key(self):
        f, report = load(body("pipe", lf.line("pipe", T1, "analytics_platform",
                                              "analytics_profile_scan_started", "tr-1",
                                              lookup_key="analytics_257", attempt=1)))
        started = [o for o in obs(f.conn) if o["attrs"].get("event") == "analytics_profile_scan_started"]
        self.assertEqual(keys_of(f.conn, started[0]["obs_id"]), [])
        self.assertEqual(len(notes(report, "non_identifier_key")), 1)
        self.assertEqual(report["unparsed"], 0)

    def test_no_capture_scope_is_written(self):
        f, _ = load(body("logfmt", *lf.lookup_pair("logfmt", T1, T2, "tr-1", ANA, "account_1")))
        self.assertEqual(f.conn.execute("select count(*) from capture_scope").fetchone()[0], 0)

    def test_reingest_is_idempotent(self):
        text = body("pipe", *lf.lookup_pair("pipe", T1, T2, "tr-1", ANA, "account_1"))
        f, first = load(text)
        second = olog.ingest(f.conn, "evidence_01", text)
        self.assertEqual(first, second)
        for t in ("observation", "subject_key", "join_key", "parse_report"):
            with self.subTest(t):
                n = f.conn.execute(f"select count(*) from {t}").fetchone()[0]
                self.assertEqual(n, {"observation": 2, "subject_key": 1, "join_key": 2,
                                     "parse_report": 1}[t])

    def test_evidence_file_gets_format_and_adapter(self):
        f, _ = load(sample_file("json_line"))
        self.assertEqual(f.conn.execute("select format, adapter from evidence_file").fetchone(),
                         ("log_obs_json_line", "observability_log"))

    def test_service_names_are_kept_as_logged(self):
        f, report = load(body("aws_kv", state_line("aws_kv", T2, "profile_service",
                                                   "profile_lookup_completed", "account_12345",
                                                   rows=1, http_status=200,
                                                   correlation_key=ANA)))
        o = one(f.conn, "account_12345")
        self.assertEqual(o["system_id"], "profile_service")
        self.assertEqual(o["category"], "USER_PROFILE")
        self.assertEqual([n["service"] for n in notes(report, "unmapped_service")],
                         ["profile_service"])

    def test_export_completed_time_differing_from_manifest_is_noted(self):
        f, report = load(sample_file("logfmt"), manifest="2026-09-15T11:59:00Z")
        self.assertEqual(notes(report, "capture_time_mismatch"), [])
        f, report = load(sample_file("logfmt"), manifest="2026-09-15T13:00:00Z")
        self.assertEqual(len(notes(report, "capture_time_mismatch")), 1)


# ---- accounting --------------------------------------------------------------------------------

class AccountingTests(unittest.TestCase):
    def test_report_counts_meaningful_parsed_unparsed_and_noise(self):
        f, report = load(body("logfmt", *lf.lookup_pair("logfmt", T1, T2, "tr-1", ANA, "account_1"),
                              lf.line("logfmt", T3, "account_service", "cache_lookup", "tr-1",
                                      result="miss", cache="v2")))
        self.assertEqual((report["meaningful"], report["parsed"], report["unparsed"],
                          report["observations"]), (2, 2, 0, 2))
        # envelope start/end, the dns noise line, the cache line
        self.assertEqual(report["noise"], 4)
        row = f.conn.execute("select adapter, meaningful, parsed, unparsed from parse_report"
                             ).fetchone()
        self.assertEqual(row, ("observability_log", 2, 2, 0))

    def test_noise_events_table_counts_each_event(self):
        _, report = load(body("pipe", *lf.lookup_pair("pipe", T1, T2, "tr-1", ANA, "account_1"),
                              lf.line("pipe", T3, "account_service", "dependency_retry", "tr-1",
                                      level="WARN", attempt=1, reason="read_timeout"),
                              lf.line("pipe", T3, "account_service", "dependency_recovered",
                                      "tr-1", attempt=2, route="primary")))
        table = notes(report, "noise_events")
        self.assertEqual(len(table), 1)
        self.assertEqual(table[0]["counts"], {"cache_refresh": 1, "dependency_recovered": 1,
                                              "dependency_retry": 1, "export_completed": 1,
                                              "export_started": 1})

    def test_unlisted_event_with_resource_is_unparsed(self):
        _, report = load(body("logfmt", state_line("logfmt", T2, "account_service",
                                                   "profile_purge_completed", "account_1",
                                                   rows=0)))
        self.assertEqual(report["unparsed"], 1)
        got = notes(report, "unlisted_event")
        self.assertEqual([(n["event"], n["locator"]) for n in got],
                         [("profile_purge_completed", "L3")])

    def test_unlisted_event_with_subject_key_is_unparsed(self):
        for field in ("lookup_key", "correlation_key", "subject_ref", "matched_value"):
            with self.subTest(field):
                _, report = load(body("pipe", lf.line("pipe", T2, "account_service",
                                                      "profile_sweep", "tr-1", **{field: ANA})))
                self.assertEqual(report["unparsed"], 1)
                self.assertEqual(len(notes(report, "unlisted_event")), 1)

    def test_unlisted_event_without_resource_or_key_is_noise(self):
        _, report = load(body("pipe", lf.line("pipe", T2, "scheduler", "timer_tick", "tr-1",
                                              unit="logrotate.timer")))
        self.assertEqual(report["unparsed"], 0)
        self.assertEqual(notes(report, "unlisted_event"), [])
        self.assertEqual(notes(report, "noise_events")[0]["counts"]["timer_tick"], 1)

    def test_state_event_without_resource_is_unparsed(self):
        _, report = load(body("logfmt", lf.line("logfmt", T2, "account_service",
                                                "profile_lookup_completed", "tr-1", rows=0,
                                                http_status=404)))
        self.assertEqual(report["unparsed"], 1)

    def test_unknown_outcome_values_are_unparsed(self):
        cases = [("account_service", "profile_lookup_completed", {"rows": 2, "http_status": 200}),
                 ("account_service", "profile_lookup_completed", {"rows": 1, "http_status": 404}),
                 ("account_service", "profile_lookup_completed", {"rows": 0, "http_status": 200}),
                 ("account_service", "profile_lookup_completed", {"http_status": 404}),
                 ("search_service", "index_query_completed", {"result": "partial"}),
                 ("security_service", "audit_record_read_completed", {"rows": 0}),
                 ("integration_service", "connector_heartbeat", {"enabled": "maybe"}),
                 ("backup_service", "restore_candidate_evaluated", {"eligible": True}),
                 ("backup_service", "restore_authorization_completed", {"decision": "defer"}),
                 ("export_service", "export_download_check_completed", {"available": False}),
                 ("analytics_platform", "analytics_profile_read",
                  {"direct_identifiers": 0, "email": ANA, "phone": None}),
                 ("support_platform", "message_pii_scan", {"detector": "direct_identifier"})]
        for svc, event, kv in cases:
            with self.subTest(event=event, kv=kv):
                f, report = load(body("logfmt", state_line("logfmt", T2, svc, event, "res_1", **kv)))
                self.assertEqual(report["unparsed"], 1, report["notes"])
                self.assertEqual(obs(f.conn, "res_1"), [])

    def test_a_line_in_another_syntax_is_unparsed(self):
        text = body("pipe", *lf.lookup_pair("pipe", T1, T2, "tr-1", ANA, "account_1"),
                    *lf.lookup_pair("logfmt", T3, T4, "tr-2", BEN, "account_2"))
        f, report = load(text)
        self.assertEqual(report["unparsed"], 2)
        self.assertEqual(len(notes(report, "syntax_mismatch")), 2)
        self.assertEqual(obs(f.conn, "account_2"), [])
        self.assertEqual(one(f.conn, "account_1")["state"], "ABSENT")

    def test_blank_lines_are_ignored(self):
        text = sample_file("logfmt").replace("\n", "\n\n")
        _, report = load(text)
        self.assertEqual(report["unparsed"], 0)
        self.assertEqual(report["meaningful"], 2)


# ---- lines that could mislead ------------------------------------------------------------------

class MisleadingLineTests(unittest.TestCase):
    def test_failed_operation_that_looks_like_success_sets_no_state(self):
        for syn in lf.SYNTAXES:
            with self.subTest(syn):
                text = body(syn, *lf.lookup_pair(syn, T1, T2, "tr-1", ANA, "account_1", rows=0,
                                                 status=503))
                f, report = load(text)
                self.assertEqual(obs(f.conn, "account_1"), [])
                self.assertEqual(report["unparsed"], 0)
                self.assertEqual([n["resource_id"] for n in notes(report, "operation_failed")],
                                 ["account_1"])
                self.assertIsNone(res(dossier_for(f), "account_1"))

    def test_other_failure_shapes_set_no_state(self):
        lines = [state_line("logfmt", T2, "search_service", "index_query_completed", "search_1",
                            index="v3", result="timeout", correlation_key=ANA),
                 state_line("logfmt", T2, "search_service", "index_query_completed", "search_2",
                            index="v3", result="error", correlation_key=ANA),
                 state_line("logfmt", T2, "account_service", "profile_lookup_failed", "account_1",
                            http_status=500, correlation_key=ANA)]
        f, report = load(body("logfmt", *lines))
        self.assertEqual([o["resource_id"] for o in obs(f.conn) if o["resource_id"]], [])
        self.assertEqual(report["unparsed"], 0, report["notes"])
        self.assertEqual(len(notes(report, "operation_failed")), 3)

    def test_retry_after_failure_keeps_only_the_real_completion(self):
        syn = "json_line"
        lines = [lf.line(syn, T1, "account_service", "profile_lookup_started", "tr-1",
                         level="DEBUG", lookup_key=ANA, attempt=1),
                 state_line(syn, T2, "account_service", "profile_lookup_completed", "account_1",
                            rows=0, http_status=503),
                 lf.line(syn, T2, "account_service", "dependency_retry", "tr-1", level="WARN",
                         attempt=1, reason="read_timeout"),
                 lf.line(syn, T3, "account_service", "dependency_recovered", "tr-1", attempt=2,
                         route="primary"),
                 state_line(syn, T4, "account_service", "profile_lookup_completed", "account_1",
                            rows=1, http_status=200)]
        f, report = load(body(syn, *lines))
        self.assertEqual([(o["state"], o["record_at"]) for o in obs(f.conn, "account_1")],
                         [("PRESENT", T4)])
        self.assertEqual(len(notes(report, "operation_failed")), 1)
        r = res(dossier_for(f), "account_1")
        self.assertEqual(r["state"], "PRESENT")
        self.assertEqual(r["link"]["method"], "trace")

    def test_two_real_completions_keep_both_and_the_later_wins(self):
        syn = "pipe"
        lines = [*lf.lookup_pair(syn, T1, T2, "tr-1", ANA, "account_1", rows=1, status=200),
                 state_line(syn, T4, "account_service", "profile_lookup_completed", "account_1",
                            rows=0, http_status=404)]
        f, report = load(body(syn, *lines))
        self.assertEqual([o["state"] for o in obs(f.conn, "account_1")], ["PRESENT", "ABSENT"])
        self.assertEqual([n["resource_id"] for n in notes(report, "retried_completion")],
                         ["account_1"])
        self.assertEqual(res(dossier_for(f), "account_1")["state"], "ABSENT")

    def test_line_after_audit_neither_links_nor_sets_state(self):
        syn = "bracketed"
        lines = [*lf.lookup_pair(syn, T1, T2, "tr-1", ANA, "account_1", rows=1, status=200),
                 state_line(syn, AFTER, "account_service", "profile_lookup_completed",
                            "account_1", rows=0, http_status=404),
                 *lf.lookup_pair(syn, AFTER, AFTER, "tr-2", ANA, "search_late")]
        f, _ = load(body(syn, *lines), manifest=MANIFEST_LATE)
        d = dossier_for(f)
        self.assertEqual(res(d, "account_1")["state"], "PRESENT")
        self.assertIsNone(res(d, "search_late"))
        self.assertEqual(d["clock"]["late_records_excluded"], 1)
        late = [o for o in obs(f.conn, "account_1") if o["record_at"] == AFTER]
        self.assertEqual(len(late), 1)

    def test_early_lines_of_a_late_file_link_but_are_not_cited(self):
        f, _ = load(body("aws_kv", *lf.lookup_pair("aws_kv", T1, T2, "tr-1", ANA, "account_1")),
                    manifest=MANIFEST_LATE)
        d = dossier_for(f)
        self.assertEqual(res(d, "account_1")["state"], "ABSENT")
        self.assertEqual(d["anchor_candidates"], [])
        self.assertEqual(d["clock"]["late_manifest_files_used"], ["evidence_01"])

    def test_line_after_manifest_is_noted(self):
        _, report = load(body("logfmt", *lf.lookup_pair("logfmt", T1, AFTER, "tr-1", ANA,
                                                        "account_1")))
        self.assertEqual([n["locator"] for n in notes(report, "record_after_manifest")], ["L4"])

    def test_duplicate_line_writes_one_observation(self):
        pair = lf.lookup_pair("syslog5424", T1, T2, "tr-1", ANA, "account_1")
        f, report = load(body("syslog5424", pair[0], pair[1], pair[1]))
        self.assertEqual(len(obs(f.conn, "account_1")), 1)
        self.assertEqual([n["locator"] for n in notes(report, "duplicate_line")], ["L5"])
        self.assertEqual((report["meaningful"], report["unparsed"]), (2, 0))
        self.assertEqual(notes(report, "retried_completion"), [])

    def test_unparseable_line_is_counted_and_the_rest_still_ingested(self):
        pair = lf.lookup_pair("logfmt", T1, T2, "tr-1", ANA, "account_1")
        f, report = load(body("logfmt", pair[0], "2026-09-15T08:51:06Z ### torn write ###", pair[1]))
        self.assertEqual(report["unparsed"], 1)
        self.assertEqual([n["locator"] for n in notes(report, "line_unparsed")], ["L4"])
        self.assertEqual(one(f.conn, "account_1")["state"], "ABSENT")
        summ = adapters.summary(f.conn)
        self.assertIn("WARNING", adapters.format_summary(summ, 0, 0))


# ---- end to end through the dossier ------------------------------------------------------------

class DossierTests(unittest.TestCase):
    def scenario(self, syn):
        return body(
            syn,
            *lf.lookup_pair(syn, T1, T2, "tr-1", ANA, "account_1"),
            state_line(syn, T2, "security_service", "audit_record_read_completed",
                       "audit_record_1", "tr-2", rows=1, integrity_check="passed",
                       correlation_key="user_1"),
            lf.line(syn, T2, "integration_service", "connector_config_read_started", "tr-3",
                    level="DEBUG", subject_key=ANA, attempt=1),
            lf.line(syn, T3, "integration_service", "connection_acquired", "tr-3", pool="p",
                    wait_ms=20),
            state_line(syn, T3, "integration_service", "connector_heartbeat", "data_flow_1",
                       "tr-3", enabled=True, scheduler_registered=True, last_run=T1),
            lf.line(syn, T3, "backup_service", "restore_policy_check_started", "tr-4",
                    level="DEBUG", subject_key=ANA, attempt=1),
            state_line(syn, T4, "backup_service", "restore_candidate_evaluated", "backup_1",
                       "tr-4", eligible=True, deletion_block=False),
            *lf.lookup_pair(syn, T3, T4, "tr-5", BEN, "account_2"))

    def test_trace_joins_link_resources_in_every_syntax(self):
        for syn in lf.SYNTAXES:
            with self.subTest(syn):
                f, report = load(self.scenario(syn))
                self.assertEqual(report["unparsed"], 0, report["notes"])
                d = dossier_for(f)
                got = {r["resource_id"]: (r["system_id"], r["state"], r["link"]["method"])
                       for r in d["resources"]}
                self.assertEqual(got, {
                    "account_1": ("account_service", "ABSENT", "trace"),
                    "audit_record_1": ("security_service", "PRESENT", "direct"),
                    "data_flow_1": ("integration_service", "ACTIVE", "trace"),
                    "backup_1": ("backup_service", "RESTORABLE", "trace")})
                self.assertEqual(d["anchor_candidates"], ["evidence_01"])

    def test_a_trace_that_names_another_subject_is_blocked(self):
        syn = "logfmt"
        lines = [lf.line(syn, T1, "account_service", "profile_lookup_started", "tr-1",
                         level="DEBUG", lookup_key=ANA, attempt=1),
                 state_line(syn, T2, "account_service", "profile_lookup_completed", "account_9",
                            rows=1, http_status=200, correlation_key=BEN)]
        f, _ = load(body(syn, *lines))
        d = dossier_for(f)
        self.assertIsNone(res(d, "account_9"))
        self.assertTrue(any(e["type"] == "a8_conflict" for e in d["events"]))

    def test_the_policy_sees_the_finding(self):
        f, _ = load(self.scenario("pipe"))
        plan = policy.evaluate(dossier_for(f), policy.rules_from_rows([
            (f"rule_{i}", c, "Always", "RETAIN" if c in policy.RETAINED else
             "REDACT" if c in ("SUPPORT_MESSAGE", "EMAIL_DELIVERY_LOG", "ACCOUNT_TOMBSTONE") else
             "ANONYMIZE" if c in ("ANALYTICS_PROFILE", "PRODUCT_TELEMETRY") else "DELETE")
            for i, c in enumerate(policy.RULE_CATEGORIES)]))
        self.assertEqual(plan["status"], "INCOMPLETE")
        ops = {(s["operation"], s["resource_id"], s["evidence_id"]) for s in plan["steps"]}
        self.assertIn(("REPORT_ACTIVE_DATA_FLOW", "data_flow_1", "evidence_01"), ops)
        self.assertIn(("REPORT_RESTORE_RISK", "backup_1", "evidence_01"), ops)
        self.assertIn(("VERIFY_RECORD_ABSENT", "account_1", ""), ops)
        self.assertIn(("VERIFY_RECORD_PRESENT", "audit_record_1", ""), ops)


if __name__ == "__main__":
    unittest.main()
