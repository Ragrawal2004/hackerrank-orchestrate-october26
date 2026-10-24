import copy
import json
import tempfile
import unittest
from pathlib import Path

from ghost import invariants as inv
from ghost import steps
from tests.helpers import DATASET

HEADER = ["case_id", "deletion_status", "steps_json"]


def S(phase, op, system, resource, evidence=""):
    return steps.make_step(phase, system, resource, op, evidence)


def row(case_id, status, step_list, raw=None):
    numbered = steps.order_and_number(step_list)
    return {"case_id": case_id, "deletion_status": status,
            "steps_json": raw if raw is not None else steps.serialize_steps(numbered)}


def edit(r, fn):
    """Return a copy of row r whose parsed steps were changed by fn(list), re-dumped compact."""
    out = dict(r)
    lst = json.loads(r["steps_json"])
    fn(lst)
    out["steps_json"] = json.dumps(lst, ensure_ascii=False, separators=(",", ":"))
    return out


def good_na(cid="case_1"):
    return row(cid, "NOT_APPLICABLE", [])


def good_complete(cid="case_2"):
    return row(cid, "COMPLETE", [
        S("VERIFICATION", "VERIFY_RECORD_ABSENT", "account_service", "account_1", "evidence_01"),
        S("VERIFICATION", "VERIFY_RECORD_PRESENT", "security_service", "audit_record_1", "evidence_01"),
    ])


def good_incomplete(cid="case_3"):
    return row(cid, "INCOMPLETE", [
        S("FINDING", "REPORT_REMAINING_RECORD", "search_service", "search_1", "evidence_01"),
        S("FINDING", "REPORT_ACTIVE_DATA_FLOW", "integration_service", "data_flow_1", "evidence_01"),
        S("FINDING", "REPORT_EXPOSED_PII", "support_service", "support_1", "evidence_01"),
        S("FINDING", "REPORT_RESTORE_RISK", "backup_service", "backup_1", "evidence_01"),
        S("REMEDIATION", "DISABLE_DATA_FLOW", "integration_service", "data_flow_1"),
        S("REMEDIATION", "BLOCK_RESTORE", "backup_service", "backup_1"),
        S("REMEDIATION", "DELETE_RECORD", "search_service", "search_1"),
        S("REMEDIATION", "REDACT_RECORD", "support_service", "support_1"),
        S("VERIFICATION", "VERIFY_RECORD_ABSENT", "account_service", "account_1"),
        S("VERIFICATION", "VERIFY_RECORD_ABSENT", "search_service", "search_1"),
        S("VERIFICATION", "VERIFY_RECORD_PRESENT", "backup_service", "backup_1"),
        S("VERIFICATION", "VERIFY_RECORD_PRESENT", "security_service", "audit_record_1"),
        S("VERIFICATION", "VERIFY_RECORD_REDACTED", "support_service", "support_1"),
        S("VERIFICATION", "VERIFY_DATA_FLOW_DISABLED", "integration_service", "data_flow_1"),
        S("VERIFICATION", "VERIFY_RESTORE_BLOCKED", "backup_service", "backup_1"),
    ])


def good_rows():
    return [good_na("case_1"), good_complete("case_2"), good_incomplete("case_3")]


def run(rows, header=None, **kw):
    return inv.check_table(HEADER if header is None else header, rows, **kw)


def ids(result):
    return sorted({v.invariant for v in result.violations})


def cases(result, invariant):
    return sorted({v.case_id for v in result.violations if v.invariant == invariant})


class CleanFixtureTests(unittest.TestCase):
    def test_clean_rows_pass(self):
        r = run(good_rows())
        self.assertEqual(r.violations, [], r.violations)

    def test_clean_rows_pass_with_expected_ids(self):
        r = run(good_rows(), expected_case_ids=["case_1", "case_2", "case_3"])
        self.assertEqual(r.violations, [])

    def test_gold_style_alphabetical_key_order_is_accepted(self):
        def alpha(lst):
            lst[:] = [{k: s[k] for k in sorted(s)} for s in lst]
        rows = [edit(r, alpha) for r in good_rows()]
        self.assertEqual(run(rows).violations, [])

    def test_unicode_escapes_are_accepted(self):
        def sym(lst):
            lst[0]["resource_id"] = "account_é"
        r = edit(good_complete("case_2"), sym)
        r["steps_json"] = json.dumps(json.loads(r["steps_json"]), separators=(",", ":"))  # ascii-escaped
        self.assertEqual(run([good_na(), r]).violations, [])

    def test_result_lists_the_deferred_checks(self):
        r = run(good_rows())
        self.assertTrue(any("determinism" in n for n in r.not_checked))
        self.assertTrue(any("retained" in n for n in r.not_checked))

    def test_every_invariant_id_has_a_title(self):
        for i in ("S1", "S2", "S3", "S4", "S5", "S6", "S7", "S8", "S9",
                  "P1", "P2", "P3", "P4", "P5", "E1"):
            self.assertIn(i, inv.TITLES)


