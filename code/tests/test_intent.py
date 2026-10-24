import json
import os
import re
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from ghost import intent, llm, loaders
from tests.helpers import DATASET, REPO_ROOT
from tests.test_llm import FAKE_KEY, FakeTransport

NON_DELETION_IDS = {f"case_{n}" for n in range(533, 543)}


def dnd(label, reason="r"):
    return json.dumps({"intent": label, "reason": reason})


def requests():
    return loaders.load_requests(DATASET)


def samples():
    return loaders.load_requests(DATASET, sample=True)


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.runs = Path(self.tmp.name) / "runs"
        env = mock.patch.dict(os.environ, {"ANTHROPIC_API_KEY": FAKE_KEY})
        env.start()
        self.addCleanup(env.stop)
        self.addCleanup(llm.reset)

    def configure(self, transport):
        llm.configure(runs_dir=self.runs, transport=transport, sleep=lambda s: None,
                      run_id="run-test")

    def events(self, name=None):
        p = self.runs / "diagnostics.jsonl"
        if not p.exists():
            return []
        rows = [json.loads(l) for l in p.read_text(encoding="utf-8").splitlines()]
        return [r for r in rows if name is None or r["event"] == name]

    def case(self, text, case_id="case_900"):
        return {"case_id": case_id, "request_text": text, "email": "someone@example.com"}


class PrefilterDataTests(unittest.TestCase):
    def test_exactly_the_ten_known_non_deletions_are_non_deletion(self):
        got = {r["case_id"] for r in requests()
               if intent.prefilter(r["request_text"]).label == "NON_DELETION"}
        self.assertEqual(got, NON_DELETION_IDS)

    def test_every_sample_is_deletion(self):
        for r in samples():
            self.assertEqual(intent.prefilter(r["request_text"]).label, "DELETION", r["case_id"])

    def test_negation_flag_matches_label(self):
        for r in requests():
            p = intent.prefilter(r["request_text"])
            self.assertEqual(p.negation, p.label == "NON_DELETION")
            if p.negation:
                self.assertTrue(p.matched)


class PrefilterRuleTests(unittest.TestCase):
    def label(self, text):
        return intent.prefilter(text).label

    def test_trap_sentences_inside_deletion_requests_stay_deletion(self):
        traps = [
            "A search result with a similar email belongs to someone else, so please do not treat that record as mine.",
            "You may retain backups if required, but they must not be able to restore my personal profile.",
            "Security records can stay if policy requires them.",
            "Please remove my name and contact details, but keeping an internal deleted-account reference is fine if other required records still depend on it.",
            "Some of those messages also relate to another customer, so the conversation itself may need to stay.",
            "Please keep only the reference needed for retained records and remove the rest.",
            "I’m deleting my account.",
            "I deleted my account and received the confirmation email.",
            "Can you verify that my account was deleted?",
            "Please delete my account but do not delete the audit records that policy requires.",
        ]
        for t in traps:
            self.assertEqual(self.label("Please delete my account. " + t), "DELETION", t)
            self.assertEqual(self.label(t), "DELETION", t)

    def test_unsure_is_deletion(self):
        for t in ["", "   ", "asdf qwer zxcv", "Hello", "????", "Thanks for your help."]:
            self.assertEqual(self.label(t), "DELETION", repr(t))

    def test_english_negations(self):
        for t in [
            "Please change my phone number. I am not asking for deletion.",
            "This is an information request, not a request to erase it.",
            "Do not close the account.",
            "Please don't delete anything.",
            "Please keep the account as it is.",
            "Please leave the account active.",
            "Please remove me from marketing lists only.",
            "I want to continue using the service.",
            "Can you correct it without removing any of my records?",
        ]:
            self.assertEqual(self.label(t), "NON_DELETION", t)

    def test_other_languages(self):
        non = [
            "No quiero eliminar mi cuenta, solo actualizar mi teléfono.",
            "Por favor, no elimine mi cuenta.",
            "Não quero excluir minha conta, apenas atualizar o telefone.",
            "Não exclua minha conta, por favor.",
            "Je ne veux pas supprimer mon compte, seulement changer mon numéro.",
            "Ne supprimez pas mon compte, s'il vous plaît.",
            "계정을 삭제하려는 것이 아닙니다. 전화번호만 변경해 주세요.",
            "계정 삭제를 원하지 않습니다.",
            "アカウントの削除は希望しません。電話番号を変更してください。",
            "アカウントを削除しないでください。",
        ]
        for t in non:
            self.assertEqual(self.label(t), "NON_DELETION", t)

    def test_deletion_in_every_language_stays_deletion(self):
        for t in [
            "Solicito a exclusão permanente da conta associada a este e-mail.",
            "Solicito la eliminación permanente de la cuenta asociada a este correo.",
            "Je demande la suppression définitive du compte associé à cette adresse e-mail.",
            "이 이메일 주소에 연결된 계정의 영구 삭제를 요청합니다.",
            "このメールアドレスに関連するアカウントの完全な削除を依頼します。",
        ]:
            p = intent.prefilter(t)
            self.assertEqual(p.label, "DELETION", t)
            self.assertTrue(p.deletion_vocab, t)

    def test_pure_function_of_text(self):
        t = "Do not close the account."
        self.assertEqual(intent.prefilter(t), intent.prefilter(t))


