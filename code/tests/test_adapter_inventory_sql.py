import json
import unittest
from pathlib import Path

from ghost import adapters, dossier, store
from ghost.adapters import inventory_sql, replica_sql
from tests import inventory_fixture as inv
from tests import sql_fixture as fx
from tests.fact_builder import Facts, case, sha

FMT = "sql_inventory"
CAP = "2026-09-16T08:37:39Z"
MANIFEST = CAP
EARLY = "2026-09-16T07:59:54Z"
AUDIT = "2026-09-16T09:00:00Z"
ANA = "ana@example.com"
PHONE = "+1 555 0100"
HASH = "a" * 64
REPO = Path(__file__).resolve().parents[2]


def load(sql, file_id="evidence_01", manifest=MANIFEST, facts=None):
    f = facts or Facts()
    f.file(file_id, manifest)
    report = inventory_sql.ingest(f.conn, file_id, sql)
    return f, report


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


def one(conn, rid):
    got = obs(conn, rid)
    assert len(got) == 1, got
    return got[0]


def keys(conn, rid):
    return sorted((k, v) for k, v in conn.execute(
        "select sk.kind, sk.value from subject_key sk join observation o on o.obs_id = sk.obs_id "
        "where o.resource_id = ?", (rid,)))


def scopes(conn):
    return sorted(conn.execute("select file_id, system_id, record_at, complete from capture_scope"))


def notes(conn, file_id="evidence_01"):
    return json.loads(conn.execute("select notes_json from parse_report where file_id = ?",
                                   (file_id,)).fetchone()[0])


def one_row(table, rid="r_1", at=EARLY, **kw):
    return load(inv.inventory_sql(CAP, rows=[inv.row(table, rid, at, **kw)]))[0]


class DetectTests(unittest.TestCase):
    def test_inventory_shape(self):
        self.assertEqual(inventory_sql.detect(inv.inventory_sql(CAP)), FMT)

    def test_empty_tables_with_scope_still_claimed(self):
        self.assertEqual(inventory_sql.detect(inv.inventory_sql(CAP, scope=["account_service"])), FMT)

    def test_no_file_name_in_detection(self):
        text = "-- customer_data_replica_inventory.sql\n" + inv.inventory_sql(CAP)
        self.assertEqual(inventory_sql.detect(text), FMT)

    def test_scope_table_with_wrong_columns_is_not_inventory(self):
        text = inv.inventory_sql(CAP).replace("origin_system TEXT PRIMARY KEY, captured_at TEXT NOT NULL",
                                              "origin_system TEXT PRIMARY KEY, taken TEXT NOT NULL")
        self.assertIsNone(inventory_sql.detect(text))

    def test_no_typed_table_is_not_inventory(self):
        text = inv.inventory_sql(CAP, tables=[], indexes=False)
        self.assertIsNone(inventory_sql.detect(text))

    def test_typed_tables_without_scope_table_are_not_inventory(self):
        text = inv.inventory_sql(CAP).replace(inv.SCHEMA_SCOPE, "").replace(
            f"INSERT INTO snapshot_scope VALUES ('account_service', '{CAP}');", "")
        text = "\n".join(l for l in text.splitlines() if "INTO snapshot_scope" not in l)
        self.assertIsNone(inventory_sql.detect(text))

    def test_replica_text_is_not_claimed(self):
        self.assertIsNone(inventory_sql.detect(fx.j_sql(CAP, {"customer_data_primary": [],
                                                              "customer_data_read_replica": []})))
        self.assertIsNone(inventory_sql.detect(fx.t_sql(CAP, [], [])))

    def test_word_inventory_in_text_does_not_claim_a_non_inventory(self):
        self.assertIsNone(inventory_sql.detect("-- customer_data_inventory_20260101.sql\nSELECT 1;\n"))
        self.assertIsNone(inventory_sql.detect(
            "-- inventory\nCREATE TABLE snapshot_scope (a TEXT);\n"))

    def test_not_sql_is_not_claimed(self):
        self.assertIsNone(inventory_sql.detect("{}"))
        self.assertIsNone(inventory_sql.detect(""))

    def test_registry_picks_inventory_and_only_inventory(self):
        a, fmt = adapters.select(inv.inventory_sql(CAP))
        self.assertIs(a, inventory_sql)
        self.assertEqual(fmt, FMT)
        a, _ = adapters.select(fx.j_sql(CAP, {"customer_data_primary": [],
                                              "customer_data_read_replica": []}))
        self.assertIs(a, replica_sql)

    def test_replica_loader_unchanged_for_replica_detection(self):
        self.assertIsNone(replica_sql.detect(inv.inventory_sql(CAP)))