class ShapeTests(unittest.TestCase):
    def test_s1_header_wrong_name(self):
        r = run(good_rows(), header=["case_id", "status", "steps_json"])
        self.assertEqual(ids(r), ["S1"])

    def test_s1_header_wrong_order(self):
        r = run(good_rows(), header=["deletion_status", "case_id", "steps_json"])
        self.assertEqual(ids(r), ["S1"])

    def test_s1_header_extra_column(self):
        r = run(good_rows(), header=HEADER + ["x"])
        self.assertEqual(ids(r), ["S1"])

    def test_s2_duplicate_case_id(self):
        rows = good_rows() + [good_na("case_3")]
        r = run(rows)
        self.assertIn("S2", ids(r))
        self.assertEqual(cases(r, "S2"), ["case_3"])

    def test_s2_not_in_numeric_order(self):
        rows = [good_na("case_100"), good_na("case_99")]
        r = run(rows)
        self.assertEqual(ids(r), ["S2"])

    def test_s2_lexicographic_order_is_wrong(self):
        rows = [good_na("case_100"), good_na("case_33"), good_na("case_99")]
        self.assertIn("S2", ids(run(rows)))

    def test_s2_numeric_order_is_right(self):
        rows = [good_na("case_33"), good_na("case_99"), good_na("case_100")]
        self.assertEqual(run(rows).violations, [])

    def test_s2_missing_expected_case(self):
        r = run(good_rows(), expected_case_ids=["case_1", "case_2", "case_3", "case_4"])
        self.assertEqual(ids(r), ["S2"])
        self.assertEqual(cases(r, "S2"), ["case_4"])

    def test_s2_unexpected_case(self):
        r = run(good_rows(), expected_case_ids=["case_1", "case_2"])
        self.assertEqual(cases(r, "S2"), ["case_3"])

    def test_s2_malformed_case_id(self):
        r = run([good_na("case_x")])
        self.assertIn("S2", ids(r))

    def test_s3_unknown_status(self):
        bad = dict(good_na(), deletion_status="DONE")
        self.assertEqual(ids(run([bad])), ["S3"])

    def test_s3_lowercase_status(self):
        bad = dict(good_na(), deletion_status="complete")
        self.assertIn("S3", ids(run([bad])))

    def test_s4_invalid_json(self):
        bad = dict(good_na(), steps_json="[")
        self.assertEqual(ids(run([bad])), ["S4"])

    def test_s4_not_a_list(self):
        bad = dict(good_na(), steps_json="{}")
        self.assertEqual(ids(run([bad])), ["S4"])

    def test_s4_empty_cell(self):
        bad = dict(good_na(), steps_json="")
        self.assertEqual(ids(run([bad])), ["S4"])

    def test_s4_step_not_an_object(self):
        self.assertEqual(ids(run([dict(good_na(), steps_json="[1]")])), ["S4"])

    def test_s4_extra_key(self):
        r = edit(good_complete(), lambda l: l[0].update(extra="x"))
        self.assertIn("S4", ids(run([good_na(), r])))

    def test_s4_missing_key(self):
        r = edit(good_complete(), lambda l: l[0].pop("state"))
        self.assertIn("S4", ids(run([good_na(), r])))

    def test_s4_null_evidence(self):
        r = edit(good_complete(), lambda l: l[0].update(evidence_id=None))
        self.assertIn("S4", ids(run([good_na(), r])))

    def test_s4_step_no_is_a_string(self):
        r = edit(good_complete(), lambda l: l[0].update(step_no="1"))
        self.assertIn("S4", ids(run([good_na(), r])))

    def test_s4_step_no_is_a_bool(self):
        r = edit(good_complete(), lambda l: l[0].update(step_no=True))
        self.assertIn("S4", ids(run([good_na(), r])))

    def test_s4_field_not_a_string(self):
        r = edit(good_complete(), lambda l: l[0].update(resource_id=5))
        self.assertIn("S4", ids(run([good_na(), r])))

    def test_s4_malformed_case_skips_dependent_checks(self):
        r = run([dict(good_incomplete(), steps_json="[")])
        self.assertEqual(ids(r), ["S4"])

    def test_s5_whitespace_after_separators(self):
        raw = json.dumps(json.loads(good_complete()["steps_json"]))  # default ", " ": "
        r = run([good_na(), dict(good_complete(), steps_json=raw)])
        self.assertEqual(ids(r), ["S5"])
        self.assertEqual(cases(r, "S5"), ["case_2"])

    def test_s5_pretty_printed(self):
        raw = json.dumps(json.loads(good_complete()["steps_json"]), indent=2)
        self.assertEqual(ids(run([dict(good_complete(), steps_json=raw)])), ["S5"])

    def test_s5_leading_space(self):
        raw = " " + good_complete()["steps_json"]
        self.assertEqual(ids(run([dict(good_complete(), steps_json=raw)])), ["S5"])

    def test_s5_empty_list_with_space(self):
        self.assertEqual(ids(run([dict(good_na(), steps_json="[ ]")])), ["S5"])

    def test_s6_step_no_starts_at_zero(self):
        r = edit(good_complete(), lambda l: [s.update(step_no=s["step_no"] - 1) for s in l])
        self.assertEqual(ids(run([good_na(), r])), ["S6"])

    def test_s6_gap(self):
        r = edit(good_complete(), lambda l: l[1].update(step_no=3))
        self.assertEqual(ids(run([good_na(), r])), ["S6"])

    def test_s6_duplicate(self):
        r = edit(good_complete(), lambda l: l[1].update(step_no=1))
        self.assertEqual(ids(run([good_na(), r])), ["S6"])

    def test_s7_unknown_operation(self):
        r = edit(good_complete(), lambda l: l[0].update(operation="VERIFY_RECORD_GONE"))
        self.assertEqual(ids(run([good_na(), r])), ["S7"])

    def test_s7_unknown_phase(self):
        r = edit(good_complete(), lambda l: l[0].update(phase="CHECK"))
        self.assertIn("S7", ids(run([good_na(), r])))

    def test_s7_wrong_state_for_operation(self):
        r = edit(good_complete(), lambda l: l[0].update(state="PRESENT"))
        self.assertEqual(ids(run([good_na(), r])), ["S7"])

    def test_s7_wrong_phase_for_operation(self):
        r = edit(good_incomplete(),
                 lambda l: [s.update(phase="VERIFICATION") for s in l if s["operation"] == "DELETE_RECORD"])
        self.assertIn("S7", ids(run([r])))

    def test_s8_two_steps_swapped(self):
        def swap(l):
            l[0], l[1] = l[1], l[0]
            l[0]["step_no"], l[1]["step_no"] = 1, 2
        r = edit(good_complete(), swap)
        self.assertEqual(ids(run([good_na(), r])), ["S8"])

    def test_s8_phase_order_violated(self):
        def move_first_to_end(l):
            l.append(l.pop(0))
            for i, s in enumerate(l, 1):
                s["step_no"] = i
        r = edit(good_incomplete(), move_first_to_end)
        self.assertIn("S8", ids(run([r])))

    def test_s8_resource_string_order_is_enforced(self):
        base = row("case_5", "INCOMPLETE", [
            S("FINDING", "REPORT_REMAINING_RECORD", "search_service", "search_1308", "evidence_01"),
            S("FINDING", "REPORT_REMAINING_RECORD", "search_service", "search_4218", "evidence_01"),
            S("REMEDIATION", "DELETE_RECORD", "search_service", "search_1308"),
            S("REMEDIATION", "DELETE_RECORD", "search_service", "search_4218"),
            S("VERIFICATION", "VERIFY_RECORD_ABSENT", "search_service", "search_1308"),
            S("VERIFICATION", "VERIFY_RECORD_ABSENT", "search_service", "search_4218"),
        ])
        self.assertEqual(run([base]).violations, [])

        def swap_findings(l):
            l[0], l[1] = l[1], l[0]
            l[0]["step_no"], l[1]["step_no"] = 1, 2
        self.assertEqual(ids(run([edit(base, swap_findings)])), ["S8"])

    def test_s8_numeric_resource_order_is_not_accepted(self):
        base = row("case_5", "INCOMPLETE", [
            S("FINDING", "REPORT_REMAINING_RECORD", "s", "r_10", "evidence_01"),
            S("FINDING", "REPORT_REMAINING_RECORD", "s", "r_9", "evidence_01"),
            S("REMEDIATION", "DELETE_RECORD", "s", "r_10"),
            S("REMEDIATION", "DELETE_RECORD", "s", "r_9"),
            S("VERIFICATION", "VERIFY_RECORD_ABSENT", "s", "r_10"),
            S("VERIFICATION", "VERIFY_RECORD_ABSENT", "s", "r_9"),
        ])

        def numeric_order(l):
            for i in range(0, len(l), 2):
                l[i], l[i + 1] = l[i + 1], l[i]
            for i, s in enumerate(l, 1):
                s["step_no"] = i
        self.assertIn("S8", ids(run([edit(base, numeric_order)])))

    def test_s9_duplicate_step(self):
        def dup(l):
            l.append(dict(l[-1], step_no=len(l) + 1))
        r = edit(good_complete(), dup)
        self.assertEqual(ids(run([good_na(), r])), ["S9"])


