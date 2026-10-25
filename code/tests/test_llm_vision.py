"""Gateway extension for T17: image blocks, per-read variants, and hard call/cost caps."""
import json
import threading

from ghost import config, llm
from tests.test_llm import Base, GOOD, SCHEMA

MODEL = config.DEFAULT_SCREENSHOT_MODEL  # priced in config.PRICES
IMG = ("image/png", b"\x89PNG\r\n\x1a\n-one")
IMG2 = ("image/png", b"\x89PNG\r\n\x1a\n-two")


class VisionTransport:
    """Scripted transport that also accepts images=. Items: (text, in_tok, out_tok) or Exception."""

    def __init__(self, *script):
        self.script = list(script)
        self.calls = []

    def __call__(self, model, prompt, max_tokens, timeout, images=()):
        self.calls.append({"model": model, "prompt": prompt, "max_tokens": max_tokens,
                           "images": list(images)})
        item = self.script.pop(0) if len(self.script) > 1 else self.script[0]
        if isinstance(item, Exception):
            raise item
        return item


class ImageCallTests(Base):
    def vcall(self, prompt="read the screen", images=(IMG,), variant="A", purpose="screenshot",
              case_id="evidence_04", model=MODEL, **kw):
        return llm.call(purpose, model, prompt, case_id, SCHEMA, images=list(images),
                        variant=variant, **kw)

    def test_images_reach_the_transport_with_the_prompt(self):
        t = VisionTransport((GOOD, 10, 5))
        self.configure(t)
        self.assertEqual(self.vcall(), {"intent": "DELETION"})
        self.assertEqual(t.calls[0]["images"], [IMG])
        self.assertEqual(t.calls[0]["prompt"], "read the screen")

    def test_calls_without_images_use_the_old_transport_signature(self):
        from tests.test_llm import FakeTransport
        t = FakeTransport((GOOD, 1, 1))  # accepts exactly (model, prompt, max_tokens, timeout)
        self.configure(t)
        self.assertEqual(self.call(), {"intent": "DELETION"})
        self.assertEqual(len(t.calls), 1)

    def test_cache_key_depends_on_the_image_bytes(self):
        t = VisionTransport((GOOD, 10, 5))
        self.configure(t)
        self.vcall(images=[IMG])
        self.vcall(images=[IMG2])
        self.assertEqual(len(t.calls), 2)

    def test_same_image_same_variant_is_a_cache_hit(self):
        t = VisionTransport((GOOD, 10, 5))
        self.configure(t)
        self.vcall()
        self.vcall()
        self.assertEqual(len(t.calls), 1)

    def test_read_b_is_not_a_cache_hit_of_read_a(self):
        t = VisionTransport((GOOD, 10, 5))
        self.configure(t)
        self.vcall(variant="A")
        self.vcall(variant="B")  # identical prompt, model and image; only the variant differs
        self.assertEqual(len(t.calls), 2)

    def test_usage_line_carries_variant_and_image_count(self):
        self.configure(VisionTransport((GOOD, 10, 5)))
        self.vcall(variant="B", images=[IMG, IMG2])
        line = self.usage()[0]
        self.assertEqual((line["variant"], line["image_count"]), ("B", 2))
        self.assertEqual((line["purpose"], line["input_tokens"], line["output_tokens"]),
                         ("screenshot", 10, 5))

    def test_max_tokens_can_be_raised_per_call(self):
        t = VisionTransport((GOOD, 1, 1))
        self.configure(t)
        self.vcall(max_tokens=4096)
        self.assertEqual(t.calls[0]["max_tokens"], 4096)

    def test_sdk_transport_puts_image_blocks_before_the_text(self):
        seen = {}

        class Msgs:
            def create(self, **kw):
                seen.update(kw)
                raise RuntimeError("stop here")

        class Client:
            def __init__(self, **kw):
                self.messages = Msgs()

        send = llm.make_sdk_transport(client_factory=Client)
        with self.assertRaises(llm.TransportError):
            send(MODEL, "describe", 100, 5, images=[IMG])
        blocks = seen["messages"][0]["content"]
        self.assertEqual([b["type"] for b in blocks], ["image", "text"])
        self.assertEqual(blocks[0]["source"]["type"], "base64")
        self.assertEqual(blocks[0]["source"]["media_type"], "image/png")
        self.assertEqual(blocks[1]["text"], "describe")

    def test_no_key_means_unavailable_even_with_images(self):
        import os
        from unittest import mock
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop(llm.KEY_ENV, None)
            self.configure(None)
            with self.assertRaises(llm.LLMUnavailable):
                self.vcall()


