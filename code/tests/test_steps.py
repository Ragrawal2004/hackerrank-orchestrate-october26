import json
import unittest

from ghost import steps

CANONICAL = [
    "REPORT_REMAINING_RECORD", "REPORT_EXPOSED_PII", "REPORT_ACTIVE_DATA_FLOW",
    "REPORT_RESTORE_RISK", "DISABLE_DATA_FLOW", "BLOCK_RESTORE", "DELETE_RECORD",
    "REDACT_RECORD", "ANONYMIZE_RECORD", "VERIFY_RECORD_ABSENT", "VERIFY_RECORD_PRESENT",
    "VERIFY_RECORD_REDACTED", "VERIFY_RECORD_ANONYMIZED", "VERIFY_DATA_FLOW_DISABLED",
    "VERIFY_RESTORE_BLOCKED",
]
STATES = {
    "REPORT_REMAINING_RECORD": "PRESENT", "REPORT_EXPOSED_PII": "PRESENT",
    "REPORT_ACTIVE_DATA_FLOW": "ACTIVE", "REPORT_RESTORE_RISK": "ACTIVE",
    "DISABLE_DATA_FLOW": "DISABLED", "BLOCK_RESTORE": "BLOCKED", "DELETE_RECORD": "ABSENT",
    "REDACT_RECORD": "REDACTED", "ANONYMIZE_RECORD": "ANONYMIZED",
    "VERIFY_RECORD_ABSENT": "ABSENT", "VERIFY_RECORD_PRESENT": "PRESENT",
    "VERIFY_RECORD_REDACTED": "REDACTED", "VERIFY_RECORD_ANONYMIZED": "ANONYMIZED",
    "VERIFY_DATA_FLOW_DISABLED": "DISABLED", "VERIFY_RESTORE_BLOCKED": "BLOCKED",
}
PHASE_OF = {**{o: "FINDING" for o in CANONICAL[:4]},
            **{o: "REMEDIATION" for o in CANONICAL[4:9]},
            **{o: "VERIFICATION" for o in CANONICAL[9:]}}


def mk(op, system="account_service", resource="account_001", evidence=""):
    return steps.make_step(PHASE_OF[op], system, resource, op, evidence)


class ConstantsTests(unittest.TestCase):
    def test_phases(self):
        self.assertEqual(list(steps.PHASES), ["FINDING", "REMEDIATION", "VERIFICATION"])

    def test_canonical_operation_order(self):
        self.assertEqual(list(steps.OPS), CANONICAL)

    def test_operation_state_table(self):
        self.assertEqual(dict(steps.OP_STATE), STATES)

    def test_operation_phase_table(self):
        self.assertEqual(dict(steps.OP_PHASE), PHASE_OF)

    def test_step_keys(self):
        self.assertEqual(list(steps.STEP_KEYS),
                         ["step_no", "phase", "system_id", "resource_id", "operation",
                          "state", "evidence_id"])

    def test_finding_templates_cover_every_finding_op(self):
        self.assertEqual(set(steps.FINDING_TEMPLATES), set(CANONICAL[:4]))

    def test_finding_templates_content(self):
        t = steps.FINDING_TEMPLATES
        self.assertEqual(t["REPORT_REMAINING_RECORD"],
                         (("DELETE_RECORD",), ("VERIFY_RECORD_ABSENT",)))
        self.assertEqual(t["REPORT_ACTIVE_DATA_FLOW"],
                         (("DISABLE_DATA_FLOW",), ("VERIFY_DATA_FLOW_DISABLED",)))
        self.assertEqual(t["REPORT_RESTORE_RISK"],
                         (("BLOCK_RESTORE",), ("VERIFY_RESTORE_BLOCKED",)))
        self.assertEqual(t["REPORT_EXPOSED_PII"],
                         (("REDACT_RECORD", "ANONYMIZE_RECORD"),
                          ("VERIFY_RECORD_REDACTED", "VERIFY_RECORD_ANONYMIZED")))

    def test_template_ops_are_in_the_right_phases(self):
        for finding, (rem, ver) in steps.FINDING_TEMPLATES.items():
            self.assertEqual(steps.OP_PHASE[finding], "FINDING")
            for o in rem:
                self.assertEqual(steps.OP_PHASE[o], "REMEDIATION")
            for o in ver:
                self.assertEqual(steps.OP_PHASE[o], "VERIFICATION")


