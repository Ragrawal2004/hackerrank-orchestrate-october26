"""Per-case dossier (DESIGN §2 steps 2-6). A pure function of the case row and the fact store.

Two clocks (SPEC U3):
- a record (observation, capture scope, activity) is usable only if its OWN timestamp is at or
  before audit_at, in any file, including files whose manifest time is later;
- a file is citable only if its manifest captured_at is at or before audit_at.
Every store read goes through a clock-gated query whose rows are re-checked (LateRecordRead), and
assert_clock re-checks the finished dossier before it is returned.

Store conventions the adapters must follow: subject_key kind "name" holds person names (names
never link on their own); join_key kinds in TRACE_KEYS join lines inside one file, kinds in
REFERENCE_KEYS hold another resource's id; every observation from one complete capture carries
that capture's capture_scope.record_at.
"""
import json
from collections import defaultdict

from . import identity, store

# U7 (open assumption, SPEC §8): a PARTIAL final checkpoint reopens a resource like a SUCCESS.
# No sample has one; confirm against the replica SQL once T9 parses it.
RECREATION_RESULTS = frozenset({"SUCCESS", "PARTIAL"})
RECREATION_KINDS = frozenset({"final_checkpoint", "repopulate"})
EXTRACT_ADAPTERS = frozenset({"inventory_sql"})  # filtered extracts: Rule O prefers a dump (U4)
TRACE_KEYS = frozenset({"trace_id", "request_id"})
REFERENCE_KEYS = frozenset({"source_resource_id", "customer_reference"})
LINK_KINDS = ("user_id", "email", "email_hash", "phone")


class ClockViolation(AssertionError):
    pass


class LateRecordRead(ClockViolation):
    pass


class LateCitation(ClockViolation):
    pass


def _norm(value):
    return value.strip().lower() if isinstance(value, str) else value


def _t(value):
    return store.epoch_us(value)


# ---- clock-gated reads ----------------------------------------------------------------------

_RECORD_AT = "coalesce(o.record_at, f.manifest_at)"
_OBS_FROM = ("from observation o left join evidence_file f on f.file_id = o.file_id "
             f"where ts({_RECORD_AT}) <= ts(?)")
_OBS_COLS = ("obs_id", "file_id", "locator", "record_at", "system_id", "resource_id",
             "category", "state", "attrs_json", "manifest_at")


def _query_observations(conn, audit_at):
    sql = (f"select o.obs_id, o.file_id, o.locator, {_RECORD_AT}, o.system_id, o.resource_id, "
           f"o.category, o.state, o.attrs_json, f.manifest_at {_OBS_FROM} order by o.obs_id")
    return [dict(zip(_OBS_COLS, r)) for r in conn.execute(sql, (audit_at,))]


def _query_subject_keys(conn, audit_at):
    return conn.execute("select sk.obs_id, sk.kind, sk.value from subject_key sk "
                        f"join observation o on o.obs_id = sk.obs_id "
                        f"left join evidence_file f on f.file_id = o.file_id "
                        f"where ts({_RECORD_AT}) <= ts(?)", (audit_at,)).fetchall()


def _query_join_keys(conn, audit_at):
    return conn.execute("select jk.obs_id, jk.key_kind, jk.key_value from join_key jk "
                        f"join observation o on o.obs_id = jk.obs_id "
                        f"left join evidence_file f on f.file_id = o.file_id "
                        f"where ts({_RECORD_AT}) <= ts(?)", (audit_at,)).fetchall()


def _query_scopes(conn, audit_at):
    cols = ("file_id", "system_id", "record_at", "complete", "manifest_at", "adapter")
    sql = ("select s.file_id, s.system_id, s.record_at, s.complete, f.manifest_at, f.adapter "
           "from capture_scope s left join evidence_file f on f.file_id = s.file_id "
           "where ts(s.record_at) <= ts(?) order by s.file_id, s.system_id, s.record_at")
    return [dict(zip(cols, r)) for r in conn.execute(sql, (audit_at,))]


def _query_activities(conn, audit_at):
    cols = ("activity_id", "event_time", "event_type", "source_system", "target_system",
            "resource_id", "reported_result", "kind", "target_resource", "downstream_resource",
            "linked_identifier", "linked_user_id")
    sql = (f"select {', '.join(cols)} from activity where ts(event_time) <= ts(?) "
           "order by ts(event_time), activity_id")
    return [dict(zip(cols, r)) for r in conn.execute(sql, (audit_at,))]


