"""In-memory SQLite fact store (DESIGN §1). Built once per run, no per-case state."""
import sqlite3
from pathlib import Path

SCHEMA = """
create table identity (
    user_id text not null, kind text not null, value text not null,
    valid_from text, valid_to text, source text not null
);
create index identity_value on identity(kind, value);
create index identity_user on identity(user_id);
create table evidence_file (
    file_id text primary key, path text not null, origin text,
    format text, adapter text, manifest_at text not null
);
create table observation (
    obs_id integer primary key, file_id text, locator text, record_at text,
    system_id text, resource_id text, category text, state text,
    state_basis text, attrs_json text
);
create table subject_key (obs_id integer, kind text, value text);
create table join_key (
    file_id text, key_kind text, key_value text, obs_id integer, record_at text
);
create table capture_scope (
    file_id text, system_id text, record_at text, complete integer
);
create table activity (
    activity_id text primary key, event_time text not null, event_type text not null,
    source_system text, target_system text, resource_id text, reported_result text, details text,
    kind text, target_resource text, downstream_resource text,
    linked_identifier text, linked_user_id text
);
create index activity_time on activity(event_time);
create table rule (
    rule_id text, category text not null, condition text, action text not null
);
create table extraction (
    file_id text, method text, model text, payload_json text
);
"""


def connect():
    return sqlite3.connect(":memory:")


def create_schema(conn):
    conn.executescript(SCHEMA)


def dump(conn, path):
    """Write the store to a SQLite file (for debugging, e.g. runs/facts.sqlite)."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        path.unlink()
    dest = sqlite3.connect(path)
    try:
        conn.backup(dest)
    finally:
        dest.close()