class StatusTests(unittest.TestCase):
    def test_p1_not_applicable_with_steps(self):
        bad = dict(good_complete("case_2"), deletion_status="NOT_APPLICABLE")
        r = run([good_na(), bad])
        self.assertIn("P1", ids(r))
        self.assertEqual(cases(r, "P1"), ["case_2"])

    def test_p1_complete_with_empty_steps(self):
        r = run([row("case_2", "COMPLETE", [])])
        self.assertEqual(ids(r), ["P1"])

    def test_p1_incomplete_with_empty_steps(self):
        r = run([row("case_3", "INCOMPLETE", [])])
        self.assertIn("P1", ids(r))
        self.assertIn("P3", ids(r))

    def test_allow_stub_skips_only_empty_deletion_rows(self):
        stub = [row("case_2", "INCOMPLETE", []), good_na("case_3")]
        r = run(stub, allow_stub=True)
        self.assertEqual(r.violations, [])
        self.assertEqual(sorted(set(r.skipped) - {"E1"}), ["P1", "P3"])

    def test_allow_stub_is_off_by_default(self):
        self.assertNotEqual(run([row("case_2", "INCOMPLETE", [])]).violations, [])

    def test_allow_stub_does_not_hide_non_empty_problems(self):
        bad = dict(good_incomplete("case_3"), deletion_status="NOT_APPLICABLE")
        self.assertIn("P1", ids(run([bad], allow_stub=True)))
        no_finding = row("case_3", "INCOMPLETE", [
            S("VERIFICATION", "VERIFY_RECORD_ABSENT", "account_service", "account_1")])
        self.assertIn("P3", ids(run([no_finding], allow_stub=True)))

    def test_p2_complete_with_a_finding(self):
        bad = row("case_2", "COMPLETE", [
            S("FINDING", "REPORT_REMAINING_RECORD", "search_service", "search_1", "evidence_01"),
            S("VERIFICATION", "VERIFY_RECORD_ABSENT", "search_service", "search_1", "evidence_01")])
        r = run([bad])
        self.assertEqual(ids(r), ["P2"])

    def test_p2_complete_with_a_remediation(self):
        bad = row("case_2", "COMPLETE", [
            S("REMEDIATION", "DELETE_RECORD", "search_service", "search_1"),
            S("VERIFICATION", "VERIFY_RECORD_ABSENT", "search_service", "search_1", "evidence_01")])
        self.assertEqual(ids(run([bad])), ["P2"])

    def test_p2_uncited_verification(self):
        r = edit(good_complete(), lambda l: l[1].update(evidence_id=""))
        res = run([good_na(), r])
        self.assertEqual(ids(res), ["P2"])
        self.assertEqual(cases(res, "P2"), ["case_2"])

    def test_p2_uncited_account_without_a_logged_guess_is_a_violation(self):
        r = edit(good_complete(), lambda l: l[0].update(evidence_id=""))
        self.assertEqual(ids(run([good_na(), r])), ["P2"])

    def test_p2_uncited_account_with_a_logged_guess_is_allowed_and_counted(self):
        r = edit(good_complete(), lambda l: l[0].update(evidence_id=""))
        res = run([good_na(), r], guessed_uncited={("case_2", "account_1")})
        self.assertEqual(res.violations, [])
        self.assertEqual(res.stats["guessed_account_uncited"], 1)

    def test_p2_guess_for_another_case_does_not_help(self):
        r = edit(good_complete(), lambda l: l[0].update(evidence_id=""))
        res = run([good_na(), r], guessed_uncited={("case_9", "account_1")})
        self.assertEqual(ids(res), ["P2"])

    def test_p2_guess_for_another_resource_does_not_help(self):
        r = edit(good_complete(), lambda l: l[0].update(evidence_id=""))
        res = run([good_na(), r], guessed_uncited={("case_2", "account_2")})
        self.assertEqual(ids(res), ["P2"])

    def test_p2_exception_is_only_for_the_account_verify_absent_step(self):
        r = edit(good_complete(), lambda l: l[1].update(evidence_id=""))  # audit record, not account
        res = run([good_na(), r], guessed_uncited={("case_2", "audit_record_1")})
        self.assertEqual(ids(res), ["P2"])

    def test_p2_exception_does_not_apply_to_a_non_account_system(self):
        bad = row("case_2", "COMPLETE", [
            S("VERIFICATION", "VERIFY_RECORD_ABSENT", "search_service", "search_1", "")])
        res = run([bad], guessed_uncited={("case_2", "search_1")})
        self.assertEqual(ids(res), ["P2"])

    def test_p2_unused_guess_is_not_counted(self):
        res = run(good_rows(), guessed_uncited={("case_2", "account_1")})
        self.assertEqual(res.stats["guessed_account_uncited"], 0)

    def test_p3_incomplete_without_findings(self):
        bad = row("case_3", "INCOMPLETE", [
            S("VERIFICATION", "VERIFY_RECORD_ABSENT", "account_service", "account_1")])
        self.assertEqual(ids(run([bad])), ["P3"])

    def test_p3_uncited_finding(self):
        r = edit(good_incomplete(), lambda l: l[0].update(evidence_id=""))
        self.assertEqual(ids(run([r])), ["P3"])

    def test_p3_remediation_with_evidence(self):
        def f(l):
            for s in l:
                if s["phase"] == "REMEDIATION":
                    s["evidence_id"] = "evidence_01"
                    break
        self.assertEqual(ids(run([edit(good_incomplete(), f)])), ["P3"])

    def test_p3_verification_with_evidence(self):
        def f(l):
            for s in l:
                if s["phase"] == "VERIFICATION":
                    s["evidence_id"] = "evidence_01"
                    break
        self.assertEqual(ids(run([edit(good_incomplete(), f)])), ["P3"])

    def test_p4_finding_without_remediation(self):
        r = edit(good_incomplete(), lambda l: l.__setitem__(
            slice(None), [s for s in l if s["operation"] != "DELETE_RECORD"]))
        self.assertIn("P4", ids(run([self.renumber(r)])))

    def test_p4_finding_without_verification(self):
        r = edit(good_incomplete(), lambda l: l.__setitem__(
            slice(None), [s for s in l if not (s["operation"] == "VERIFY_RECORD_ABSENT"
                                               and s["resource_id"] == "search_1")]))
        self.assertIn("P4", ids(run([self.renumber(r)])))

    def test_p4_remediation_without_finding(self):
        r = edit(good_incomplete(), lambda l: l.__setitem__(
            slice(None), [s for s in l if s["operation"] != "REPORT_ACTIVE_DATA_FLOW"]))
        res = run([self.renumber(r)])
        self.assertIn("P4", ids(res))

    def test_p4_pii_redact_must_pair_with_redacted_verification(self):
        def f(l):
            for s in l:
                if s["operation"] == "VERIFY_RECORD_REDACTED":
                    s["operation"], s["state"] = "VERIFY_RECORD_ANONYMIZED", "ANONYMIZED"
        self.assertIn("P4", ids(run([edit(good_incomplete(), f)])))

    def test_p4_pii_anonymize_pairs_with_anonymized_verification(self):
        def f(l):
            for s in l:
                if s["operation"] == "REDACT_RECORD":
                    s["operation"], s["state"] = "ANONYMIZE_RECORD", "ANONYMIZED"
                if s["operation"] == "VERIFY_RECORD_REDACTED":
                    s["operation"], s["state"] = "VERIFY_RECORD_ANONYMIZED", "ANONYMIZED"
        self.assertEqual(run([edit(good_incomplete(), f)]).violations, [])

    def test_p4_wrong_remediation_for_the_finding(self):
        def f(l):
            for s in l:
                if s["operation"] == "DELETE_RECORD":
                    s["operation"], s["state"] = "REDACT_RECORD", "REDACTED"
        self.assertIn("P4", ids(run([edit(good_incomplete(), f)])))

    def test_p4_remediation_on_a_different_resource(self):
        def f(l):
            for s in l:
                if s["operation"] == "DELETE_RECORD":
                    s["resource_id"] = "search_2"
        self.assertIn("P4", ids(run([edit(good_incomplete(), f)])))

    def test_p4_extra_verification_without_remediation_is_allowed(self):
        # the full end state is verified, including resources that were already compliant
        self.assertEqual(run([good_incomplete()]).violations, [])

    def test_p5_delete_and_redact_the_same_resource(self):
        def f(l):
            l.append(dict(l[0], phase="REMEDIATION", operation="REDACT_RECORD", state="REDACTED",
                          system_id="search_service", resource_id="search_1", evidence_id="", step_no=0))
        res = run([self.renumber(edit(good_incomplete(), f), sort=True)])
        self.assertIn("P5", ids(res))

    def test_p5_verify_absent_and_present_the_same_resource(self):
        def f(l):
            l.append(dict(l[-1], operation="VERIFY_RECORD_PRESENT", state="PRESENT",
                          system_id="search_service", resource_id="search_1", step_no=0))
        res = run([self.renumber(edit(good_incomplete(), f), sort=True)])
        self.assertIn("P5", ids(res))

    def test_p5_present_and_blocked_on_a_backup_is_fine(self):
        self.assertEqual(run([good_incomplete()]).violations, [])  # backup_1: PRESENT + BLOCKED

    def test_p5_same_resource_in_different_systems_is_fine(self):
        r = row("case_2", "COMPLETE", [
            S("VERIFICATION", "VERIFY_RECORD_ABSENT", "a_service", "x_1", "evidence_01"),
            S("VERIFICATION", "VERIFY_RECORD_PRESENT", "b_service", "x_1", "evidence_01")])
        self.assertEqual(run([r]).violations, [])

    def test_p5_complete_case_conflict(self):
        r = row("case_2", "COMPLETE", [
            S("VERIFICATION", "VERIFY_RECORD_ABSENT", "s", "x_1", "evidence_01"),
            S("VERIFICATION", "VERIFY_RECORD_REDACTED", "s", "x_1", "evidence_01")])
        self.assertEqual(ids(run([r])), ["P5"])

    @staticmethod
    def renumber(r, sort=False):
        lst = json.loads(r["steps_json"])
        if sort:
            lst = steps.order_and_number([{k: s[k] for k in steps.STEP_KEYS[1:]} for s in lst])
        else:
            for i, s in enumerate(lst, 1):
                s["step_no"] = i
        return dict(r, steps_json=json.dumps(lst, ensure_ascii=False, separators=(",", ":")))


