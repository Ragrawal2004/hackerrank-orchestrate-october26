"""Synthetic inventory SQL text for adapter tests. No dataset files are read."""

COLS = {
    "customer_profiles": ("resource_id", "customer_reference", "full_name", "email", "phone",
                          "email_hash", "account_status", "updated_at"),
    "customer_search_documents": ("resource_id", "customer_reference", "display_name", "email",
                                  "phone", "email_hash", "index_name", "updated_at"),
    "customer_analytics_profiles": ("resource_id", "customer_reference", "email", "phone",
                                    "email_hash", "audience_cohort", "updated_at"),
    "support_records": ("resource_id", "customer_reference", "requester_name", "requester_email",
                        "requester_phone", "message_excerpt", "updated_at"),
    "customer_exports": ("resource_id", "customer_reference", "owner_email", "owner_phone",
                         "object_key", "updated_at"),
    "connector_subscriptions": ("resource_id", "customer_reference", "customer_email",
                                "connector_name", "enabled", "last_delivery_status", "updated_at"),
    "backup_restore_entries": ("resource_id", "customer_reference", "customer_email",
                               "archive_key", "restore_allowed", "updated_at"),
    "security_audit_records": ("resource_id", "subject_reference", "event_code",
                               "retention_until", "updated_at"),
}
SYSTEM = {"customer_profiles": "account_service", "customer_search_documents": "search_service",
          "customer_analytics_profiles": "analytics_platform", "support_records": "support_platform",
          "customer_exports": "export_service", "connector_subscriptions": "integration_service",
          "backup_restore_entries": "backup_service", "security_audit_records": "security_service"}
ALL_SYSTEMS = sorted(SYSTEM.values())

_DEFAULTS = {
    "customer_profiles": {"account_status": "DELETION_PENDING"},
    "customer_search_documents": {"index_name": "customer_directory_v3"},
    "customer_analytics_profiles": {"audience_cohort": "returning_customer"},
    "support_records": {"message_excerpt": "Customer asked support to correct delivery details."},
    "customer_exports": {"object_key": "exports/x/customer_data.zip"},
    "connector_subscriptions": {"connector_name": "analytics_identity_bridge", "enabled": 1,
                                "last_delivery_status": "delivered"},
    "backup_restore_entries": {"archive_key": "archive/customer-data/b.tar.zst", "restore_allowed": 1},
    "security_audit_records": {"event_code": "SUBJECT_REQUEST_AUDITED",
                               "retention_until": "2033-12-31T23:59:59Z"},
}
_NOT_NULL = {"snapshot_scope": ("origin_system", "captured_at")}

SCHEMA_SCOPE = "CREATE TABLE snapshot_scope (origin_system TEXT PRIMARY KEY, captured_at TEXT NOT NULL);"
INDEXES = [
    "CREATE INDEX idx_customer_profiles_email ON customer_profiles(email);",
    "CREATE INDEX idx_customer_search_email ON customer_search_documents(email);",
    "CREATE INDEX idx_customer_analytics_email ON customer_analytics_profiles(email);",
    "CREATE INDEX idx_support_requester_email ON support_records(requester_email);",
    "CREATE INDEX idx_customer_exports_owner_email ON customer_exports(owner_email);",
]


def _q(v):
    if v is None:
        return "NULL"
    if isinstance(v, int):
        return str(v)
    return "'" + str(v).replace("'", "''") + "'"


def create(table, cols=None):
    cols = cols or COLS[table]
    kind = {"resource_id": " PRIMARY KEY", "enabled": " INTEGER", "restore_allowed": " INTEGER"}
    return f"CREATE TABLE {table} ({', '.join(c + kind.get(c, '') for c in cols)});"


def row(table, rid, updated_at, **kw):
    """One INSERT for a typed table; unspecified columns are NULL or a default."""
    vals = {**_DEFAULTS[table], **kw, "resource_id": rid, "updated_at": updated_at}
    bad = set(vals) - set(COLS[table])
    assert not bad, bad
    return (f"INSERT INTO {table} VALUES ({', '.join(_q(vals.get(c)) for c in COLS[table])});")


def inventory_sql(captured_at, scope=None, rows=(), extra="", footer=True, indexes=True,
                  tables=None, scope_rows=None):
    """scope: systems in snapshot_scope (default all, captured at captured_at); rows: INSERT
    strings; extra: raw statements appended before the indexes."""
    scope = ALL_SYSTEMS if scope is None else scope
    out = ["PRAGMA foreign_keys=ON;", "BEGIN TRANSACTION;", SCHEMA_SCOPE]
    out += [create(t) for t in (COLS if tables is None else tables)]
    for s in scope:
        out.append(f"INSERT INTO snapshot_scope VALUES ({_q(s)}, {_q(captured_at)});")
    out += list(scope_rows or [])
    out += list(rows)
    if extra:
        out.append(extra)
    if indexes:
        out += INDEXES
    out.append("COMMIT;")
    if footer:
        out.append(f"-- snapshot_completed_at={captured_at}")
    return "\n".join(out) + "\n"
