"""Append-only event log: runs/diagnostics.jsonl (DESIGN §7).

One JSON object per line with `case_id` and `event` plus the caller's fields. Every line goes
through llm.redact() so an API key can never reach disk. No timestamps, so runs stay diffable.
"""
import json
import threading
from pathlib import Path

from . import llm

FILE_NAME = "diagnostics.jsonl"
_lock = threading.Lock()


def log_event(runs_dir, case_id, event, **fields):
    path = Path(runs_dir) / FILE_NAME
    line = llm.redact(json.dumps({"case_id": case_id, "event": event, **fields},
                                 ensure_ascii=False, sort_keys=True))
    with _lock:
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "a", encoding="utf-8", newline="\n") as f:
            f.write(line + "\n")