class MakeStepTests(unittest.TestCase):
    def test_state_comes_from_the_operation(self):
        for op in CANONICAL:
            s = steps.make_step(PHASE_OF[op], "sys", "res_1", op)
            self.assertEqual(s["state"], STATES[op])

    def test_fields(self):
        s = steps.make_step("FINDING", "account_service", "account_005",
                            "REPORT_REMAINING_RECORD", "evidence_01")
        self.assertEqual(s, {"phase": "FINDING", "system_id": "account_service",
                             "resource_id": "account_005", "operation": "REPORT_REMAINING_RECORD",
                             "state": "PRESENT", "evidence_id": "evidence_01"})

    def test_evidence_defaults_to_empty_string(self):
        self.assertEqual(steps.make_step("REMEDIATION", "s", "r", "DELETE_RECORD")["evidence_id"], "")

    def test_unknown_operation_rejected(self):
        with self.assertRaises(ValueError):
            steps.make_step("FINDING", "s", "r", "REPORT_SOMETHING")

    def test_unknown_phase_rejected(self):
        with self.assertRaises(ValueError):
            steps.make_step("PLANNING", "s", "r", "DELETE_RECORD")

    def test_phase_must_match_operation(self):
        with self.assertRaises(ValueError):
            steps.make_step("VERIFICATION", "s", "r", "DELETE_RECORD")

    def test_none_evidence_rejected(self):
        with self.assertRaises(ValueError):
            steps.make_step("FINDING", "s", "r", "REPORT_REMAINING_RECORD", None)


class OrderingTests(unittest.TestCase):
    def ops(self, out):
        return [s["operation"] for s in out]

    def test_phase_beats_everything(self):
        shuffled = [mk("VERIFY_RECORD_ABSENT", "a", "a"), mk("DELETE_RECORD", "b", "b"),
                    mk("REPORT_REMAINING_RECORD", "z", "z", "e")]
        out = steps.order_and_number(shuffled)
        self.assertEqual(self.ops(out),
                         ["REPORT_REMAINING_RECORD", "DELETE_RECORD", "VERIFY_RECORD_ABSENT"])

    def test_canonical_operation_order_within_a_phase(self):
        shuffled = [mk(o, "s", "r") for o in reversed(CANONICAL)]
        out = steps.order_and_number(shuffled)
        self.assertEqual(self.ops(out), CANONICAL)

    def test_disable_and_block_come_before_record_changes(self):
        out = steps.order_and_number([mk("DELETE_RECORD", "a", "a"), mk("BLOCK_RESTORE", "z", "z"),
                                      mk("DISABLE_DATA_FLOW", "z", "y")])
        self.assertEqual(self.ops(out), ["DISABLE_DATA_FLOW", "BLOCK_RESTORE", "DELETE_RECORD"])

    def test_operation_beats_system_id(self):
        out = steps.order_and_number([mk("REDACT_RECORD", "a_sys", "r"), mk("DELETE_RECORD", "z_sys", "r")])
        self.assertEqual(self.ops(out), ["DELETE_RECORD", "REDACT_RECORD"])

    def test_system_id_then_resource_id(self):
        out = steps.order_and_number([
            mk("DELETE_RECORD", "search_service", "search_1"),
            mk("DELETE_RECORD", "account_service", "account_9"),
            mk("DELETE_RECORD", "account_service", "account_2"),
        ])
        self.assertEqual([(s["system_id"], s["resource_id"]) for s in out],
                         [("account_service", "account_2"), ("account_service", "account_9"),
                          ("search_service", "search_1")])

    def test_resource_id_is_plain_string_order_not_numeric(self):
        out = steps.order_and_number([mk("DELETE_RECORD", "search_service", "search_4218"),
                                      mk("DELETE_RECORD", "search_service", "search_1308"),
                                      mk("DELETE_RECORD", "search_service", "search_999")])
        self.assertEqual([s["resource_id"] for s in out],
                         ["search_1308", "search_4218", "search_999"])

    def test_account_before_search_before_audit_prefix_rule_is_plain_strings(self):
        out = steps.order_and_number([mk("VERIFY_RECORD_PRESENT", "security_service", "audit_record_9"),
                                      mk("VERIFY_RECORD_PRESENT", "security_service", "audit_record_10")])
        self.assertEqual([s["resource_id"] for s in out], ["audit_record_10", "audit_record_9"])

    def test_uppercase_sorts_before_lowercase_codepoint_order(self):
        out = steps.order_and_number([mk("DELETE_RECORD", "s", "b"), mk("DELETE_RECORD", "s", "B")])
        self.assertEqual([s["resource_id"] for s in out], ["B", "b"])

    def test_evidence_id_does_not_affect_order(self):
        a = mk("REPORT_REMAINING_RECORD", "s", "r1", "evidence_99")
        b = mk("REPORT_REMAINING_RECORD", "s", "r2", "evidence_01")
        out = steps.order_and_number([b, a])
        self.assertEqual([s["resource_id"] for s in out], ["r1", "r2"])

    def test_sort_key_shape(self):
        k = steps.sort_key(mk("DELETE_RECORD", "sys", "res"))
        self.assertEqual(k, (1, CANONICAL.index("DELETE_RECORD"), "sys", "res"))


