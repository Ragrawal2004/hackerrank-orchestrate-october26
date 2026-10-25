"""Hand-built fact stores for dossier tests. Writes the same tables the adapters will write."""
import hashlib
import json

from ghost import store
from ghost.activity_parse import parse_details


def sha(s):
    return hashlib.sha256(s.encode()).hexdigest()


class Facts:
    def __init__(self):
        self.conn = store.connect()
        store.create_schema(self.conn)
        self._obs = 0
        self._act = 0

    def user(self, user_id, email, phone="+10000000000", name=None, old_emails=(), old_phones=()):
        """Current snapshot row plus audit history, shaped like loaders._identity_rows."""
        rows = [(user_id, "user_id", user_id, None, None, "snapshot"),
                (user_id, "name", name or user_id.replace("_", " "), None, None, "snapshot"),
                (user_id, "email", email, "2026-01-01T00:00:00Z", None, "snapshot"),
                (user_id, "phone", phone, "2026-01-01T00:00:00Z", None, "snapshot")]
        for e in old_emails:
            rows.append((user_id, "email", e, None, "2025-12-01T00:00:00Z", "audit:t"))
        for p in old_phones:
            rows.append((user_id, "phone", p, None, "2025-12-01T00:00:00Z", "audit:t"))
        for r in list(rows):
            if r[1] == "email":
                rows.append((user_id, "email_hash", sha(r[2]), r[3], r[4], "computed"))
        self.conn.executemany("insert into identity values (?,?,?,?,?,?)", rows)
        return self

    def file(self, file_id, manifest_at, origin="test"):
        self.conn.execute(
            "insert into evidence_file(file_id,path,origin,format,adapter,manifest_at) "
            "values (?,?,?,null,null,?)", (file_id, f"test/{file_id}", origin, manifest_at))
        return self

    def obs(self, file_id, record_at, system_id=None, resource_id=None, state=None,
            category=None, keys=(), joins=(), attrs=None):
        """keys: (kind, value) subject identifiers; joins: (key_kind, key_value)."""
        self._obs += 1
        oid = self._obs
        self.conn.execute(
            "insert into observation values (?,?,?,?,?,?,?,?,?,?)",
            (oid, file_id, f"L{oid}", record_at, system_id, resource_id, category, state,
             "explicit", json.dumps(attrs or {}, sort_keys=True)))
        self.conn.executemany("insert into subject_key values (?,?,?)",
                              [(oid, k, v) for k, v in keys])
        self.conn.executemany("insert into join_key values (?,?,?,?,?)",
                              [(file_id, k, v, oid, record_at) for k, v in joins])
        return oid

    def scope(self, file_id, system_id, record_at, complete=1):
        self.conn.execute("insert into capture_scope values (?,?,?,?)",
                          (file_id, system_id, record_at, complete))
        return self

    def activity(self, event_time, event_type, source, target, resource_id, result, details,
                 activity_id=None):
        self._act += 1
        aid = activity_id or f"activity_{self._act}"
        p = parse_details(event_type, resource_id, details)
        self.conn.execute(
            "insert into activity(activity_id, event_time, event_type, source_system, "
            "target_system, resource_id, reported_result, details, kind, target_resource, "
            "downstream_resource, linked_identifier, linked_user_id) "
            "values (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (aid, event_time, event_type, source, target, resource_id, result, details,
             p["kind"], p["target_resource"], p["downstream_resource"],
             p["linked_email"], p["linked_user_id"]))
        return aid

    def checkpoint(self, event_time, resource_id, result="SUCCESS", system="account_service"):
        return self.activity(event_time, "SYNC_RECORD", system, "account_replication_cluster",
                             resource_id, result,
                             f"Replication worker reported its final checkpoint for {resource_id}.")

    def repopulate(self, event_time, flow_id, downstream, result="SUCCESS",
                   target="search_service"):
        return self.activity(event_time, "SYNC_RECORD", "integration_service", target, flow_id,
                             result, f"Active connector repopulated downstream resource {downstream}.")

    def link_identity(self, event_time, resource_id, email, user_id, target="account_service"):
        return self.activity(event_time, "LINK_RESOURCE_IDENTITY", "data_catalog", target,
                             resource_id, "SUCCESS",
                             f"Data catalog linked {resource_id} ({resource_id}) to {email} and {user_id}.")


def case(email, audit_at, case_id="case_t", requested_at="2026-09-01T00:00:00Z",
         reported_at=None):
    return {"case_id": case_id, "email": email, "requested_at": requested_at,
            "reported_status": "COMPLETE", "reported_at": reported_at or requested_at,
            "audit_at": audit_at, "request_text": "Please delete my account."}