class LoaderTests(unittest.TestCase):
    def test_create_index_is_accepted_and_not_unparsed(self):
        f, rep = load(inv.inventory_sql(CAP))
        self.assertEqual(rep["unparsed"], 0)

    def test_create_index_is_not_counted_as_meaningful(self):
        _, with_idx = load(inv.inventory_sql(CAP))
        _, without = load(inv.inventory_sql(CAP, indexes=False), "evidence_02")
        self.assertEqual(with_idx["meaningful"], without["meaningful"])

    def test_failing_index_is_unparsed(self):
        bad = "CREATE INDEX idx_bad ON no_such_table(email);"
        f, rep = load(inv.inventory_sql(CAP, extra=bad))
        self.assertEqual(rep["unparsed"], 1)
        self.assertEqual(notes(f.conn)[0]["type"], "statement_error")

    def test_create_index_does_not_change_replica_counts(self):
        db, stats = replica_sql.load_sql("CREATE TABLE t (a TEXT);\nCREATE INDEX i ON t(a);\n")
        self.assertEqual(stats["meaningful"], 1)
        self.assertEqual(stats["failed"], 0)

    def test_update_drop_select_never_run_and_counted(self):
        for stmt in ("UPDATE customer_profiles SET email = 'x';", "DROP TABLE customer_profiles;",
                     "SELECT * FROM customer_profiles;", "DELETE FROM snapshot_scope;"):
            row = inv.row("customer_profiles", "account_1", EARLY, email=ANA)
            f, rep = load(inv.inventory_sql(CAP, rows=[row], extra=stmt))
            self.assertEqual(rep["unparsed"], 1, stmt)
            self.assertEqual(one(f.conn, "account_1")["attrs"]["email"], ANA, stmt)

    def test_attach_and_extension_denied(self):
        db, stats = replica_sql.load_sql("ATTACH DATABASE '/nonexistent_dir/ghost_x.db' AS x;\n")
        self.assertEqual(stats["failed"], 1)
        self.assertFalse(Path("/nonexistent_dir/ghost_x.db").exists())

    def test_authorizer_denies_pragma_functions_inside_an_insert(self):
        db, stats = replica_sql.load_sql(
            "CREATE TABLE t (a TEXT);\nINSERT INTO t SELECT name FROM pragma_database_list;\n")
        self.assertEqual(stats["notes"][-1]["error"], "not authorized")
        self.assertEqual(db.execute("select count(*) from t").fetchone()[0], 0)

    def test_load_extension_function_denied(self):
        db, stats = replica_sql.load_sql(
            "CREATE TABLE t (a TEXT);\nINSERT INTO t VALUES (load_extension('x'));\n")
        self.assertEqual(stats["failed"], 1)

    def test_pragma_and_transaction_lines_are_ignored(self):
        _, rep = load(inv.inventory_sql(CAP, scope=[]))
        self.assertEqual(rep["unparsed"], 0)


