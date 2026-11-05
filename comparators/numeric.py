"""Numeric comparator: tolerant of currency symbols, decimal commas and unit labels."""
from __future__ import annotations

import re
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
    if _close(a, b, ctx):
        return CompareResult(1.0, "numeric (tolerance)", evidence=ev)
    # uploaded values are often display-rounded (rating "4.5" vs production 4.4694): equal once
    # production is rounded to the uploaded value's decimal places
    places = _decimals(up_vals[0])
    if ctx.round_to_uploaded and places and _decimals(raw_b, b) > places and round(b, places) == round(a, places):
        ev["note"] = f"equal after rounding production to {places} decimal place{'s' if places > 1 else ''}"
        return CompareResult(1.0, "numeric (rounded to uploaded precision)", evidence=ev)
    return CompareResult(0.0, "numeric (tolerance)", evidence=ev)


def _decimals(raw: Any, value: float | None = None) -> int:
    """Decimal places as written: '4.5' -> 1, '13,27 €' -> 2, 4.469387 -> 6, '3656' -> 0."""
    if isinstance(raw, bool):
        return 0
    if isinstance(raw, (int, float)) and value is None:
        value = float(raw)
    if isinstance(raw, str):
        m = re.search(r"\d+[.,](\d+)", raw)
        return len(m.group(1)) if m else 0
    if value is not None:
        s = repr(float(value))
        return 0 if s.endswith(".0") else len(s.split(".")[1]) if "." in s else 0
    return 0
