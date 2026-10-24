"""Score a prediction output.csv against a gold CSV (default: the 32 sample answers).

Universe = the gold cases. A gold case with no prediction row is wrong (status, steps and
sequence); prediction rows that are not in gold are ignored and counted. Two modes come from one
pass: strict compares all six step fields, relaxed ignores evidence_id (matching, field accuracy
and sequence). step_no is not scored (numbering is an invariant, not a content claim).
"""
import csv
import io
import json
from collections import Counter
from pathlib import Path

from ghost.loaders import case_sort_key

HEADER = ["case_id", "deletion_status", "steps_json"]
FIELDS = ("phase", "system_id", "resource_id", "operation", "state", "evidence_id")
ALIGN_KEY = ("phase", "operation", "resource_id")
MODES = {"strict": FIELDS, "relaxed": tuple(f for f in FIELDS if f != "evidence_id")}
MISSING = "(no row)"


class InputError(Exception):
    """Unreadable or malformed gold/prediction input."""


def load_rows_text(text, name="input"):
    """-> {case_id: (status, steps)}; steps is None when steps_json is not a valid step array."""
    reader = csv.reader(io.StringIO(text))
    rows = list(reader)
    if not rows or rows[0] != HEADER:
        raise InputError(f"{name}: header must be {','.join(HEADER)}")
    out = {}
    for i, row in enumerate(rows[1:], start=2):
        if len(row) != 3:
            raise InputError(f"{name}: line {i} has {len(row)} columns, expected 3")
        case_id, status, raw = row
        if case_id in out:
            raise InputError(f"{name}: duplicate case_id {case_id}")
        out[case_id] = (status, _parse_steps(raw))
    return out


def load_rows(path):
    path = Path(path)
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as e:
        raise InputError(f"cannot read {path}: {e}") from e
    return load_rows_text(text, str(path))


def _parse_steps(raw):
    try:
        steps = json.loads(raw)
    except ValueError:
        return None
    if not isinstance(steps, list) or not all(isinstance(s, dict) for s in steps):
        return None
    return steps


def _tuple(step, fields):
    return tuple(str(step.get(f, "")) for f in fields)


def _align(gold_steps, pred_steps):
    """Pair steps by (phase, operation, resource_id), k-th occurrence with k-th occurrence.

    -> (pairs [(gold, pred)], missing [gold], extra [pred], order_ok)
    """
    def labelled(steps):
        seen, out = Counter(), {}
        order = []
        for s in steps:
            key = _tuple(s, ALIGN_KEY)
            label = (key, seen[key])
            seen[key] += 1
            out[label] = s
            order.append(label)
        return out, order

    g, g_order = labelled(gold_steps)
    p, p_order = labelled(pred_steps)
    common = set(g) & set(p)
    pairs = [(g[lab], p[lab]) for lab in g_order if lab in common]
    missing = [g[lab] for lab in g_order if lab not in common]
    extra = [p[lab] for lab in p_order if lab not in common]
    order_ok = [lab for lab in g_order if lab in common] == [lab for lab in p_order if lab in common]
    return pairs, missing, extra, order_ok


def _desc(step):
    return f"{step.get('operation')} {step.get('resource_id')}"


def _case_reasons(gold, pred, fields):
    """Short reasons a case differs under the given field set (empty list = matches)."""
    g_status, g_steps = gold
    if pred is None:
        return ["no prediction row"]
    p_status, p_steps = pred
    reasons = []
    if p_status != g_status:
        reasons.append(f"status wrong (pred {p_status}, gold {g_status})")
    if p_steps is None:
        reasons.append("bad steps_json")
        return reasons
    pairs, missing, extra, order_ok = _align(g_steps, p_steps)
    for g, p in pairs:
        bad = [f for f in fields if f not in ALIGN_KEY and str(g.get(f, "")) != str(p.get(f, ""))]
        if bad:
            detail = ", ".join(f"{f}: pred {p.get(f, '')!r} gold {g.get(f, '')!r}" for f in bad)
            reasons.append(f"wrong field ({detail}) on {_desc(g)}")
    reasons += [f"missing step ({_desc(s)})" for s in missing]
    reasons += [f"extra step ({_desc(s)})" for s in extra]
    if not order_ok:
        reasons.append("wrong order")
    return reasons


def _div(num, den, empty):
    return num / den if den else empty


