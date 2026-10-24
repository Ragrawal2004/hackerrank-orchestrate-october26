import re
import unittest

from tests.helpers import REPO_ROOT

CODE = REPO_ROOT / "code"


def py_files(*parts):
    return sorted((CODE.joinpath(*parts)).rglob("*.py"))


class SdkGatewayTests(unittest.TestCase):
    def test_only_llm_py_imports_the_sdk(self):
        pat = re.compile(r"^\s*(import|from)\s+anthropic\b", re.M)
        offenders = [str(p.relative_to(REPO_ROOT)) for p in py_files()
                     if p.name != "llm.py" and p.name != "test_invariants.py"
                     and pat.search(p.read_text(encoding="utf-8"))]
        self.assertEqual(offenders, [])

    def test_nothing_else_talks_to_the_api_host(self):
        offenders = [str(p.relative_to(REPO_ROOT)) for p in py_files()
                     if p.name not in ("llm.py", "test_invariants.py")
                     and "api.anthropic.com" in p.read_text(encoding="utf-8")]
        self.assertEqual(offenders, [])

    def test_nothing_imports_the_smoke_script(self):
        pat = re.compile(r"smoke_llm")
        offenders = [str(p.relative_to(REPO_ROOT)) for p in py_files()
                     if p.name not in ("smoke_llm.py", "test_invariants.py")
                     and pat.search(p.read_text(encoding="utf-8"))]
        self.assertEqual(offenders, [])

    def test_the_sdk_import_in_llm_py_is_lazy(self):
        src = (CODE / "ghost" / "llm.py").read_text(encoding="utf-8")
        top_level = [l for l in src.splitlines() if re.match(r"(import|from)\s+anthropic\b", l)]
        self.assertEqual(top_level, [])


if __name__ == "__main__":
    unittest.main()
