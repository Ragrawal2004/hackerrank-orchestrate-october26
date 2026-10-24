"""Synthetic snapshot-bundle JSON for adapter tests. No dataset files are read."""
import json


def snap(captured_at, data_store, collection, records):
    return {"captured_at": captured_at, "data_store": data_store, "collection": collection,
            "records": records}


def history(recorded_at, system, resource_id, record_type, result, lookup_value):
    return {"recorded_at": recorded_at, "system": system, "resource_id": resource_id,
            "record_type": record_type, "result": result, "lookup_value": lookup_value}


def bundle(snapshots, exported_at=None, source="records_assurance_service"):
    """JSON text of a bundle; exported_at defaults to the latest snapshot time."""
    if exported_at is None:
        exported_at = max((s["captured_at"] for s in snapshots if isinstance(s, dict)
                           and isinstance(s.get("captured_at"), str)), default="2026-01-01T00:00:00Z")
    return json.dumps({"exported_at": exported_at, "source": source, "snapshots": snapshots},
                      indent=2)


def profile(rid, email, user="user_1", name="Ana Lima", phone="+1 555 0100"):
    return {"resource_id": rid, "customer_reference": user, "full_name": name, "email": email,
            "phone": phone, "account_status": "ACTIVE", "signup_channel": "organic_search"}


def search_doc(rid, email, user="user_1", name="Ana Lima"):
    return {"resource_id": rid, "index_name": "customer_directory_v3",
            "document": {"display_name": name, "email": email, "customer_reference": user}}


def analytics(rid, user="user_1", email=None, phone=None, email_hash=None):
    return {"resource_id": rid, "user_id": user, "email": email, "phone": phone,
            "email_hash": email_hash, "reporting_window": "last_90_days", "metrics": {},
            "recent_activity": []}


def support(rid, user="user_1", text="Please send the update to nobody."):
    return {"resource_id": rid, "ticket_id": "CS-1", "subject_reference": user,
            "channel": "email", "message_text": text}


def attachment(rid, uploaded_by):
    return {"resource_id": rid, "ticket_id": "CS-1", "file_name": "a.pdf",
            "uploaded_by": uploaded_by, "storage_class": "ticket_attachment"}


def export_job(rid, owner):
    return {"resource_id": rid, "owner_email": owner, "file_name": "x.zip",
            "created_at": "2026-01-01T00:00:00Z", "expires_at": "2026-02-01T00:00:00Z",
            "download_available": True, "included_sections": ["profile"]}


def connector(rid, selector, enabled=True):
    return {"resource_id": rid, "connector_name": "c", "destination": "warehouse",
            "subject_selector": selector, "enabled": enabled, "mode": "continuous_sync"}


def backup(rid, subject, allowed=True):
    return {"resource_id": rid, "archive_path": "s3://b/x", "subject_reference": subject,
            "restore_allowed": allowed, "encryption": "AES-256"}


def audit(rid, user="user_1"):
    return {"resource_id": rid, "subject_reference": user, "event_name": "reviewed",
            "actor_type": "automated_privacy_workflow", "integrity_seal": "sha256:00"}


def legal_hold(rid, user="user_1"):
    return {"resource_id": rid, "subject_reference": user, "order_reference": "ORD-1",
            "retention_reason": "tax", "retained_until": "2033-12-31T23:59:59Z"}


def tombstone(rid, user="user_1", name=None, email=None, phone=None, email_hash=None):
    return {"resource_id": rid, "subject_reference": user, "name": name, "email": email,
            "phone": phone, "email_hash": email_hash, "account_status": "DELETED"}
