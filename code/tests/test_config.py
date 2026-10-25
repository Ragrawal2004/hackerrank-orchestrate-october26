import unittest

from ghost import config


class ConfigTests(unittest.TestCase):
    def test_defaults(self):
        cfg = config.build_config(environ={})
        self.assertEqual(cfg.link_fallback, "omit")
        self.assertEqual(cfg.category_llm, "off")
        self.assertFalse(cfg.no_llm)
        self.assertFalse(cfg.cold)
        self.assertFalse(cfg.has_api_key)

    def test_default_models(self):
        cfg = config.build_config(environ={})
        self.assertEqual(cfg.intent_model, "claude-haiku-5-5")
        self.assertEqual(cfg.category_model, "claude-haiku-5-5")
        self.assertEqual(cfg.screenshot_model, "claude-opus-5-5")

    def test_env_overrides(self):
        cfg = config.build_config(
            environ={
                "LINK_FALLBACK": "omit",
                "CATEGORY_LLM": "on",
                "GHOST_INTENT_MODEL": "m-intent",
                "GHOST_SCREENSHOT_MODEL": "m-shot",
                "GHOST_CATEGORY_MODEL": "m-cat",
            }
        )
        self.assertEqual(cfg.link_fallback, "omit")
        self.assertEqual(cfg.category_llm, "on")
        self.assertEqual(cfg.intent_model, "m-intent")
        self.assertEqual(cfg.screenshot_model, "m-shot")
        self.assertEqual(cfg.category_model, "m-cat")

    def test_cli_beats_env(self):
        cfg = config.build_config(
            cli_link_fallback="guess",
            cli_category_llm="off",
            environ={"LINK_FALLBACK": "omit", "CATEGORY_LLM": "on"},
        )
        self.assertEqual(cfg.link_fallback, "guess")
        self.assertEqual(cfg.category_llm, "off")

    def test_invalid_values_rejected(self):
        with self.assertRaises(ValueError):
            config.build_config(environ={"LINK_FALLBACK": "maybe"})
        with self.assertRaises(ValueError):
            config.build_config(environ={"CATEGORY_LLM": "sometimes"})
        with self.assertRaises(ValueError):
            config.build_config(cli_link_fallback="nope", environ={})

    def test_api_key_presence_only(self):
        cfg = config.build_config(environ={"ANTHROPIC_API_KEY": "sk-test"})
        self.assertTrue(cfg.has_api_key)
        self.assertNotIn("sk-test", repr(cfg))

    def test_no_llm_forces_category_off(self):
        cfg = config.build_config(no_llm=True, environ={"CATEGORY_LLM": "on"})
        self.assertTrue(cfg.no_llm)
        self.assertEqual(cfg.category_llm, "off")

    def test_paths_from_repo_root(self):
        cfg = config.build_config(environ={})
        self.assertTrue((cfg.repo_root / "AGENTS.md").is_file())
        self.assertEqual(cfg.dataset_dir, cfg.repo_root / "dataset")
        self.assertEqual(cfg.default_output, cfg.repo_root / "output.csv")
        self.assertEqual(cfg.runs_dir, cfg.repo_root / "runs")

    def test_runs_dir_env_override(self):
        cfg = config.build_config(environ={"GHOST_RUNS_DIR": "/tmp/ghost-runs-x"})
        self.assertEqual(str(cfg.runs_dir), "/tmp/ghost-runs-x")


if __name__ == "__main__":
    unittest.main()