class NumberingTests(unittest.TestCase):
    def test_numbered_from_one_without_gaps(self):
        out = steps.order_and_number([mk("DELETE_RECORD", "s", r) for r in "cab"])
        self.assertEqual([s["step_no"] for s in out], [1, 2, 3])
        self.assertEqual([s["resource_id"] for s in out], ["a", "b", "c"])

    def test_empty(self):
        self.assertEqual(steps.order_and_number([]), [])

    def test_input_not_mutated(self):
        src = [mk("DELETE_RECORD", "s", "b"), mk("DELETE_RECORD", "s", "a")]
        before = json.dumps(src)
        steps.order_and_number(src)
        self.assertEqual(json.dumps(src), before)
        self.assertNotIn("step_no", src[0])

    def test_existing_step_no_is_overwritten(self):
        a = dict(mk("DELETE_RECORD", "s", "a"), step_no=99)
        b = dict(mk("DELETE_RECORD", "s", "b"), step_no=1)
        out = steps.order_and_number([b, a])
        self.assertEqual([(s["resource_id"], s["step_no"]) for s in out], [("a", 1), ("b", 2)])

    def test_stable_for_equal_keys(self):
        a = mk("DELETE_RECORD", "s", "r", "")
        b = mk("DELETE_RECORD", "s", "r", "")
        self.assertEqual(steps.order_and_number([a, b]), steps.order_and_number([a, b]))

    def test_idempotent(self):
        once = steps.order_and_number([mk(o, "s", "r") for o in reversed(CANONICAL)])
        self.assertEqual(steps.order_and_number(once), once)

    def test_input_order_does_not_change_the_result(self):
        items = [mk("DELETE_RECORD", "s", "b"), mk("REPORT_REMAINING_RECORD", "s", "b", "e1"),
                 mk("VERIFY_RECORD_ABSENT", "s", "b"), mk("REPORT_ACTIVE_DATA_FLOW", "t", "d", "e2")]
        self.assertEqual(steps.order_and_number(items), steps.order_and_number(list(reversed(items))))

    def test_output_keys_are_in_contract_order(self):
        out = steps.order_and_number([mk("DELETE_RECORD", "s", "a")])
        self.assertEqual(list(out[0]), list(steps.STEP_KEYS))


class SerializeTests(unittest.TestCase):
    def test_empty_is_exact_brackets(self):
        self.assertEqual(steps.serialize_steps([]), "[]")

    def test_exact_bytes_and_key_order(self):
        out = steps.order_and_number([steps.make_step(
            "FINDING", "account_service", "account_005", "REPORT_REMAINING_RECORD", "evidence_01")])
        self.assertEqual(
            steps.serialize_steps(out),
            '[{"step_no":1,"phase":"FINDING","system_id":"account_service",'
            '"resource_id":"account_005","operation":"REPORT_REMAINING_RECORD",'
            '"state":"PRESENT","evidence_id":"evidence_01"}]')

    def test_key_order_is_fixed_even_if_the_input_dict_order_differs(self):
        s = steps.order_and_number([mk("DELETE_RECORD", "s", "r")])[0]
        scrambled = {k: s[k] for k in sorted(s, reverse=True)}
        self.assertEqual(steps.serialize_steps([scrambled]), steps.serialize_steps([s]))

    def test_compact_separators_no_spaces(self):
        text = steps.serialize_steps(steps.order_and_number([mk("DELETE_RECORD", "s", "a"),
                                                             mk("DELETE_RECORD", "s", "b")]))
        self.assertNotIn(", ", text)
        self.assertNotIn(": ", text)
        self.assertNotIn("\n", text)

    def test_round_trips_through_json(self):
        out = steps.order_and_number([mk(o, "s", "r") for o in CANONICAL])
        self.assertEqual(json.loads(steps.serialize_steps(out)), out)

    def test_non_ascii_is_kept_not_escaped(self):
        s = steps.make_step("FINDING", "sys", "résumé_é", "REPORT_REMAINING_RECORD", "evidence_01")
        text = steps.serialize_steps([dict(s, step_no=1)])
        self.assertIn("résumé_é", text)
        self.assertNotIn("\\u", text)

    def test_deterministic(self):
        out = steps.order_and_number([mk(o, "s", "r") for o in CANONICAL])
        self.assertEqual(steps.serialize_steps(out), steps.serialize_steps(list(out)))

    def test_extra_or_missing_key_rejected(self):
        s = steps.order_and_number([mk("DELETE_RECORD", "s", "a")])[0]
        with self.assertRaises(ValueError):
            steps.serialize_steps([dict(s, extra=1)])
        broken = dict(s)
        del broken["state"]
        with self.assertRaises(ValueError):
            steps.serialize_steps([broken])


if __name__ == "__main__":
    unittest.main()