class RowMappingTests(unittest.TestCase):
    def test_profile(self):
        f = one_row("customer_profiles", "account_1", email="Ana@Example.COM", phone=PHONE,
                    full_name="Ana Lima", customer_reference="user_1", email_hash=HASH)
        o = one(f.conn, "account_1")
        self.assertEqual((o["system_id"], o["category"], o["state"], o["state_basis"]),
                         ("account_service", "USER_PROFILE", "PRESENT", "explicit"))
        self.assertEqual(o["attrs"]["email"], ANA)
        self.assertEqual(o["attrs"]["account_status"], "DELETION_PENDING")
        self.assertEqual(keys(f.conn, "account_1"),
                         [("email", ANA), ("email_hash", HASH), ("name", "Ana Lima"),
                          ("phone", PHONE), ("user_id", "user_1")])

    def test_profile_with_only_customer_reference(self):
        f = one_row("customer_profiles", "account_1", customer_reference="user_7")
        self.assertEqual(keys(f.conn, "account_1"), [("user_id", "user_7")])

    def test_uppercase_email_is_lowercased_in_keys(self):
        f = one_row("customer_profiles", "account_1", email="O.ERIKSEN10@YAHOO.COM")
        self.assertEqual(keys(f.conn, "account_1"), [("email", "o.eriksen10@yahoo.com")])

    def test_search(self):
        f = one_row("customer_search_documents", "search_1", email=ANA, display_name="Ana Lima")
        o = one(f.conn, "search_1")
        self.assertEqual((o["system_id"], o["category"], o["state"]),
                         ("search_service", "SEARCH_INDEX_ENTRY", "PRESENT"))
        self.assertEqual(keys(f.conn, "search_1"), [("email", ANA), ("name", "Ana Lima")])

    def test_analytics_identifiable_and_anonymized(self):
        for kw in ({"email": ANA}, {"phone": PHONE}, {"email_hash": HASH},
                   {"customer_reference": "user_1"}):
            f = one_row("customer_analytics_profiles", "analytics_1", **kw)
            self.assertEqual(one(f.conn, "analytics_1")["state"], "IDENTIFIABLE", kw)
        f = one_row("customer_analytics_profiles", "analytics_1")
        o = one(f.conn, "analytics_1")
        self.assertEqual((o["system_id"], o["category"], o["state"]),
                         ("analytics_platform", "ANALYTICS_PROFILE", "ANONYMIZED"))
        self.assertEqual(keys(f.conn, "analytics_1"), [])

    def test_support_with_requester_email_is_identifiable(self):
        f = one_row("support_records", "support_1", requester_email=ANA)
        o = one(f.conn, "support_1")
        self.assertEqual((o["system_id"], o["category"], o["state"]),
                         ("support_platform", "SUPPORT_MESSAGE", "IDENTIFIABLE"))
        self.assertEqual(keys(f.conn, "support_1"), [("email", ANA)])

    def test_support_name_or_phone_or_email_in_excerpt_is_identifiable(self):
        for kw in ({"requester_name": "Ana Lima"}, {"requester_phone": PHONE},
                   {"message_excerpt": f"Please write to {ANA} about the order."}):
            f = one_row("support_records", "support_1", **kw)
            self.assertEqual(one(f.conn, "support_1")["state"], "IDENTIFIABLE", kw)

    def test_support_with_only_customer_reference_is_redacted(self):
        f = one_row("support_records", "support_1", customer_reference="user_1")
        self.assertEqual(one(f.conn, "support_1")["state"], "REDACTED")
        self.assertEqual(keys(f.conn, "support_1"), [("user_id", "user_1")])

    def test_export(self):
        f = one_row("customer_exports", "export_1", owner_email=ANA, owner_phone=PHONE)
        o = one(f.conn, "export_1")
        self.assertEqual((o["system_id"], o["category"], o["state"]),
                         ("export_service", "USER_EXPORT", "PRESENT"))
        self.assertEqual(keys(f.conn, "export_1"), [("email", ANA), ("phone", PHONE)])

    def test_connector_enabled_flag(self):
        for flag, state in ((1, "ACTIVE"), (0, "DISABLED")):
            f = one_row("connector_subscriptions", "data_flow_1", customer_email=ANA, enabled=flag)
            o = one(f.conn, "data_flow_1")
            self.assertEqual((o["system_id"], o["category"], o["state"]),
                             ("integration_service", "DATA_FLOW", state))
            self.assertEqual(o["attrs"]["enabled"], bool(flag))
            self.assertEqual(keys(f.conn, "data_flow_1"), [("email", ANA)])

    def test_connector_enabled_given_as_text_in_the_file(self):
        sql = inv.inventory_sql(CAP, rows=[inv.row("connector_subscriptions", "data_flow_1", EARLY,
                                                   customer_email=ANA, enabled="1")])
        f, rep = load(sql)
        self.assertEqual(rep["unparsed"], 0)
        self.assertEqual(one(f.conn, "data_flow_1")["state"], "ACTIVE")

    def test_backup_restore_flag(self):
        for flag, state in ((1, "RESTORABLE"), (0, "BLOCKED")):
            f = one_row("backup_restore_entries", "backup_1", customer_email=ANA, restore_allowed=flag)
            o = one(f.conn, "backup_1")
            self.assertEqual((o["system_id"], o["category"], o["state"]),
                             ("backup_service", "BACKUP_ARCHIVE", state))
            self.assertEqual(o["attrs"]["restore_allowed"], bool(flag))

    def test_audit_record(self):
        f = one_row("security_audit_records", "audit_record_1", subject_reference="user_1",
                    retention_until="2033-12-31T23:59:59Z")
        o = one(f.conn, "audit_record_1")
        self.assertEqual((o["system_id"], o["category"], o["state"]),
                         ("security_service", "SECURITY_AUDIT_LOG", "PRESENT"))
        self.assertEqual(o["attrs"]["retention_until"], "2033-12-31T23:59:59Z")
        self.assertEqual(keys(f.conn, "audit_record_1"), [("user_id", "user_1")])

    def test_attrs_carry_table_and_both_clocks(self):
        f = one_row("customer_profiles", "account_1", email=ANA)
        a = one(f.conn, "account_1")["attrs"]
        self.assertEqual((a["table"], a["captured_at"], a["row_updated_at"], a["in_scope"]),
                         ("customer_profiles", CAP, EARLY, True))

    def test_file_is_marked_with_adapter_and_format(self):
        f, _ = load(inv.inventory_sql(CAP))
        self.assertEqual(f.conn.execute("select format, adapter from evidence_file").fetchone(),
                         (FMT, "inventory_sql"))

    def test_no_join_keys(self):
        f = one_row("customer_profiles", "account_1", email=ANA)
        self.assertEqual(f.conn.execute("select count(*) from join_key").fetchone()[0], 0)


