"""The committed screenshot/PDF extractions hold ids, indicators and hashes only, and still match
the files they were read from."""
import hashlib
import json
import re
import unittest

from ghost import config, loaders
from tests.helpers import DATASET
from tests.test_adapter_multimodal import raw_text_leaks

DOC_NUMBER = re.compile(r"[A-Z]{2,5}-[0-9]{4}-[0-9]{4}-[0-9]{4}")
KEYS = ["detail_reference", "detail_resource_id", "file_id", "indicators", "model",
        "schema_version", "screen_kind", "sha256", "subject_refs", "variants"]


class CommittedExtractionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.files = sorted(config.EXTRACTIONS_DIR.glob("evidence_*.json"))
        cls.manifest = {r["file_id"]: r["file_path"] for r in loaders.load_evidence_manifest(DATASET)}
        cls.identities = loaders.load_identities(DATASET)

    def test_there_is_one_extraction_per_screenshot_and_pdf(self):
        self.assertEqual(len(self.files), 15)

    def test_no_literal_email_phone_or_name_is_stored(self):
        values = {v.strip().lower() for r in self.identities
                  for v in (r["name"], r["email"], r["phone"]) if v.strip()}
        for path in self.files:
            text = path.read_text(encoding="utf-8")
            ref = json.loads(text)["detail_reference"]
            # a document number such as NST-2026-0718-1842 is digits and hyphens but not a phone
            scanned = text.replace(ref, "") if ref and DOC_NUMBER.fullmatch(ref) else text
            self.assertEqual(raw_text_leaks(scanned), set(), path.name)
            self.assertNotIn("@", text, path.name)
            low = text.lower()
            for v in values:
                self.assertNotIn(v, low, f"{path.name} holds an identity value")

    def test_only_the_minimal_fields_are_present(self):
        for path in self.files:
            self.assertEqual(sorted(json.loads(path.read_text(encoding="utf-8"))), KEYS, path.name)

    def test_each_extraction_matches_the_file_it_was_read_from(self):
        for path in self.files:
            doc = json.loads(path.read_text(encoding="utf-8"))
            data = (DATASET / self.manifest[doc["file_id"]]).read_bytes()
            self.assertEqual(doc["sha256"], hashlib.sha256(data).hexdigest(), path.name)


if __name__ == "__main__":
    unittest.main()
