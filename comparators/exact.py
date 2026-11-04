"""Exact (after normalization), name-like and boolean comparators."""
from __future__ import annotations

from collections import Counter
from typing import Any

from comparators.base import CompareContext, CompareResult
from utils.normalization import norm_strict, parse_bool, tokens


def compare_exact(up: Any, prod: Any, ctx: CompareContext) -> CompareResult:
    a, b = norm_strict(up), norm_strict(prod)
    if a == b:
        return CompareResult(1.0, "exact", evidence={"kind": "exact", "equal": True})
    # identical once spaces are removed (e.g. "in_stock" vs "in stock")
    if a.replace(" ", "") == b.replace(" ", ""):
        return CompareResult(1.0, "exact", evidence={"kind": "exact", "equal": True, "note": "differs only in spacing/punctuation"})
    return CompareResult(0.0, "exact", evidence={"kind": "exact", "equal": False})


def token_f1(a_tokens: list[str], b_tokens: list[str]) -> tuple[float, list[str], list[str], int]:
    """Multiset token F1 = 2*|common| / (|a|+|b|). Returns score, missing (a-b), extra (b-a), common count."""
    ca, cb = Counter(a_tokens), Counter(b_tokens)
    common = sum((ca & cb).values())
    total = sum(ca.values()) + sum(cb.values())
    score = (2 * common / total) if total else 1.0
    missing_c, extra_c = ca - cb, cb - ca
    # keep order of first appearance for readability
    missing = list(dict.fromkeys(t for t in a_tokens if missing_c.get(t)))
    extra = list(dict.fromkeys(t for t in b_tokens if extra_c.get(t)))
    return score, missing, extra, common


def compare_name(up: Any, prod: Any, ctx: CompareContext) -> CompareResult:
    """Exact after normalization = 100%; otherwise word-level F1 so near-identical names score high."""
    a, b = norm_strict(up), norm_strict(prod)
    if a == b:
        return CompareResult(1.0, "exact (normalized)", evidence={"kind": "exact", "equal": True})
    ta, tb = tokens(up), tokens(prod)
    score, missing, extra, common = token_f1(ta, tb)
    return CompareResult(score, "word overlap (F1)", missing, extra,
                         {"kind": "words", "common": common, "up_count": len(ta), "prod_count": len(tb)})


def compare_boolean(up: Any, prod: Any, ctx: CompareContext) -> CompareResult:
    a, b = parse_bool(up), parse_bool(prod)
    if a is None or b is None:
        return compare_exact(up, prod, ctx)
    return CompareResult(1.0 if a == b else 0.0, "boolean", evidence={"kind": "boolean", "up": a, "prod": b})