class ClockTests(unittest.TestCase):
    def test_record_at_is_the_rows_own_updated_at(self):
        f = one_row("customer_search_documents", "search_1", at=EARLY, email=ANA)
        self.assertEqual(one(f.conn, "search_1")["record_at"], EARLY)

    def test_row_dated_after_capture_is_stored_and_noted(self):
        late = "2026-09-17T00:00:00Z"
        f = one_row("customer_search_documents", "search_1", at=late, email=ANA)
        self.assertEqual(one(f.conn, "search_1")["record_at"], late)
        self.assertEqual([n["type"] for n in notes(f.conn)], ["row_after_capture"])

    def test_row_after_audit_is_excluded_by_the_dossier(self):
        f = Facts().user("user_1", ANA, phone=PHONE)
        rows = [inv.row("customer_search_documents", "search_1", "2026-09-16T09:30:00Z", email=ANA)]
        load(inv.inventory_sql(CAP, rows=rows), facts=f)
        d = dossier.build_dossier(f.conn, case(ANA, AUDIT))
        self.assertEqual([r for r in d["resources"] if r["resource_id"] == "search_1"], [])

    def test_footer_mismatch_with_manifest_is_noted(self):
        f, rep = load(inv.inventory_sql(CAP), manifest="2026-09-16T08:00:00Z")
        self.assertEqual([n["type"] for n in notes(f.conn)], ["capture_time_mismatch"])
        self.assertEqual(rep["unparsed"], 0)


