"""The custom-case generator and its runner: reproducible, independent of dataset/, well formed,
a real check (it fails when the expectation is wrong), and every case has a negative control."""
import csv
import json
import shutil
import tempfile
import unittest
from pathlib import Path

from evaluation import scoring
from evaluation.custom_cases import build, cases, render, runner
from ghost import loaders
from tests.helpers import DATASET, tree_fingerprint


def rows(path):
    with open(path, newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def read_pred(path):
    return {r["case_id"]: (r["deletion_status"], json.loads(r["steps_json"])) for r in rows(path)}


class Built(unittest.TestCase):
    """One real run of the whole custom dataset, shared by the tests that only read it."""

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.work = Path(cls.tmp.name) / "ds"
        cls.work.mkdir()
        cls.runs = Path(cls.tmp.name) / "runs"
        cls.runs.mkdir()
        cls.spec = build.build_dataset(cls.work)
        cls.proc = runner.run_cli(cls.work, cls.work / "pred.csv", cls.runs)
        cls.result = runner.evaluate(cls.work, cls.spec, cls.runs, cls.proc)

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()


class GeneratorTests(unittest.TestCase):
    def test_build_is_byte_identical_twice(self):
        with tempfile.TemporaryDirectory() as a, tempfile.TemporaryDirectory() as b:
            build.build_dataset(a)
            build.build_dataset(b)
            fa, fb = tree_fingerprint(a), tree_fingerprint(b)
            self.assertEqual({k: v[2] for k, v in fa.items()}, {k: v[2] for k, v in fb.items()})
            self.assertIn("expected_output.csv", fa)

    def test_controls_change_the_data(self):
        with tempfile.TemporaryDirectory() as a, tempfile.TemporaryDirectory() as b:
            build.build_dataset(a)
            build.build_dataset(b, {"CC1"})
            fa, fb = tree_fingerprint(a), tree_fingerprint(b)
            changed = sorted(k for k in fa if fa[k][2] != fb[k][2])
            self.assertEqual(changed, ["evidence/logs/lookup_trace_cc1.log"])

    def test_every_scenario_has_cases_and_a_control(self):
        spec = cases.build_spec()
        self.assertEqual(len(spec.requests), 8)
        self.assertEqual(sorted(spec.expected), [q.case_id for q in spec.requests])
        self.assertEqual(sorted(set(spec.labels.values())),
                         ["CC1", "CC2a", "CC2b", "CC3a", "CC3b", "CC4", "CC5", "CC6"])
        self.assertEqual(sorted(cases.SCENARIOS), ["CC1", "CC2", "CC3", "CC4", "CC5", "CC6"])
        # CC6 is a status-only case (no evidence, nothing to remove); the others have a control
        self.assertEqual(sorted(spec.control_status),
                         ["case_901", "case_902", "case_904", "case_906", "case_907"])

    def test_expected_steps_follow_the_operation_table(self):
        spec = cases.build_spec()
        for cid, (status, steps) in spec.expected.items():
            for s in steps:
                self.assertEqual((s["phase"], s["state"]), cases.OPS[s["operation"]], cid)
            # findings and remediation cite per 6.6: only findings carry evidence
            for s in steps:
                self.assertEqual(bool(s["evidence_id"]), s["phase"] == "FINDING"
                                 or status == "COMPLETE", f"{cid} {s['operation']}")
            self.assertEqual(status == "NOT_APPLICABLE", steps == [], cid)


class IndependenceTests(unittest.TestCase):
    """Nothing in the synthetic dataset comes from dataset/ (read here, never at generation)."""

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.spec = build.build_dataset(cls.tmp.name)
        cls.real_text = "\n".join(
            p.read_text(encoding="utf-8", errors="ignore")
            for p in sorted(DATASET.rglob("*")) if p.is_file() and p.suffix in
            {".csv", ".log", ".json", ".sql", ".txt"})

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def test_no_identifier_appears_in_the_real_dataset(self):
        mine = set()
        for u in self.spec.users:
            mine |= {u.user_id, u.email, u.phone, u.name, *[e for e, _ in u.old_emails]}
        for ev in self.spec.evidence:
            mine.add(ev.file_id)
        for a in self.spec.activities:
            mine |= {a[0], a[5]}
        for q in self.spec.requests:
            mine |= {q.case_id}
        for ident in sorted(mine):
            if ident in {"Sunniva Haugen"}:  # duplicated on purpose inside this dataset only
                continue
            self.assertNotIn(ident, self.real_text, ident)

    def test_no_request_sentence_is_copied(self):
        for q in self.spec.requests:
            for sentence in [s.strip() for s in q.text.replace("!", ".").split(".") if s.strip()]:
                self.assertNotIn(sentence, self.real_text, sentence)

    def test_the_rule_table_matches_the_real_policy(self):
        real = {(r["data_category"], r["required_action"]) for r in loaders.load_rules(DATASET)}
        mine = {(c, a) for c, _, a in cases.RULES}
        self.assertEqual(mine, real)

    def test_the_generator_has_no_path_to_dataset_or_test_code(self):
        import ast
        for mod in (build, cases, render, runner):
            tree = ast.parse(Path(mod.__file__).read_text(encoding="utf-8"))
            names = [n.module or "" for n in ast.walk(tree) if isinstance(n, ast.ImportFrom)]
            names += [a.name for n in ast.walk(tree) if isinstance(n, ast.Import) for a in n.names]
            literals = [n.value for n in ast.walk(tree)
                        if isinstance(n, ast.Constant) and isinstance(n.value, str)]
            self.assertFalse([n for n in names if n.split(".")[0] == "tests"], mod.__name__)
            self.assertNotIn("dataset", literals, mod.__name__)
            self.assertFalse([l for l in literals if "sample_output" in l or "sample_deletion" in l],
                             mod.__name__)


class WellFormedTests(Built):
    def test_every_evidence_file_is_claimed_and_fully_parsed(self):
        report = {}
        conn = loaders.load_store(self.work, report=report)
        self.assertEqual(report["unclaimed"], [])
        for fid, meaningful, parsed, unparsed in conn.execute(
                "select file_id, meaningful, parsed, unparsed from parse_report"):
            self.assertGreater(meaningful, 0, fid)
            self.assertEqual(unparsed, 0, fid)

    def test_the_expected_answers_pass_the_output_invariants(self):
        res = runner.invariants.check_file(self.work / "expected_output.csv",
                                           self.work / "deletion_requests.csv", self.work)
        self.assertEqual(list(res.violations), [])

    def test_renderers(self):
        self.assertEqual(render._value("a b"), '"a b"')
        self.assertEqual(render._value(None), "null")
        self.assertEqual(render._value(True), "true")
        lines = render.trace_file([render.profile_absent("2026-01-01T00:00:01Z", "2026-01-01T00:00:02Z",
                                                          "rb-1", "account_1", "x@example.test")],
                                  "2026-01-01T00:00:00Z", "2026-01-01T00:00:03Z").splitlines()
        self.assertIn("target_count=1", lines[0])
        self.assertIn("queries=1", lines[-1])
        self.assertEqual(json.loads(render.bundle([], "2026-01-01T00:00:00Z"))["snapshots"], [])


class RunnerTests(Built):
    def test_the_real_cli_ran_cleanly(self):
        self.assertEqual(self.proc.returncode, 0, self.proc.stderr)
        self.assertEqual(sorted(read_pred(self.work / "pred.csv")), sorted(self.spec.expected))

    def test_every_case_matches_strictly(self):
        for c in self.result.cases:
            self.assertEqual(c.reasons, [], c.case_id)
            self.assertEqual(c.missing_events, [], c.case_id)
        self.assertEqual(len(self.result.cases), 8)
        self.assertEqual(self.result.invariant_violations, [])
        self.assertTrue(self.result.ok)
        self.assertEqual(self.result.score["strict"]["status_accuracy"], 1.0)
        self.assertEqual(self.result.score["strict"]["sequence_accuracy"], 1.0)

    def test_the_report_lists_every_case(self):
        text = runner.format_result(self.result)
        for q in self.spec.requests:
            self.assertIn(q.case_id, text)
        self.assertIn("8/8 custom cases match", text)

    def perturbed(self, edit):
        """Re-evaluate the same run against an edited expected_output.csv."""
        with tempfile.TemporaryDirectory() as d:
            work = Path(d) / "ds"
            shutil.copytree(self.work, work)
            exp = work / "expected_output.csv"
            data = {r["case_id"]: r for r in rows(exp)}
            edit(data)
            with open(exp, "w", newline="", encoding="utf-8") as f:
                w = csv.writer(f, lineterminator="\n")
                w.writerow(build.HEADERS["expected_output.csv"])
                for cid in sorted(data):
                    w.writerow([cid, data[cid]["deletion_status"], data[cid]["steps_json"]])
            return runner.evaluate(work, self.spec, self.runs, self.proc)

    def test_a_wrong_expected_status_fails_the_runner(self):
        res = self.perturbed(lambda d: d["case_901"].update(deletion_status="COMPLETE"))
        self.assertFalse(res.ok)
        bad = [c.case_id for c in res.cases if not c.ok]
        self.assertEqual(bad, ["case_901"])
        self.assertIn("status wrong", " ".join(res.cases[0].reasons))

    def test_a_wrong_expected_order_fails_the_runner(self):
        def swap(d):
            steps = json.loads(d["case_907"]["steps_json"])
            steps[3], steps[4] = steps[4], steps[3]  # delete-before-disable
            d["case_907"]["steps_json"] = json.dumps(steps)
        res = self.perturbed(swap)
        self.assertEqual([c.case_id for c in res.cases if not c.ok], ["case_907"])
        self.assertIn("wrong order", " ".join(res.cases[6].reasons))

    def test_a_wrong_expected_citation_fails_the_runner(self):
        def recite(d):
            steps = json.loads(d["case_905"]["steps_json"])
            steps[0]["evidence_id"] = "evidence_904"  # the file before the late bundle
            d["case_905"]["steps_json"] = json.dumps(steps)
        res = self.perturbed(recite)
        self.assertEqual([c.case_id for c in res.cases if not c.ok], ["case_905"])
        self.assertIn("evidence_id", " ".join(res.cases[4].reasons))

    def test_a_missing_expected_step_fails_the_runner(self):
        def drop(d):
            steps = json.loads(d["case_903"]["steps_json"])
            d["case_903"]["steps_json"] = json.dumps(steps[:-1])
        res = self.perturbed(drop)
        self.assertEqual([c.case_id for c in res.cases if not c.ok], ["case_903"])
        self.assertIn("extra step", " ".join(res.cases[2].reasons))

    def test_a_missing_prediction_row_is_reported(self):
        with tempfile.TemporaryDirectory() as d:
            work = Path(d) / "ds"
            shutil.copytree(self.work, work)
            data = rows(work / "pred.csv")
            with open(work / "pred.csv", "w", newline="", encoding="utf-8") as f:
                w = csv.writer(f, lineterminator="\n")
                w.writerow(["case_id", "deletion_status", "steps_json"])
                for r in data[1:]:
                    w.writerow([r["case_id"], r["deletion_status"], r["steps_json"]])
            res = runner.evaluate(work, self.spec, self.runs, self.proc)
        self.assertFalse(res.ok)
        self.assertEqual(res.cases[0].actual_status, "(no row)")

    def test_a_missing_diagnostics_event_fails_the_runner(self):
        empty = Path(self.tmp.name) / "empty_runs"
        empty.mkdir()
        res = runner.evaluate(self.work, self.spec, empty, self.proc)
        self.assertFalse(res.ok)
        self.assertEqual(sorted(c.case_id for c in res.cases if c.missing_events),
                         ["case_902", "case_904"])

    def test_no_llm_was_used(self):
        self.assertFalse((self.runs / "usage.jsonl").exists())

    def test_a_failing_cli_is_reported_not_raised(self):
        with tempfile.TemporaryDirectory() as d:
            proc = runner.run_cli(Path(d) / "missing", Path(d) / "pred.csv", d)
            res = runner.evaluate(d, self.spec, d, proc)
        self.assertNotEqual(proc.returncode, 0)
        self.assertFalse(res.ok)


class ControlTests(unittest.TestCase):
    """Each case has a negative control: remove the hazard and the answer must change."""

    def run_control(self, label):
        with tempfile.TemporaryDirectory() as d:
            work, runs = Path(d) / "ds", Path(d) / "runs"
            work.mkdir()
            runs.mkdir()
            spec = build.build_dataset(work, {label})
            proc = runner.run_cli(work, work / "pred.csv", runs)
            self.assertEqual(proc.returncode, 0, proc.stderr)
            return spec, read_pred(work / "pred.csv")

    def check_status(self, label):
        spec, pred = self.run_control(label)
        for cid, status in spec.control_status.items():
            if spec.labels[cid].startswith(label):
                self.assertEqual(pred[cid][0], status, cid)
        return pred

    def test_cc1_a_real_404_in_place_of_the_503_completes_the_case(self):
        pred = self.check_status("CC1")
        self.assertEqual(pred["case_901"][0], "COMPLETE")
        self.assertEqual([s["operation"] for s in pred["case_901"][1]], ["VERIFY_RECORD_ABSENT"])

    def test_cc2_a_trace_naming_one_subject_links_the_record_to_that_subject(self):
        pred = self.check_status("CC2")
        self.assertIn("account_9201", [s["resource_id"] for s in pred["case_902"][1]])

    def test_cc3_a_bundle_inside_the_audit_window_changes_the_answer(self):
        pred = self.check_status("CC3")
        self.assertEqual(pred["case_904"][1][0]["evidence_id"], "evidence_905")

    def test_cc4_a_later_read_that_still_shows_the_account_gone_completes_the_case(self):
        self.check_status("CC4")

    def test_cc5_an_expired_retention_drops_the_retained_record_step(self):
        pred = self.check_status("CC5")
        ids = [s["resource_id"] for s in pred["case_907"][1]]
        self.assertNotIn("audit_record_9503", ids)
        self.assertIn("data_flow_9504", ids)  # the rest of the case is unchanged


class SafetyTests(unittest.TestCase):
    def test_the_runner_leaves_dataset_and_repo_runs_untouched(self):
        before = tree_fingerprint(DATASET)
        repo_runs = DATASET.parent / "runs"
        runs_before = tree_fingerprint(repo_runs) if repo_runs.exists() else None
        res = runner.run()
        self.assertTrue(res.ok, runner.format_result(res))
        self.assertEqual(tree_fingerprint(DATASET), before)
        self.assertEqual(tree_fingerprint(repo_runs) if repo_runs.exists() else None, runs_before)

    def test_keep_leaves_the_files_where_asked(self):
        with tempfile.TemporaryDirectory() as d:
            res = runner.run(keep=Path(d) / "kept")
            self.assertTrue(res.ok)
            self.assertTrue((Path(d) / "kept" / "expected_output.csv").exists())
            self.assertTrue((Path(d) / "kept" / "pred.csv").exists())

    def test_the_cli_flag_returns_zero(self):
        import contextlib
        import io
        from evaluation import main as ev_main
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = ev_main.main(["--custom"])
        self.assertEqual(code, 0)
        self.assertIn("8/8 custom cases match", out.getvalue())


class ScoringIntegration(unittest.TestCase):
    def test_expected_csv_is_a_valid_gold_file(self):
        with tempfile.TemporaryDirectory() as d:
            build.build_dataset(d)
            gold = scoring.load_rows(Path(d) / "expected_output.csv")
            self.assertEqual(len(gold), 8)
            res = scoring.score(gold, gold)
            self.assertEqual(res["strict"]["f1"], 1.0)


if __name__ == "__main__":
    unittest.main()
