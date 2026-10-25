"""The only way code may reach the LLM API (DESIGN §6).

`call()` sends one prompt, validates the reply against a schema, caches it
under runs/cache/, and appends a usage line to runs/usage.jsonl with the token counts the API
reported. Any failure surfaces as LLMUnavailable so callers can use their deterministic path.

The API key is read from os.environ only when the real transport is built. It is never
returned, stored on a config object, or written anywhere; everything that leaves this module
as text goes through redact().
"""
import base64
import hashlib
import json
import math
import os
import re
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

from . import config, schema

KEY_ENV = "ANTHROPIC_API_KEY"
FORBID_NETWORK_ENV = "GHOST_FORBID_NETWORK"
_KEY_PATTERN = re.compile(r"sk-ant-[A-Za-z0-9_\-]+")
_TRANSIENT_NAMES = {"APITimeoutError", "APIConnectionError"}
# Largest visual-token cost of one image on the high-resolution tier (4,784 patches of 28 px,
# rounded up). Used only to bound the worst case of a call against a budget.
IMAGE_TOKENS_UPPER = 5000
CHARS_PER_TOKEN = 3  # deliberately low, so the worst-case estimate of a text prompt is high


class LLMUnavailable(Exception):
    """The LLM could not give a valid answer. Callers fall back to the deterministic path."""


class TransportError(Exception):
    def __init__(self, message="", kind=None):
        super().__init__(message)
        self.kind = kind or type(self).__name__


class Transient(TransportError):
    """Worth retrying: timeout, connection problem, 429, 5xx."""


class Permanent(TransportError):
    """Not worth retrying: auth, bad request, anything unrecognised."""


def redact(text):
    """Remove the live API key and anything shaped like one."""
    text = str(text)
    key = os.environ.get(KEY_ENV)
    if key:
        text = text.replace(key, "[REDACTED]")
    return _KEY_PATTERN.sub("[REDACTED]", text)


# --- real transport ------------------------------------------------------------------------

def _classify(exc):
    name = type(exc).__name__
    status = getattr(exc, "status_code", None)
    transient = (name in _TRANSIENT_NAMES or status == 429
                 or (isinstance(status, int) and status >= 500))
    return (Transient if transient else Permanent)(redact(exc), kind=name)


def make_sdk_transport(client_factory=None):
    """Build send(model, prompt, max_tokens, timeout) -> (text, in_tok, out_tok).

    Sampling parameters are deliberately not sent: Messages.create() in the pinned SDK takes
    none, and extra_body is not used to smuggle one in.
    """
    if client_factory is None and os.environ.get(FORBID_NETWORK_ENV):
        raise LLMUnavailable("network access is disabled (GHOST_FORBID_NETWORK is set)")
    key = os.environ.get(KEY_ENV)
    if not key:
        raise LLMUnavailable(f"{KEY_ENV} is not set")
    if client_factory is None:
        import anthropic  # lazy: only the real run needs the SDK installed
        client_factory = anthropic.Anthropic
    client = client_factory(api_key=key, max_retries=0)  # retries are done here, not by the SDK

    def send(model, prompt, max_tokens, timeout, images=()):
        content = prompt
        if images:  # image blocks first, then the text (vision guidance)
            content = [{"type": "image", "source": {
                "type": "base64", "media_type": media,
                "data": base64.b64encode(data).decode("ascii")}} for media, data in images]
            content.append({"type": "text", "text": prompt})
        try:
            resp = client.messages.create(
                model=model, max_tokens=max_tokens,
                messages=[{"role": "user", "content": content}], timeout=timeout)
            text = "".join(b.text for b in resp.content if getattr(b, "type", None) == "text")
            return text, int(resp.usage.input_tokens), int(resp.usage.output_tokens)
        except Exception as exc:
            raise _classify(exc) from None

    return send


# --- state ---------------------------------------------------------------------------------

_state = None
_lock = threading.Lock()


