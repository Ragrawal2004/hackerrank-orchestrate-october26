import json
import tempfile
import unittest
from pathlib import Path

from evaluation import usage_report

PRICES = {
    "m-a": {"input_per_mtok": 1.0, "output_per_mtok": 5.0,
            "source_url": "https://example.test/pricing", "as_of": "2026-01-01"},
    "m-b": {"input_per_mtok": 3.0, "output_per_mtok": 15.0,
            "source_url": "https://example.test/pricing", "as_of": "2026-01-01"},
}


def line(model, i, o, hit=False, error=None, purpose="intent", case="case_33"):
    return {"ts": "2026-10-24T00:00:00Z", "purpose": purpose, "model": model, "case_id": case,
            "input_tokens": i, "output_tokens": o, "cache_hit": hit, "error": error,
            "run_id": "r"}


class UsageReportTests(unittest.TestCase):
    def test_per_model_and_overall_totals(self):
        lines = [line("m-a", 1_000_000, 200_000), line("m-a", 500_000, 0),
                 line("m-b", 100_000, 10_000)]
        s = usage_report.summarize(lines, PRICES, n_requests=3)
        a = s["models"]["m-a"]
        self.assertEqual((a["calls"], a["input_tokens"], a["output_tokens"]), (2, 1_500_000, 200_000))
        self.assertAlmostEqual(a["cost"], 1.5 * 1.0 + 0.2 * 5.0)
        b = s["models"]["m-b"]
        self.assertAlmostEqual(b["cost"], 0.1 * 3.0 + 0.01 * 15.0)
        self.assertEqual(s["total"]["calls"], 3)
        self.assertEqual(s["total"]["input_tokens"], 1_600_000)
        self.assertAlmostEqual(s["total"]["cost"], a["cost"] + b["cost"])
        self.assertAlmostEqual(s["total"]["tokens_per_request"], (1_600_000 + 210_000) / 3)
        self.assertAlmostEqual(s["total"]["cost_per_request"], s["total"]["cost"] / 3)

    def test_cache_hits_are_not_calls_and_cost_nothing(self):
        lines = [line("m-a", 1000, 100), line("m-a", 0, 0, hit=True), line("m-a", 0, 0, hit=True)]
        s = usage_report.summarize(lines, PRICES, n_requests=1)
        self.assertEqual(s["models"]["m-a"]["calls"], 1)
        self.assertEqual(s["models"]["m-a"]["cache_hits"], 2)
        self.assertEqual(s["total"]["cache_hits"], 2)

    def test_failed_attempts_count_as_calls_and_are_reported(self):
        lines = [line("m-a", 0, 0, error="APITimeoutError"), line("m-a", 10, 2)]
        s = usage_report.summarize(lines, PRICES, n_requests=1)
        self.assertEqual(s["models"]["m-a"]["calls"], 2)
        self.assertEqual(s["models"]["m-a"]["errors"], 1)

    def test_missing_price_fails_loudly(self):
        prices = {"m-a": {"input_per_mtok": None, "output_per_mtok": None,
                          "source_url": None, "as_of": None}}
        with self.assertRaises(usage_report.MissingPrice) as cm:
            usage_report.summarize([line("m-a", 1, 1)], prices, n_requests=1)
        self.assertIn("m-a", str(cm.exception))
        self.assertIn("input_per_mtok", str(cm.exception))

    def test_each_price_field_is_required(self):
        for field in ("input_per_mtok", "output_per_mtok", "source_url", "as_of"):
            with self.subTest(field=field):
                prices = {"m-a": {**PRICES["m-a"], field: None}}
                with self.assertRaises(usage_report.MissingPrice) as cm:
                    usage_report.summarize([line("m-a", 1, 1)], prices, n_requests=1)
                self.assertIn(field, str(cm.exception))

    def test_unknown_model_fails_loudly(self):
        with self.assertRaises(usage_report.MissingPrice):
            usage_report.summarize([line("m-zzz", 1, 1)], PRICES, n_requests=1)

    def test_run_filter(self):
        a, b = line("m-a", 10, 1), line("m-a", 20, 2)
        b["run_id"] = "other"
        s = usage_report.summarize([a, b], PRICES, n_requests=1, run_id="r")
        self.assertEqual(s["total"]["input_tokens"], 10)

    def test_render_markdown_mentions_models_and_source(self):
        s = usage_report.summarize([line("m-a", 1000, 100)], PRICES, n_requests=1)
        md = usage_report.render_markdown(s, PRICES)
        self.assertIn("m-a", md)
        self.assertIn("https://example.test/pricing", md)

    def test_load_usage_reads_jsonl(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "usage.jsonl"
            p.write_text("\n".join(json.dumps(l) for l in [line("m-a", 1, 1), line("m-a", 2, 2)]) + "\n",
                         encoding="utf-8")
            self.assertEqual(len(usage_report.load_usage(p)), 2)

    def test_default_price_table_is_an_unfilled_todo(self):
        # Guards against inventing numbers: the shipped table must be filled by the user.
        from ghost import config
        for model, p in config.PRICES.items():
            with self.subTest(model=model):
                self.assertTrue(any(v is None for v in p.values()))

    def test_main_exits_nonzero_on_missing_price(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "usage.jsonl"
            p.write_text(json.dumps(line("claude-haiku-5-5", 1, 1)) + "\n", encoding="utf-8")
            self.assertNotEqual(usage_report.main(["--usage", str(p), "--requests", "1"]), 0)


if __name__ == "__main__":
    unittest.main()
