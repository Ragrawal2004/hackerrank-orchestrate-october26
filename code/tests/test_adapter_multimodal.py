"""Adapter 8: screenshots and PDFs as facts (DESIGN §3, T17). The reader is always a fake."""
import json
import re
import tempfile
import unittest
from pathlib import Path

from ghost import adapters, llm, policy
from ghost.adapters import multimodal as mm
from tests.fact_builder import Facts, sha
from tests.mm_fixture import PDF, PNG, FakeReader, ForbiddenReader, payload

FILE = "evidence_04"
MANIFEST = "2026-09-20T09:00:00Z"
ANA = "ana@example.com"
OLD = "ana.old@example.com"
BEN = "ben@example.com"
STRANGER = "stranger@example.net"
LINK_T = "2026-09-20T09:07:01Z"


def world(*links):
    """Ana (user_1, one old email) and Ben (user_2); each link is (resource_id, system)."""
    f = Facts().user("user_1", ANA, phone="+1 555 0100", name="Ana Lima",
                     old_emails=(OLD,)).user("user_2", BEN, phone="+1 555 0222", name="Ben Roy")
    f.file(FILE, MANIFEST)
    for rid, system in links:
        f.link_identity(LINK_T, rid, ANA, "user_1", target=system)
    return f


def obs(conn, rid=None):
    q = ("select obs_id, file_id, locator, record_at, system_id, resource_id, category, state, "
         "state_basis, attrs_json from observation")
    args = ()
    if rid:
        q, args = q + " where resource_id = ?", (rid,)
    cols = ("obs_id", "file_id", "locator", "record_at", "system_id", "resource_id", "category",
            "state", "state_basis", "attrs")
    out = []
    for r in conn.execute(q + " order by obs_id", args):
        d = dict(zip(cols, r))
        d["attrs"] = json.loads(d["attrs"])
        out.append(d)
    return out


def keys_of(conn, oid):
    return sorted(conn.execute("select kind, value from subject_key where obs_id = ?", (oid,)))


def notes(report, kind):
    return [n for n in report["notes"] if n["type"] == kind]


def run(f, pl, data=PNG, file_id=FILE, extractions_dir=None, b=None):
    reader = FakeReader(pl, b)
    return reader, mm.ingest(f.conn, file_id, data, reader, extractions_dir=extractions_dir)


class DetectTests(unittest.TestCase):
    def test_png_and_pdf_are_found_by_their_signature(self):
        self.assertEqual(mm.detect(PNG), "png")
        self.assertEqual(mm.detect(PDF), "pdf")

    def test_other_bytes_are_not_claimed(self):
        for data in (b"", b"hello", b"timestamp level=INFO\n", b"\x00\x01\x02", "x".encode()):
            self.assertIsNone(mm.detect(data), data)

    def test_detection_ignores_the_file_name(self):
        f = world()
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            (root / "evidence").mkdir()
            (root / "evidence" / "note.txt").write_bytes(PNG)  # a PNG named .txt
            (root / "evidence" / "shot.png").write_text("plain text, not a png", encoding="utf-8")
            f.conn.executemany(
                "insert into evidence_file(file_id,path,origin,format,adapter,manifest_at) "
                "values (?,?,?,null,null,?)",
                [("evidence_70", "evidence/note.txt", "t", MANIFEST),
                 ("evidence_71", "evidence/shot.png", "t", MANIFEST)])
            f.conn.execute("insert into activity(activity_id,event_time,event_type,target_system,"
                           "resource_id,details,kind,target_resource) values "
                           "('a1',?, 'LINK_RESOURCE_IDENTITY','account_service','account_1','x',"
                           "'link_identity','account_1')", (LINK_T,))
            reader = FakeReader(payload("account", "account_1", emails=[ANA]))
            res = adapters.load_evidence(f.conn, root, reader=reader)
        self.assertIn("evidence_70", res["claimed"])
        self.assertNotIn("evidence_71", res["claimed"])
        self.assertEqual([c[0] for c in reader.calls], ["evidence_70"])