def _checked(rows, field, audit_at, what):
    limit = _t(audit_at)
    for r in rows:
        t = _t(r[field])
        if t is None or t > limit:
            raise LateRecordRead(f"{what} {r} has {field}={r[field]} after audit_at {audit_at}")
    return rows


def _count_late_subject_records(conn, audit_at, values):
    if not values:
        return 0
    marks = ",".join("?" * len(values))
    sql = ("select count(distinct o.obs_id) from observation o "
           "join subject_key sk on sk.obs_id = o.obs_id "
           "left join evidence_file f on f.file_id = o.file_id "
           f"where ts({_RECORD_AT}) > ts(?) and sk.kind != 'name' "
           f"and lower(trim(sk.value)) in ({marks})")
    return conn.execute(sql, (audit_at, *sorted(values))).fetchone()[0]


# ---- build ----------------------------------------------------------------------------------

def build_dossier(conn, case):
    store.register_functions(conn)
    audit_at = case["audit_at"]
    limit = _t(audit_at)
    uid, via = identity.resolve_email_detailed(conn, case["email"])
    ids = identity.identifiers(conn, uid, as_of=audit_at)
    subject = {_norm(v) for k in LINK_KINDS for v in ids[k]}
    names = {_norm(v) for v in ids["name"]}
    owners = defaultdict(set)
    for u, value in conn.execute("select user_id, value from identity where kind != 'name'"):
        owners[_norm(value)].add(u)
    events = []

    obs = {o["obs_id"]: o for o in
           _checked(_query_observations(conn, audit_at), "record_at", audit_at, "observation")}
    keys, joins = defaultdict(list), defaultdict(list)
    for oid, kind, value in _query_subject_keys(conn, audit_at):
        if oid in obs:
            keys[oid].append((kind, _norm(value)))
    for oid, kind, value in _query_join_keys(conn, audit_at):
        if oid in obs and value:
            joins[oid].append((kind, value.strip()))
    scopes = _checked(_query_scopes(conn, audit_at), "record_at", audit_at, "capture_scope")
    acts = _checked(_query_activities(conn, audit_at), "event_time", audit_at, "activity")

    by_res = defaultdict(list)
    for oid in sorted(obs):
        if obs[oid]["resource_id"]:
            by_res[obs[oid]["resource_id"]].append(oid)
    trace_index, ref_index = defaultdict(set), defaultdict(set)
    for oid, js in joins.items():
        for kind, value in js:
            if kind in TRACE_KEYS:
                trace_index[(obs[oid]["file_id"], kind, value)].add(oid)
            elif kind in REFERENCE_KEYS:
                ref_index[value].add(oid)

    def matches_subject(oid):
        return any(v in subject for k, v in keys[oid] if k != "name")

    def other_owners(oids):
        """A8: users other than the subject named by these records, if none names the subject."""
        if any(matches_subject(o) for o in oids):
            return []
        return sorted({u for o in oids for k, v in keys[o] if k != "name"
                       for u in owners.get(v, ())} - {uid})

    linked_obs, linked_res, blocked = {}, {}, set()

    def block(key, method, link_via, found, **where):
        blocked.add(key)
        events.append({"type": "a8_conflict", **where, "method": method, "via": link_via,
                       "owners": found})

    def add_res(rid, method, link_via):
        if rid in linked_res or ("res", rid) in blocked:
            return False
        if method != "direct":
            found = other_owners(by_res.get(rid, []))
            if found:
                block(("res", rid), method, link_via, found, resource_id=rid)
                return False
        linked_res[rid] = {"method": method, "via": link_via, "confidence": "high"}
        return True

    def add_obs(oid, method, link_via):
        if oid in linked_obs or ("obs", oid) in blocked:
            return False
        rid = obs[oid]["resource_id"]
        if rid:
            add_res(rid, method, link_via)
            if rid not in linked_res:
                return False
        elif method != "direct":
            found = other_owners([oid])
            if found:
                block(("obs", oid), method, link_via, found, resource_id=None, obs_id=oid)
                return False
        linked_obs[oid] = {"method": method, "via": link_via}
        return True

    changed = True
    while changed:
        changed = False
        for oid in sorted(obs):
            if matches_subject(oid):
                changed |= add_obs(oid, "direct", {"obs_id": oid})
        for rid in sorted(linked_res):
            for oid in by_res.get(rid, ()):
                changed |= add_obs(oid, "resource", {"resource_id": rid})
        for oid in sorted(linked_obs):
            for kind, value in joins[oid]:
                if kind in TRACE_KEYS:
                    for other in sorted(trace_index[(obs[oid]["file_id"], kind, value)]):
                        changed |= add_obs(other, "trace", {"obs_id": oid, "key": value})
        for oid in sorted(linked_obs):
            for kind, value in joins[oid]:
                # a reference holding an identifier (customer_reference=user_N) is not a resource
                if kind in REFERENCE_KEYS and _norm(value) not in owners:
                    changed |= add_res(value, "reference", {"obs_id": oid})
        for rid in sorted(linked_res):
            for other in sorted(ref_index.get(rid, ())):
                changed |= add_obs(other, "reference", {"resource_id": rid})
        for a in acts:
            if a["kind"] == "link_identity" and a["target_resource"] and (
                    _norm(a["linked_identifier"]) in subject or _norm(a["linked_user_id"]) in subject):
                changed |= add_res(a["target_resource"], "activity",
                                   {"activity_id": a["activity_id"]})
            elif a["kind"] == "repopulate" and a["resource_id"] in linked_res:
                changed |= add_res(a["downstream_resource"], "activity",
                                   {"activity_id": a["activity_id"]})

    for oid in sorted(obs):
        if oid not in linked_obs and any(k == "name" and v in names for k, v in keys[oid]):
            events.append({"type": "name_only_match", "obs_id": oid,
                           "file_id": obs[oid]["file_id"], "resource_id": obs[oid]["resource_id"]})

    used_acts = {v["via"]["activity_id"] for v in linked_res.values() if "activity_id" in v["via"]}
    used_obs = set(linked_obs)
    late_files = {obs[o]["file_id"] for o in linked_obs if _t(obs[o]["manifest_at"]) is None
                  or _t(obs[o]["manifest_at"]) > limit}
    resources = []
    for rid in sorted(linked_res):
        systems = sorted({obs[o]["system_id"] for o in by_res.get(rid, ()) if obs[o]["system_id"]})
        if not systems:
            found = _activity_system(rid, acts)
            if found:
                systems = [found[0]]
                used_acts.add(found[1])
            else:
                systems = [None]
                events.append({"type": "system_unknown", "resource_id": rid})
        for sys in systems:
            entry = _resolve(rid, sys, linked_res[rid], obs, by_res.get(rid, []), scopes, acts,
                             limit, events)
            resources.append(entry)
            src = entry["state_source"]
            if src and src["obs_id"] is not None:
                used_obs.add(src["obs_id"])
            if src and (_t(src["manifest_at"]) is None or _t(src["manifest_at"]) > limit):
                late_files.add(src["file_id"])
            if entry["recreated_by"]:
                used_acts.add(entry["recreated_by"]["activity_id"])

    times = [_t(obs[o]["record_at"]) for o in used_obs]
    times += [_t(a["event_time"]) for a in acts if a["activity_id"] in used_acts]
    max_used = None
    if times:
        top = max(times)
        max_used = next(x for x in [obs[o]["record_at"] for o in used_obs] +
                        [a["event_time"] for a in acts if a["activity_id"] in used_acts]
                        if _t(x) == top)
    d = {
        "case_id": case["case_id"],
        "audit_at": audit_at,
        "user_id": uid,
        "resolved_via": via,
        "identifiers": {k: sorted(v) for k, v in sorted(ids.items())},
        "resources": resources,
        "observations_used": sorted(used_obs),
        "activities_used": sorted(used_acts),
        "anchor_candidates": sorted({obs[o]["file_id"] for o in linked_obs if matches_subject(o)
                                     and _citable(obs[o]["manifest_at"], limit)}),
        "account_linked": any(r["system_id"] == "account_service" for r in resources),
        "events": events,
        "clock": {
            "max_record_at_used": max_used,
            "late_records_excluded": _count_late_subject_records(conn, audit_at, subject),
            "late_manifest_files_used": sorted(late_files),
        },
    }
    assert_clock(conn, d)
    return d


