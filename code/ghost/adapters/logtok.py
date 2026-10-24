"""Shared log tokenizer (DESIGN §3, adapters 5/6): one parser per observability log syntax.

Every parser turns one raw line into Line(ts, level, service, trace_id, event, kv) or None. A
line parses under exactly one syntax:

    logfmt      TS level=L service=S ... trace_id=T ... event=E k=v ...
    json_line   TS {"severity": L, "service": S, "trace_id": T, "event": E, ...}
    syslog5424  TS <PRI>1 HOST SERVICE SLOT - - level=L k="v" ...
    bracketed   TS [L] [SERVICE] [SLOT] EVENT: k="v" ...
    pipe        TS | L | SERVICE | SLOT | EVENT | k="v" ...
    aws_kv      TS requestId=SLOT level=L function=SERVICE message="k=\\"v\\" ..."

SLOT is the trace id, except on infrastructure lines where it repeats the line's `instance`.
kv values are strings with quotes and escapes removed; `null` (bare, quoted or JSON) is None and
JSON booleans/numbers become their text ("true", "1").
"""
import json
import re
from collections import Counter, namedtuple

SYNTAXES = ("logfmt", "json_line", "syslog5424", "bracketed", "pipe", "aws_kv")
Line = namedtuple("Line", "ts level service trace_id event kv")

_TS = r"(\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d(?:\.\d+)?Z)"
_QUOTED = r'"(?:[^"\\]|\\.)*"'
_PAIR = rf'\w+=(?:{_QUOTED}|[^\s"]*)'
_PAIRS = re.compile(rf"(?:{_PAIR}(?: {_PAIR})*)?")
_ONE = re.compile(rf'(\w+)=({_QUOTED}|[^\s"]*)')
_ESC = re.compile(r"\\(.)")
_EVENT = re.compile(r"^\w+$")

_HEAD = {
    "logfmt": re.compile(rf"^{_TS} (level=.*)$"),
    "json_line": re.compile(rf"^{_TS} (\{{.*\}})$"),
    "syslog5424": re.compile(rf"^{_TS} <(\d{{1,3}})>1 (\S+) (\S+) (\S+) - - (.*)$"),
    "bracketed": re.compile(rf"^{_TS} \[(\w+)\] \[([^\]\s]+)\] \[([^\]\s]+)\] (\w+): ?(.*)$"),
    "pipe": re.compile(rf"^{_TS} \| (\w+) \| (\S+) \| (\S+) \| (\w+) \| ?(.*)$"),
    "aws_kv": re.compile(rf"^{_TS} requestId=(\S+) level=(\w+) function=(\S+) "
                         rf'message="((?:[^"\\]|\\.)*)"$'),
}


def _unescape(s):
    return _ESC.sub(r"\1", s)


def _value(raw):
    v = _unescape(raw[1:-1]) if raw.startswith('"') else raw
    return None if v == "null" else v


def _kv(text):
    """dict of k=v pairs when the whole text is pairs separated by single spaces, else None."""
    if not _PAIRS.fullmatch(text):
        return None
    return {k: _value(v) for k, v in _ONE.findall(text)}


def _json_value(v):
    if v is None or v == "null":
        return None
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, (int, float)):
        return str(v)
    return v if isinstance(v, str) else json.dumps(v, sort_keys=True)


def _trace(slot, kv):
    return None if slot == kv.get("instance") else slot


def _line(ts, level, service, trace_id, event, kv):
    if not (isinstance(event, str) and _EVENT.match(event) and service and level):
        return None
    return Line(ts, level, service, trace_id, event, kv)


def parse_line(syntax, text):
    """The Line for one raw line under `syntax`, or None when it does not parse."""
    m = _HEAD[syntax].match(text or "")
    if not m:
        return None
    g = m.groups()
    if syntax == "logfmt":
        kv = _kv(g[1])
        if kv is None:
            return None
        return _line(g[0], kv.get("level"), kv.get("service"), kv.get("trace_id"),
                     kv.get("event"), kv)
    if syntax == "json_line":
        try:
            body = json.loads(g[1])
        except (ValueError, RecursionError):
            return None
        if not isinstance(body, dict):
            return None
        kv = {str(k): _json_value(v) for k, v in body.items()}
        return _line(g[0], kv.get("severity") or kv.get("level"),
                     kv.get("service") or kv.get("logger"), kv.get("trace_id"), kv.get("event"),
                     kv)
    if syntax == "syslog5424":
        kv = _kv(g[5])
        if kv is None:
            return None
        return _line(g[0], kv.get("level"), g[3], _trace(g[4], kv), kv.get("event"), kv)
    if syntax in ("bracketed", "pipe"):
        kv = _kv(g[5])
        if kv is None:
            return None
        return _line(g[0], g[1], g[2], _trace(g[3], kv), g[4], kv)
    kv = _kv(_unescape(g[4]))  # aws_kv: un-escape the message, then split it
    if kv is None:
        return None
    return _line(g[0], g[2], g[3], _trace(g[1], kv), kv.get("event"), kv)


_LEVELED = re.compile(rf"^{_TS} (\w+) (service=.*)$")


def parse_leveled_line(text):
    """Line for a ledger-style line `TS LEVEL service=S event=E k=v ...` (the level is a bare
    word, not `level=`, so it is none of the six syntaxes above and `parses` never returns it),
    else None. The trace id is always None."""
    m = _LEVELED.match(text or "")
    if not m:
        return None
    kv = _kv(m.group(3))
    if kv is None:
        return None
    return _line(m.group(1), m.group(2), kv.get("service"), None, kv.get("event"), kv)


def parses(text):
    """The syntaxes under which one line parses."""
    return [s for s in SYNTAXES if parse_line(s, text) is not None]


def file_syntax(lines):
    """The single syntax that parses the most non-blank lines; None on a tie or when none does."""
    counts = Counter()
    for text in lines:
        if text.strip():
            counts.update(parses(text))
    top = counts.most_common(2)
    if not top or (len(top) == 2 and top[0][1] == top[1][1]):
        return None
    return top[0][0]
