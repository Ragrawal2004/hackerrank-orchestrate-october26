import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from tests.helpers import DATASET, REPO_ROOT, tree_fingerprint
from tests.test_cli_pipeline import run_cli

EVAL = REPO_ROOT / "code" / "evaluation" / "main.py"


def run_eval(*args):
    return subprocess.run([sys.executable, str(EVAL), *args], capture_output=True, text=True)


class InvariantsCliTests(unittest.TestCase):
    def test_sample_gold_passes_exit_zero(self):
        before = tree_fingerprint(DATASET)
        p = run_eval("--invariants", "--pred", str(DATASET / "sample_output.csv"),
                     "--requests", str(DATASET / "sample_deletion_requests.csv"),
                     "--dataset", str(DATASET))
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        self.assertIn("RESULT: PASS", p.stdout)
        self.assertNotIn("SKIPPED", p.stdout)
        self.assertEqual(tree_fingerprint(DATASET), before)

    def test_stub_passes_only_with_allow_stub(self):
        with tempfile.TemporaryDirectory() as d:
            out = Path(d) / "stub.csv"
            self.assertEqual(run_cli("--no-llm", "--out", str(out)).returncode, 0)
            strict = run_eval("--invariants", "--pred", str(out))
            self.assertEqual(strict.returncode, 1, strict.stdout)
            self.assertIn("FAIL P1", strict.stdout)
            self.assertIn("FAIL P3", strict.stdout)
            self.assertIn("case_39", strict.stdout)  # first case still left without steps
            relaxed = run_eval("--invariants", "--allow-stub", "--pred", str(out))
            self.assertEqual(relaxed.returncode, 0, relaxed.stdout)
            self.assertIn("RESULT: PASS", relaxed.stdout)
            self.assertIn("SKIPPED P1", relaxed.stdout)

    def test_a_broken_file_exits_one_and_names_the_cases(self):
        with tempfile.TemporaryDirectory() as d:
            bad = Path(d) / "bad.csv"
            bad.write_text("case_id,deletion_status,steps_json\ncase_5,NOPE,[]\n", encoding="utf-8")
            p = run_eval("--invariants", "--pred", str(bad), "--requests",
                         str(DATASET / "sample_deletion_requests.csv"))
            self.assertEqual(p.returncode, 1)
            self.assertIn("FAIL S3", p.stdout)
            self.assertIn("case_5", p.stdout)
            self.assertIn("FAIL S2", p.stdout)  # 31 sample cases missing

    def test_missing_pred_is_an_input_error(self):
        p = run_eval("--invariants", "--pred", "/nonexistent/out.csv")
        self.assertEqual(p.returncode, 2)
        self.assertIn("error", p.stderr)

    def test_default_mode_is_still_scoring(self):
        p = run_eval("--pred", str(DATASET / "sample_output.csv"))
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertNotIn("Invariant check", p.stdout)

    def test_diagnostics_flag_allows_logged_uncited_account(self):
        import json
        with tempfile.TemporaryDirectory() as d:
            d = Path(d)
            step = {"step_no": 1, "phase": "VERIFICATION", "system_id": "account_service",
                    "resource_id": "account_7", "operation": "VERIFY_RECORD_ABSENT",
                    "state": "ABSENT", "evidence_id": ""}
            out = d / "o.csv"
            out.write_text("case_id,deletion_status,steps_json\ncase_1,COMPLETE,\""
                           + json.dumps([step], separators=(",", ":")).replace('"', '""') + "\"\n",
                           encoding="utf-8")
            reqs = d / "r.csv"
            reqs.write_text("case_id,email,requested_at,reported_status,reported_at,audit_at,request_text\n"
                            "case_1,a@b.c,t,COMPLETE,t,2026-09-15T09:00:00Z,x\n", encoding="utf-8")
            without = run_eval("--invariants", "--pred", str(out), "--requests", str(reqs))
            self.assertEqual(without.returncode, 1)
            diag = d / "diag.jsonl"
            diag.write_text('{"case_id":"case_1","event":"guessed_account_uncited",'
                            '"resource_id":"account_7"}\n', encoding="utf-8")
            with_diag = run_eval("--invariants", "--pred", str(out), "--requests", str(reqs),
                                 "--diagnostics", str(diag))
            self.assertEqual(with_diag.returncode, 0, with_diag.stdout)
            self.assertIn("exceptions used: 1", with_diag.stdout)


if __name__ == "__main__":
    unittest.main()
