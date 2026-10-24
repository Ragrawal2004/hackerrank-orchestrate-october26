"""Run configuration: paths, env flags, model names.

Reads os.environ only. A .env file is loaded into os.environ at startup by
ghost.envfile (see cli.main), never here, and the API key is never stored on Config.
"""
import os
from dataclasses import dataclass, field
from pathlib import Path

LINK_FALLBACK_VALUES = ("guess", "omit")
CATEGORY_LLM_VALUES = ("on", "off")

DEFAULT_INTENT_MODEL = "claude-haiku-5-5"
DEFAULT_CATEGORY_MODEL = "claude-haiku-5-5"
DEFAULT_SCREENSHOT_MODEL = "claude-sonnet-5-5"

# LLM call settings (ghost.llm). max_tokens is always sent; the API requires it.
LLM_MAX_TOKENS = 1024
LLM_MAX_ATTEMPTS = 3
LLM_BACKOFF_SECONDS = 1.0
LLM_TIMEOUT_SECONDS = 60.0

# TODO(user): fill in from the provider's published pricing. Do NOT guess numbers.
# Prices are USD per million tokens. Record where they came from (source_url) and the
# date you read them (as_of, YYYY-MM-DD). evaluation/usage_report.py fails loudly while
# any value is None for a model that appears in runs/usage.jsonl.
_UNPRICED = {"input_per_mtok": None, "output_per_mtok": None, "source_url": None, "as_of": None}
PRICES = {
    DEFAULT_INTENT_MODEL: dict(_UNPRICED),
    DEFAULT_SCREENSHOT_MODEL: dict(_UNPRICED),
}


def find_repo_root(start=None):
    """Directory containing AGENTS.md, searching upward from this file."""
    p = Path(start or __file__).resolve()
    for d in [p, *p.parents]:
        if (d / "AGENTS.md").is_file():
            return d
    raise RuntimeError("AGENTS.md not found above " + str(p))


def _choice(name, value, allowed):
    value = str(value).strip().lower()
    if value not in allowed:
        raise ValueError(f"{name} must be one of {allowed}, got {value!r}")
    return value


@dataclass(frozen=True)
class Config:
    repo_root: Path
    link_fallback: str
    category_llm: str
    no_llm: bool
    cold: bool
    intent_model: str
    category_model: str
    screenshot_model: str
    has_api_key: bool
    dataset_dir: Path = field(default=None)
    default_output: Path = field(default=None)
    runs_dir: Path = field(default=None)


def build_config(cli_link_fallback=None, cli_category_llm=None, no_llm=False,
                 cold=False, environ=None):
    """CLI value beats env value beats default. Never stores the API key."""
    env = os.environ if environ is None else environ
    root = find_repo_root()
    link = _choice("LINK_FALLBACK", cli_link_fallback or env.get("LINK_FALLBACK") or "guess",
                   LINK_FALLBACK_VALUES)
    cat = _choice("CATEGORY_LLM", cli_category_llm or env.get("CATEGORY_LLM") or "off",
                  CATEGORY_LLM_VALUES)
    if no_llm:
        cat = "off"
    return Config(
        repo_root=root,
        link_fallback=link,
        category_llm=cat,
        no_llm=bool(no_llm),
        cold=bool(cold),
        intent_model=env.get("GHOST_INTENT_MODEL") or DEFAULT_INTENT_MODEL,
        category_model=env.get("GHOST_CATEGORY_MODEL") or DEFAULT_CATEGORY_MODEL,
        screenshot_model=env.get("GHOST_SCREENSHOT_MODEL") or DEFAULT_SCREENSHOT_MODEL,
        has_api_key=bool(env.get("ANTHROPIC_API_KEY")),
        dataset_dir=root / "dataset",
        default_output=root / "output.csv",
        runs_dir=root / "runs",
    )
