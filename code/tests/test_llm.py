import inspect
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from ghost import config, llm

FAKE_KEY = "sk-ant-api03-FAKEKEYFORTESTS-0123456789"
SCHEMA = {"type": "object", "required": ["intent"],
          "properties": {"intent": {"type": "string", "enum": ["DELETION", "NON_DELETION"]}},
          "additionalProperties": False}
GOOD = json.dumps({"intent": "DELETION"})


class FakeTransport:
    """Scripted stand-in for the SDK transport. Items: (text, in_tok, out_tok) or an exception."""

    def __init__(self, *script):
        self.script = list(script)
        self.calls = []

    def __call__(self, model, prompt, max_tokens, timeout):
        self.calls.append({"model": model, "prompt": prompt, "max_tokens": max_tokens,
                           "timeout": timeout})
        item = self.script.pop(0) if len(self.script) > 1 else self.script[0]
        if isinstance(item, Exception):
            raise item
        return item


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.runs = Path(self.tmp.name) / "runs"
        env = mock.patch.dict(os.environ, {"ANTHROPIC_API_KEY": FAKE_KEY})
        env.start()
        self.addCleanup(env.stop)
        self.sleeps = []

    def configure(self, transport, cold=False, **kw):
        llm.configure(runs_dir=self.runs, cold=cold, transport=transport,
                      sleep=self.sleeps.append, run_id="run-test", **kw)
        self.addCleanup(llm.reset)

    def call(self, prompt="classify this", model="m-1", case_id="case_33", purpose="intent",
             schema=SCHEMA):
        return llm.call(purpose, model, prompt, case_id, schema)

    def usage(self):
        p = self.runs / "usage.jsonl"
        if not p.exists():
            return []
        return [json.loads(l) for l in p.read_text(encoding="utf-8").splitlines()]

    def all_text(self):
        """Every byte written under runs/, for leak checks."""
        return "".join(p.read_text(encoding="utf-8") for p in self.runs.rglob("*") if p.is_file())


class CallTests(Base):
    def test_returns_parsed_validated_object(self):
        t = FakeTransport((GOOD, 11, 5))
        self.configure(t)
        self.assertEqual(self.call(), {"intent": "DELETION"})

    def test_no_temperature_is_passed_to_the_transport(self):
        t = FakeTransport((GOOD, 1, 1))
        self.configure(t)
        self.call()
        self.assertNotIn("temperature", t.calls[0])
        self.assertFalse(hasattr(llm, "TEMPERATURE"))
        self.assertNotIn("temperature", inspect.signature(llm.make_sdk_transport(
            client_factory=lambda **kw: StubSdkClient(None))).parameters)
        self.assertEqual(t.calls[0]["model"], "m-1")
        self.assertEqual(t.calls[0]["prompt"], "classify this")

    def test_accepts_fenced_json(self):
        self.configure(FakeTransport(("```json\n" + GOOD + "\n```", 1, 1)))
        self.assertEqual(self.call(), {"intent": "DELETION"})

    def test_prose_around_json_is_a_bad_response(self):
        self.configure(FakeTransport(("Sure! " + GOOD, 1, 1)))
        with self.assertRaises(llm.LLMUnavailable):
            self.call()

    def test_schema_violation_raises_unavailable(self):
        self.configure(FakeTransport((json.dumps({"intent": "MAYBE"}), 7, 3)))
        with self.assertRaises(llm.LLMUnavailable):
            self.call()

    def test_not_json_raises_unavailable(self):
        self.configure(FakeTransport(("hello", 7, 3)))
        with self.assertRaises(llm.LLMUnavailable):
            self.call()

    def test_invalid_schema_raises_before_any_api_call(self):
        t = FakeTransport((GOOD, 1, 1))
        self.configure(t)
        with self.assertRaises(ValueError):
            self.call(schema={"type": "object", "pattern": "x"})
        self.assertEqual(t.calls, [])
        self.assertEqual(self.usage(), [])

    def test_call_before_configure_is_unavailable_not_a_crash(self):
        llm.reset()
        with self.assertRaises(llm.LLMUnavailable):
            self.call()

    def test_only_unavailable_escapes(self):
        for exc in (llm.Transient("t"), llm.Permanent("p"), ValueError("v-from-transport"),
                    RuntimeError("r")):
            with self.subTest(exc=type(exc).__name__):
                self.configure(FakeTransport(exc), max_attempts=1)
                with self.assertRaises(llm.LLMUnavailable):
                    self.call(prompt="p-" + type(exc).__name__)