def configure(runs_dir, cold=False, transport=None, sleep=time.sleep, run_id=None,
              max_attempts=None, backoff=None, timeout=None, max_tokens=None, budgets=None):
    """Set the runs directory, cold flag and (for tests) a fake transport. Call once per run."""
    global _state
    budgets = _check_budgets(budgets)
    if max_tokens is not None and (
            not isinstance(max_tokens, int) or isinstance(max_tokens, bool) or max_tokens < 0):
        raise ValueError(f"max_tokens must be a positive integer, got {max_tokens!r}")
    _state = {
        "runs_dir": Path(runs_dir),
        "cold": bool(cold),
        "transport": transport,
        "sleep": sleep,
        "run_id": run_id or datetime.now(timezone.utc).strftime("run-%Y%m%dT%H%M%SZ"),
        "max_attempts": max_attempts or config.LLM_MAX_ATTEMPTS,
        "backoff": config.LLM_BACKOFF_SECONDS if backoff is None else backoff,
        "timeout": timeout or config.LLM_TIMEOUT_SECONDS,
        "max_tokens": max_tokens or config.LLM_MAX_TOKENS,  # the API requires it; 0/None -> default
        "budgets": budgets,
        "spent": {p: {"calls": 0, "usd": 0.0, "inflight": 0.0} for p in budgets},
    }


def _check_budgets(budgets):
    """{purpose: {"max_calls": int >= 0, "max_usd": number >= 0}}; anything else is a ValueError."""
    out = {}
    for purpose, b in (budgets or {}).items():
        calls, usd = (b or {}).get("max_calls"), (b or {}).get("max_usd")
        if (not isinstance(calls, int) or isinstance(calls, bool) or calls < 0
                or not isinstance(usd, (int, float)) or isinstance(usd, bool) or usd < 0):
            raise ValueError(f"budget for {purpose!r} needs max_calls (int >= 0) and "
                             f"max_usd (number >= 0), got {b!r}")
        out[purpose] = {"max_calls": calls, "max_usd": float(usd)}
    return out


def reset():
    global _state
    _state = None


# --- cache and usage -----------------------------------------------------------------------

