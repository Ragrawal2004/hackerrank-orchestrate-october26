import csv
import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from ghost import invariants
from tests.helpers import DATASET, MAIN, tree_fingerprint


# A path that does not exist, so the CLI never reads a real .env during tests.
NO_ENV_FILE = str(Path(tempfile.gettempdir()) / "ghost-tests-no-such-env-file")


def run_cli(*args, runs_dir=None, env=None):
    """Run the CLI with no .env; runs/ goes to a throwaway dir unless one is given."""
    with tempfile.TemporaryDirectory() as tmp:
        return subprocess.run(
            [sys.executable, str(MAIN), *args],
            capture_output=True,
            text=True,
            env={"PATH": "/usr/bin:/bin", "GHOST_ENV_FILE": NO_ENV_FILE,
                 "GHOST_RUNS_DIR": str(runs_dir or tmp), **(env or {})},
        )


def copy_context(ds):
    """Copy the identity, audit, activity, rule and manifest CSVs next to a fixture request file."""
    for name in ("user_identities.csv", "user_audit_log.csv", "system_activity.csv",
                 "deletion_rules.csv", "evidence_files.csv"):
        shutil.copy(DATASET / name, ds / name)


def read_out(path):
    with open(path, newline="", encoding="utf-8") as f:
        return list(csv.reader(f))


def read_events(runs_dir):
    path = Path(runs_dir) / "diagnostics.jsonl"
    return [json.loads(l) for l in path.read_text(encoding="utf-8").splitlines() if l.strip()]


class PipelineCliTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.out = Path(cls.tmp.name) / "output.csv"
        cls.runs = Path(cls.tmp.name) / "runs"
        cls.before = tree_fingerprint(DATASET)
        cls.proc = run_cli("--out", str(cls.out), runs_dir=cls.runs)
        cls.rows = read_out(cls.out) if cls.out.exists() else []
        cls.events = read_events(cls.runs) if cls.runs.exists() else []

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

    def test_only_the_ten_non_deletions_are_not_applicable(self):
        na = [r[0] for r in self.rows[1:] if r[1] == "NOT_APPLICABLE"]
        self.assertEqual(na, [f"case_{n}" for n in range(533, 543)])
        for r in self.rows[1:]:
            if r[0] in na:
                self.assertEqual(r[2], "[]")
            else:
                self.assertIn(r[1], ("COMPLETE", "INCOMPLETE"))

    def test_every_deletion_case_logs_a_dossier_and_empty_rows_are_excused(self):
        # Adapters land one task at a time; a deletion case with no evidence yet is INCOMPLETE []
        # and must log no_steps.
        deletion = {r[0] for r in self.rows[1:] if r[1] != "NOT_APPLICABLE"}
        empty = {r[0] for r in self.rows[1:] if r[1] == "INCOMPLETE" and r[2] == "[]"}
        no_steps = {e["case_id"] for e in self.events if e["event"] == "no_steps"}
        dossiers = {e["case_id"] for e in self.events if e["event"] == "dossier"}
        self.assertEqual(len(deletion), 508)
        self.assertEqual(no_steps, empty)
        self.assertEqual(dossiers, deletion)

    def test_invariants_pass_without_allow_stub(self):
        res = invariants.check_file(self.out, DATASET / "deletion_requests.csv", DATASET,
                                    self.runs / "diagnostics.jsonl")
        self.assertEqual(res.violations, [], res.violations[:5])
        self.assertEqual(res.skipped, {})
        no_steps = {e["case_id"] for e in self.events if e["event"] == "no_steps"}
        self.assertEqual(res.stats["no_steps_excused"], len(no_steps))

    def test_prints_the_intent_report(self):
        self.assertIn("Agreement (LLM vs prefilter)", self.proc.stdout)
        self.assertIn("NON_DELETION by source", self.proc.stdout)

    def test_prints_the_policy_summary(self):
        self.assertRegex(self.proc.stdout, r"Status: COMPLETE=\d+ INCOMPLETE=\d+ NOT_APPLICABLE=10")
        self.assertRegex(self.proc.stdout, r"no_steps: \d+ cases")
        self.assertRegex(self.proc.stdout, r"retained_missing: \d+ cases / \d+ records")

    def test_prints_the_adapter_summary_with_zero_unparsed(self):
        self.assertIn("adapters: files=75 ", self.proc.stdout)
        self.assertIn(" unclaimed_files=19", self.proc.stdout)
        self.assertIn(" unparsed=0 ", self.proc.stdout)
        self.assertIn(" clock_choice_matters=0 ", self.proc.stdout)
        self.assertNotIn("WARNING", self.proc.stdout)

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
            copy_context(ds)
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
            copy_context(ds)
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


