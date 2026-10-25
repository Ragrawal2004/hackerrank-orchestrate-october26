import argparse
import csv
import sys
from pathlib import Path

from collections import Counter

from . import (adapters, config, diagnostics, dossier, envfile, identity, intent, linker, llm, loaders,
               policy, steps)
from .adapters import replica_sql

HEADER = ["case_id", "deletion_status", "steps_json"]


def build_parser():
    p = argparse.ArgumentParser(prog="main.py", description="Ghost Data deletion-audit agent")
    p.add_argument("--dataset", type=Path, help="dataset directory (default: <repo>/dataset)")
    p.add_argument("--out", type=Path, help="output CSV (default: <repo>/output.csv)")
    p.add_argument("--cases", help="comma-separated case_ids to run (default: all)")
    p.add_argument("--link-fallback", choices=config.LINK_FALLBACK_VALUES,
                   help="account linking when evidence links no account: omit (default) emits no "
                        "guessed step; guess adds low-confidence VERIFY_RECORD_ABSENT steps (M6). "
                        "Overrides the LINK_FALLBACK env var")
    p.add_argument("--category-llm", choices=config.CATEGORY_LLM_VALUES)
    p.add_argument("--no-llm", action="store_true", help="never call an LLM")
    p.add_argument("--cold", action="store_true", help="ignore the LLM response cache")
    return p


def write_output(path, rows):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f, lineterminator="\n")
        w.writerow(HEADER)
        w.writerows(rows)


def _log_events(runs_dir, case_id, events):
    for ev in events:
        fields = {k: v for k, v in ev.items() if k != "type"}
        diagnostics.log_event(runs_dir, case_id, ev["type"], **fields)


def audit_case(conn, rules, case, runs_dir, d=None, pick=None, link_events=(), tally=None):
    """(status, steps_json, retained_missing, no_steps) for one deletion case; logs its events.

    d: the case's dossier (built here when None; None from the caller's pool means no identity).
    pick / link_events: the account linker's low-confidence pick and the linkers' events."""
    cid = case["case_id"]
    try:
        d = d or dossier.build_dossier(conn, case)
    except identity.UnknownIdentity as e:
        diagnostics.log_event(runs_dir, cid, "no_identity", email=str(e))
        diagnostics.log_event(runs_dir, cid, "no_steps", reason="no_identity")
        return "INCOMPLETE", steps.serialize_steps([]), [], True
    d = dossier.add_guessed_account(conn, d, pick)
    plan = policy.evaluate(d, rules)
    clock_events = replica_sql.clock_choice_events(conn, case)
    for events in (clock_events, d["events"], link_events, plan["events"]):
        _log_events(runs_dir, cid, events)
        if tally is not None:
            tally.update(ev["type"] for ev in events)
    diagnostics.log_event(
        runs_dir, cid, "dossier", user_id=d["user_id"], resolved_via=d["resolved_via"],
        anchor_candidates=d["anchor_candidates"], account_linked=d["account_linked"],
        clock=d["clock"],
        resources=[{"system_id": e["system_id"], "resource_id": e["resource_id"],
                    "category": e["category"], "basis": e["category_basis"],
                    "required": e["required"], "observed": e["observed"],
                    "verdict": e["verdict"], "finding": e["finding"],
                    "confidence": e["confidence"]} for e in plan["entries"]])
    no_steps = any(ev["type"] == "no_steps" for ev in plan["events"])
    return plan["status"], steps.serialize_steps(plan["steps"]), plan["retained_missing"], no_steps


def format_linker_summary(mode, tally):
    return (f"linker: mode={mode} picks={tally['account_guess']} "
            f"unresolved={tally['link_unresolved']} uncited={tally['guessed_account_uncited']} "
            f"contradicted={tally['guessed_account_contradicted']} "
            f"analytics_withheld={tally['analytics_candidate']} "
            f"analytics_unresolved={tally['analytics_unresolved']}")


