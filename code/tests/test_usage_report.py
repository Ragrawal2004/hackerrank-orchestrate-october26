import contextlib
import io
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

    def test_shipped_price_table_matches_the_published_prices(self):
        from ghost import config
        src = "https://platform.claude.com/docs/en/about-claude/pricing"
        expected = {"claude-haiku-5-5": (0.10, 0.50), "claude-sonnet-5-5": (2.00, 10.00)}
        for model, (inp, out) in expected.items():
            with self.subTest(model=model):
                p = config.PRICES[model]
                self.assertEqual((p["input_per_mtok"], p["output_per_mtok"]), (inp, out))
                self.assertEqual(p["source_url"], src)
                self.assertEqual(p["as_of"], "2026-10-24")

    def test_every_default_model_is_priced(self):
        from ghost import config
        for model in {config.DEFAULT_INTENT_MODEL, config.DEFAULT_CATEGORY_MODEL,
                      config.DEFAULT_SCREENSHOT_MODEL}:
            with self.subTest(model=model):
                self.assertTrue(all(v is not None for v in config.PRICES[model].values()))

    def test_main_exits_nonzero_on_missing_price(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "usage.jsonl"
            p.write_text(json.dumps(line("model-with-no-price", 1, 1)) + "\n", encoding="utf-8")
            self.assertNotEqual(usage_report.main(["--usage", str(p), "--requests", "1"]), 0)

    def _usage_file(self, d, lines):
        p = Path(d) / "usage.jsonl"
        p.write_text("\n".join(json.dumps(l) for l in lines) + "\n", encoding="utf-8")
        return p

    def test_main_succeeds_on_priced_models_within_the_haiku_limit(self):
        with tempfile.TemporaryDirectory() as d:
            p = self._usage_file(d, [line("claude-haiku-5-5", 100_000, 50),
                                     line("claude-sonnet-5-5", 5_000, 50)])
            with contextlib.redirect_stdout(io.StringIO()) as out:
                rc = usage_report.main(["--usage", str(p), "--requests", "2"])
            self.assertEqual(rc, 0)
            self.assertIn("claude-haiku-5-5", out.getvalue())

    def test_haiku_line_over_100000_input_tokens_fails(self):
        with tempfile.TemporaryDirectory() as d:
            p = self._usage_file(d, [line("claude-haiku-5-5", 10, 1),
                                     line("claude-haiku-5-5", 100_001, 1)])
            with contextlib.redirect_stderr(io.StringIO()) as err:
                rc = usage_report.main(["--usage", str(p), "--requests", "2"])
            self.assertNotEqual(rc, 0)
            self.assertIn("100000", err.getvalue())
            self.assertIn("claude-haiku-5-5", err.getvalue())

    def test_summarize_raises_the_tier_error_for_one_oversized_haiku_line(self):
        from ghost import config
        lines = [line("claude-haiku-5-5", 100_001, 1)]
        with self.assertRaises(usage_report.PriceTierExceeded):
            usage_report.summarize(lines, config.PRICES, n_requests=1)

    def test_the_limit_is_per_line_not_a_total(self):
        from ghost import config
        lines = [line("claude-haiku-5-5", 60_000, 1), line("claude-haiku-5-5", 60_000, 1)]
        s = usage_report.summarize(lines, config.PRICES, n_requests=2)
        self.assertEqual(s["total"]["input_tokens"], 120_000)

    def test_the_haiku_limit_does_not_apply_to_other_models(self):
        from ghost import config
        s = usage_report.summarize([line("claude-sonnet-5-5", 150_000, 1)], config.PRICES, n_requests=1)
        self.assertEqual(s["total"]["input_tokens"], 150_000)

    def test_lines_outside_the_selected_run_are_not_checked(self):
        from ghost import config
        old = line("claude-haiku-5-5", 200_000, 1)
        old["run_id"] = "old"
        s = usage_report.summarize([old, line("claude-haiku-5-5", 10, 1)], config.PRICES,
                                   n_requests=1, run_id="r")
        self.assertEqual(s["total"]["input_tokens"], 10)


if __name__ == "__main__":
    unittest.main()
