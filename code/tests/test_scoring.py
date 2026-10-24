import csv
import io
import json
import tempfile
import unittest
from pathlib import Path

from evaluation import scoring
from tests.helpers import DATASET


def step(n, phase, op, system, resource, state, ev=""):
    return {"step_no": n, "phase": phase, "system_id": system, "resource_id": resource,
            "operation": op, "state": state, "evidence_id": ev}


def inc_steps():
    return [
        step(1, "FINDING", "REPORT_REMAINING_RECORD", "account_service", "account_005", "PRESENT", "evidence_01"),
        step(2, "REMEDIATION", "DELETE_RECORD", "account_service", "account_005", "ABSENT"),
        step(3, "VERIFICATION", "VERIFY_RECORD_ABSENT", "account_service", "account_005", "ABSENT"),
        step(4, "VERIFICATION", "VERIFY_RECORD_PRESENT", "security_service", "audit_record_9", "PRESENT"),
    ]


def com_steps():
    return [step(1, "VERIFICATION", "VERIFY_RECORD_ABSENT", "account_service", "account_007", "ABSENT", "evidence_54")]


def gold_rows():
    return {"case_01": ("INCOMPLETE", inc_steps()),
            "case_02": ("COMPLETE", com_steps()),
            "case_03": ("NOT_APPLICABLE", [])}


def csv_text(rows, compact=True):
    """rows: {case_id: (status, steps)} -> CSV text with the output.csv header."""
    buf = io.StringIO()
    w = csv.writer(buf, lineterminator="\n")
    w.writerow(["case_id", "deletion_status", "steps_json"])
    for cid, (status, steps) in rows.items():
        w.writerow([cid, status, json.dumps(steps, separators=(",", ":") if compact else None)])
    return buf.getvalue()


def score(pred, gold=None):
    gold = gold_rows() if gold is None else gold
    return scoring.score_texts(csv_text(gold), csv_text(pred) if isinstance(pred, dict) else pred)


def mutate(steps_by_case, case, fn):
    rows = {c: (s, [dict(x) for x in st]) for c, (s, st) in gold_rows().items()}
    status, steps = rows[case]
    rows[case] = (status, fn(steps))
    return rows


class PerfectTests(unittest.TestCase):
    def test_perfect_is_one_in_both_modes(self):
        r = score(gold_rows())
        for mode in ("strict", "relaxed"):
            m = r[mode]
            self.assertEqual(m["status_accuracy"], 1.0)
            self.assertEqual((m["precision"], m["recall"], m["f1"]), (1.0, 1.0, 1.0))
            self.assertEqual(m["sequence_accuracy"], 1.0)
            for f, v in m["field_accuracy"].items():
                self.assertIn(v, (1.0, None), f)
        self.assertEqual(r["mismatches"], [])

    def test_all_not_applicable_perfect_is_one(self):
        gold = {"case_01": ("NOT_APPLICABLE", []), "case_02": ("NOT_APPLICABLE", [])}
        r = score(gold, gold)
        for mode in ("strict", "relaxed"):
            self.assertEqual((r[mode]["precision"], r[mode]["recall"], r[mode]["f1"]), (1.0, 1.0, 1.0))
            self.assertEqual(r[mode]["sequence_accuracy"], 1.0)

    def test_key_order_and_whitespace_do_not_matter(self):
        gold_text = csv_text(gold_rows())
        pred_text = csv_text({c: (s, [dict(sorted(x.items())) for x in st]) for c, (s, st) in gold_rows().items()},
                             compact=False)
        r = scoring.score_texts(gold_text, pred_text)
        self.assertEqual(r["strict"]["sequence_accuracy"], 1.0)
        self.assertEqual(r["strict"]["f1"], 1.0)

    def test_step_no_is_ignored(self):
        rows = mutate(None, "case_01", lambda s: [dict(x, step_no=99) for x in s])
        self.assertEqual(score(rows)["strict"]["sequence_accuracy"], 1.0)

    def test_gold_against_itself_on_real_samples(self):
        text = (DATASET / "sample_output.csv").read_text(encoding="utf-8")
        r = scoring.score_texts(text, text)
        for mode in ("strict", "relaxed"):
            self.assertEqual(r[mode]["status_accuracy"], 1.0)
            self.assertEqual(r[mode]["f1"], 1.0)
            self.assertEqual(r[mode]["sequence_accuracy"], 1.0)
        self.assertEqual(r["gold_cases"], 32)
        self.assertEqual(r["mismatches"], [])