class BudgetTests(Base):
    def vcall(self, prompt="p", purpose="screenshot", model=MODEL, images=()):
        return llm.call(purpose, model, prompt, "evidence_04", SCHEMA, images=list(images),
                        variant="A")

    def budgets(self, **kw):
        return {"screenshot": kw}

    def test_call_cap_refuses_the_next_real_call(self):
        t = VisionTransport((GOOD, 10, 5))
        self.configure(t, budgets=self.budgets(max_calls=1, max_usd=100.0))
        self.vcall("one")
        with self.assertRaises(llm.LLMUnavailable) as cm:
            self.vcall("two")
        self.assertIn("budget_exceeded", str(cm.exception))
        self.assertEqual(len(t.calls), 1)

    def test_refusal_is_logged_and_costs_no_tokens(self):
        self.configure(VisionTransport((GOOD, 10, 5)), budgets=self.budgets(max_calls=1,
                                                                            max_usd=100.0))
        self.vcall("one")
        with self.assertRaises(llm.LLMUnavailable):
            self.vcall("two")
        last = self.usage()[-1]
        self.assertEqual(last["error"], "BudgetExceeded")
        self.assertEqual((last["input_tokens"], last["output_tokens"]), (0, 0))

    def test_cache_hits_are_free_and_uncounted(self):
        t = VisionTransport((GOOD, 10, 5))
        self.configure(t, budgets=self.budgets(max_calls=1, max_usd=100.0))
        for _ in range(5):
            self.vcall("same")
        self.assertEqual(len(t.calls), 1)

    def test_cost_cap_uses_the_tokens_the_api_reported(self):
        price_in = config.PRICES[MODEL]["input_per_mtok"]
        spent = 1_000_000 * price_in / 1e6  # one call that reports 1M input tokens
        t = VisionTransport((GOOD, 1_000_000, 0))
        self.configure(t, budgets=self.budgets(max_calls=50, max_usd=spent))
        self.vcall("one")  # allowed: nothing spent yet
        with self.assertRaises(llm.LLMUnavailable):
            self.vcall("two")  # spent >= cap

    def test_worst_case_of_the_next_call_is_checked_before_sending(self):
        t = VisionTransport((GOOD, 1, 1))
        # an image alone is assumed to cost up to IMAGE_TOKENS_UPPER input tokens
        floor = llm.IMAGE_TOKENS_UPPER * config.PRICES[MODEL]["input_per_mtok"] / 1e6
        self.configure(t, budgets=self.budgets(max_calls=50, max_usd=floor / 2))
        with self.assertRaises(llm.LLMUnavailable):
            self.vcall("big", images=[("image/png", b"\x89PNG-x")])
        self.assertEqual(t.calls, [])

    def test_a_budgeted_purpose_with_an_unpriced_model_is_refused(self):
        t = VisionTransport((GOOD, 1, 1))
        self.configure(t, budgets=self.budgets(max_calls=5, max_usd=5.0))
        with self.assertRaises(llm.LLMUnavailable):
            self.vcall("x", model="m-unpriced")
        self.assertEqual(t.calls, [])

    def test_other_purposes_are_not_limited(self):
        t = VisionTransport((GOOD, 1, 1))
        self.configure(t, budgets=self.budgets(max_calls=1, max_usd=100.0))
        self.vcall("a", purpose="intent")
        self.vcall("b", purpose="intent")
        self.vcall("c", purpose="intent")
        self.assertEqual(len(t.calls), 3)

    def test_no_budget_keeps_todays_behaviour(self):
        t = VisionTransport((GOOD, 1, 1))
        self.configure(t)
        for i in range(10):
            self.vcall(f"p{i}")
        self.assertEqual(len(t.calls), 10)

    def test_the_cap_holds_under_concurrent_calls(self):
        t = VisionTransport((GOOD, 1, 1))
        self.configure(t, budgets=self.budgets(max_calls=3, max_usd=100.0))
        results = []

        def work(i):
            try:
                self.vcall(f"p{i}")
                results.append("ok")
            except llm.LLMUnavailable:
                results.append("refused")

        threads = [threading.Thread(target=work, args=(i,)) for i in range(12)]
        [th.start() for th in threads]
        [th.join() for th in threads]
        self.assertEqual(len(t.calls), 3)
        self.assertEqual(results.count("ok"), 3)

    def test_configure_rejects_a_malformed_budget(self):
        for bad in ({"screenshot": {"max_calls": -1, "max_usd": 1.0}},
                    {"screenshot": {"max_calls": 1}},
                    {"screenshot": {"max_calls": 1, "max_usd": "x"}}):
            with self.assertRaises(ValueError):
                llm.configure(runs_dir=self.runs, budgets=bad)
        llm.reset()


class ConfigTests(Base):
    def test_opus_is_priced_with_a_source(self):
        p = config.PRICES["claude-opus-5-5"]
        self.assertEqual((p["input_per_mtok"], p["output_per_mtok"]), (4.00, 20.00))
        self.assertTrue(p["source_url"].startswith("https://"))
        self.assertTrue(p["as_of"])

    def test_default_screenshot_budget_is_the_approved_cap(self):
        self.assertEqual(config.DEFAULT_SCREENSHOT_BUDGET, {"max_calls": 40, "max_usd": 3.00})

    def test_usage_report_prices_screenshot_and_pdf_lines(self):
        from evaluation import usage_report
        usage_report._check_prices({MODEL, "claude-opus-5-5"}, config.PRICES)  # must not raise
        json.dumps(config.DEFAULT_SCREENSHOT_BUDGET)
