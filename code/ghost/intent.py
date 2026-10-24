"""Step 0 of every case: does the request authorize deletion? (DESIGN §2 step 1)

Two classifiers run side by side. The prefilter is deterministic and asymmetric: it says
NON_DELETION only when it finds an explicit keep/negation clause aimed at the person's own
account or records, and says DELETION in every other case, including when it is unsure, because
a wrong NON_DELETION loses the whole case. The LLM (llm.call only) reads the text too.
A prefilter negation overrides the LLM; otherwise the LLM wins; with no LLM label the prefilter
label is used.
"""
import os
import re
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import NamedTuple

from . import diagnostics, llm, loaders

INTENT_SCHEMA = {
    "type": "object",
    "required": ["intent", "reason"],
    "properties": {
        "intent": {"type": "string", "enum": ["DELETION", "NON_DELETION"]},
        "reason": {"type": "string"},
    },
    "additionalProperties": False,
}
PROMPT_PATH = Path(__file__).resolve().parents[1] / "prompts" / "intent.md"
DEFAULT_WORKERS = 8

# Words that make a "don't delete/remove" clause about something other than the person's own
# account (retained records), so it is not a request to keep the account.
_RETAINED = r"(?:backups?|security|audit|logs?|legal|reference)"

# (rule id, regex). All matched case-insensitively against the text with curly quotes straightened.
_NEGATION_RULES = [
    ("en_not_asking", r"\bnot\s+(?:asking|requesting|looking)\s+(?:for|to)\b[^.]*?\b(?:delet|eras|clos|remov)"),
    ("en_not_a_request", r"\bnot\s+a\s+request\s+to\s+(?:delete|erase|close|remove)\b"),
    ("en_dont", r"\b(?:do\s+not|don't|dont|never)\s+(?:close|delete|erase|remove|deactivate)\b"
                r"(?![^.]{0,40}\b" + _RETAINED + r"\b)"),
    ("en_keep_account", r"\b(?:keep|leave)\s+(?:the|my|this)\s+(?:account|profile|data|information)\b"
                        r"[^.]*\b(?:as\s+it\s+is|active|in\s+place|intact|untouched|unchanged|open)\b"),
    ("en_without_removing", r"\bwithout\s+(?:removing|deleting|erasing|closing)\b"
                            r"(?![^.]{0,40}\b" + _RETAINED + r"\b)"),
    ("en_marketing_only", r"\b(?:marketing|promotional)\b[^.]{0,30}\bonly\b"
                          r"|\bonly\b[^.]{0,20}\b(?:marketing|promotional)\b"),
    ("en_continue_using", r"\bcontinue\s+(?:to\s+)?us(?:e|ing)\b"
                          r"|\bstill\s+(?:use|using|need)\s+(?:it|the\s+account|this\s+account|my\s+account|the\s+service)\b"),
    ("es", r"\bno\s+(?:quiero|deseo|estoy\s+(?:pidiendo|solicitando))\s+(?:que\s+)?(?:eliminar|borrar|cerrar|elimin\w+|borr\w+)"
           r"|\bno\s+(?:elimin|borr|cierr)\w*\s+(?:mi|la|su)\s+cuenta|\bmant[eé]ng\w+\s+(?:mi|la)\s+cuenta"),
    ("pt", r"\bn[ãa]o\s+(?:quero|desejo|estou\s+pedindo|estou\s+solicitando)\s+(?:a\s+)?(?:excluir|apagar|encerrar|exclus[ãa]o)"
           r"|\bn[ãa]o\s+(?:exclua|apague|encerre|feche)\b"),
    ("fr", r"\bne\s+(?:veux|souhaite|demande|désire)\s+pas\s+(?:de\s+)?(?:supprimer|suppression|effacer|fermer)"
           r"|\bne\s+(?:supprimez|effacez|fermez)\s+pas\b|\bgardez\s+(?:mon|le)\s+compte"),
    ("ko", r"(?:삭제|탈퇴|해지)[^.。]{0,12}(?:원하지\s*않|(?:것|게)\s*(?:이\s*)?아닙|하지\s*마|말아)"
           r"|계정[을를]?\s*(?:그대로|유지)"),
    ("ja", r"削除(?:は|を)?\s*(?:希望しません|希望していません|しないで|依頼するものではありません|ではありません)"
           r"|アカウント(?:は|を)?(?:そのまま|維持|残して)"),
]
_NEGATION = [(rid, re.compile(rx, re.IGNORECASE)) for rid, rx in _NEGATION_RULES]

# Supporting information only; it never changes the label.
_DELETION_VOCAB = re.compile(
    r"delet|eras(?:e|ure)|clos(?:e|ing|ed)\b.*account|remov|eliminaci|eliminar|borrar|exclus|excluir"
    r"|suppress|supprim|effac|삭제|탈퇴|削除|消去|閉鎖", re.IGNORECASE)


class Prefilter(NamedTuple):
    label: str
    negation: bool
    matched: list
    deletion_vocab: bool