class IngestTests(unittest.TestCase):
    def test_two_agreeing_reads_write_one_explicit_observation_at_manifest_time(self):
        f = world(("account_5762", "account_service"))
        _, rep = run(f, payload("account", "account_5762", emails=[ANA]))
        self.assertEqual(rep["status"], "agreed")
        (o,) = obs(f.conn)
        self.assertEqual((o["file_id"], o["system_id"], o["resource_id"], o["category"],
                          o["state"], o["state_basis"], o["record_at"]),
                         (FILE, "account_service", "account_5762", "USER_PROFILE", "PRESENT",
                          "explicit", MANIFEST))
        self.assertIn(("email", ANA), keys_of(f.conn, o["obs_id"]))

    def test_no_scope_and_no_join_rows(self):
        f = world(("account_5762", "account_service"))
        run(f, payload("account", "account_5762", emails=[ANA]))
        self.assertEqual(f.conn.execute("select count(*) from capture_scope").fetchone()[0], 0)
        self.assertEqual(f.conn.execute("select count(*) from join_key").fetchone()[0], 0)

    def test_record_time_is_the_manifest_not_the_printed_time(self):
        f = world(("account_5762", "account_service"))
        _, rep = run(f, payload("account", "account_5762", emails=[ANA],
                                screen_time="2026-09-19T08:00:00Z"))
        self.assertEqual(obs(f.conn)[0]["record_at"], MANIFEST)
        self.assertEqual(len(notes(rep, "screen_time_mismatch")), 1)

    def test_disagreeing_reads_emit_nothing_and_log_both_sides(self):
        f = world(("account_5762", "account_service"))
        a = payload("account", "account_5762", emails=[ANA])
        b = payload("account", "account_5762", emails=[BEN])  # a different subject on read B
        _, rep = run(f, a, b=b)
        self.assertEqual(rep["status"], "disagreed")
        self.assertEqual(obs(f.conn), [])
        (n,) = notes(rep, "image_read_disagreement")
        self.assertIn("a", n)
        self.assertIn("b", n)

    def test_a_disagreement_on_an_indicator_is_a_disagreement(self):
        f = world(("data_flow_1", "integration_service"))
        a = payload("connector", "data_flow_1", emails=[ANA], enabled="enabled")
        b = payload("connector", "data_flow_1", emails=[ANA], enabled="disabled")
        _, rep = run(f, a, b=b)
        self.assertEqual(rep["status"], "disagreed")
        self.assertEqual(obs(f.conn), [])

    def test_names_and_printed_time_do_not_cause_a_disagreement(self):
        f = world(("account_5762", "account_service"))
        a = payload("account", "account_5762", emails=[ANA], names=["Ana Lima"],
                    screen_time="2026-09-20T09:00:00Z")
        b = payload("account", "account_5762", emails=[ANA], names=["A. Lima"], screen_time=None)
        _, rep = run(f, a, b=b)
        self.assertEqual(rep["status"], "agreed")

    def test_unknown_resource_id_is_dropped_and_logged(self):
        f = world(("account_5762", "account_service"))
        _, rep = run(f, payload("account", "account_9999", emails=[ANA]))
        self.assertEqual(obs(f.conn), [])
        (n,) = notes(rep, "unknown_resource_id_dropped")
        self.assertEqual(n["resource_id"], "account_9999")

    def test_an_id_known_from_a_text_adapter_observation_is_known(self):
        f = world()
        f.file("evidence_05", "2026-09-10T00:00:00Z")
        f.obs("evidence_05", "2026-09-10T00:00:00Z", "search_service", "search_5", "PRESENT",
              "SEARCH_INDEX_ENTRY", keys=[("email", ANA)])
        run(f, payload("search", "search_5", emails=[ANA]))
        self.assertEqual([o["resource_id"] for o in obs(f.conn) if o["file_id"] == FILE],
                         ["search_5"])

    def test_a_foreign_identifier_is_not_a_subject_key_and_is_not_logged_verbatim(self):
        f = world(("search_428", "search_service"))
        _, rep = run(f, payload("search", "search_428", emails=[ANA, STRANGER]))
        (o,) = obs(f.conn)
        self.assertEqual([k for k in keys_of(f.conn, o["obs_id"]) if k[0] == "email"],
                         [("email", ANA)])
        self.assertEqual(notes(rep, "foreign_identifier")[0]["count"], 1)
        self.assertNotIn(STRANGER, json.dumps(rep))

    def test_only_foreign_identifiers_is_an_identity_mismatch_with_no_observation(self):
        f = world(("marketing_001", "marketing_platform"))
        _, rep = run(f, payload("marketing", "marketing_001", emails=[STRANGER],
                                names=["Someone Else"]))
        self.assertEqual(obs(f.conn), [])
        self.assertEqual(len(notes(rep, "identity_mismatch")), 1)

    def test_an_identifier_of_another_known_user_than_the_link_is_a_mismatch(self):
        f = world(("marketing_001", "marketing_platform"))  # the catalog says Ana
        _, rep = run(f, payload("marketing", "marketing_001", emails=[BEN]))
        self.assertEqual(obs(f.conn), [])
        self.assertEqual(len(notes(rep, "identity_mismatch")), 1)

    def test_list_rows_other_than_the_detail_pane_never_create_observations(self):
        f = world(("account_5762", "account_service"))
        _, rep = run(f, payload("account", "account_5762", emails=[ANA], context_rows=3))
        self.assertEqual(len(obs(f.conn)), 1)
        self.assertEqual(notes(rep, "context_row_ignored")[0]["count"], 3)

    def test_the_old_email_of_the_subject_is_a_valid_identifier(self):
        f = world(("account_5762", "account_service"))
        run(f, payload("account", "account_5762", emails=[OLD.upper()]))
        (o,) = obs(f.conn)
        self.assertIn(("email", OLD), keys_of(f.conn, o["obs_id"]))

    # ---- the state map -----------------------------------------------------------------------

    def test_connector_needs_an_explicit_enabled_indicator(self):
        f = world(("data_flow_1121", "integration_service"))
        _, rep = run(f, payload("connector", "data_flow_1121", emails=[ANA]))  # "Healthy" only
        self.assertEqual(obs(f.conn), [])
        self.assertEqual(notes(rep, "no_state_field")[0]["field"], "enabled")
        for flag, state in (("enabled", "ACTIVE"), ("disabled", "DISABLED")):
            g = world(("data_flow_1121", "integration_service"))
            run(g, payload("connector", "data_flow_1121", emails=[ANA], enabled=flag))
            (o,) = obs(g.conn)
            self.assertEqual((o["state"], o["category"]), (state, policy.DATA_FLOW))

    def test_backup_needs_an_explicit_restore_indicator(self):
        f = world(("backup_9287", "backup_service"))
        _, rep = run(f, payload("backup", "backup_9287", emails=[ANA]))
        self.assertEqual(obs(f.conn), [])
        self.assertEqual(notes(rep, "no_state_field")[0]["field"], "restore_allowed")
        for flag, state in (("allowed", "RESTORABLE"), ("blocked", "BLOCKED")):
            g = world(("backup_9287", "backup_service"))
            run(g, payload("backup", "backup_9287", emails=[ANA], restore=flag))
            (o,) = obs(g.conn)
            self.assertEqual((o["state"], o["category"]), (state, "BACKUP_ARCHIVE"))

    def test_analytics_is_identifiable_or_anonymized_by_what_is_visible(self):
        f = world(("analytics_7438", "analytics_platform"))
        run(f, payload("analytics", "analytics_7438", emails=[ANA], user_refs=["user_1"]))
        self.assertEqual(obs(f.conn)[0]["state"], "IDENTIFIABLE")
        g = world(("analytics_7438", "analytics_platform"))
        run(g, payload("analytics", "analytics_7438"))
        self.assertEqual((obs(g.conn)[0]["state"], obs(g.conn)[0]["category"]),
                         ("ANONYMIZED", "ANALYTICS_PROFILE"))

    def test_support_ticket_is_identifiable_when_the_requester_is_shown(self):
        f = world(("support_1034", "support_platform"))
        run(f, payload("support", "support_1034", emails=[ANA]))
        self.assertEqual((obs(f.conn)[0]["state"], obs(f.conn)[0]["category"]),
                         ("IDENTIFIABLE", "SUPPORT_MESSAGE"))
        g = world(("support_1034", "support_platform"))
        run(g, payload("support", "support_1034", user_refs=["user_1"], identifiers_present=False))
        self.assertEqual(obs(g.conn)[0]["state"], "REDACTED")

    def test_search_export_and_security_screens_are_present(self):
        cases = [("search", "search_428", "search_service", "SEARCH_INDEX_ENTRY"),
                 ("export", "export_1260", "export_service", "USER_EXPORT"),
                 ("security", "audit_record_003", "security_service", "SECURITY_AUDIT_LOG")]
        for kind, rid, system, category in cases:
            with self.subTest(kind=kind):
                f = world((rid, system))
                run(f, payload(kind, rid, emails=[ANA], user_refs=["user_1"]))
                (o,) = obs(f.conn)
                self.assertEqual((o["system_id"], o["category"], o["state"]),
                                 (system, category, "PRESENT"))

    def test_object_storage_object_is_a_present_attachment_without_any_identifier(self):
        f = world()
        f.activity(LINK_T, "LINK_RESOURCE_IDENTITY", "data_catalog", "object_storage", "object_19",
                   "SUCCESS", f"Attachment catalog confirmed object_19 (delivery_19.pdf) belongs "
                              f"only to {ANA} and user_1; no other subject is linked.")
        run(f, payload("object_storage", "object_19"))
        (o,) = obs(f.conn)
        self.assertEqual((o["system_id"], o["category"], o["state"]),
                         ("object_storage", "SUPPORT_ATTACHMENT", "PRESENT"))

    def test_the_registry_screen_resolves_through_the_activity_parenthetical(self):
        f = world()
        f.activity(LINK_T, "LINK_RESOURCE_IDENTITY", "data_catalog", "identity_service",
                   "tombstone_17", "SUCCESS",
                   f"Data catalog linked tombstone_17 (user_1) to {ANA} and user_1.")
        run(f, payload("registry", None, ref="user_1", user_refs=["user_1"],
                       identifiers_present=False))
        (o,) = obs(f.conn)
        self.assertEqual((o["system_id"], o["resource_id"], o["category"], o["state"]),
                         ("identity_service", "tombstone_17", "ACCOUNT_TOMBSTONE", "REDACTED"))

    def test_the_registry_screen_with_a_direct_identifier_is_identifiable(self):
        f = world()
        f.activity(LINK_T, "LINK_RESOURCE_IDENTITY", "data_catalog", "identity_service",
                   "tombstone_17", "SUCCESS",
                   f"Data catalog linked tombstone_17 (user_1) to {ANA} and user_1.")
        run(f, payload("registry", None, ref="user_1", emails=[ANA]))
        self.assertEqual(obs(f.conn)[0]["state"], "IDENTIFIABLE")

    def test_a_pdf_statement_resolves_to_the_export_it_is_linked_to(self):
        f = world()
        f.activity(LINK_T, "LINK_RESOURCE_IDENTITY", "data_catalog", "export_service", "export_1842",
                   "SUCCESS", f"Data catalog linked export_1842 (NST-2026-0718-1842) to {ANA} "
                              f"and user_1.")
        run(f, payload("document_statement", None, ref="NST-2026-0718-1842", emails=[ANA],
                       user_refs=["user_1"]), data=PDF)
        (o,) = obs(f.conn)
        self.assertEqual((o["resource_id"], o["category"], o["state"]),
                         ("export_1842", "USER_EXPORT", "PRESENT"))

    def test_a_pdf_attachment_and_audit_extract(self):
        f = world()
        f.activity(LINK_T, "DELETE_RECORD", "support_platform", "support_platform", "support_001",
                   "SUCCESS", "Deletion workflow reported success. Target resource: support_001.")
        run(f, payload("document_attachment", "support_001", emails=[ANA], names=["Ana Lima"],
                       phones=["+1 555 0100"]), data=PDF)
        (o,) = obs(f.conn)
        self.assertEqual((o["system_id"], o["category"], o["state"]),
                         ("support_platform", "SUPPORT_ATTACHMENT", "PRESENT"))
        g = world(("audit_record_003", "security_service"))
        run(g, payload("document_audit_extract", "audit_record_003", user_refs=["user_1"]),
            data=PDF)
        self.assertEqual((obs(g.conn)[0]["category"], obs(g.conn)[0]["state"]),
                         ("SECURITY_AUDIT_LOG", "PRESENT"))

    def test_a_register_with_no_store_resource_id_gives_nothing_and_says_why(self):
        f = world()
        _, rep = run(f, payload("document_register", None, user_refs=["user_1"]), data=PDF)
        self.assertEqual(obs(f.conn), [])
        self.assertEqual(len(notes(rep, "no_resource_id")), 1)

    # ---- bookkeeping ---------------------------------------------------------------------------

    def test_the_file_is_marked_and_reported(self):
        f = world(("account_5762", "account_service"))
        run(f, payload("account", "account_5762", emails=[ANA]))
        row = f.conn.execute("select format, adapter from evidence_file where file_id = ?",
                             (FILE,)).fetchone()
        self.assertEqual(row, ("multimodal_png", "multimodal"))
        self.assertEqual(f.conn.execute("select adapter, unparsed from parse_report where "
                                        "file_id = ?", (FILE,)).fetchone(), ("multimodal", 0))

    def test_ingest_is_idempotent(self):
        f = world(("account_5762", "account_service"))
        pl = payload("account", "account_5762", emails=[ANA])
        run(f, pl)
        run(f, pl)
        self.assertEqual(len(obs(f.conn)), 1)
        self.assertEqual(f.conn.execute("select count(*) from parse_report").fetchone()[0], 1)

    def test_an_unavailable_reader_skips_the_file_and_says_so(self):
        f = world(("account_5762", "account_service"))

        def broken(file_id, kind, data):
            raise llm.LLMUnavailable("no key")

        rep = mm.ingest(f.conn, FILE, PNG, broken)
        self.assertEqual(rep["status"], "skipped")
        self.assertEqual(len(notes(rep, "read_unavailable")), 1)
        self.assertEqual(obs(f.conn), [])
        self.assertIsNone(f.conn.execute("select format from evidence_file where file_id = ?",
                                         (FILE,)).fetchone()[0])

    def test_no_reader_and_no_extraction_skips(self):
        f = world(("account_5762", "account_service"))
        rep = mm.ingest(f.conn, FILE, PNG, None)
        self.assertEqual((rep["status"], len(notes(rep, "no_reader"))), ("skipped", 1))

    def test_a_pdf_without_a_text_layer_is_listed_not_guessed(self):
        f = world()

        def no_text(file_id, kind, data):
            raise mm.NoTextLayer("pdftotext returned nothing")

        rep = mm.ingest(f.conn, FILE, PDF, no_text)
        self.assertEqual(rep["status"], "skipped")
        self.assertEqual(len(notes(rep, "no_text_layer")), 1)
        self.assertEqual(obs(f.conn), [])

    def test_a_schema_violating_read_is_a_bad_read_not_a_crash(self):
        f = world(("account_5762", "account_service"))
        bad = payload("account", "account_5762", emails=[ANA])
        bad["screen_kind"] = "spreadsheet"
        _, rep = run(f, bad)
        self.assertEqual(rep["status"], "disagreed")  # a read that fails the schema never agrees
        self.assertEqual(obs(f.conn), [])
        self.assertEqual(len(notes(rep, "image_read_disagreement")), 1)


class ExtractionStoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = Path(self.tmp.name)

    def ingest_once(self, data=PNG, ref_payload=None):
        f = world(("account_5762", "account_service"))
        pl = ref_payload or payload("account", "account_5762", emails=[ANA, OLD],
                                    user_refs=["user_1"], phones=["+1 555 0100"],
                                    names=["Ana Lima"], screen_time="2026-09-20T08:59:00Z")
        run(f, pl, data=data, extractions_dir=self.dir)
        return f

    def test_an_agreed_read_is_stored_with_the_file_hash(self):
        self.ingest_once()
        path = self.dir / f"{FILE}.json"
        self.assertTrue(path.is_file())
        ex = json.loads(path.read_text(encoding="utf-8"))
        import hashlib
        self.assertEqual(ex["sha256"], hashlib.sha256(PNG).hexdigest())
        self.assertEqual(ex["file_id"], FILE)
        self.assertEqual(ex["variants"], ["A", "B"])
        self.assertEqual(ex["model"], "m-test")
        self.assertEqual(ex["detail_resource_id"], "account_5762")

    def test_the_stored_file_holds_no_raw_dataset_text(self):
        self.ingest_once()
        text = (self.dir / f"{FILE}.json").read_text(encoding="utf-8")
        self.assertNotIn("@", text)
        self.assertNotIn("Ana Lima", text)
        self.assertNotIn("555", text)
        self.assertNotIn("2026-09-20T08:59", text)  # not even the printed time
        self.assertIsNone(re.search(r"\+?\d[\d ()-]{8,}", re.sub(r"[0-9a-f]{64}", "", text)))
        self.assertIn(sha(ANA), text)  # an email is stored only as its sha256
        # the same scan must be able to fail: inject a literal phone and a literal email
        self.assertEqual(raw_text_leaks(text), set())
        self.assertEqual(raw_text_leaks(text.replace('"model"', '"phone": "+91 98765 43210", '
                                                                  '"model"')), {"phone"})
        self.assertEqual(raw_text_leaks(text.replace('"model"', '"email": "a.b@example.org", '
                                                                  '"model"')), {"email"})
        both = text.replace('"model"', '"x": "+91 98765 43210 a.b@example.org", "model"')
        self.assertEqual(raw_text_leaks(both), {"phone", "email"})
        ex = json.loads(text)
        self.assertEqual(sorted(ex), ["detail_reference", "detail_resource_id", "file_id",
                                      "indicators", "model", "schema_version", "screen_kind",
                                      "sha256", "subject_refs", "variants"])

    def test_a_stored_extraction_is_used_instead_of_the_reader(self):
        live = self.ingest_once()
        f = world(("account_5762", "account_service"))
        rep = mm.ingest(f.conn, FILE, PNG, ForbiddenReader(), extractions_dir=self.dir)
        self.assertEqual(rep["status"], "loaded")
        mine, theirs = obs(f.conn)[0], obs(live.conn)[0]
        for k in ("system_id", "resource_id", "category", "state", "record_at", "state_basis"):
            self.assertEqual(mine[k], theirs[k])
        self.assertEqual(keys_of(f.conn, mine["obs_id"]), keys_of(live.conn, theirs["obs_id"]))

    def test_a_stale_hash_is_ignored_and_the_reader_runs_again(self):
        self.ingest_once()
        f = world(("account_5762", "account_service"))
        reader = FakeReader(payload("account", "account_5762", emails=[ANA]))
        rep = mm.ingest(f.conn, FILE, PNG + b"changed", reader, extractions_dir=self.dir)
        self.assertEqual(len(reader.calls), 1)
        self.assertEqual(len(notes(rep, "extraction_stale")), 1)

    def test_a_disagreement_stores_nothing(self):
        f = world(("account_5762", "account_service"))
        run(f, payload("account", "account_5762", emails=[ANA]),
            b=payload("account", "account_5762", emails=[BEN]), extractions_dir=self.dir)
        self.assertEqual(list(self.dir.glob("*.json")), [])

    def test_a_corrupt_extraction_file_is_ignored(self):
        (self.dir / f"{FILE}.json").write_text("{not json", encoding="utf-8")
        f = world(("account_5762", "account_service"))
        reader = FakeReader(payload("account", "account_5762", emails=[ANA]))
        rep = mm.ingest(f.conn, FILE, PNG, reader, extractions_dir=self.dir)
        self.assertEqual(rep["status"], "agreed")
        self.assertEqual(len(notes(rep, "extraction_stale")), 1)


