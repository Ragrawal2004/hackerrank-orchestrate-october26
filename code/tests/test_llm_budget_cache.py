"""Added after a mutation check: a cache hit must not use up a call slot of the cap."""
from ghost import config, llm
from tests.test_llm import GOOD, SCHEMA, Base
from tests.test_llm_vision import VisionTransport

MODEL = config.DEFAULT_SCREENSHOT_MODEL


class CacheHitsLeaveTheCapAloneTests(Base):
    def vcall(self, prompt):
        return llm.call("screenshot", MODEL, prompt, "evidence_04", SCHEMA, variant="A")

    def test_hits_do_not_use_call_slots(self):
        t = VisionTransport((GOOD, 10, 5))
        self.configure(t, budgets={"screenshot": {"max_calls": 2, "max_usd": 100.0}})
        self.vcall("first")        # real call 1
        for _ in range(3):
            self.vcall("first")    # three cache hits
        self.vcall("second")       # real call 2: still inside the cap
        self.assertEqual(len(t.calls), 2)
        with self.assertRaises(llm.LLMUnavailable):
            self.vcall("third")    # real call 3: over the cap
        self.assertEqual(len(t.calls), 2)
