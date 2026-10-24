import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from ghost import envfile


class EnvfileTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / ".env"
        p = mock.patch.dict(os.environ, {}, clear=True)
        p.start()
        self.addCleanup(p.stop)
        self.addCleanup(self.tmp.cleanup)

    def write(self, text):
        self.path.write_text(text, encoding="utf-8")

    def test_sets_unset_keys(self):
        self.write("FAKE_A=one\nFAKE_B=two\n")
        names = envfile.load_dotenv(self.path)
        self.assertEqual(os.environ["FAKE_A"], "one")
        self.assertEqual(os.environ["FAKE_B"], "two")
        self.assertEqual(sorted(names), ["FAKE_A", "FAKE_B"])

    def test_does_not_override_existing(self):
        os.environ["FAKE_A"] = "from-shell"
        self.write("FAKE_A=from-file\n")
        names = envfile.load_dotenv(self.path)
        self.assertEqual(os.environ["FAKE_A"], "from-shell")
        self.assertEqual(names, [])

    def test_existing_but_empty_counts_as_unset(self):
        os.environ["FAKE_A"] = ""
        self.write("FAKE_A=from-file\n")
        envfile.load_dotenv(self.path)
        self.assertEqual(os.environ["FAKE_A"], "from-file")

    def test_empty_file_values_are_skipped(self):
        self.write("FAKE_A=\nFAKE_B=\"\"\n")
        names = envfile.load_dotenv(self.path)
        self.assertNotIn("FAKE_A", os.environ)
        self.assertNotIn("FAKE_B", os.environ)
        self.assertEqual(names, [])

    def test_comments_blank_lines_export_and_quotes(self):
        self.write("# a comment\n\nexport FAKE_A=one\nFAKE_B='two words'\n"
                   "FAKE_C=\"three\"\nFAKE_D=four # trailing\n  FAKE_E = five  \n")
        envfile.load_dotenv(self.path)
        self.assertEqual(os.environ["FAKE_A"], "one")
        self.assertEqual(os.environ["FAKE_B"], "two words")
        self.assertEqual(os.environ["FAKE_C"], "three")
        self.assertEqual(os.environ["FAKE_D"], "four")
        self.assertEqual(os.environ["FAKE_E"], "five")

    def test_value_may_contain_equals(self):
        self.write("FAKE_A=a=b==\n")
        envfile.load_dotenv(self.path)
        self.assertEqual(os.environ["FAKE_A"], "a=b==")

    def test_garbage_lines_are_ignored(self):
        self.write("not a pair\n=novalue\nFAKE_A=ok\n")
        self.assertEqual(envfile.load_dotenv(self.path), ["FAKE_A"])

    def test_missing_file_is_a_noop(self):
        self.assertEqual(envfile.load_dotenv(Path(self.tmp.name) / "nope"), [])

    def test_never_prints_values(self):
        self.write("FAKE_A=very-secret-value\n")
        with mock.patch("sys.stdout") as out, mock.patch("sys.stderr") as err:
            names = envfile.load_dotenv(self.path)
        out.write.assert_not_called()
        err.write.assert_not_called()
        self.assertNotIn("very-secret-value", repr(names))


if __name__ == "__main__":
    unittest.main()