def raw_text_leaks(text):
    """{'email', 'phone'} for each kind of literal personal text found in `text`. 64-hex hashes
    are removed first so their digit runs are not mistaken for phone numbers."""
    body, found = re.sub(r"[0-9a-f]{64}", "", text), set()
    if re.search(r"[\w.+'-]+@[\w-]+(?:\.[\w-]+)+", body):
        found.add("email")
    if re.search(r"\+?\d[\d ()-]{8,}", body):
        found.add("phone")
    return found


class LoadEvidenceTests(unittest.TestCase):
    def dataset(self, files):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        (root / "evidence").mkdir()
        for name, data in files.items():
            (root / "evidence" / name).write_bytes(data)
        return root

    def test_a_png_goes_to_the_reader_and_is_claimed(self):
        root = self.dataset({"a.png": PNG})
        f = world(("account_5762", "account_service"))
        f.conn.execute("update evidence_file set path = 'evidence/a.png' where file_id = ?", (FILE,))
        reader = FakeReader(payload("account", "account_5762", emails=[ANA]))
        res = adapters.load_evidence(f.conn, root, reader=reader)
        self.assertEqual(res["claimed"], [FILE])
        self.assertEqual(res["multimodal"], {FILE: "agreed"})
        self.assertEqual(len(obs(f.conn)), 1)

    def test_without_a_reader_the_binary_file_stays_unclaimed(self):
        root = self.dataset({"a.png": PNG})
        f = world()
        f.conn.execute("update evidence_file set path = 'evidence/a.png' where file_id = ?", (FILE,))
        res = adapters.load_evidence(f.conn, root)
        self.assertEqual((res["claimed"], res["unclaimed"]), ([], [FILE]))
        self.assertEqual(res["multimodal"], {FILE: "skipped"})

    def test_summary_line_counts_the_outcomes(self):
        line = adapters.format_multimodal({"e1": "agreed", "e2": "disagreed", "e3": "skipped",
                                           "e4": "loaded"})
        self.assertEqual(line, "multimodal: files=4 agreed=1 loaded=1 disagreed=1 skipped=1")


if __name__ == "__main__":
    unittest.main()