def cache_key(model, prompt, params):
    blob = json.dumps({"model": model, "prompt": prompt, "params": params},
                      sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def _parse(text):
    text = text.strip()
    if text.startswith("```"):
        text = re.sub(r"^```[A-Za-z0-9]*\s*\n", "", text)
        text = re.sub(r"\n?```\s*$", "", text)
    return json.loads(text)


def _cache_path(st, key):
    return st["runs_dir"] / "cache" / f"{key}.json"


def _cache_read(st, key, sch):
    try:
        entry = json.loads(_cache_path(st, key).read_text(encoding="utf-8"))
        obj = _parse(entry["text"])
        schema.validate(obj, sch)
        return obj
    except Exception:
        return None  # missing, corrupt or no longer valid: treat as a miss


def _cache_write(st, key, text):
    path = _cache_path(st, key)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f"{path.name}.{os.getpid()}.{threading.get_ident()}.tmp")
    tmp.write_text(json.dumps({"text": text}, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, path)


def _log_usage(st, purpose, model, case_id, input_tokens, output_tokens, cache_hit, error,
               variant=None, image_count=0):
    line = {
        "ts": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "purpose": purpose, "model": model, "case_id": case_id,
        "input_tokens": input_tokens, "output_tokens": output_tokens,
        "cache_hit": cache_hit,
        "error": redact(error) if error else None,
        "run_id": st["run_id"],
    }
    if variant is not None or image_count:
        line["variant"], line["image_count"] = variant, image_count
    st["runs_dir"].mkdir(parents=True, exist_ok=True)
    with _lock, open(st["runs_dir"] / "usage.jsonl", "a", encoding="utf-8") as f:
        f.write(json.dumps(line, ensure_ascii=False) + "\n")


# --- the one entry point -------------------------------------------------------------------

def _price(model):
    p = config.PRICES.get(model) or {}
    if p.get("input_per_mtok") is None or p.get("output_per_mtok") is None:
        return None
    return p["input_per_mtok"], p["output_per_mtok"]


def _reserve(st, purpose, model, prompt, n_images, max_tokens):
    """Take one call slot and reserve the worst-case cost of this call, or say why not.

    Returns (worst_case_usd) when the call may go ahead, else raises LLMUnavailable. A purpose
    without a budget is not limited. Runs under the lock so concurrent callers cannot overshoot."""
    budget = st["budgets"].get(purpose)
    if budget is None:
        return None
    price = _price(model)
    with _lock:
        sp = st["spent"][purpose]
        if price is None:
            why = f"no price for {model}"
        else:
            worst = ((math.ceil(len(prompt) / CHARS_PER_TOKEN) + n_images * IMAGE_TOKENS_UPPER)
                     * price[0] + max_tokens * price[1]) / 1e6
            if sp["calls"] >= budget["max_calls"]:
                why = f"call cap {budget['max_calls']} reached"
            elif sp["usd"] >= budget["max_usd"]:
                why = f"cost cap ${budget['max_usd']:.2f} reached"
            elif sp["usd"] + sp["inflight"] + worst > budget["max_usd"]:
                why = f"next call could exceed the ${budget['max_usd']:.2f} cost cap"
            else:
                sp["calls"] += 1
                sp["inflight"] += worst
                return worst
    raise LLMUnavailable(f"budget_exceeded: {why}")


def _settle(st, purpose, model, worst, in_tok, out_tok):
    """Replace the reserved worst case by the cost of the tokens the API reported."""
    if worst is None:
        return
    price = _price(model)
    with _lock:
        sp = st["spent"][purpose]
        sp["inflight"] = max(0.0, sp["inflight"] - worst)
        sp["usd"] += (in_tok * price[0] + out_tok * price[1]) / 1e6


def call(purpose, model, prompt, case_id, schema_, images=(), variant=None, max_tokens=None):
    """Send `prompt` (after any `images`) to `model` and return the reply parsed and validated.

    images: [(media_type, bytes)]. variant: names an independent read of the same inputs, so a
    second read is a different cache entry. max_tokens overrides the run default for this call.
    Raises LLMUnavailable on any failure (a budget refusal included), and ValueError if the
    schema uses a keyword the validator does not enforce (checked before anything is spent).
    """
    schema.check_schema(schema_)
    st = _state
    if st is None:
        raise LLMUnavailable("llm.configure() has not been called")
    images = [(m, bytes(d)) for m, d in images]
    max_tok = max_tokens or st["max_tokens"]
    params = {"max_tokens": max_tok, "schema": schema_}
    if images:
        params["images"] = [[m, hashlib.sha256(d).hexdigest()] for m, d in images]
    if variant is not None:
        params["variant"] = variant
    extra = {"variant": variant, "image_count": len(images)}
    key = cache_key(model, prompt, params)

    if not st["cold"]:
        hit = _cache_read(st, key, schema_)
        if hit is not None:
            _log_usage(st, purpose, model, case_id, 0, 0, True, None, **extra)
            return hit

    if st["transport"] is None:
        st["transport"] = make_sdk_transport()
    send = st["transport"]

    for attempt in range(1, st["max_attempts"] + 1):
        try:
            worst = _reserve(st, purpose, model, prompt, len(images), max_tok)
        except LLMUnavailable:
            _log_usage(st, purpose, model, case_id, 0, 0, False, "BudgetExceeded", **extra)
            raise
        try:
            if images:
                text, in_tok, out_tok = send(model, prompt, max_tok, st["timeout"], images=images)
            else:
                text, in_tok, out_tok = send(model, prompt, max_tok, st["timeout"])
        except Transient as exc:
            _settle(st, purpose, model, worst, 0, 0)
            _log_usage(st, purpose, model, case_id, 0, 0, False, exc.kind, **extra)
            if attempt < st["max_attempts"]:
                st["sleep"](st["backoff"] * 2 ** (attempt - 1))
                continue
            raise LLMUnavailable(
                f"retries exhausted after {attempt} attempts ({exc.kind}): {redact(exc)}") from None
        except Exception as exc:  # Permanent, or anything unexpected: no retry
            _settle(st, purpose, model, worst, 0, 0)
            kind = getattr(exc, "kind", type(exc).__name__)
            _log_usage(st, purpose, model, case_id, 0, 0, False, kind, **extra)
            raise LLMUnavailable(f"request failed ({kind}): {redact(exc)}") from None
        _settle(st, purpose, model, worst, in_tok, out_tok)

        try:
            obj = _parse(text)
            schema.validate(obj, schema_)
        except ValueError as exc:  # JSONDecodeError and SchemaError are both ValueErrors
            # The call cost tokens even though the answer is unusable, so log them.
            _log_usage(st, purpose, model, case_id, in_tok, out_tok, False, "BadResponse", **extra)
            raise LLMUnavailable(f"bad response: {redact(exc)}") from None
        _log_usage(st, purpose, model, case_id, in_tok, out_tok, False, None, **extra)
        _cache_write(st, key, text)
        return obj