class ScopeRowTests(unittest.TestCase):
    def test_one_row_per_snapshot_scope_system_with_its_own_time_and_never_complete(self):
        sql = inv.inventory_sql(CAP, scope=["account_service", "search_service"])
        f, _ = load(sql)
        self.assertEqual(scopes(f.conn), [("evidence_01", "account_service", CAP, 0),
                                          ("evidence_01", "search_service", CAP, 0)])

    def test_scope_systems_are_not_derived_from_tables_or_rows(self):
        rows = [inv.row("customer_exports", "export_1", EARLY, owner_email=ANA)]
        f, _ = load(inv.inventory_sql(CAP, scope=["account_service"], rows=rows))
        self.assertEqual([s[1] for s in scopes(f.conn)], ["account_service"])
        self.assertEqual(one(f.conn, "export_1")["attrs"]["in_scope"], False)
        self.assertEqual([n["type"] for n in notes(f.conn)], ["row_outside_scope"])

    def test_each_scope_row_keeps_its_own_captured_at(self):
        other = "2026-09-16T08:30:00Z"
        sql = inv.inventory_sql(CAP, scope=["account_service"],
                                scope_rows=[f"INSERT INTO snapshot_scope VALUES ('search_service', '{other}');"])
        f, _ = load(sql)
        self.assertEqual({s[1]: s[2] for s in scopes(f.conn)},
                         {"account_service": CAP, "search_service": other})

    def test_unparseable_scope_time_is_unparsed_and_has_no_scope_row(self):
        sql = inv.inventory_sql(CAP, scope=["account_service"],
                                scope_rows=["INSERT INTO snapshot_scope VALUES ('search_service', 'soon');"])
        f, rep = load(sql)
        self.assertEqual([s[1] for s in scopes(f.conn)], ["account_service"])
        self.assertEqual(rep["unparsed"], 1)

    def test_empty_scope_table_writes_no_scope(self):
        f, rep = load(inv.inventory_sql(CAP, scope=[]))
        self.assertEqual(scopes(f.conn), [])
        self.assertEqual(rep["unparsed"], 0)