def _norm(text):
    return (text or "").replace("’", "'").replace("‘", "'")


def prefilter(text):
    t = _norm(text)
    matched = [rid for rid, rx in _NEGATION if rx.search(t)]
    return Prefilter("NON_DELETION" if matched else "DELETION", bool(matched), matched,
                     bool(_DELETION_VOCAB.search(t)))


def combine(pre, llm_label):
    """(label, source). A prefilter negation always wins over an LLM label, whatever it said."""
    if llm_label is None:
        return pre.label, "prefilter"
    if pre.negation:
        return "NON_DELETION", "negation_override"
    return llm_label, "llm"


def build_prompt(text):
    return PROMPT_PATH.read_text(encoding="utf-8").replace("{request_text}", text)


def _classify(case, model, use_llm):
    """Pure per-case work (safe to run in a thread). Returns (result, events)."""
    case_id, text = case["case_id"], case["request_text"]
    pre = prefilter(text)
    events = []
    llm_label = llm_reason = error = None
    if use_llm and os.environ.get(llm.KEY_ENV):
        try:
            out = llm.call("intent", model, build_prompt(text), case_id, INTENT_SCHEMA)
            llm_label, llm_reason = out["intent"], out["reason"]
        except llm.LLMUnavailable as exc:
            error = str(exc)
            events.append(("intent_fallback", {"error": error, "prefilter": pre.label}))
    label, source = combine(pre, llm_label)
    if llm_label is not None and llm_label != pre.label:
        events.append(("intent_disagreement", {
            "prefilter": pre.label, "llm": llm_label,
            "winner": "negation" if source == "negation_override" else "llm",
            "llm_reason": llm_reason, "matched": list(pre.matched)}))
    result = {"case_id": case_id, "label": label, "source": source, "prefilter": pre.label,
              "llm": llm_label, "llm_reason": llm_reason, "matched": list(pre.matched),
              "error": error}
    return result, events


def classify(case, model, use_llm, runs_dir):
    result, events = _classify(case, model, use_llm)
    for name, fields in events:
        diagnostics.log_event(runs_dir, case["case_id"], name, **fields)
    return result


def classify_all(cases, model, use_llm, runs_dir, workers=DEFAULT_WORKERS):
    """Classify every case on a bounded pool. Results and events are emitted in numeric
    case_id order after the pool finishes, so the output never depends on thread timing."""
    cases = list(cases)
    with ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
        done = list(pool.map(lambda c: _classify(c, model, use_llm), cases))
    done.sort(key=lambda pair: loaders.case_sort_key(pair[0]["case_id"]))
    for result, events in done:
        for name, fields in events:
            diagnostics.log_event(runs_dir, result["case_id"], name, **fields)
    return [result for result, _ in done]


def report(results):
    llm_rows = [r for r in results if r["llm"] is not None]
    disagree = sorted((r["case_id"] for r in llm_rows if r["llm"] != r["prefilter"]),
                      key=loaders.case_sort_key)
    by_source = Counter(r["source"] for r in results if r["label"] == "NON_DELETION")
    llm_only = [r for r in results if r["label"] == "NON_DELETION" and r["source"] == "llm"]
    return {
        "total": len(results),
        "llm_labelled": len(llm_rows),
        "fallback": len(results) - len(llm_rows),
        "agreement_rate": (1 - len(disagree) / len(llm_rows)) if llm_rows else None,
        "negation_overrides": sum(1 for r in results if r["source"] == "negation_override"),
        "disagreements": disagree,
        "non_deletion_by_source": {k: by_source.get(k, 0)
                                   for k in ("negation_override", "llm", "prefilter")},
        "llm_only_non_deletion": sorted((r["case_id"] for r in llm_only),
                                        key=loaders.case_sort_key),
        "llm_only_reasons": {r["case_id"]: r["llm_reason"] for r in llm_only},
    }


def format_report(rep):
    lines = ["NON_DELETION without prefilter negation (LLM only; review these first): "
             + (", ".join(f"{c} ({rep['llm_only_reasons'].get(c)})"
                          for c in rep["llm_only_non_deletion"]) or "none")]
    rate = rep["agreement_rate"]
    n, k = rep["llm_labelled"], rep["llm_labelled"] - len(rep["disagreements"])
    lines.append("Agreement (LLM vs prefilter): "
                 + (f"{rate * 100:.1f}% ({k}/{n})" if rate is not None else "n/a (no LLM labels)"))
    lines.append(f"Negation overrides: {rep['negation_overrides']}")
    src = rep["non_deletion_by_source"]
    lines.append("NON_DELETION by source: "
                 + " ".join(f"{k}={src[k]}" for k in ("negation_override", "llm", "prefilter")))
    lines.append(f"Fallback to prefilter (no LLM label): {rep['fallback']} of {rep['total']}")
    lines.append("Disagreeing cases: " + (", ".join(rep["disagreements"]) or "none"))
    return "\n".join(lines)
