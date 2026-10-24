import json
import os
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

from ghost import diagnostics, llm
from tests.helpers import DATASET
from tests.test_llm import FAKE_KEY


def read_lines(runs):
    raw = (Path(runs) / "diagnostics.jsonl").read_text(encoding="utf-8")
    return raw, [json.loads(l) for l in raw.splitlines()]


class DiagnosticsTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.runs = Path(self.tmp.name) / "runs"  # does not exist yet
        env = mock.patch.dict(os.environ, {"ANTHROPIC_API_KEY": FAKE_KEY})
        env.start()
        self.addCleanup(env.stop)
        self.addCleanup(llm.reset)

    def test_creates_dir_and_writes_one_json_line_per_event(self):
        diagnostics.log_event(self.runs, "case_1", "alpha", x=1, note="héllo 削除")
        diagnostics.log_event(self.runs, "case_2", "beta")
        raw, rows = read_lines(self.runs)
        self.assertEqual(len(raw.splitlines()), 2)
        self.assertTrue(raw.endswith("\n"))
        self.assertEqual(rows[0], {"case_id": "case_1", "event": "alpha", "x": 1, "note": "héllo 削除"})
        self.assertEqual(rows[1], {"case_id": "case_2", "event": "beta"})
        self.assertIn("削除", raw)  # not \\u-escaped

    def test_appends_and_never_truncates(self):
        diagnostics.log_event(self.runs, "case_1", "a")
        first = (self.runs / "diagnostics.jsonl").read_text(encoding="utf-8")
        diagnostics.log_event(self.runs, "case_1", "b")
        both = (self.runs / "diagnostics.jsonl").read_text(encoding="utf-8")
        self.assertTrue(both.startswith(first))
        self.assertEqual(len(both.splitlines()), 2)

    def test_embedded_newlines_stay_on_one_line(self):
        diagnostics.log_event(self.runs, "case_1", "a", reason="line one\nline two\r\nthree")
        raw, rows = read_lines(self.runs)
        self.assertEqual(len(raw.splitlines()), 1)
        self.assertEqual(rows[0]["reason"], "line one\nline two\r\nthree")

    def test_api_key_and_key_shaped_strings_are_redacted(self):
        diagnostics.log_event(self.runs, "case_1", "a", note=f"oops {FAKE_KEY}",
                              other="sk-ant-api03-SOMEOTHERKEY-123")
        raw, rows = read_lines(self.runs)
        self.assertNotIn(FAKE_KEY, raw)
        self.assertNotIn("SOMEOTHERKEY", raw)
        self.assertIn("[REDACTED]", rows[0]["note"])

    def test_key_names_cannot_leak_through_field_names_or_case_id(self):
        diagnostics.log_event(self.runs, FAKE_KEY, "a", **{FAKE_KEY: 1})
        raw, _ = read_lines(self.runs)
        self.assertNotIn(FAKE_KEY, raw)

    def test_output_is_deterministic_keys_sorted_no_timestamp(self):
        diagnostics.log_event(self.runs, "case_1", "a", z=1, a=2)
        raw, _ = read_lines(self.runs)
        self.assertEqual(raw.strip(), '{"a": 2, "case_id": "case_1", "event": "a", "z": 1}')
        self.assertNotIn("ts", json.loads(raw))

    def test_concurrent_writers_produce_no_torn_or_lost_lines(self):
        n_threads, per = 16, 60
        big = "x" * 5000  # long lines are where interleaving shows up

        def work(t):
            for i in range(per):
                diagnostics.log_event(self.runs, f"case_{t}_{i}", "e", blob=big, t=t, i=i)

        threads = [threading.Thread(target=work, args=(t,)) for t in range(n_threads)]
        for th in threads:
            th.start()
        for th in threads:
            th.join()
        raw, rows = read_lines(self.runs)  # json.loads on every line => no torn lines
        self.assertEqual(len(rows), n_threads * per)
        self.assertEqual(len({r["case_id"] for r in rows}), n_threads * per)
        self.assertTrue(all(r["blob"] == big for r in rows))


class PoolOrderTests(unittest.TestCase):
    """classify_all writes events after the pool, in sorted case order, whatever the worker count."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        env = mock.patch.dict(os.environ, {"ANTHROPIC_API_KEY": FAKE_KEY})
        env.start()
        self.addCleanup(env.stop)
        self.addCleanup(llm.reset)

    def test_event_order_is_sorted_by_numeric_case_id_for_any_worker_count(self):
        from ghost import intent, loaders

        def transport(model, prompt, max_tokens, timeout):
            # every request gets a DELETION answer, so the 10 negation cases disagree
            return (json.dumps({"intent": "DELETION", "reason": "r"}), 4, 2)

        cases = list(reversed(loaders.load_requests(DATASET)))  # worst-case input order
        order = {}
        for workers in (1, 8):
            runs = Path(self.tmp.name) / f"r{workers}"
            llm.configure(runs_dir=runs, transport=transport, sleep=lambda s: None, run_id="x")
            intent.classify_all(cases, "m-1", True, runs, workers=workers)
            _, rows = read_lines(runs)
            order[workers] = [(r["event"], r["case_id"]) for r in rows]
        self.assertEqual(len(order[1]), 10)
        self.assertEqual(order[1], order[8])
        ids = [c for _, c in order[8]]
        self.assertEqual(ids, sorted(ids, key=loaders.case_sort_key))
        self.assertEqual({e for e, _ in order[8]}, {"intent_disagreement"})


if __name__ == "__main__":
    unittest.main()