class LinkFallbackCliTests(unittest.TestCase):
    """T16: LINK_FALLBACK=guess adds only low-confidence account steps; omit adds none."""

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        d = Path(cls.tmp.name)
        cls.runs, cls.rows, cls.events, cls.proc = {}, {}, {}, {}
        for mode in ("guess", "omit"):
            out, runs = d / f"{mode}.csv", d / f"runs_{mode}"
            cls.proc[mode] = run_cli("--out", str(out), "--link-fallback", mode, runs_dir=runs)
            cls.rows[mode] = ({r[0]: (r[1], json.loads(r[2])) for r in read_out(out)[1:]}
                              if out.exists() else {})
            cls.events[mode] = read_events(runs) if runs.exists() else []
            cls.runs[mode] = (out, runs)

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def guesses(self, mode):
        return {(e["case_id"], e["resource_id"]) for e in self.events[mode]
                if e["event"] == "account_guess"}

    def test_both_modes_exit_zero(self):
        for mode in ("guess", "omit"):
            self.assertEqual(self.proc[mode].returncode, 0, self.proc[mode].stderr)

    def test_guess_is_m6_clock_safe_with_24_picks(self):
        ev = [e for e in self.events["guess"] if e["event"] == "account_guess"]
        self.assertEqual(len(ev), 24)
        self.assertTrue(all(e["method"] == "M6" and e["confidence"] == "low" for e in ev))

    def test_statuses_identical_in_both_modes(self):
        self.assertTrue(self.rows["guess"])
        self.assertEqual({c: v[0] for c, v in self.rows["guess"].items()},
                         {c: v[0] for c, v in self.rows["omit"].items()})

    def test_diff_is_exactly_the_low_confidence_steps(self):
        def strip(s):
            return {k: v for k, v in s.items() if k != "step_no"}
        added = set()
        for cid, (_, g_steps) in self.rows["guess"].items():
            o = [strip(s) for s in self.rows["omit"][cid][1]]
            g = [strip(s) for s in g_steps]
            self.assertEqual([s for s in o if s not in g], [], cid)
            for s in (s for s in g if s not in o):
                self.assertEqual((s["phase"], s["operation"], s["system_id"]),
                                 ("VERIFICATION", "VERIFY_RECORD_ABSENT", "account_service"), cid)
                added.add((cid, s["resource_id"]))
        self.assertTrue(added)
        self.assertEqual(added, self.guesses("guess"))

    def test_omit_has_no_guessed_step(self):
        self.assertEqual(self.guesses("omit"), set())
        unresolved = [e for e in self.events["omit"] if e["event"] == "link_unresolved"]
        self.assertTrue(unresolved)
        self.assertTrue(all(e["reason"] == "omit" for e in unresolved))

    def test_default_mode_is_omit_and_emits_no_guessed_step(self):
        """No flag and no LINK_FALLBACK env var: the default must not guess."""
        with tempfile.TemporaryDirectory() as d:
            out, runs = Path(d) / "o.csv", Path(d) / "runs"
            p = run_cli("--out", str(out), runs_dir=runs)
            self.assertEqual(p.returncode, 0, p.stderr)
            self.assertRegex(p.stdout, r"linker: mode=omit picks=0 ")
            self.assertEqual([e for e in read_events(runs) if e["event"] == "account_guess"], [])
            self.assertEqual(out.read_bytes(), self.runs["omit"][0].read_bytes())

    def test_linker_line_is_printed(self):
        for mode in ("guess", "omit"):
            self.assertRegex(self.proc[mode].stdout, rf"linker: mode={mode} picks=\d+ ")

    def test_ma_candidates_are_withheld(self):
        withheld = {(e["case_id"], e["resource_id"]) for e in self.events["guess"]
                    if e["event"] == "analytics_candidate"}
        self.assertTrue(withheld)
        for cid, rid in withheld:
            self.assertNotIn(rid, [s["resource_id"] for s in self.rows["guess"][cid][1]])

    def test_invariants_pass_in_both_modes(self):
        for mode in ("guess", "omit"):
            out, runs = self.runs[mode]
            res = invariants.check_file(out, DATASET / "deletion_requests.csv", DATASET,
                                        runs / "diagnostics.jsonl")
            self.assertEqual(res.violations, [], (mode, res.violations[:5]))
            self.assertNotIn("every deletion case has an account step", " ".join(res.not_checked))

    def test_two_runs_are_byte_identical_in_both_modes(self):
        for mode in ("guess", "omit"):
            out, runs = self.runs[mode]
            with tempfile.TemporaryDirectory() as d:
                again, runs2 = Path(d) / "o.csv", Path(d) / "runs"
                run_cli("--out", str(again), "--link-fallback", mode, runs_dir=runs2)
                self.assertEqual(again.read_bytes(), out.read_bytes(), mode)
                self.assertEqual((runs2 / "diagnostics.jsonl").read_bytes(),
                                 (runs / "diagnostics.jsonl").read_bytes(), mode)

    def test_env_sets_the_mode_and_the_flag_overrides_it(self):
        cid = sorted(self.guesses("guess"))[0][0]
        with tempfile.TemporaryDirectory() as d:
            a, b = Path(d) / "a.csv", Path(d) / "b.csv"
            run_cli("--out", str(a), "--cases", cid, env={"LINK_FALLBACK": "omit"})
            run_cli("--out", str(b), "--cases", cid, "--link-fallback", "guess",
                    env={"LINK_FALLBACK": "omit"})
            self.assertEqual(json.loads(read_out(a)[1][2]), self.rows["omit"][cid][1])
            self.assertEqual(json.loads(read_out(b)[1][2]), self.rows["guess"][cid][1])

    def test_cases_filter_does_not_change_a_guess(self):
        for cid, _ in sorted(self.guesses("guess"))[:3]:
            with tempfile.TemporaryDirectory() as d:
                out = Path(d) / "o.csv"
                run_cli("--out", str(out), "--cases", cid, "--link-fallback", "guess")
                self.assertEqual(json.loads(read_out(out)[1][2]), self.rows["guess"][cid][1], cid)


if __name__ == "__main__":
    unittest.main()