def _citable(manifest_at, limit):
    t = _t(manifest_at)
    return t is not None and t <= limit


def _activity_system(rid, acts):
    """Logical system of a resource named only by activities: (system_id, activity_id)."""
    for a in reversed(acts):
        if a["kind"] == "final_checkpoint" and a["target_resource"] == rid:
            return a["source_system"], a["activity_id"]
        if a["kind"] == "repopulate":
            if a["downstream_resource"] == rid:
                return a["target_system"], a["activity_id"]
            continue
        if rid in (a["resource_id"], a["target_resource"]) and a["target_system"]:
            return a["target_system"], a["activity_id"]
    return None


def _recreates(a, sys, rid):
    if a["kind"] == "final_checkpoint":
        return a["target_resource"] == rid and a["source_system"] == sys
    return a["downstream_resource"] == rid and a["target_system"] == sys


def _resolve(rid, sys, link, obs, oids, scopes, acts, limit, events):
    """State and citation candidates of one (system_id, resource_id)."""
    mine = [obs[o] for o in oids if obs[o]["system_id"] == sys]
    cands = []
    for o in mine:
        cands.append(((_t(o["record_at"]), 1, _t(o["manifest_at"]) or -1, -o["obs_id"], o["file_id"]),
                      {"basis": "explicit", "state": o["state"], "obs": o}))
    captured = {(o["file_id"], _t(o["record_at"])) for o in mine}
    omissions = []
    for s in scopes:
        if s["complete"] and s["system_id"] == sys and \
                (s["file_id"], _t(s["record_at"])) not in captured:
            omissions.append(s)
            cands.append(((_t(s["record_at"]), 0, _t(s["manifest_at"]) or -1, 0, s["file_id"]),
                          {"basis": "omission", "state": "ABSENT", "scope": s}))

    state = basis = source = recreated = None
    if cands:
        win_key, win = max(cands, key=lambda c: c[0])
        state, basis = win["state"], win["basis"]
        if basis == "explicit":
            o = win["obs"]
            source = {"obs_id": o["obs_id"], "file_id": o["file_id"], "locator": o["locator"],
                      "record_at": o["record_at"], "manifest_at": o["manifest_at"],
                      "attrs": json.loads(o["attrs_json"] or "{}")}
        else:
            s = win["scope"]
            source = {"obs_id": None, "file_id": s["file_id"], "locator": None,
                      "record_at": s["record_at"], "manifest_at": s["manifest_at"], "attrs": {}}
        if state == "ABSENT":
            later = [a for a in acts if a["kind"] in RECREATION_KINDS
                     and a["reported_result"] in RECREATION_RESULTS
                     and _recreates(a, sys, rid) and _t(a["event_time"]) > win_key[0]]
            if later:
                a = later[-1]
                state, basis = "PRESENT", "recreation"
                recreated = {"activity_id": a["activity_id"], "event_time": a["event_time"],
                             "result": a["reported_result"]}
                events.append({"type": "recreation_reopen", "system_id": sys, "resource_id": rid,
                               "activity_id": a["activity_id"], "result": a["reported_result"]})
                if a["reported_result"] == "PARTIAL":
                    events.append({"type": "recreation_partial", "system_id": sys,
                                   "resource_id": rid, "activity_id": a["activity_id"]})

    per_file = {}
    for o in sorted(mine, key=lambda o: (_t(o["record_at"]), o["obs_id"])):
        if _citable(o["manifest_at"], limit):
            per_file[o["file_id"]] = {"file_id": o["file_id"], "manifest_at": o["manifest_at"],
                                      "state": o["state"]}
    rule_e = sorted(per_file.values(), key=lambda x: (-_t(x["manifest_at"]), x["file_id"]))
    # U4: a table dump is cited before an inventory (a filtered extract), then latest first.
    rule_o = sorted({s["file_id"]: {"file_id": s["file_id"], "manifest_at": s["manifest_at"],
                                    "adapter": s.get("adapter")}
                     for s in omissions if _citable(s["manifest_at"], limit)}.values(),
                    key=lambda x: (x["adapter"] in EXTRACT_ADAPTERS, -_t(x["manifest_at"]),
                                   x["file_id"]))

    rule = pick = None
    if recreated:
        rule, pick = "activity", recreated["activity_id"]
    elif state is not None:
        agree = [e for e in rule_e if e["state"] == state]
        if agree:
            rule, pick = "E", agree[0]["file_id"]
        elif state == "ABSENT" and rule_o:
            rule, pick = "O", rule_o[0]["file_id"]
            if len(rule_o) > 1:
                events.append({"type": "rule_o_ambiguous", "system_id": sys, "resource_id": rid,
                               "candidates": [x["file_id"] for x in rule_o]})
    if pick is None:
        events.append({"type": "unciteable", "system_id": sys, "resource_id": rid, "state": state})

    category = None
    for o in sorted(mine, key=lambda o: (_t(o["record_at"]), o["obs_id"])):
        category = o["category"] or category
    if source and source["obs_id"] is not None:
        category = obs[source["obs_id"]]["category"] or category
    return {
        "system_id": sys,
        "resource_id": rid,
        "link": link,
        "category": category,
        "state": state,
        "state_basis": basis,
        "state_source": source,
        "recreated_by": recreated,
        "citation": {"rule": rule, "pick": pick, "rule_e": rule_e, "rule_o": rule_o},
    }