def build_pool(conn, requests):
    """{case_id: dossier or None} for every request in the file; None when the email is unknown."""
    out = {}
    for case in requests:
        try:
            out[case["case_id"]] = dossier.build_dossier(conn, case)
        except identity.UnknownIdentity:
            out[case["case_id"]] = None
    return out


def format_policy_summary(statuses, no_steps, retained_missing):
    cases = sorted(retained_missing, key=loaders.case_sort_key)
    records = sum(len(v) for v in retained_missing.values())
    return "\n".join([
        "Status: " + " ".join(f"{k}={statuses.get(k, 0)}"
                              for k in ("COMPLETE", "INCOMPLETE", "NOT_APPLICABLE")),
        f"no_steps: {no_steps} cases (INCOMPLETE with [])",
        f"retained_missing: {len(cases)} cases / {records} records"
        + (" (" + ", ".join(cases) + ")" if cases else "")])


def main(argv=None):
    args = build_parser().parse_args(argv)
    envfile.load_dotenv(envfile.env_file_path(config.find_repo_root()))  # before build_config reads the env
    try:
        cfg = config.build_config(args.link_fallback, args.category_llm, args.no_llm, args.cold)
    except ValueError as e:
        print(f"error: {e}", file=sys.stderr)
        return 2
    llm.configure(runs_dir=cfg.runs_dir, cold=cfg.cold)
    dataset = args.dataset or cfg.dataset_dir
    out = args.out or cfg.default_output
    requests = loaders.load_requests(dataset)
    everyone = sorted(requests, key=lambda r: loaders.case_sort_key(r["case_id"]))  # the linker pool
    if args.cases:
        wanted = {c.strip() for c in args.cases.split(",") if c.strip()}
        requests = [r for r in requests if r["case_id"] in wanted]
    requests.sort(key=lambda r: loaders.case_sort_key(r["case_id"]))
    (cfg.runs_dir / diagnostics.FILE_NAME).unlink(missing_ok=True)  # diagnostics are per run
    results = intent.classify_all(requests, cfg.intent_model, not cfg.no_llm, cfg.runs_dir)
    non_deletion = {r["case_id"] for r in results if r["label"] == "NON_DELETION"}
    conn = loaders.load_store(dataset)
    rules = policy.load_rules(conn)
    rows, statuses, retained_missing, no_steps, tally = [], Counter(), {}, 0, Counter()
    dossiers = build_pool(conn, everyone)
    pool = [(c, dossiers[c["case_id"]]) for c in everyone if dossiers[c["case_id"]] is not None]
    targets = {r["case_id"] for r in requests} - non_deletion
    accounts = linker.link_accounts(conn, pool, cfg.link_fallback, targets)
    analytics = linker.link_analytics(conn, pool, targets)
    for case in requests:
        cid = case["case_id"]
        if cid in non_deletion:
            status, steps_json = "NOT_APPLICABLE", steps.serialize_steps([])
        else:
            link = accounts.get(cid) or {"pick": None, "events": []}
            status, steps_json, missing, empty = audit_case(
                conn, rules, case, cfg.runs_dir, dossiers[cid], link["pick"],
                link["events"] + analytics.get(cid, []), tally)
            no_steps += empty
            if missing:
                retained_missing[cid] = missing
                diagnostics.log_event(cfg.runs_dir, cid, "retained_missing_case", records=missing)
        statuses[status] += 1
        rows.append([cid, status, steps_json])
    write_output(out, rows)
    print(intent.format_report(intent.report(results)))
    summ = adapters.summary(conn)
    unclaimed = conn.execute("select count(*) from evidence_file where format is null").fetchone()[0]
    clock = replica_sql.count_clock_choice(conn, [c for c in requests if c["case_id"] not in non_deletion])
    print(adapters.format_summary(summ, clock, unclaimed))
    print(format_policy_summary(statuses, no_steps, retained_missing))
    print(format_linker_summary(cfg.link_fallback, tally))
    print(f"wrote {len(requests)} rows to {out}")
    return 0
