"""Numeric comparator: tolerant of currency symbols, decimal commas and unit labels."""
from __future__ import annotations

from typing import Any

from comparators.base import CompareContext, CompareResult
from comparators.exact import compare_exact
from utils.normalization import as_list, parse_number, parse_unit


def _close(a: float, b: float, ctx: CompareContext) -> bool:
    diff = abs(a - b)
    return diff <= ctx.abs_tol + 1e-9 or diff <= ctx.rel_tol * max(abs(a), abs(b))


def compare_numeric(up: Any, prod: Any, ctx: CompareContext) -> CompareResult:
    # production nested paths can return a list (e.g. parsed.promotions.discount -> [0.2, 0.1])
    prod_vals = as_list(prod) if isinstance(prod, list) else [prod]
    up_vals = as_list(up) if isinstance(up, list) else [up]
    if len(up_vals) > 1:
        from comparators.listset import compare_list
        return compare_list(up, prod, ctx)

    a = parse_number(up_vals[0])
    if a is None:
        res = compare_exact(up, prod, ctx)
        res.evidence["note"] = "uploaded value is not numeric; compared as text"
        return res
    nums = [(parse_number(v), v) for v in prod_vals]
    nums = [(n, raw) for n, raw in nums if n is not None]
    if not nums:
        res = compare_exact(up, prod, ctx)
        res.evidence["note"] = "production value is not numeric; compared as text"
        return res

    ua = parse_unit(up_vals[0])
    best = min(nums, key=lambda x: abs(x[0] - a))
    b, raw_b = best
    ub = parse_unit(raw_b)
    ev = {"kind": "numeric", "up": a, "prod": b, "delta": round(b - a, 6),
          "up_unit": ua, "prod_unit": ub, "prod_candidates": len(nums)}
    if ua and ub and ua != ub:
        ev["unit_mismatch"] = True
        return CompareResult(0.0, "numeric", evidence=ev)
    score = 1.0 if _close(a, b, ctx) else 0.0
    return CompareResult(score, "numeric (tolerance)", evidence=ev)