# ---- guessed account (SPEC L1) --------------------------------------------------------------

def add_guessed_account(conn, d, pick):
    """Add the linker's low-confidence account to the dossier (returned, changed in place).

    Its required end state is ABSENT and nothing in it is evidence of a link: the state is the
    latest clock-gated observation of that account if any (so policy can see a contradiction),
    otherwise ABSENT with basis `l1_guess`. It never takes a Rule E citation; it cites the latest
    in-scope capture of account_service that omits it (Rule O, dump before inventory), and a
    partial (complete=0) capture may be cited because nothing is inferred from it (SPEC T16-C)."""
    if pick is None:
        return d
    store.register_functions(conn)
    audit_at, rid = d["audit_at"], pick["resource_id"]
    limit = _t(audit_at)
    seen = conn.execute(
        f"select o.obs_id, o.file_id, {_RECORD_AT}, o.state {_OBS_FROM} "
        "and o.system_id = 'account_service' and o.resource_id = ?", (audit_at, rid)).fetchall()
    seen.sort(key=lambda r: (_t(r[2]), r[0]))
    state = seen[-1][3] if seen else "ABSENT"
    captured = {(r[1], _t(r[2])) for r in seen}
    omitting = {}
    if state == "ABSENT":
        for s in _checked(_query_scopes(conn, audit_at), "record_at", audit_at, "capture_scope"):
            if s["system_id"] == "account_service" and _citable(s["manifest_at"], limit) \
                    and (s["file_id"], _t(s["record_at"])) not in captured:
                omitting[s["file_id"]] = {"file_id": s["file_id"], "manifest_at": s["manifest_at"],
                                          "adapter": s.get("adapter")}
    rule_o = sorted(omitting.values(), key=lambda x: (x["adapter"] in EXTRACT_ADAPTERS,
                                                      -_t(x["manifest_at"]), x["file_id"]))
    if len(rule_o) > 1:
        d["events"].append({"type": "rule_o_ambiguous", "system_id": "account_service",
                            "resource_id": rid, "candidates": [x["file_id"] for x in rule_o]})
    d["resources"].append({
        "system_id": "account_service",
        "resource_id": rid,
        "link": {"method": pick["method"], "via": {"activity_id": pick["activity_id"]},
                 "confidence": "low"},
        "category": "USER_PROFILE",
        "state": state,
        "state_basis": "l1_guess",
        "state_source": None,
        "recreated_by": None,
        "citation": {"rule": "O" if rule_o else None, "pick": rule_o[0]["file_id"] if rule_o else None,
                     "rule_e": [], "rule_o": rule_o},
    })
    d["activities_used"] = sorted(set(d["activities_used"]) | {pick["activity_id"]})
    assert_clock(conn, d)
    return d


