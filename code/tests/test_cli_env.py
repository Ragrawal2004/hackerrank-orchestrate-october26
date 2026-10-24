import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from ghost import cli, config, envfile
from tests.helpers import DATASET

PROBE = "FAKE_GHOST_PROBE"  # harmless name; the files below are fake temp files only


class CliEnvFileTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        d = Path(self.tmp.name)
        self.root = d / "root"
        self.root.mkdir()
        (self.root / ".env").write_text(f"{PROBE}=from-root-env\n", encoding="utf-8")
        self.other = d / "other.env"
        self.other.write_text(f"{PROBE}=from-other-env\n", encoding="utf-8")
        self.out = d / "out.csv"
        env = mock.patch.dict(os.environ, {}, clear=True)  # restored after each test
        env.start()
        self.addCleanup(env.stop)
        fr = mock.patch.object(config, "find_repo_root", return_value=self.root)
        fr.start()
        self.addCleanup(fr.stop)

    def run_main(self):
        rc = cli.main(["--dataset", str(DATASET), "--out", str(self.out), "--cases", "case_33"])
        self.assertEqual(rc, 0)

    def test_default_path_is_dot_env_in_the_repo_root(self):
        self.run_main()
        self.assertEqual(os.environ.get(PROBE), "from-root-env")

    def test_ghost_env_file_overrides_the_default(self):
        os.environ["GHOST_ENV_FILE"] = str(self.other)
        self.run_main()
        self.assertEqual(os.environ.get(PROBE), "from-other-env")

    def test_a_real_dot_env_is_ignored_when_ghost_env_file_points_elsewhere(self):
        os.environ["GHOST_ENV_FILE"] = str(Path(self.tmp.name) / "does-not-exist.env")
        self.run_main()
        self.assertNotIn(PROBE, os.environ)

    def test_empty_ghost_env_file_falls_back_to_the_default(self):
        os.environ["GHOST_ENV_FILE"] = ""
        self.run_main()
        self.assertEqual(os.environ.get(PROBE), "from-root-env")

    def test_env_file_path_helper(self):
        self.assertEqual(envfile.env_file_path(self.root, {}), self.root / ".env")
        self.assertEqual(envfile.env_file_path(self.root, {"GHOST_ENV_FILE": "x/y.env"}), Path("x/y.env"))


if __name__ == "__main__":
    unittest.main()