class CacheTests(Base):
    def test_second_identical_call_is_served_from_cache(self):
        t = FakeTransport((GOOD, 11, 5))
        self.configure(t)
        a = self.call()
        b = self.call()
        self.assertEqual(a, b)
        self.assertEqual(len(t.calls), 1)

    def test_cache_hit_logs_zero_tokens_and_flag(self):
        self.configure(FakeTransport((GOOD, 11, 5)))
        self.call()
        self.call()
        first, second = self.usage()
        self.assertFalse(first["cache_hit"])
        self.assertEqual((first["input_tokens"], first["output_tokens"]), (11, 5))
        self.assertTrue(second["cache_hit"])
        self.assertEqual((second["input_tokens"], second["output_tokens"]), (0, 0))

    def test_cache_lives_in_runs_cache(self):
        self.configure(FakeTransport((GOOD, 1, 1)))
        self.call()
        self.assertEqual(len(list((self.runs / "cache").glob("*.json"))), 1)

    def test_key_depends_on_model_prompt_and_params(self):
        base = dict(model="m", prompt="p", params={"max_tokens": 10, "schema": SCHEMA})
        k = llm.cache_key(**base)
        self.assertEqual(k, llm.cache_key(**base))
        self.assertNotEqual(k, llm.cache_key(**{**base, "model": "m2"}))
        self.assertNotEqual(k, llm.cache_key(**{**base, "prompt": "p2"}))
        self.assertNotEqual(k, llm.cache_key(**{**base, "params": {**base["params"], "max_tokens": 11}}))
        self.assertNotEqual(k, llm.cache_key(**{**base, "params": {**base["params"], "schema": {"type": "object"}}}))

    def test_different_model_or_prompt_misses(self):
        t = FakeTransport((GOOD, 1, 1))
        self.configure(t)
        self.call()
        self.call(model="m-2")
        self.call(prompt="another")
        self.assertEqual(len(t.calls), 3)

    def test_real_key_is_over_model_prompt_max_tokens_and_schema_only(self):
        self.configure(FakeTransport((GOOD, 1, 1)), max_tokens=321)
        self.call()
        expected = llm.cache_key("m-1", "classify this", {"max_tokens": 321, "schema": SCHEMA})
        self.assertEqual([p.stem for p in (self.runs / "cache").glob("*.json")], [expected])

    def test_case_id_and_purpose_do_not_affect_the_key(self):
        t = FakeTransport((GOOD, 1, 1))
        self.configure(t)
        self.call(case_id="case_33", purpose="intent")
        self.call(case_id="case_34", purpose="category")
        self.assertEqual(len(t.calls), 1)
        self.assertEqual([u["case_id"] for u in self.usage()], ["case_33", "case_34"])

    def test_corrupt_cache_entry_is_a_miss(self):
        t = FakeTransport((GOOD, 1, 1))
        self.configure(t)
        self.call()
        entry = next((self.runs / "cache").glob("*.json"))
        entry.write_text("{not json", encoding="utf-8")
        self.call()
        self.assertEqual(len(t.calls), 2)

    def test_cached_entry_failing_the_schema_is_a_miss(self):
        t = FakeTransport((GOOD, 1, 1))
        self.configure(t)
        self.call()
        entry = next((self.runs / "cache").glob("*.json"))
        entry.write_text(json.dumps({"text": json.dumps({"intent": "WRONG"})}), encoding="utf-8")
        self.assertEqual(self.call(), {"intent": "DELETION"})
        self.assertEqual(len(t.calls), 2)

    def test_failed_calls_are_not_cached(self):
        t = FakeTransport(("garbage", 4, 2), (GOOD, 4, 2))
        self.configure(t)
        with self.assertRaises(llm.LLMUnavailable):
            self.call()
        self.assertEqual(self.call(), {"intent": "DELETION"})
        self.assertEqual(len(t.calls), 2)


