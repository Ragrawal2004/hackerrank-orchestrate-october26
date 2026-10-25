"""Synthetic screenshot/PDF facts for the multimodal adapter tests. No dataset file is read.

`payload` builds what a reader returns for one read of one file (the extraction schema in
ghost.adapters.multimodal.SCHEMA). `FakeReader` stands in for the vision/PDF reader: it returns
the scripted [read A, read B] pair and counts its calls. Nothing here talks to a model.
"""
import copy

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 64
PDF = b"%PDF-1.4\n% synthetic\n" + b"\x00" * 64


def payload(kind="account", rid=None, ref=None, emails=(), user_refs=(), phones=(), names=(),
            enabled="not_shown", restore="not_shown", identifiers_present=None, listed=True,
            screen_time=None, context_rows=0):
    if identifiers_present is None:
        identifiers_present = bool(emails or user_refs or phones or names)
    return {
        "screen_kind": kind,
        "detail_resource_id": rid,
        "detail_reference": ref,
        "detail_identifiers": {"emails": list(emails), "user_refs": list(user_refs),
                               "phones": list(phones), "names": list(names)},
        "indicators": {"enabled": enabled, "restore_allowed": restore,
                       "identifiers_present": identifiers_present, "listed_present": listed},
        "screen_time": screen_time,
        "context_row_count": context_rows,
    }


class FakeReader:
    """reader(file_id, kind, data) -> [read A, read B]. `b` defaults to a copy of `a`."""
    model = "m-test"

    def __init__(self, a, b=None):
        self.a = a
        self.b = copy.deepcopy(a) if b is None else b
        self.calls = []

    def __call__(self, file_id, kind, data):
        self.calls.append((file_id, kind, len(data)))
        return [copy.deepcopy(self.a), copy.deepcopy(self.b)]


class ForbiddenReader:
    """A reader that must never be called (used to prove an extraction file was reused)."""
    model = "m-test"

    def __call__(self, file_id, kind, data):
        raise AssertionError("the reader was called but a stored extraction should have been used")