# ---- final check ----------------------------------------------------------------------------

def assert_clock(conn, d):
    """Raise unless every record the dossier used is at or before audit_at and every citation is
    a file whose manifest time is at or before audit_at (or a recreation activity)."""
    limit = _t(d["audit_at"])

    def record_at(oid):
        row = conn.execute("select coalesce(o.record_at, f.manifest_at) from observation o "
                           "left join evidence_file f on f.file_id = o.file_id "
                           "where o.obs_id = ?", (oid,)).fetchone()
        return row[0] if row else None

    def event_time(aid):
        row = conn.execute("select event_time from activity where activity_id = ?",
                           (aid,)).fetchone()
        return row[0] if row else None

    def manifest(fid):
        row = conn.execute("select manifest_at from evidence_file where file_id = ?",
                           (fid,)).fetchone()
        return row[0] if row else None

    def late(value):
        t = _t(value)
        return t is None or t > limit

    for oid in d["observations_used"]:
        if late(record_at(oid)):
            raise LateRecordRead(f"{d['case_id']}: observation {oid} is after audit_at")
    for aid in d["activities_used"]:
        if late(event_time(aid)):
            raise LateRecordRead(f"{d['case_id']}: activity {aid} is after audit_at")
    for r in d["resources"]:
        src = r["state_source"]
        if src and late(src["record_at"]):
            raise LateRecordRead(f"{d['case_id']}: state of {r['resource_id']} is after audit_at")
        c = r["citation"]
        files = [x["file_id"] for x in c["rule_e"] + c["rule_o"]]
        if c["rule"] in ("E", "O"):
            files.append(c["pick"])
        elif c["rule"] == "activity" and late(event_time(c["pick"])):
            raise LateCitation(f"{d['case_id']}: cites late activity {c['pick']}")
        for fid in files:
            if late(manifest(fid)):
                raise LateCitation(f"{d['case_id']}: cites {fid} captured after audit_at")
