"""Minimal runtime .env loader. Values are never printed, logged or returned."""
import os
from pathlib import Path


def _parse_value(raw):
    raw = raw.strip()
    if len(raw) >= 2 and raw[0] == raw[-1] and raw[0] in "\"'":
        return raw[1:-1]
    if " #" in raw:
        raw = raw.split(" #", 1)[0]
    return raw.strip()


def env_file_path(repo_root, environ=None):
    """GHOST_ENV_FILE if set and non-empty, else <repo_root>/.env."""
    env = os.environ if environ is None else environ
    override = env.get("GHOST_ENV_FILE")
    return Path(override) if override else Path(repo_root) / ".env"


def load_dotenv(path):
    """Set os.environ from a KEY=VALUE file for keys that are unset or empty.

    Empty file values are skipped. A missing file is a no-op. Returns the names
    (never the values) of the keys that were set.
    """
    path = Path(path)
    if not path.is_file():
        return []
    names = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export "):].lstrip()
        key, sep, raw = line.partition("=")
        key = key.strip()
        if not sep or not key:
            continue
        value = _parse_value(raw)
        if not value or os.environ.get(key):
            continue
        os.environ[key] = value
        names.append(key)
    return names
