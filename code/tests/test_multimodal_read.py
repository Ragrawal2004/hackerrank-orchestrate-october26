"""The reader: two independent gateway calls per file, prompt sections, PDF text path."""
import json

from ghost import multimodal_read as mr
from ghost.adapters.multimodal import NoTextLayer
from tests.mm_fixture import PDF, PNG, payload
from tests.test_llm import Base
from tests.test_llm_vision import VisionTransport

MODEL = "claude-sonnet-5-5"


class ReaderTests(Base):
    def setUp(self):
        super().setUp()
        self.reply = (json.dumps(payload("account", "account_1")), 10, 5)

    def test_an_image_is_read_twice_with_different_prompts_and_variants(self):
        t = VisionTransport(self.reply)
        self.configure(t)
        reads = mr.make_reader(MODEL)("evidence_04", "png", PNG)
        self.assertEqual(len(reads), 2)
        self.assertEqual(len(t.calls), 2)  # read B is not a cache hit of read A
        self.assertNotEqual(t.calls[0]["prompt"], t.calls[1]["prompt"])
        self.assertTrue(all(c["images"] == [("image/png", PNG)] for c in t.calls))
        self.assertEqual([u["variant"] for u in self.usage()], ["A", "B"])

    def test_the_reader_names_its_model(self):
        self.assertEqual(mr.make_reader(MODEL).model, MODEL)

    def test_a_pdf_is_read_from_its_text_without_images(self):
        t = VisionTransport(self.reply)
        self.configure(t)
        reads = mr.make_reader(MODEL, text_of=lambda data: "Statement NST-1")(
            "evidence_18", "pdf", PDF)
        self.assertEqual(len(reads), 2)
        self.assertTrue(all(c["images"] == [] and "Statement NST-1" in c["prompt"]
                            for c in t.calls))

    def test_a_pdf_without_text_raises_and_calls_nothing(self):
        t = VisionTransport(self.reply)
        self.configure(t)

        def none(data):
            raise NoTextLayer("empty")
        with self.assertRaises(NoTextLayer):
            mr.make_reader(MODEL, text_of=none)("evidence_18", "pdf", PDF)
        self.assertEqual(t.calls, [])

    def test_every_prompt_section_exists(self):
        sections = mr.load_prompts()
        for name in ("COMMON", "IMAGE_A", "IMAGE_B", "PDF_A", "PDF_B"):
            self.assertIn(name, sections)
        self.assertIn("{document_text}", sections["PDF_A"])
        self.assertIn("{document_text}", sections["PDF_B"])