class UnparsedTests(unittest.TestCase):
    def test_clean_file_has_zero_unparsed_and_counts_match(self):
        rows = [inv.row("customer_profiles", "account_1", EARLY, email=ANA),
                inv.row("customer_exports", "export_1", EARLY, owner_email=ANA)]
        f, rep = load(inv.inventory_sql(CAP, rows=rows))
        self.assertEqual((rep["unparsed"], rep["observations"]), (0, 2))
        self.assertEqual(rep["parsed"], rep["meaningful"])
        row = f.conn.execute("select * from parse_report").fetchone()
        self.assertEqual(row[:5], ("evidence_01", "inventory_sql", rep["meaningful"],
                                   rep["parsed"], 0))

    def test_rows_in_an_unknown_table_are_unparsed(self):
        extra = "CREATE TABLE audit_notes (a TEXT);\nINSERT INTO audit_notes VALUES ('x');"
        f, rep = load(inv.inventory_sql(CAP, extra=extra))
        self.assertEqual(rep["unparsed"], 1)
        self.assertEqual([n["type"] for n in notes(f.conn)], ["unrecognised_table"])

    def test_known_table_name_with_other_columns_is_unrecognised(self):
        sql = inv.inventory_sql(CAP, tables=["customer_profiles"], indexes=False)
        sql = sql.replace(inv.create("customer_profiles"),
                          "CREATE TABLE customer_profiles (resource_id TEXT PRIMARY KEY, nickname TEXT);")
        sql = sql.replace("COMMIT;", "INSERT INTO customer_profiles VALUES ('account_1', 'a');\nCOMMIT;")
        sql = sql.replace("CREATE TABLE snapshot_scope", inv.create("customer_search_documents") +
                          "\nCREATE TABLE snapshot_scope")
        f, rep = load(sql)
        self.assertEqual(rep["unparsed"], 1)
        self.assertEqual(obs(f.conn), [])

    def test_row_without_resource_id_is_unparsed(self):
        sql = inv.inventory_sql(CAP, rows=[
            "INSERT INTO customer_profiles VALUES ('', NULL, NULL, 'a@b.com', NULL, NULL, 'X', "
            f"'{EARLY}');"])
        f, rep = load(sql)
        self.assertEqual((rep["unparsed"], obs(f.conn)), (1, []))

    def test_row_with_bad_updated_at_or_flag_is_unparsed(self):
        bad_time = inv.row("customer_profiles", "account_1", "yesterday", email=ANA)
        bad_flag = ("INSERT INTO connector_subscriptions VALUES ('data_flow_1', NULL, NULL, 'c', 5, "
                    f"'x', '{EARLY}');")
        # the CHECK-less fixture schema lets the bad flag in, so only the adapter can catch it
        f, rep = load(inv.inventory_sql(CAP, rows=[bad_time, bad_flag]))
        self.assertEqual((rep["unparsed"], obs(f.conn)), (2, []))

    def test_reingest_is_idempotent(self):
        sql = inv.inventory_sql(CAP, rows=[inv.row("customer_profiles", "account_1", EARLY, email=ANA)])
        f, _ = load(sql)
        inventory_sql.ingest(f.conn, "evidence_01", sql)
        self.assertEqual(len(obs(f.conn)), 1)
        self.assertEqual(len(scopes(f.conn)), 8)
        self.assertEqual(f.conn.execute("select count(*) from parse_report").fetchone()[0], 1)

    def test_summary_counts_the_file(self):
        f, _ = load(inv.inventory_sql(CAP, rows=[inv.row("customer_profiles", "account_1", EARLY)]))
        s = adapters.summary(f.conn)
        self.assertEqual((s["files"], s["observations"], s["unparsed"]), (1, 1, 0))

    def test_ingest_of_a_file_not_in_the_manifest_raises(self):
        with self.assertRaises(ValueError):
            inventory_sql.ingest(Facts().conn, "nope", inv.inventory_sql(CAP))

    def test_ingest_of_a_non_inventory_raises(self):
        f = Facts().file("evidence_01", CAP)
        with self.assertRaises(ValueError):
            inventory_sql.ingest(f.conn, "evidence_01", "{}")


T_OLD = "2026-09-10T06:00:00Z"
T_MID = "2026-09-12T06:00:00Z"


def subject():
    return Facts().user("user_1", ANA, phone=PHONE, name="Ana Lima")


def build(f, audit=AUDIT):
    return dossier.build_dossier(f.conn, case(ANA, audit))


def res(d, rid):
    hits = [r for r in d["resources"] if r["resource_id"] == rid]
    return hits[0] if len(hits) == 1 else None


