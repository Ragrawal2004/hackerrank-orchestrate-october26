"""Run configuration: paths, env flags, model names.

Reads os.environ only. A .env file is loaded into os.environ at startup by
ghost.envfile (see cli.main), never here, and the API key is never stored on Config.
"""
import os
from dataclasses import dataclass, field
from pathlib import Path

LINK_FALLBACK_VALUES = ("guess", "omit")
DEFAULT_LINK_FALLBACK = "omit"  # guess is an opt-in switch (SPEC L1)
CATEGORY_LLM_VALUES = ("on", "off")

DEFAULT_INTENT_MODEL = "claude-haiku-5-5"
DEFAULT_CATEGORY_MODEL = "claude-haiku-5-5"
DEFAULT_SCREENSHOT_MODEL = "claude-opus-5-5"  # T17 bake-off: Sonnet 5.5 misread 4 of 15 files

# LLM call settings (ghost.llm). max_tokens is always sent; the API requires it.
LLM_MAX_TOKENS = 1024
LLM_MAX_ATTEMPTS = 3
LLM_BACKOFF_SECONDS = 1.0
LLM_TIMEOUT_SECONDS = 60.0

# Prices are USD per million tokens, from the provider's published pricing page, as read on
# `as_of`. evaluation/usage_report.py fails loudly if a model in runs/usage.jsonl has any
# value missing here.
_PRICING_URL = "https://platform.claude.com/docs/en/about-claude/pricing"
_PRICING_AS_OF = "2026-10-24"
PRICES = {
    DEFAULT_INTENT_MODEL: {  # also DEFAULT_CATEGORY_MODEL (same model); rate holds for prompts up to 100,000 tokens
        "input_per_mtok": 0.10, "output_per_mtok": 0.50,
        "source_url": _PRICING_URL, "as_of": _PRICING_AS_OF},
    "claude-sonnet-5-5": {  # the other model in the screenshot bake-off (T17)
        "input_per_mtok": 2.00, "output_per_mtok": 10.00,
        "source_url": _PRICING_URL, "as_of": _PRICING_AS_OF},
    DEFAULT_SCREENSHOT_MODEL: {  # claude-opus-5-5
        "input_per_mtok": 4.00, "output_per_mtok": 20.00,
        "source_url": _PRICING_URL, "as_of": "2026-10-25"},
}

# Hard cap for the screenshot + PDF reads of one run (llm.configure(budgets=...)); the gateway
# refuses a real call that would pass either limit. Cache hits are free and not counted.
DEFAULT_SCREENSHOT_BUDGET = {"max_calls": 40, "max_usd": 3.00}

# Agreed screenshot/PDF facts (hashes and ids only) committed with the code, so a run without an
# API key reproduces the same observations. Written only by a run that has a reader.
EXTRACTIONS_DIR = Path(__file__).resolve().parents[1] / "extractions"


def extractions_dir(environ=None):
    """GHOST_EXTRACTIONS_DIR (used by the model bake-off to keep trial reads out of the committed
    folder) or the committed default."""
    env = os.environ if environ is None else environ
    return Path(env["GHOST_EXTRACTIONS_DIR"]) if env.get("GHOST_EXTRACTIONS_DIR") else EXTRACTIONS_DIR

# Largest input (tokens in one call) the PRICES rate is valid for, per model. A usage line
# above it means the price is not known to apply, so the usage report refuses to price it.
MAX_PRICED_INPUT_TOKENS = {DEFAULT_INTENT_MODEL: 100_000}


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
    link = _choice("LINK_FALLBACK", cli_link_fallback or env.get("LINK_FALLBACK") or DEFAULT_LINK_FALLBACK,
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
        runs_dir=Path(env["GHOST_RUNS_DIR"]) if env.get("GHOST_RUNS_DIR") else root / "runs",
    )
