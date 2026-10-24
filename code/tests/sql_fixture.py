"""Synthetic replica SQL text for adapter tests. No dataset files are read."""
import json

J_COLS = ("resource_id TEXT PRIMARY KEY, origin_system TEXT NOT NULL, collection_name TEXT NOT NULL, "
          "record_json TEXT NOT NULL CHECK (json_valid(record_json)), updated_at TEXT NOT NULL")
T_COLS = ("resource_id TEXT PRIMARY KEY, user_id TEXT NOT NULL, full_name TEXT NOT NULL, "
          "email TEXT NOT NULL, phone TEXT NOT NULL, account_status TEXT NOT NULL, "
          "updated_at TEXT NOT NULL")


def _q(value):
    return "'" + str(value).replace("'", "''") + "'"


def header(captured_at, topology):
    return (f"-- Ghost Data Still Exists: point-in-time database snapshot\n"
            f"-- Dialect: SQLite-compatible SQL\n-- Captured at: {captured_at}\n"
            f"-- Database topology: {topology}\n"
            f"-- Compare the primary and any replica tables for replication drift.\n\n")


def row(rid, system, collection, record, updated_at):
    """One record-json row: record is a dict (resource_id is added)."""
    return (rid, system, collection, json.dumps({"resource_id": rid, **record}), updated_at)


def j_sql(captured_at, tables, topology="read_replica", extra=""):
    """tables: {table_name: [row(...), ...]}; first table should be customer_data_primary."""
    out = [header(captured_at, topology), "BEGIN TRANSACTION;\n"]
    for name, rows in tables.items():
        out.append(f"CREATE TABLE {name} (\n            {J_COLS}\n        );\n")
        for r in rows:
            out.append(f'INSERT INTO "{name}" VALUES({", ".join(_q(v) for v in r)});\n')
    out.append(extra)
    out.append("COMMIT;\n")
    return "".join(out)


def t_sql(captured_at, primary, replica, topology="failover_replica"):
    """primary/replica: [(resource_id, user_id, full_name, email, phone, status, updated_at)]."""
    out = [header(captured_at, topology), "BEGIN TRANSACTION;\n"]
    for name, rows in (("customer_profiles_failover_replica", replica),
                       ("customer_profiles_primary", primary)):
        out.append(f"CREATE TABLE {name} (\n                {T_COLS}\n            );\n")
        for r in rows:
            out.append(f'INSERT INTO "{name}" VALUES({", ".join(_q(v) for v in r)});\n')
    out.append("COMMIT;\n")
    return "".join(out)


def inventory_sql(captured_at):
    """The shape of another adapter (typed tables + snapshot_scope); must not be claimed."""
    return (header(captured_at, "primary_only") + "BEGIN TRANSACTION;\n"
            "CREATE TABLE snapshot_scope (origin_system TEXT, captured_at TEXT);\n"
            "CREATE TABLE customer_profiles (resource_id TEXT PRIMARY KEY, customer_reference TEXT, "
            "full_name TEXT, email TEXT, phone TEXT, email_hash TEXT, account_status TEXT, "
            "updated_at TEXT);\n"
            "INSERT INTO \"snapshot_scope\" VALUES('account_service','" + captured_at + "');\n"
            "COMMIT;\n")
