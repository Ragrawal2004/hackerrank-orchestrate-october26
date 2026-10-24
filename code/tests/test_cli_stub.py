import csv
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from tests.helpers import DATASET, MAIN, tree_fingerprint


def run_cli(*args):
    return subprocess.run(
        [sys.executable, str(MAIN), *args],
        capture_output=True,
        text=True,
        env={"PATH": "/usr/bin:/bin"},
    )


def read_out(path):
    with open(path, newline="", encoding="utf-8") as f:
        return list(csv.reader(f))


class StubCliTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.out = Path(cls.tmp.name) / "output.csv"
        cls.before = tree_fingerprint(DATASET)
        cls.proc = run_cli("--out", str(cls.out))
        cls.rows = read_out(cls.out) if cls.out.exists() else []

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def test_exit_zero(self):
        self.assertEqual(self.proc.returncode, 0, self.proc.stderr)

    def test_header(self):
        self.assertEqual(self.rows[0], ["case_id", "deletion_status", "steps_json"])

    def test_518_unique_rows(self):
        ids = [r[0] for r in self.rows[1:]]
        self.assertEqual(len(ids), 518)
        self.assertEqual(len(set(ids)), 518)

    def test_numeric_order(self):
        ids = [r[0] for r in self.rows[1:]]
        self.assertEqual(ids[0], "case_33")
        self.assertEqual(ids[-1], "case_550")
        self.assertLess(ids.index("case_99"), ids.index("case_100"))
        nums = [int(i.split("_")[1]) for i in ids]
        self.assertEqual(nums, sorted(nums))

    def test_stub_rows_not_applicable_empty(self):
        for r in self.rows[1:]:
            self.assertEqual(r[1], "NOT_APPLICABLE")
            self.assertEqual(r[2], "[]")

    def test_dataset_untouched(self):
        self.assertEqual(tree_fingerprint(DATASET), self.before)

    def test_cases_filter(self):
        with tempfile.TemporaryDirectory() as d:
            out = Path(d) / "o.csv"
            p = run_cli("--out", str(out), "--cases", "case_34,case_33")
            self.assertEqual(p.returncode, 0, p.stderr)
            self.assertEqual([r[0] for r in read_out(out)[1:]], ["case_33", "case_34"])

    def test_dataset_flag(self):
        with tempfile.TemporaryDirectory() as d:
            ds = Path(d) / "ds"
            ds.mkdir()
            with open(DATASET / "deletion_requests.csv", newline="", encoding="utf-8") as f:
                reader = csv.reader(f)
                header = next(reader)
                rows = [next(reader) for _ in range(2)]
            with open(ds / "deletion_requests.csv", "w", newline="", encoding="utf-8") as f:
                w = csv.writer(f)
                w.writerow(header)
                w.writerows(rows)
            out = Path(d) / "o.csv"
            p = run_cli("--dataset", str(ds), "--out", str(out))
            self.assertEqual(p.returncode, 0, p.stderr)
            self.assertEqual(len(read_out(out)) - 1, 2)

    def test_sorts_shuffled_input_numerically(self):
        # The real file is already in numeric order, so only a shuffled fixture
        # proves the CLI itself sorts (and sorts numerically, not lexicographically).
        with open(DATASET / "deletion_requests.csv", newline="", encoding="utf-8") as f:
            reader = csv.reader(f)
            header = next(reader)
            by_id = {r[0]: r for r in reader}
        order = ["case_100", "case_33", "case_99", "case_550", "case_34"]
        with tempfile.TemporaryDirectory() as d:
            ds = Path(d) / "ds"
            ds.mkdir()
            with open(ds / "deletion_requests.csv", "w", newline="", encoding="utf-8") as f:
                w = csv.writer(f)
                w.writerow(header)
                w.writerows(by_id[c] for c in order)
            out = Path(d) / "o.csv"
            p = run_cli("--dataset", str(ds), "--out", str(out))
            self.assertEqual(p.returncode, 0, p.stderr)
            self.assertEqual(
                [r[0] for r in read_out(out)[1:]],
                ["case_33", "case_34", "case_99", "case_100", "case_550"],
            )

    def test_deterministic(self):
        with tempfile.TemporaryDirectory() as d:
            a, b = Path(d) / "a.csv", Path(d) / "b.csv"
            run_cli("--out", str(a))
            run_cli("--out", str(b))
            self.assertEqual(a.read_bytes(), b.read_bytes())

    def test_flags_accepted(self):
        with tempfile.TemporaryDirectory() as d:
            out = Path(d) / "o.csv"
            p = run_cli(
                "--out", str(out), "--cases", "case_33", "--link-fallback", "omit",
                "--category-llm", "off", "--no-llm", "--cold",
            )
            self.assertEqual(p.returncode, 0, p.stderr)

    def test_invalid_flag_value_fails(self):
        p = run_cli("--link-fallback", "maybe")
        self.assertNotEqual(p.returncode, 0)


if __name__ == "__main__":
    unittest.main()