class CombineTests(unittest.TestCase):
    def pre(self, label):
        return intent.Prefilter(label, label == "NON_DELETION", ["x"] if label == "NON_DELETION" else [], False)

    def test_negation_overrides_llm_deletion(self):
        self.assertEqual(intent.combine(self.pre("NON_DELETION"), "DELETION"),
                         ("NON_DELETION", "negation_override"))

    def test_negation_is_always_labelled_override_whatever_the_llm_said(self):
        self.assertEqual(intent.combine(self.pre("NON_DELETION"), "NON_DELETION"),
                         ("NON_DELETION", "negation_override"))

    def test_llm_wins_without_negation(self):
        self.assertEqual(intent.combine(self.pre("DELETION"), "NON_DELETION"), ("NON_DELETION", "llm"))
        self.assertEqual(intent.combine(self.pre("DELETION"), "DELETION"), ("DELETION", "llm"))

    def test_no_llm_label_uses_prefilter(self):
        self.assertEqual(intent.combine(self.pre("DELETION"), None), ("DELETION", "prefilter"))
        self.assertEqual(intent.combine(self.pre("NON_DELETION"), None), ("NON_DELETION", "prefilter"))


class ClassifyTests(Base):
    def test_negation_override_logs_disagreement(self):
        t = FakeTransport((dnd("DELETION", "mentions deletion"), 10, 5))
        self.configure(t)
        r = intent.classify(self.case("Do not close the account."), "m-1", True, self.runs)
        self.assertEqual((r["label"], r["source"]), ("NON_DELETION", "negation_override"))
        ev = self.events("intent_disagreement")
        self.assertEqual(len(ev), 1)
        self.assertEqual(ev[0]["case_id"], "case_900")
        self.assertEqual(ev[0]["prefilter"], "NON_DELETION")
        self.assertEqual(ev[0]["llm"], "DELETION")
        self.assertEqual(ev[0]["winner"], "negation")
        self.assertEqual(ev[0]["llm_reason"], "mentions deletion")

    def test_llm_wins_when_no_negation_and_is_logged(self):
        self.configure(FakeTransport((dnd("NON_DELETION", "just asks for a copy"), 10, 5)))
        r = intent.classify(self.case("Send me a copy of my data."), "m-1", True, self.runs)
        self.assertEqual((r["label"], r["source"]), ("NON_DELETION", "llm"))
        ev = self.events("intent_disagreement")
        self.assertEqual([e["winner"] for e in ev], ["llm"])
        self.assertEqual(ev[0]["prefilter"], "DELETION")

    def test_agreement_logs_nothing(self):
        self.configure(FakeTransport((dnd("DELETION"), 1, 1)))
        r = intent.classify(self.case("Please delete my account."), "m-1", True, self.runs)
        self.assertEqual((r["label"], r["source"]), ("DELETION", "llm"))
        self.assertEqual(self.events("intent_disagreement"), [])

    def test_fallback_on_every_llm_failure(self):
        failures = [
            [llm.Permanent("401", kind="AuthenticationError")],
            [llm.Transient("t", kind="APITimeoutError")],
            [("not json", 3, 3)],
            [(json.dumps({"intent": "MAYBE", "reason": "x"}), 3, 3)],
            [(json.dumps({"intent": "DELETION"}), 3, 3)],  # reason missing
        ]
        for script in failures:
            self.configure(FakeTransport(*script))
            for text, want in [("Please delete my account.", "DELETION"),
                               ("Do not close the account.", "NON_DELETION")]:
                r = intent.classify(self.case(text), "m-1", True, self.runs)
                self.assertEqual((r["label"], r["source"]), (want, "prefilter"), (script, text))
                self.assertIsNone(r["llm"])
        self.assertEqual(self.events("intent_disagreement"), [])

    def test_no_key_falls_back_without_a_network_call(self):
        t = FakeTransport((dnd("DELETION"), 1, 1))
        self.configure(t)  # a transport is wired in, so only the intent code can stop the call
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("ANTHROPIC_API_KEY", None)
            r = intent.classify(self.case("Do not close the account."), "m-1", True, self.runs)
            r2 = intent.classify(self.case("Please delete my account."), "m-1", True, self.runs)
        self.assertEqual(t.calls, [])
        self.assertFalse((self.runs / "usage.jsonl").exists())
        self.assertEqual((r["label"], r["source"], r["llm"]), ("NON_DELETION", "prefilter", None))
        self.assertEqual((r2["label"], r2["source"], r2["llm"]), ("DELETION", "prefilter", None))

    def test_llm_not_configured_falls_back(self):
        llm.reset()
        r = intent.classify(self.case("Please delete my account."), "m-1", True, self.runs)
        self.assertEqual((r["label"], r["source"]), ("DELETION", "prefilter"))

    def test_no_llm_flag_never_calls(self):
        t = FakeTransport((dnd("DELETION"), 1, 1))
        self.configure(t)
        r = intent.classify(self.case("Do not close the account."), "m-1", False, self.runs)
        self.assertEqual((r["label"], r["source"]), ("NON_DELETION", "prefilter"))
        self.assertEqual(t.calls, [])

    def test_prompt_has_text_but_not_email_and_uses_intent_purpose(self):
        t = FakeTransport((dnd("DELETION"), 12, 4))
        self.configure(t)
        intent.classify(self.case("Please delete {my} account. 私は削除を依頼します。"),
                        "model-x", True, self.runs)
        self.assertEqual(len(t.calls), 1)
        self.assertEqual(t.calls[0]["model"], "model-x")
        self.assertIn("Please delete {my} account. 私は削除を依頼します。", t.calls[0]["prompt"])
        self.assertNotIn("someone@example.com", t.calls[0]["prompt"])
        self.assertNotIn("{request_text}", t.calls[0]["prompt"])
        usage = [json.loads(l) for l in (self.runs / "usage.jsonl").read_text().splitlines()]
        self.assertEqual([(u["purpose"], u["case_id"]) for u in usage], [("intent", "case_900")])

    def _oracle(self):
        by_text = {r["request_text"]: r["case_id"] for r in requests()}

        def oracle(model, prompt, max_tokens, timeout):
            hit = [cid for text, cid in by_text.items() if text in prompt]
            nd = bool(hit) and hit[0] in NON_DELETION_IDS
            return (dnd("NON_DELETION" if nd else "DELETION", "fake"), 5, 2)
        return oracle

    def test_whole_run_518_has_exactly_ten_non_deletions(self):
        self.configure(self._oracle())
        res = intent.classify_all(requests(), "m-1", True, self.runs)
        self.assertEqual(len(res), 518)
        self.assertEqual(len({r["case_id"] for r in res}), 518)
        nd = [r["case_id"] for r in res if r["label"] == "NON_DELETION"]
        self.assertEqual(len(nd), 10)
        self.assertEqual(set(nd), NON_DELETION_IDS)
        for r in res:
            if r["case_id"] in NON_DELETION_IDS:
                self.assertEqual((r["prefilter"], r["llm"]), ("NON_DELETION", "NON_DELETION"))
                self.assertEqual(r["source"], "negation_override")  # negation fired => override
            else:
                self.assertEqual((r["prefilter"], r["llm"], r["source"]),
                                 ("DELETION", "DELETION", "llm"))
        rep = intent.report(res)
        self.assertEqual(rep["non_deletion_by_source"],
                         {"negation_override": 10, "llm": 0, "prefilter": 0})
        self.assertEqual(rep["negation_overrides"], 10)
        self.assertEqual(rep["llm_only_non_deletion"], [])
        self.assertEqual(self.events("intent_disagreement"), [])

    def test_the_32_samples_are_all_deletion(self):
        self.configure(self._oracle())
        res = intent.classify_all(samples(), "m-1", True, self.runs)
        self.assertEqual(len(res), 32)
        self.assertTrue(all(r["label"] == "DELETION" for r in res))

    def test_whole_run_without_llm_gives_the_same_ten_from_the_prefilter(self):
        res = intent.classify_all(requests(), "m-1", False, self.runs)
        nd = {r["case_id"] for r in res if r["label"] == "NON_DELETION"}
        self.assertEqual(nd, NON_DELETION_IDS)
        self.assertEqual({r["source"] for r in res}, {"prefilter"})
        self.assertEqual(intent.report(res)["non_deletion_by_source"],
                         {"negation_override": 0, "llm": 0, "prefilter": 10})

    def test_classify_all_sorts_results_by_numeric_case_id(self):
        self.configure(FakeTransport((dnd("DELETION"), 1, 1)))
        cs = [self.case("Please delete my account.", f"case_{n}") for n in (100, 5, 33, 4)]
        res = intent.classify_all(cs, "m-1", True, self.runs, workers=1)
        self.assertEqual([r["case_id"] for r in res], ["case_4", "case_5", "case_33", "case_100"])

    def _mixed_cases(self):
        """Cases that produce all three kinds of event: agree, negation disagree, llm disagree."""
        cs = []
        for i, r in enumerate(requests()[:60] + requests()[-20:]):
            cs.append(r)
        for n, t in enumerate(["Please send me a copy of my data.", "Can I get an export?"]):
            cs.append(self.case(t, f"case_9{n}"))
        return cs

    @staticmethod
    def _mixed_transport(model, prompt, max_tokens, timeout):
        # Thread-safe pure function of the prompt. Says NON_DELETION for "copy/export" requests
        # (LLM-wins disagreement) and DELETION for everything else (so the 10 negation cases
        # disagree and are overridden).
        nd = ("copy of my data" in prompt) or ("an export" in prompt)
        return (dnd("NON_DELETION" if nd else "DELETION", "fake reason"), 7, 3)

    def test_output_is_identical_with_one_worker_and_eight(self):
        outs = {}
        for workers in (1, 8):
            runs = Path(self.tmp.name) / f"runs{workers}"
            llm.configure(runs_dir=runs, transport=self._mixed_transport, sleep=lambda s: None,
                          run_id="run-test")
            res = intent.classify_all(self._mixed_cases(), "m-1", True, runs, workers=workers)
            diag = (runs / "diagnostics.jsonl").read_text(encoding="utf-8")
            usage = [json.loads(l) for l in (runs / "usage.jsonl").read_text().splitlines()]
            outs[workers] = (res, diag, usage)
        self.assertEqual(outs[1][0], outs[8][0])
        self.assertEqual(outs[1][1], outs[8][1])          # byte-identical diagnostics
        self.assertGreater(len(outs[1][1].splitlines()), 0)
        ids = [r["case_id"] for r in outs[8][0]]
        self.assertEqual(ids, sorted(ids, key=loaders.case_sort_key))
        # nothing lost or torn under 8 workers: one parseable usage line per case
        self.assertEqual(len(outs[8][2]), len(ids))
        self.assertEqual(sorted(u["case_id"] for u in outs[8][2]),
                         sorted(ids))

    def test_default_worker_count_is_eight(self):
        import inspect
        self.assertEqual(inspect.signature(intent.classify_all).parameters["workers"].default, 8)