class EvidenceTimeTests(unittest.TestCase):
    TIMES = {
        "evidence_01": "2026-09-01T10:00:00Z",
        "evidence_02": "2026-09-17T10:00:00Z",
        "activity_5": "2026-09-01T09:00:00Z",
        "activity_6": "2026-09-20T09:00:00Z",
        "audit_7": "2026-08-01T00:00:00Z",
        "audit_8": "2026-09-30T00:00:00Z",
    }
    AUDIT_AT = {"case_1": "2026-09-15T09:00:00Z", "case_2": "2026-09-15T09:00:00Z",
                "case_3": "2026-09-15T09:00:00Z"}

    def run_e1(self, rows):
        return run(rows, evidence_times=self.TIMES, audit_at=self.AUDIT_AT)

    def test_clean(self):
        self.assertEqual(self.run_e1(good_rows()).violations, [])

    def test_late_file(self):
        r = edit(good_complete(), lambda l: l[0].update(evidence_id="evidence_02"))
        res = self.run_e1([good_na(), r])
        self.assertEqual(ids(res), ["E1"])
        self.assertEqual(cases(res, "E1"), ["case_2"])

    def test_boundary_is_inclusive(self):
        times = dict(self.TIMES, evidence_01="2026-09-15T09:00:00Z")
        res = run(good_rows(), evidence_times=times, audit_at=self.AUDIT_AT)
        self.assertEqual(res.violations, [])

    def test_unknown_id(self):
        r = edit(good_complete(), lambda l: l[0].update(evidence_id="evidence_99"))
        self.assertEqual(ids(self.run_e1([good_na(), r])), ["E1"])

    def test_activity_and_audit_ids_are_allowed_when_early_enough(self):
        r = edit(good_complete(), lambda l: (l[0].update(evidence_id="activity_5"),
                                             l[1].update(evidence_id="audit_7")))
        self.assertEqual(self.run_e1([good_na(), r]).violations, [])

    def test_late_activity(self):
        r = edit(good_complete(), lambda l: l[0].update(evidence_id="activity_6"))
        self.assertEqual(ids(self.run_e1([good_na(), r])), ["E1"])

    def test_late_audit(self):
        r = edit(good_complete(), lambda l: l[0].update(evidence_id="audit_8"))
        self.assertEqual(ids(self.run_e1([good_na(), r])), ["E1"])

    def test_late_finding_citation_in_incomplete(self):
        r = edit(good_incomplete(), lambda l: l[0].update(evidence_id="evidence_02"))
        self.assertEqual(ids(self.run_e1([r])), ["E1"])

    def test_empty_evidence_is_never_checked(self):
        self.assertEqual(self.run_e1([good_incomplete()]).violations, [])

    def test_case_without_an_audit_at_is_reported(self):
        res = run([good_complete("case_7")], evidence_times=self.TIMES, audit_at=self.AUDIT_AT)
        self.assertEqual(ids(res), ["E1"])

    def test_skipped_with_a_notice_when_no_dataset(self):
        r = edit(good_complete(), lambda l: l[0].update(evidence_id="evidence_99"))
        res = run([good_na(), r])
        self.assertEqual(res.violations, [])
        self.assertIn("E1", res.skipped)