class EvidenceIdTests(unittest.TestCase):
    def setUp(self):
        def bad(steps):
            steps[0]["evidence_id"] = "evidence_99"
            return steps
        self.r = score(mutate(None, "case_01", bad))

    def test_strict_drops(self):
        s = self.r["strict"]
        self.assertLess(s["precision"], 1.0)
        self.assertLess(s["recall"], 1.0)
        self.assertLess(s["sequence_accuracy"], 1.0)
        self.assertLess(s["field_accuracy"]["evidence_id"], 1.0)
        self.assertEqual(s["status_accuracy"], 1.0)

    def test_relaxed_unchanged(self):
        s = self.r["relaxed"]
        self.assertEqual((s["precision"], s["recall"], s["f1"], s["sequence_accuracy"]), (1.0,) * 4)
        self.assertIsNone(s["field_accuracy"]["evidence_id"])
        for f in ("system_id", "state"):
            self.assertEqual(s["field_accuracy"][f], 1.0)

    def test_tagged_strict_only(self):
        (mm,) = self.r["mismatches"]
        self.assertEqual(mm["case_id"], "case_01")
        self.assertTrue(mm["strict_only"])
        self.assertTrue(any("evidence_id" in x for x in mm["reasons"]))


class StepDiffTests(unittest.TestCase):
    def test_missing_step(self):
        r = score(mutate(None, "case_01", lambda s: s[:-1]))
        for mode in ("strict", "relaxed"):
            m = r[mode]
            self.assertEqual(m["precision"], 1.0)
            self.assertLess(m["recall"], 1.0)
            self.assertLess(m["sequence_accuracy"], 1.0)
        (mm,) = r["mismatches"]
        self.assertTrue(any(x.startswith("missing step") and "VERIFY_RECORD_PRESENT" in x and "audit_record_9" in x
                            for x in mm["reasons"]), mm)
        self.assertFalse(mm["strict_only"])

    def test_extra_step(self):
        extra = step(5, "VERIFICATION", "VERIFY_RECORD_ABSENT", "search_service", "search_1", "ABSENT")
        r = score(mutate(None, "case_01", lambda s: s + [extra]))
        for mode in ("strict", "relaxed"):
            m = r[mode]
            self.assertLess(m["precision"], 1.0)
            self.assertEqual(m["recall"], 1.0)
            self.assertLess(m["sequence_accuracy"], 1.0)
        (mm,) = r["mismatches"]
        self.assertTrue(any(x.startswith("extra step") and "search_1" in x for x in mm["reasons"]), mm)

    def test_order_swap_changes_only_sequence(self):
        def swap(s):
            s[1], s[2] = s[2], s[1]
            return s
        r = score(mutate(None, "case_01", swap))
        for mode in ("strict", "relaxed"):
            m = r[mode]
            self.assertEqual((m["precision"], m["recall"], m["f1"]), (1.0, 1.0, 1.0))
            self.assertAlmostEqual(m["sequence_accuracy"], 2 / 3)
            for v in m["field_accuracy"].values():
                self.assertIn(v, (1.0, None))
        (mm,) = r["mismatches"]
        self.assertEqual(mm["reasons"], ["wrong order"])

    def test_wrong_system_id_and_state(self):
        def bad(s):
            s[1]["system_id"] = "replica_service"
            s[2]["state"] = "PRESENT"
            return s
        r = score(mutate(None, "case_01", bad))
        for mode in ("strict", "relaxed"):
            m = r[mode]
            self.assertLess(m["field_accuracy"]["system_id"], 1.0)
            self.assertLess(m["field_accuracy"]["state"], 1.0)
            self.assertEqual(m["field_accuracy"]["resource_id"], 1.0)
            self.assertLess(m["precision"], 1.0)
        (mm,) = r["mismatches"]
        self.assertTrue(any(x.startswith("wrong field") and "system_id" in x for x in mm["reasons"]), mm)
        self.assertTrue(any(x.startswith("wrong field") and "state" in x for x in mm["reasons"]), mm)

    def test_field_accuracy_exact_fraction(self):
        # 6 aligned steps in case_01+case_02 gold (4 + 1 = 5); one wrong system_id -> 4/5
        def bad(s):
            s[0]["system_id"] = "x"
            return s
        r = score(mutate(None, "case_01", bad))
        self.assertAlmostEqual(r["strict"]["field_accuracy"]["system_id"], 4 / 5)
        self.assertEqual(r["strict"]["aligned_steps"], 5)


