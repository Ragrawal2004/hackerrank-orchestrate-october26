"""When two reads of one file count as agreeing: the label (`detail_reference`) is compared only
when it resolved the resource; kind, resource, identifiers and indicators always are."""
import json
import tempfile
import unittest
from pathlib import Path

from ghost.adapters import multimodal as mm
from tests.test_adapter_multimodal import (ANA, FILE, LINK_T, obs, run, world)
from tests.mm_fixture import payload


def alias_world():
    """Ana's export_1842 is known by the statement number NST-1 (a parenthetical alias)."""
    f = world(("account_5762", "account_service"))
    f.activity(LINK_T, "LINK_RESOURCE_IDENTITY", "data_catalog", "export_service", "export_1842",
               "SUCCESS", f"Data catalog linked export_1842 (NST-1) to {ANA} and user_1.")
    return f


class LabelOnlyDifferenceTests(unittest.TestCase):
    def test_a_label_the_reads_disagree_on_does_not_matter_when_the_id_is_printed(self):
        f = world(("account_5762", "account_service"))
        a = payload("account", "account_5762", ref="user_1", emails=[ANA])
        b = payload("account", "account_5762", ref=None, emails=[ANA])
        _, rep = run(f, a, b=b)
        self.assertEqual(rep["status"], "agreed")
        self.assertEqual(len(obs(f.conn)), 1)

    def test_two_different_labels_with_a_printed_id_still_agree(self):
        f = world(("account_5762", "account_service"))
        a = payload("account", "account_5762", ref="one", emails=[ANA])
        b = payload("account", "account_5762", ref="two", emails=[ANA])
        self.assertEqual(run(f, a, b=b)[1]["status"], "agreed")


class RealDifferencesTests(unittest.TestCase):
    def test_a_screen_kind_difference_disagrees(self):
        f = world(("account_5762", "account_service"))
        a = payload("account", "account_5762", emails=[ANA])
        b = payload("support", "account_5762", emails=[ANA])
        rep = run(f, a, b=b)[1]
        self.assertEqual(rep["status"], "disagreed")
        self.assertEqual(obs(f.conn), [])

    def test_a_resource_difference_disagrees(self):
        f = world(("account_5762", "account_service"), ("account_6000", "account_service"))
        a = payload("account", "account_5762", emails=[ANA])
        b = payload("account", "account_6000", emails=[ANA])
        self.assertEqual(run(f, a, b=b)[1]["status"], "disagreed")

    def test_an_identifier_difference_disagrees(self):
        f = world(("account_5762", "account_service"))
        a = payload("account", "account_5762", emails=[ANA])
        b = payload("account", "account_5762", emails=[])
        self.assertEqual(run(f, a, b=b)[1]["status"], "disagreed")

    def test_an_indicator_difference_disagrees(self):
        f = world(("backup_9", "backup_service"))
        a = payload("backup", "backup_9", emails=[ANA], restore="allowed")
        b = payload("backup", "backup_9", emails=[ANA], restore="blocked")
        self.assertEqual(run(f, a, b=b)[1]["status"], "disagreed")


class ResolvingReferenceTests(unittest.TestCase):
    def test_a_reference_that_resolved_the_resource_must_match_in_both_reads(self):
        f = alias_world()
        a = payload("document_statement", None, ref="NST-1", emails=[ANA])
        b = payload("document_statement", None, ref=None, emails=[ANA])  # B found no id at all
        rep = run(f, a, b=b)[1]
        self.assertEqual(rep["status"], "disagreed")

    def test_two_references_that_resolve_to_different_resources_disagree(self):
        f = alias_world()
        f.activity(LINK_T, "LINK_RESOURCE_IDENTITY", "data_catalog", "export_service",
                   "export_1843", "SUCCESS",
                   f"Data catalog linked export_1843 (NST-2) to {ANA} and user_1.")
        a = payload("document_statement", None, ref="NST-1", emails=[ANA])
        b = payload("document_statement", None, ref="NST-2", emails=[ANA])
        self.assertEqual(run(f, a, b=b)[1]["status"], "disagreed")

    def test_the_same_resolving_reference_agrees_and_is_kept(self):
        f = alias_world()
        a = payload("document_statement", None, ref="NST-1", emails=[ANA])
        rep = run(f, a)[1]
        self.assertEqual(rep["status"], "agreed")
        self.assertEqual(obs(f.conn, "export_1842")[0]["attrs"]["detail_reference"], "NST-1")


class StoredLabelTests(unittest.TestCase):
    def stored(self, pl):
        with tempfile.TemporaryDirectory() as d:
            f = world(("account_5762", "account_service"))
            run(f, pl, extractions_dir=d)
            return json.loads((Path(d) / f"{FILE}.json").read_text(encoding="utf-8"))

    def test_a_label_that_did_not_resolve_anything_is_not_stored(self):
        doc = self.stored(payload("account", "account_5762", ref="user_1", emails=[ANA]))
        self.assertIsNone(doc["detail_reference"])

    def test_the_stored_keys_are_unchanged(self):
        doc = self.stored(payload("account", "account_5762", emails=[ANA]))
        self.assertEqual(sorted(doc), ["detail_reference", "detail_resource_id", "file_id",
                                       "indicators", "model", "schema_version",
                                       "screen_kind", "sha256", "subject_refs", "variants"])
        self.assertTrue(hasattr(mm, "ingest"))