class RuleOScopeTrapTests(unittest.TestCase):
    """A resource missing from a partial capture must not count as absent."""

    def linked(self, rid, system):
        f = subject()
        f.link_identity("2026-09-10T00:00:00Z", rid, ANA, "user_1", target=system)
        return f

    def test_system_not_in_scope_proves_nothing(self):
        f = self.linked("backup_1", "backup_service")
        sql = inv.inventory_sql(CAP, scope=[s for s in inv.ALL_SYSTEMS if s != "backup_service"])
        load(sql, facts=f)
        r = res(build(f), "backup_1")
        self.assertNotEqual(r["state"], "ABSENT")
        self.assertIsNone(r["state"])
        self.assertEqual(r["citation"]["rule_o"], [])

    def test_account_only_capture_proves_nothing_for_other_systems(self):
        f = self.linked("export_1", "export_service")
        load(inv.inventory_sql(CAP, scope=["account_service"]), facts=f)
        r = res(build(f), "export_1")
        self.assertIsNone(r["state"])
        self.assertEqual(r["citation"]["rule_o"], [])

    def test_in_scope_omission_from_an_inventory_is_still_not_absence(self):
        f = self.linked("account_1", "account_service")
        load(inv.inventory_sql(CAP), facts=f)
        r = res(build(f), "account_1")
        self.assertIsNone(r["state"])
        self.assertEqual(r["citation"]["rule_o"], [])

    def test_empty_in_scope_table_does_not_flip_an_earlier_present_row(self):
        f = subject()
        f.file("evidence_00", T_OLD)
        f.obs("evidence_00", T_OLD, "account_service", "account_1", "PRESENT", keys=[("email", ANA)])
        load(inv.inventory_sql(CAP, scope=["account_service"]), facts=f)
        r = res(build(f), "account_1")
        self.assertEqual(r["state"], "PRESENT")

    def test_later_omission_from_a_whole_store_dump_still_flips_it(self):
        # control: the same shape with a complete=1 capture does give absence
        f = subject()
        f.file("evidence_00", T_OLD).file("dump", T_MID)
        f.obs("evidence_00", T_OLD, "account_service", "account_1", "PRESENT", keys=[("email", ANA)])
        f.scope("dump", "account_service", T_MID)
        load(inv.inventory_sql(CAP, scope=["account_service"]), facts=f)
        r = res(build(f), "account_1")
        self.assertEqual((r["state"], r["state_basis"]), ("ABSENT", "omission"))
        self.assertEqual(r["citation"]["pick"], "dump")

    def test_explicit_rows_in_the_inventory_still_count(self):
        f = subject()
        rows = [inv.row("backup_restore_entries", "backup_1", EARLY, customer_email=ANA,
                        restore_allowed=1)]
        load(inv.inventory_sql(CAP, rows=rows), facts=f)
        r = res(build(f), "backup_1")
        self.assertEqual((r["state"], r["state_basis"]), ("RESTORABLE", "explicit"))
        self.assertEqual(r["citation"]["pick"], "evidence_01")

    def test_row_in_a_table_outside_scope_is_observed_but_creates_no_scope(self):
        f = subject()
        rows = [inv.row("customer_exports", "export_1", EARLY, owner_email=ANA)]
        load(inv.inventory_sql(CAP, scope=["account_service"], rows=rows), facts=f)
        self.assertEqual(f.conn.execute(
            "select count(*) from capture_scope where system_id = 'export_service'").fetchone()[0], 0)
        self.assertEqual(res(build(f), "export_1")["state"], "PRESENT")

    def test_scope_after_audit_is_not_read(self):
        f = self.linked("account_1", "account_service")
        load(inv.inventory_sql("2026-09-17T08:00:00Z"), facts=f, manifest="2026-09-17T08:00:00Z")
        self.assertIsNone(res(build(f), "account_1")["state"])

    def test_case_05_shape_an_in_scope_empty_inventory_does_not_hide_a_ghost(self):
        # replica dump shows the ghost; a later empty account_service inventory must not undo it
        f = subject()
        f.file("dump", T_OLD)
        f.obs("dump", T_OLD, "account_service", "account_1", "PRESENT", keys=[("email", ANA)])
        f.scope("dump", "account_service", T_OLD)
        load(inv.inventory_sql("2026-09-12T07:18:01Z", scope=["account_service"]), facts=f,
             manifest="2026-09-12T07:18:01Z")
        r = res(build(f), "account_1")
        self.assertEqual((r["state"], r["citation"]["pick"]), ("PRESENT", "dump"))