class StatusTests(unittest.TestCase):
    def test_wrong_status(self):
        rows = gold_rows()
        rows["case_02"] = ("INCOMPLETE", rows["case_02"][1])
        r = score(rows)
        for mode in ("strict", "relaxed"):
            self.assertAlmostEqual(r[mode]["status_accuracy"], 2 / 3)
        (mm,) = r["mismatches"]
        self.assertEqual(mm["case_id"], "case_02")
        self.assertIn("status wrong (pred INCOMPLETE, gold COMPLETE)", mm["reasons"])
        self.assertEqual(r["confusion"][("COMPLETE", "INCOMPLETE")], 1)


class EmptyAndMissingTests(unittest.TestCase):
    def test_header_only_prediction_scores_zero(self):
        r = score("case_id,deletion_status,steps_json\n")
        for mode in ("strict", "relaxed"):
            m = r[mode]
            self.assertEqual(m["status_accuracy"], 0.0)
            self.assertEqual((m["precision"], m["recall"], m["f1"]), (0.0, 0.0, 0.0))
            self.assertEqual(m["sequence_accuracy"], 0.0)
        self.assertEqual(len(r["mismatches"]), 3)
        for mm in r["mismatches"]:
            self.assertIn("no prediction row", mm["reasons"])

    def test_all_empty_steps_prediction_scores_zero_on_steps(self):
        pred = {c: ("INCOMPLETE", []) for c in gold_rows()}
        r = score(pred)
        for mode in ("strict", "relaxed"):
            m = r[mode]
            self.assertAlmostEqual(m["status_accuracy"], 1 / 3)
            self.assertEqual((m["precision"], m["recall"], m["f1"]), (0.0, 0.0, 0.0))
            self.assertAlmostEqual(m["sequence_accuracy"], 0.0 + 1 / 3)  # only the NOT_APPLICABLE [] case equals

    def test_missing_row_counts_wrong_not_skipped(self):
        rows = gold_rows()
        del rows["case_02"]
        r = score(rows)
        self.assertAlmostEqual(r["strict"]["status_accuracy"], 2 / 3)
        self.assertAlmostEqual(r["strict"]["recall"], 4 / 5)
        self.assertEqual(r["strict"]["precision"], 1.0)

    def test_extra_rows_ignored_and_counted(self):
        rows = gold_rows()
        rows["case_99"] = ("INCOMPLETE", inc_steps())
        r = score(rows)
        self.assertEqual(r["extra_rows"], 1)
        self.assertEqual(r["strict"]["precision"], 1.0)
        self.assertEqual(r["mismatches"], [])

    def test_bad_steps_json_flagged(self):
        text = csv_text({c: v for c, v in gold_rows().items() if c != "case_02"}) + "case_02,COMPLETE,not json\n"
        r = scoring.score_texts(csv_text(gold_rows()), text)
        (mm,) = r["mismatches"]
        self.assertEqual(mm["case_id"], "case_02")
        self.assertIn("bad steps_json", mm["reasons"])
        self.assertLess(r["strict"]["recall"], 1.0)


class InputErrorTests(unittest.TestCase):
    def test_duplicate_case_id_in_pred(self):
        text = csv_text(gold_rows()) + csv_text(gold_rows()).split("\n", 1)[1]
        with self.assertRaises(scoring.InputError):
            scoring.score_texts(csv_text(gold_rows()), text)

    def test_bad_header(self):
        with self.assertRaises(scoring.InputError):
            scoring.score_texts(csv_text(gold_rows()), "a,b,c\ncase_01,x,[]\n")

    def test_load_missing_file(self):
        with self.assertRaises(scoring.InputError):
            scoring.load_rows(Path(tempfile.gettempdir()) / "ghost-tests-no-such-file.csv")


class FormatTests(unittest.TestCase):
    def test_report_has_both_columns_and_mismatches(self):
        rows = gold_rows()
        rows["case_02"] = ("INCOMPLETE", [])
        text = scoring.format_report(score(rows))
        for needle in ("strict", "relaxed", "Case status accuracy", "F1", "Sequence accuracy",
                       "case_02", "status wrong"):
            self.assertIn(needle, text)


if __name__ == "__main__":
    unittest.main()