class ReportTests(unittest.TestCase):
    def res(self, cid, label, source, pre, llm_label, reason="r"):
        return {"case_id": cid, "label": label, "source": source, "prefilter": pre,
                "llm": llm_label, "llm_reason": reason}

    def rows(self):
        return [
            self.res("case_1", "DELETION", "llm", "DELETION", "DELETION"),
            self.res("case_2", "NON_DELETION", "negation_override", "NON_DELETION", "DELETION"),
            self.res("case_3", "NON_DELETION", "llm", "DELETION", "NON_DELETION", "asks for copy"),
            self.res("case_4", "NON_DELETION", "negation_override", "NON_DELETION", "NON_DELETION"),
            self.res("case_5", "DELETION", "prefilter", "DELETION", None, None),
            self.res("case_6", "NON_DELETION", "prefilter", "NON_DELETION", None, None),
        ]

    def test_counts(self):
        rep = intent.report(self.rows())
        self.assertEqual(rep["total"], 6)
        self.assertEqual(rep["llm_labelled"], 4)
        self.assertEqual(rep["fallback"], 2)
        self.assertEqual(rep["agreement_rate"], 0.5)
        self.assertEqual(rep["negation_overrides"], 2)
        self.assertEqual(rep["disagreements"], ["case_2", "case_3"])
        self.assertEqual(rep["non_deletion_by_source"],
                         {"negation_override": 2, "llm": 1, "prefilter": 1})

    def test_llm_only_non_deletion_lists_cases_without_negation(self):
        rep = intent.report(self.rows())
        self.assertEqual(rep["llm_only_non_deletion"], ["case_3"])

    def test_text_leads_with_llm_only_non_deletion_then_the_rest(self):
        text = intent.format_report(intent.report(self.rows()))
        low = text.lower()
        head = low.index("without prefilter negation")
        self.assertLess(head, low.index("agreement"))
        self.assertLess(head, low.index("negation override"))
        self.assertLess(head, low.index("by source"))
        self.assertLess(text.index("case_3"), low.index("agreement"))
        for needle in ("50.0%", "case_2", "negation_override", "prefilter"):
            self.assertIn(needle, text)

    def test_text_says_none_when_no_llm_only_non_deletion(self):
        rows = [self.res("case_1", "DELETION", "llm", "DELETION", "DELETION")]
        text = intent.format_report(intent.report(rows))
        self.assertLess(text.lower().index("without prefilter negation"), text.lower().index("agreement"))
        self.assertIn("none", text.lower())

    def test_no_llm_labels_gives_no_rate(self):
        rep = intent.report([self.res("case_1", "DELETION", "prefilter", "DELETION", None, None)])
        self.assertIsNone(rep["agreement_rate"])
        self.assertEqual(rep["disagreements"], [])
        self.assertIn("n/a", intent.format_report(rep))


class StructureTests(unittest.TestCase):
    def test_intent_module_never_imports_the_sdk(self):
        src = (REPO_ROOT / "code" / "ghost" / "intent.py").read_text(encoding="utf-8")
        self.assertIsNone(re.search(r"^\s*(import|from)\s+anthropic\b", src, re.M))

    def test_prompt_file_exists_with_placeholder(self):
        p = REPO_ROOT / "code" / "prompts" / "intent.md"
        self.assertIn("{request_text}", p.read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
