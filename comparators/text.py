"""Long-text comparator: word-level F1 with missing/extra words and sentence coverage."""
from __future__ import annotations

import re
from typing import Any

from comparators.base import CompareContext, CompareResult
from comparators.exact import token_f1
from utils.normalization import norm_strict, norm_text, tokens

_SENT = re.compile(r"(?<=[.!?])\s+|\n+|\s*\|\s*")


def _sentences(text: str) -> list[str]:
    return [s.strip() for s in _SENT.split(text) if len(s.strip()) > 2]


def compare_text(up: Any, prod: Any, ctx: CompareContext) -> CompareResult:
    if norm_strict(up) == norm_strict(prod):
        note = None if norm_text(up) == norm_text(prod) else "differs only in case/punctuation/whitespace"
        ev = {"kind": "exact", "equal": True}
        if note:
            ev["note"] = note
        return CompareResult(1.0, "text (normalized equal)", evidence=ev)

    ta, tb = tokens(up), tokens(prod)
    score, missing, extra, common = token_f1(ta, tb)
    ev: dict[str, Any] = {"kind": "text", "common": common, "up_count": len(ta), "prod_count": len(tb)}

    # sentence coverage helps explain long descriptions: which uploaded sentences are absent
    sa = _sentences(norm_text(up))
    if len(sa) > 1:
        pb = norm_strict(prod)
        missing_sents = [s for s in sa if norm_strict(s) and norm_strict(s) not in pb]
        ev["up_sentences"] = len(sa)
        ev["missing_sentences"] = missing_sents[:5]
    pa = norm_strict(up)
    pb_ = norm_strict(prod)
    if pa and pb_ and (pa in pb_ or pb_ in pa):
        ev["containment"] = "production contains uploaded text" if pa in pb_ else "uploaded contains production text"
    return CompareResult(score, "word overlap (F1)", missing, extra, ev)