class ColdTests(Base):
    def test_cold_ignores_the_cache(self):
        t = FakeTransport((GOOD, 11, 5))
        self.configure(t)
        self.call()
        self.configure(t, cold=True)
        self.call()
        self.assertEqual(len(t.calls), 2)
        self.assertFalse(self.usage()[-1]["cache_hit"])
        self.assertEqual(self.usage()[-1]["input_tokens"], 11)

    def test_cold_does_not_reuse_within_the_run(self):
        t = FakeTransport((GOOD, 11, 5))
        self.configure(t, cold=True)
        self.call()
        self.call()
        self.assertEqual(len(t.calls), 2)
        self.assertTrue(all(not u["cache_hit"] for u in self.usage()))

    def test_cold_refreshes_the_cache_for_later_warm_runs(self):
        t = FakeTransport((GOOD, 1, 1))
        self.configure(t, cold=True)
        self.call()
        self.configure(t, cold=False)
        self.call()
        self.assertEqual(len(t.calls), 1)
        self.assertTrue(self.usage()[-1]["cache_hit"])


class UsageTests(Base):
    def test_line_fields_and_real_token_counts(self):
        self.configure(FakeTransport((GOOD, 123, 45)))
        self.call(purpose="intent", model="m-1", case_id="case_99")
        (u,) = self.usage()
        self.assertEqual(
            set(u), {"ts", "purpose", "model", "case_id", "input_tokens", "output_tokens",
                     "cache_hit", "error", "run_id"})
        self.assertEqual((u["purpose"], u["model"], u["case_id"]), ("intent", "m-1", "case_99"))
        self.assertEqual((u["input_tokens"], u["output_tokens"]), (123, 45))
        self.assertIsNone(u["error"])
        self.assertEqual(u["run_id"], "run-test")
        self.assertRegex(u["ts"], r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d")

    def test_input_and_output_are_not_swapped(self):
        self.configure(FakeTransport((GOOD, 1000, 7)))
        self.call()
        (u,) = self.usage()
        self.assertGreater(u["input_tokens"], u["output_tokens"])

    def test_bad_response_still_logs_the_tokens_it_cost(self):
        self.configure(FakeTransport(("not json", 321, 9)))
        with self.assertRaises(llm.LLMUnavailable):
            self.call()
        (u,) = self.usage()
        self.assertEqual((u["input_tokens"], u["output_tokens"]), (321, 9))
        self.assertEqual(u["error"], "BadResponse")
        self.assertFalse(u["cache_hit"])

    def test_bad_response_logs_the_failure_kind_and_never_the_reply(self):
        # T19 item 2: a rejected reply may echo personal data, so only its kind is recorded.
        marker = "REPLY-MARKER-4471"
        replies = {
            "JSONDecodeError": f"Sure! {marker} {{not json",
            "SchemaError": json.dumps({"intent": marker}),  # wrong type of value echoed by the validator
        }
        for kind, text in replies.items():
            self.configure(FakeTransport((text, 11, 7)))
            with self.assertRaises(llm.LLMUnavailable) as cm:
                self.call()
            self.assertIn(kind, str(cm.exception))
            self.assertNotIn(marker, str(cm.exception))
            u = self.usage()[-1]
            self.assertEqual((u["error"], u["bad_response_kind"]), ("BadResponse", kind))
            self.assertNotIn(marker, json.dumps(u))

    def test_no_response_logs_zero_tokens_and_the_class_name_only(self):
        self.configure(FakeTransport(llm.Permanent("secret-detail-xyz", kind="AuthenticationError")))
        with self.assertRaises(llm.LLMUnavailable):
            self.call()
        (u,) = self.usage()
        self.assertEqual((u["input_tokens"], u["output_tokens"]), (0, 0))
        self.assertEqual(u["error"], "AuthenticationError")
        self.assertNotIn("secret-detail-xyz", json.dumps(u))

    def test_one_line_per_attempt(self):
        t = FakeTransport(llm.Transient("x", kind="APITimeoutError"),
                          llm.Transient("x", kind="APITimeoutError"), (GOOD, 8, 2))
        self.configure(t)
        self.call()
        us = self.usage()
        self.assertEqual([u["error"] for u in us], ["APITimeoutError", "APITimeoutError", None])
        self.assertEqual([u["input_tokens"] for u in us], [0, 0, 8])

    def test_appends_across_configures(self):
        self.configure(FakeTransport((GOOD, 1, 1)))
        self.call(prompt="a")
        self.configure(FakeTransport((GOOD, 1, 1)))
        self.call(prompt="b")
        self.assertEqual(len(self.usage()), 2)


class RetryTests(Base):
    def test_transient_twice_then_success_with_backoff(self):
        t = FakeTransport(llm.Transient("a"), llm.Transient("b"), (GOOD, 1, 1))
        self.configure(t)
        self.assertEqual(self.call(), {"intent": "DELETION"})
        self.assertEqual(len(t.calls), 3)
        self.assertEqual(self.sleeps, [1.0, 2.0])

    def test_exhausted_retries_raise_unavailable(self):
        t = FakeTransport(llm.Transient("down"))
        self.configure(t)
        with self.assertRaises(llm.LLMUnavailable):
            self.call()
        self.assertEqual(len(t.calls), 3)
        self.assertEqual(self.sleeps, [1.0, 2.0])  # no sleep after the last attempt

    def test_max_attempts_is_configurable(self):
        t = FakeTransport(llm.Transient("down"))
        self.configure(t, max_attempts=2)
        with self.assertRaises(llm.LLMUnavailable):
            self.call()
        self.assertEqual(len(t.calls), 2)

    def test_permanent_error_is_not_retried(self):
        t = FakeTransport(llm.Permanent("bad request"))
        self.configure(t)
        with self.assertRaises(llm.LLMUnavailable):
            self.call()
        self.assertEqual(len(t.calls), 1)
        self.assertEqual(self.sleeps, [])

    def test_bad_response_is_not_retried(self):
        t = FakeTransport(("nope", 1, 1))
        self.configure(t)
        with self.assertRaises(llm.LLMUnavailable):
            self.call()
        self.assertEqual(len(t.calls), 1)

    def test_max_tokens_is_always_a_positive_int(self):
        for kw in ({}, {"max_tokens": None}, {"max_tokens": 0}):
            with self.subTest(kw=kw):
                t = FakeTransport((GOOD, 1, 1))
                self.configure(t, **kw)
                self.call(prompt=f"p{kw}")
                mt = t.calls[0]["max_tokens"]
                self.assertIsInstance(mt, int)
                self.assertGreater(mt, 0)
                self.assertEqual(mt, config.LLM_MAX_TOKENS)

    def test_invalid_max_tokens_is_rejected_at_configure(self):
        for bad in (-1, 1.5, "100", True):
            with self.subTest(bad=bad):
                with self.assertRaises(ValueError):
                    llm.configure(runs_dir=self.runs, max_tokens=bad)
        llm.reset()

    def test_timeout_is_passed_to_the_transport(self):
        t = FakeTransport((GOOD, 1, 1))
        self.configure(t, timeout=12.5, max_tokens=77)
        self.call()
        self.assertEqual(t.calls[0]["timeout"], 12.5)
        self.assertEqual(t.calls[0]["max_tokens"], 77)


class RedactionTests(Base):
    def test_redact_removes_live_key_and_key_pattern(self):
        out = llm.redact(f"boom {FAKE_KEY} and sk-ant-other-KEY_123 end")
        self.assertNotIn(FAKE_KEY, out)
        self.assertNotIn("sk-ant-other", out)
        self.assertIn("[REDACTED]", out)

    def test_redact_removes_a_non_standard_key_value(self):
        with mock.patch.dict(os.environ, {"ANTHROPIC_API_KEY": "plainsecretvalue99"}):
            self.assertNotIn("plainsecretvalue99", llm.redact("x plainsecretvalue99 y"))

    def test_error_text_cache_and_usage_never_contain_the_key(self):
        leak = f"401 invalid x-api-key {FAKE_KEY}"
        for exc in (llm.Transient(leak), llm.Permanent(leak), RuntimeError(leak)):
            with self.subTest(exc=type(exc).__name__):
                self.configure(FakeTransport(exc), max_attempts=2)
                with self.assertRaises(llm.LLMUnavailable) as cm:
                    self.call(prompt="p-" + type(exc).__name__)
                self.assertNotIn(FAKE_KEY, str(cm.exception))
                self.assertNotIn(FAKE_KEY, repr(cm.exception))
                self.assertIsNone(cm.exception.__cause__)
                self.assertTrue(cm.exception.__suppress_context__)
        self.assertNotIn(FAKE_KEY, self.all_text())

    def test_bad_response_text_containing_the_key_is_not_leaked(self):
        self.configure(FakeTransport((f"here is the key {FAKE_KEY}", 3, 3)))
        with self.assertRaises(llm.LLMUnavailable) as cm:
            self.call()
        self.assertNotIn(FAKE_KEY, str(cm.exception))
        self.assertNotIn(FAKE_KEY, self.all_text())

    def test_return_value_does_not_carry_the_key(self):
        self.configure(FakeTransport((GOOD, 1, 1)))
        self.assertNotIn(FAKE_KEY, json.dumps(self.call()))
        self.assertNotIn(FAKE_KEY, self.all_text())


class StubSdkResponse:
    def __init__(self, text, i, o):
        block = type("Block", (), {"type": "text", "text": text})()
        self.content = [block]
        self.usage = type("Usage", (), {"input_tokens": i, "output_tokens": o})()


class StubSdkClient:
    def __init__(self, result):
        self.result = result
        self.kwargs = None
        self.messages = self

    def create(self, **kwargs):
        self.kwargs = kwargs
        if isinstance(self.result, Exception):
            raise self.result
        return self.result


class SdkTransportTests(Base):
    """The real transport, driven by a stub shaped like the SDK. No SDK import, no network."""

    def transport(self, result):
        client = StubSdkClient(result)
        return llm.make_sdk_transport(client_factory=lambda **kw: client), client

    def test_maps_response_text_and_usage(self):
        send, client = self.transport(StubSdkResponse(GOOD, 31, 4))
        self.assertEqual(send("m-1", "hi", 50, 9.0), (GOOD, 31, 4))
        self.assertEqual(client.kwargs["model"], "m-1")
        self.assertNotIn("temperature", client.kwargs)
        self.assertNotIn("extra_body", client.kwargs)
        self.assertEqual(client.kwargs["max_tokens"], 50)
        self.assertIsInstance(client.kwargs["max_tokens"], int)
        self.assertEqual(client.kwargs["messages"], [{"role": "user", "content": "hi"}])

    def _err(self, name, status=None):
        e = type(name, (Exception,), {})("detail")
        if status is not None:
            e.status_code = status
        return e

    def test_timeout_connection_429_and_5xx_are_transient(self):
        for name, status in (("APITimeoutError", None), ("APIConnectionError", None),
                             ("RateLimitError", 429), ("InternalServerError", 500),
                             ("APIStatusError", 529)):
            with self.subTest(name=name):
                send, _ = self.transport(self._err(name, status))
                with self.assertRaises(llm.Transient) as cm:
                    send("m", "p", 10, 1.0)
                self.assertEqual(cm.exception.kind, name)

    def test_auth_and_bad_request_are_permanent(self):
        for name, status in (("AuthenticationError", 401), ("BadRequestError", 400),
                             ("NotFoundError", 404)):
            with self.subTest(name=name):
                send, _ = self.transport(self._err(name, status))
                with self.assertRaises(llm.Permanent):
                    send("m", "p", 10, 1.0)

    def test_unknown_exception_is_permanent(self):
        send, _ = self.transport(RuntimeError("weird"))
        with self.assertRaises(llm.Permanent):
            send("m", "p", 10, 1.0)

    def test_sdk_error_text_is_redacted_when_surfaced(self):
        send, _ = self.transport(self._err("AuthenticationError", 401))
        e = self._err("AuthenticationError", 401)
        e.args = (f"bad key {FAKE_KEY}",)
        send, _ = self.transport(e)
        self.configure(send)
        with self.assertRaises(llm.LLMUnavailable) as cm:
            self.call()
        self.assertNotIn(FAKE_KEY, str(cm.exception))
        self.assertNotIn(FAKE_KEY, self.all_text())

    def test_missing_key_is_unavailable(self):
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("ANTHROPIC_API_KEY", None)
            with self.assertRaises(llm.LLMUnavailable):
                llm.make_sdk_transport(client_factory=lambda **kw: StubSdkClient(None))

    def test_network_is_forbidden_under_tests(self):
        # tests/__init__.py sets GHOST_FORBID_NETWORK; the real client can never be built here
        with self.assertRaises(llm.LLMUnavailable):
            llm.make_sdk_transport()

    def test_default_transport_under_tests_is_unavailable_not_a_network_call(self):
        llm.configure(runs_dir=self.runs, cold=False, sleep=self.sleeps.append)
        self.addCleanup(llm.reset)
        with self.assertRaises(llm.LLMUnavailable):
            self.call()


if __name__ == "__main__":
    unittest.main()
