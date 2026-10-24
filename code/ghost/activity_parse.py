"""Parse system_activity `details` text into structured link/target fields.

Activities are claims; this only extracts which resources and identifiers a row names.
"""
import re

_PRIMARY = re.compile(r"submitted primary profile (\S+?) to \S+?\.?$")
_TARGET = re.compile(r"Target resource: (\S+?)\.?$")
_REPOP = re.compile(r"repopulated downstream resource (\S+?)\.?$")
# "linked X (X) to <email> and <user>" and "confirmed X (file.pdf) belongs only to <email> and <user>;"
_LINK = re.compile(
    r"(?:linked|confirmed) (\S+) \([^)]*\) (?:belongs only to|to) (\S+@\S+?) and (user_\d+)[.;]")
_CLASSIFY = re.compile(r"classified request for (\S+) as non-deletion")


def parse_details(event_type, resource_id, details):
    out = {
        "kind": "other",
        "target_resource": None,
        "downstream_resource": None,
        "linked_email": None,
        "linked_user_id": None,
    }
    text = (details or "").strip()
    if event_type == "LINK_RESOURCE_IDENTITY":
        m = _LINK.search(text)
        if m:
            out.update(kind="link_identity", target_resource=m.group(1),
                       linked_email=m.group(2).strip().lower(), linked_user_id=m.group(3))
    elif event_type == "CLASSIFY_REQUEST":
        m = _CLASSIFY.search(text)
        if m:
            out.update(kind="classify_non_deletion", target_resource=m.group(1))
    else:
        m = _REPOP.search(text)
        if m:
            out.update(kind="repopulate", downstream_resource=m.group(1))
        elif (m := _PRIMARY.search(text)):
            out.update(kind="primary_profile", target_resource=m.group(1))
        elif (m := _TARGET.search(text)):
            out.update(kind="target_resource", target_resource=m.group(1))
    return out
