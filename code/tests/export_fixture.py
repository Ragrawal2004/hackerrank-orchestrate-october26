"""Synthetic service read-back export JSON for adapter tests. No dataset files are read."""
import json


def envelope(services, exported_at="2026-09-15T11:44:55Z", region="global",
             source="records_assurance_gateway"):
    """JSON text of an export; services is {service_id: {section: [rows]}}."""
    return json.dumps({"exported_at": exported_at, "region": region, "source": source,
                       "service_exports": services}, indent=2)


def one(service, section, rows, **kw):
    return envelope({service: {section: rows}}, **kw)


def profile_lookup(rid, at, email=None, status=None, **profile):
    """A 200 lookup when email or profile fields are given, else a 404 lookup."""
    if status is None:
        status = 200 if (email or profile) else 404
    body = None
    if status == 200:
        body = {"customer_reference": None, "email": email, "email_hash": None,
                "full_name": None, "phone": None, **profile}
    return {"observed_at": at, "profile": body, "resource_id": rid, "status_code": status}


def analytics(rid, at, **fields):
    return {"audience_cohort": "general_shopper", "customer_reference": None, "email": None,
            "email_hash": None, "observed_at": at, "phone": None, "resource_id": rid, **fields}


def restore(rid, at, allowed, email=None, ref=None):
    return {"archive_key": f"archive/customer-data/{rid}.tar.zst", "customer_email": email,
            "customer_reference": ref, "observed_at": at, "resource_id": rid,
            "restore_allowed": allowed}


def export_job(rid, at, owner=None, phone=None, ref=None):
    return {"customer_reference": ref, "object_key": f"exports/{rid}/customer_data.zip",
            "observed_at": at, "owner_email": owner, "owner_phone": phone, "resource_id": rid}


def connector(rid, at, enabled, email=None, ref=None):
    return {"connector_name": "analytics_identity_bridge", "customer_email": email,
            "customer_reference": ref, "enabled": enabled,
            "last_delivery_status": "delivered" if enabled else "stopped", "observed_at": at,
            "resource_id": rid}


def search(rid, at, query, docs=None, total=None, doc_rid=None, **doc_fields):
    """A search read-back; by default one hit whose email is the query."""
    if docs is None:
        docs = [{"customer_reference": None, "display_name": None, "email": query,
                 "email_hash": None, "phone": None, "resource_id": doc_rid or rid, **doc_fields}]
    return {"hits": {"documents": docs, "total": len(docs) if total is None else total},
            "observed_at": at, "query": query, "resource_id": rid}


def audit(rid, at, ref="user_1", until="2034-09-30T23:59:59Z"):
    return {"event_code": "SUBJECT_REQUEST_AUDITED", "observed_at": at, "resource_id": rid,
            "retention_until": until, "subject_reference": ref}


def ticket(rid, at, email=None, ref=None, name=None, phone=None):
    return {"customer_reference": ref, "message_excerpt": "Customer asked about a delivery.",
            "observed_at": at, "requester_email": email, "requester_name": name,
            "requester_phone": phone, "resource_id": rid}