class LoadersTests(unittest.TestCase):
    def test_load_evidence_times_and_audit_at_from_a_dataset_dir(self):
        with tempfile.TemporaryDirectory() as d:
            d = Path(d)
            (d / "evidence_files.csv").write_text(
                "file_id,file_path,origin,captured_at\nevidence_01,e/a.log,x,2026-09-01T10:00:00Z\n")
            (d / "system_activity.csv").write_text(
                "activity_id,event_time,event_type,source_system,target_system,resource_id,"
                "reported_result,details\nactivity_5,2026-09-01T09:00:00Z,T,a,b,r,SUCCESS,d\n")
            (d / "user_audit_log.csv").write_text(
                "audit_id,user_id,event_time,field_name,old_value,new_value\n"
                "audit_7,user_1,2026-08-01T00:00:00Z,phone,1,2\n")
            (d / "deletion_requests.csv").write_text(
                "case_id,email,requested_at,reported_status,reported_at,audit_at,request_text\n"
                "case_1,a@b.c,t,COMPLETE,t,2026-09-15T09:00:00Z,x\n")
            times = inv.load_evidence_times(d)
            self.assertEqual(times, {"evidence_01": "2026-09-01T10:00:00Z",
                                     "activity_5": "2026-09-01T09:00:00Z",
                                     "audit_7": "2026-08-01T00:00:00Z"})
            self.assertEqual(inv.load_audit_at(d / "deletion_requests.csv"),
                             {"case_1": "2026-09-15T09:00:00Z"})

    def test_load_guessed_uncited_reads_only_that_event(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "diagnostics.jsonl"
            p.write_text(
                '{"case_id":"case_2","event":"guessed_account_uncited","resource_id":"account_1"}\n'
                '{"case_id":"case_3","event":"unciteable","resource_id":"search_1"}\n'
                '{"case_id":"case_4","event":"guessed_account_uncited","resource_id":"account_9"}\n')
            self.assertEqual(inv.load_guessed_uncited(p),
                             {("case_2", "account_1"), ("case_4", "account_9")})

    def test_load_guessed_uncited_missing_file_is_empty(self):
        self.assertEqual(inv.load_guessed_uncited(Path("/nonexistent/diag.jsonl")), set())

    def test_load_output_returns_header_and_rows(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "o.csv"
            p.write_text('case_id,deletion_status,steps_json\ncase_1,NOT_APPLICABLE,[]\n')
            header, rows = inv.load_output(p)
            self.assertEqual(header, HEADER)
            self.assertEqual(rows, [{"case_id": "case_1", "deletion_status": "NOT_APPLICABLE",
                                     "steps_json": "[]"}])

    def test_load_output_short_row_is_kept_visible(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "o.csv"
            p.write_text('case_id,deletion_status,steps_json\ncase_1,NOT_APPLICABLE\n')
            _, rows = inv.load_output(p)
            self.assertEqual(ids(run(rows)), ["S4"])


class ReportTests(unittest.TestCase):
    def test_report_lists_every_violated_invariant_with_its_case_ids(self):
        rows = [dict(good_na("case_1"), deletion_status="DONE"),
                dict(good_na("case_2"), steps_json="["),
                dict(good_na("case_3"), steps_json="["),
                good_na("case_4")]
        text = inv.format_report(run(rows), "x.csv")
        self.assertIn("FAIL S3", text)
        self.assertIn("FAIL S4", text)
        self.assertIn("case_2", text)
        self.assertIn("case_3", text)
        self.assertIn("PASS S1", text)
        self.assertIn("RESULT: FAIL", text)

    def test_report_passes(self):
        text = inv.format_report(run(good_rows()), "x.csv")
        self.assertIn("RESULT: PASS", text)
        self.assertNotIn("FAIL", text)

    def test_report_mentions_skips_and_deferred_checks(self):
        text = inv.format_report(run([row("case_2", "INCOMPLETE", [])], allow_stub=True), "x.csv")
        self.assertIn("SKIPPED", text)
        self.assertIn("NOT CHECKED YET", text)

    def test_report_prints_all_case_ids_not_a_truncated_list(self):
        rows = [dict(good_na(f"case_{n}"), deletion_status="DONE") for n in range(1, 40)]
        text = inv.format_report(run(rows), "x.csv")
        self.assertIn("case_39", text)


class RealDataTests(unittest.TestCase):
    def test_sample_gold_passes(self):
        header, rows = inv.load_output(DATASET / "sample_output.csv")
        expected = [r["case_id"] for r in inv.load_requests_rows(DATASET / "sample_deletion_requests.csv")]
        res = run(rows, header=header, expected_case_ids=expected,
                  evidence_times=inv.load_evidence_times(DATASET),
                  audit_at=inv.load_audit_at(DATASET / "sample_deletion_requests.csv"))
        self.assertEqual(res.violations, [], res.violations[:5])
        self.assertEqual(res.skipped, {})
        self.assertEqual(len(rows), 32)

    def test_sample_gold_is_not_vacuous_it_has_steps_in_every_phase(self):
        _, rows = inv.load_output(DATASET / "sample_output.csv")
        phases = {s["phase"] for r in rows for s in json.loads(r["steps_json"])}
        self.assertEqual(phases, set(steps.PHASES))

    def test_stub_output_passes_with_allow_stub_and_fails_without(self):
        from tests.test_cli_stub import run_cli
        with tempfile.TemporaryDirectory() as d:
            out = Path(d) / "stub.csv"
            p = run_cli("--no-llm", "--out", str(out))
            self.assertEqual(p.returncode, 0, p.stderr)
            header, rows = inv.load_output(out)
            expected = [r["case_id"] for r in inv.load_requests_rows(DATASET / "deletion_requests.csv")]
            kw = dict(header=header, expected_case_ids=expected,
                      evidence_times=inv.load_evidence_times(DATASET),
                      audit_at=inv.load_audit_at(DATASET / "deletion_requests.csv"))
            self.assertEqual(len(rows), 518)
            self.assertEqual(run(rows, allow_stub=True, **kw).violations, [])
            strict = run(rows, **kw)
            self.assertEqual(ids(strict), ["P1", "P3"])


class MutationHelperIsolationTests(unittest.TestCase):
    def test_fixtures_are_not_shared_between_tests(self):
        a, b = good_rows(), good_rows()
        a[0]["deletion_status"] = "X"
        self.assertEqual(b[0]["deletion_status"], "NOT_APPLICABLE")
        self.assertIsNot(copy.copy(a), b)


if __name__ == "__main__":
    unittest.main()