def _mode_metrics(gold, pred, fields):
    status_ok = tp = g_total = p_total = seq_ok = 0
    aligned = 0
    field_ok = Counter()
    for case_id, (g_status, g_steps) in gold.items():
        p = pred.get(case_id)
        p_steps = (p[1] or []) if p else []
        status_ok += bool(p) and p[0] == g_status
        g_ids = Counter(_tuple(s, fields) for s in g_steps)
        p_ids = Counter(_tuple(s, fields) for s in p_steps)
        tp += sum((g_ids & p_ids).values())
        g_total += len(g_steps)
        p_total += len(p_steps)
        if p and p[1] is not None:
            seq_ok += [_tuple(s, fields) for s in g_steps] == [_tuple(s, fields) for s in p_steps]
        pairs = _align(g_steps, p_steps)[0]
        aligned += len(pairs)
        for g, q in pairs:
            for f in FIELDS:
                field_ok[f] += str(g.get(f, "")) == str(q.get(f, ""))
    precision = _div(tp, p_total, 1.0 if g_total == 0 else 0.0)
    recall = _div(tp, g_total, 1.0 if p_total == 0 else 0.0)
    f1 = _div(2 * precision * recall, precision + recall, 0.0)
    n = len(gold)
    return {
        "status_accuracy": _div(status_ok, n, 0.0),
        "precision": precision, "recall": recall, "f1": f1,
        "field_accuracy": {f: (_div(field_ok[f], aligned, 0.0) if f in fields else None) for f in FIELDS},
        "aligned_steps": aligned,
        "sequence_accuracy": _div(seq_ok, n, 0.0),
        "tp": tp, "gold_steps": g_total, "pred_steps": p_total,
    }


def score(gold, pred):
    """gold, pred: {case_id: (status, steps_or_None)} -> result dict (see format_report)."""
    result = {"gold_cases": len(gold),
              "extra_rows": len([c for c in pred if c not in gold]),
              "missing_rows": len([c for c in gold if c not in pred])}
    for mode, fields in MODES.items():
        result[mode] = _mode_metrics(gold, pred, fields)
    confusion = Counter((g[0], pred[c][0] if c in pred else MISSING) for c, g in gold.items())
    result["confusion"] = dict(confusion)
    mismatches = []
    for case_id in sorted(gold, key=case_sort_key):
        strict = _case_reasons(gold[case_id], pred.get(case_id), MODES["strict"])
        if strict:
            relaxed = _case_reasons(gold[case_id], pred.get(case_id), MODES["relaxed"])
            mismatches.append({"case_id": case_id, "reasons": strict, "strict_only": not relaxed})
    result["mismatches"] = mismatches
    return result


def score_texts(gold_text, pred_text):
    return score(load_rows_text(gold_text, "gold"), load_rows_text(pred_text, "prediction"))


def score_files(gold_path, pred_path):
    return score(load_rows(gold_path), load_rows(pred_path))


def _pct(v):
    return "n/a" if v is None else f"{v:.4f}"


def format_report(r):
    s, x = r["strict"], r["relaxed"]
    lines = [f"Gold cases: {r['gold_cases']}   missing prediction rows: {r['missing_rows']}   "
             f"extra prediction rows ignored: {r['extra_rows']}",
             "",
             f"{'metric':<28}{'strict':>10}{'relaxed':>10}"]

    def row(label, a, b):
        lines.append(f"{label:<28}{_pct(a):>10}{_pct(b):>10}")

    row("Case status accuracy", s["status_accuracy"], x["status_accuracy"])
    row("Step precision", s["precision"], x["precision"])
    row("Step recall", s["recall"], x["recall"])
    row("Step F1", s["f1"], x["f1"])
    for f in FIELDS:
        row(f"Field accuracy: {f}", s["field_accuracy"][f], x["field_accuracy"][f])
    row("Sequence accuracy", s["sequence_accuracy"], x["sequence_accuracy"])
    lines += ["",
              f"Steps: gold {s['gold_steps']}, predicted {s['pred_steps']}, matched strict {s['tp']} / "
              f"relaxed {x['tp']}; aligned for field accuracy: {s['aligned_steps']} "
              "(phase/operation/resource_id are the alignment key, so 1.0 by construction)",
              "Status confusion (gold -> pred): " + (", ".join(
                  f"{g}->{p}: {n}" for (g, p), n in sorted(r["confusion"].items())) or "none"),
              "", f"Mismatching cases: {len(r['mismatches'])}"]
    for m in r["mismatches"]:
        tag = "  [strict only]" if m["strict_only"] else ""
        lines.append(f"  {m['case_id']}{tag}: " + "; ".join(m["reasons"]))
    return "\n".join(lines)