class RuleOInventoryRankTests(unittest.TestCase):
    """U4: a table dump is preferred over an inventory as a Rule O citation."""

    def setUp(self):
        self.f = subject()
        self.f.link_identity("2026-09-01T00:00:00Z", "account_4", ANA, "user_1")

    def mark(self, file_id, adapter):
        self.f.conn.execute("update evidence_file set adapter = ? where file_id = ?",
                            (adapter, file_id))

    def test_dump_wins_over_a_later_inventory(self):
        self.f.file("dump", T_OLD).file("inventory", T_MID)
        self.f.scope("dump", "account_service", T_OLD).scope("inventory", "account_service", T_MID)
        self.mark("dump", "replica_sql")
        self.mark("inventory", "inventory_sql")
        c = res(build(self.f), "account_4")["citation"]
        self.assertEqual((c["rule"], c["pick"]), ("O", "dump"))
        self.assertEqual([x["file_id"] for x in c["rule_o"]], ["dump", "inventory"])

    def test_inventory_alone_is_still_citable(self):
        self.f.file("inventory", T_MID).scope("inventory", "account_service", T_MID)
        self.mark("inventory", "inventory_sql")
        c = res(build(self.f), "account_4")["citation"]
        self.assertEqual((c["rule"], c["pick"]), ("O", "inventory"))

    def test_two_inventories_keep_latest_first(self):
        self.f.file("i1", T_OLD).file("i2", T_MID)
        self.f.scope("i1", "account_service", T_OLD).scope("i2", "account_service", T_MID)
        self.mark("i1", "inventory_sql")
        self.mark("i2", "inventory_sql")
        c = res(build(self.f), "account_4")["citation"]
        self.assertEqual([x["file_id"] for x in c["rule_o"]], ["i2", "i1"])

    def test_files_without_an_adapter_rank_by_time_as_before(self):
        self.f.file("dump", T_OLD).file("other", T_MID)
        self.f.scope("dump", "account_service", T_OLD).scope("other", "account_service", T_MID)
        c = res(build(self.f), "account_4")["citation"]
        self.assertEqual([x["file_id"] for x in c["rule_o"]], ["other", "dump"])


@unittest.skipUnless((REPO / "dataset" / "evidence" / "databases").exists(), "dataset not present")
class RealFileTests(unittest.TestCase):
    DB = REPO / "dataset" / "evidence" / "databases"

    def ingest(self, name, manifest):
        text = (self.DB / name).read_text(encoding="utf-8")
        self.assertEqual(adapters.select(text)[1], FMT)
        return load(text, manifest=manifest)

    def test_full_scope_file(self):
        f, rep = self.ingest("customer_data_inventory_20260916.sql", "2026-09-16T08:37:39Z")
        self.assertEqual(rep["unparsed"], 0)
        self.assertEqual(len(scopes(f.conn)), 8)
        self.assertEqual({s[3] for s in scopes(f.conn)}, {0})
        o = one(f.conn, "backup_4978")
        self.assertEqual((o["system_id"], o["state"], o["category"]),
                         ("backup_service", "RESTORABLE", "BACKUP_ARCHIVE"))
        self.assertEqual(keys(f.conn, "backup_4978"), [("email", "k.appiah88@gmail.com")])

    def test_account_only_scope_file(self):
        f, rep = self.ingest("customer_data_inventory_20260905.sql", "2026-09-05T07:18:01Z")
        self.assertEqual((rep["unparsed"], rep["observations"]), (0, 0))
        self.assertEqual([s[1] for s in scopes(f.conn)], ["account_service"])

    def test_partial_scope_files_list_exactly_their_scope_rows(self):
        f, rep = self.ingest("customer_data_inventory_20260504.sql", "2026-05-04T09:41:47Z")
        self.assertEqual(rep["unparsed"], 0)
        self.assertNotIn("export_service", [s[1] for s in scopes(f.conn)])
        self.assertEqual(len(scopes(f.conn)), 7)


if __name__ == "__main__":
    unittest.main()
