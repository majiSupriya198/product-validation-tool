"""Nested comparator: lists of objects (reviews/comments, promotions)."""
from __future__ import annotations

from typing import Any

from comparators.base import CompareContext, CompareResult
from comparators.listset import compare_list
from utils.normalization import as_list, is_empty, norm_strict, parse_number

ID_KEYS = ("reviewId", "id", "commentId")


def _key(obj: dict, i: int) -> str:
    for k in ID_KEYS:
        if not is_empty(obj.get(k)):
            return f"{k}={obj[k]}"
    return f"#{i}"


def _is_plain_number(v: Any) -> bool:
    if isinstance(v, (int, float)) and not isinstance(v, bool):
        return True
    return isinstance(v, str) and v.strip().replace(".", "", 1).replace(",", "", 1).lstrip("-").isdigit()


def _field_equal(a: Any, b: Any) -> bool:
    if _is_plain_number(a) and _is_plain_number(b):
        return abs(parse_number(a) - parse_number(b)) < 0.011
    return norm_strict(a) == norm_strict(b)


def compare_nested(up: Any, prod: Any, ctx: CompareContext) -> CompareResult:
    a = [x for x in as_list(up) if isinstance(x, dict)]
    b = [x for x in as_list(prod) if isinstance(x, dict)]
    if not a or not b:
        return compare_list(up, prod, ctx)
    bmap = {_key(o, i): o for i, o in enumerate(b)}
    item_scores, missing, field_diffs = [], [], []
    seen = set()
    for i, o in enumerate(a):
        k = _key(o, i)
        other = bmap.get(k)
        if other is None:
            missing.append(k)
            item_scores.append(0.0)
            continue
        seen.add(k)
        fields = [f for f in o if not is_empty(o.get(f))]
        if not fields:
            continue
        ok = 0
        for f in fields:
            if _field_equal(o.get(f), other.get(f)):
                ok += 1
            else:
                field_diffs.append(f"{k}.{f}")
        item_scores.append(ok / len(fields))
    extra = [k for k in bmap if k not in seen]
    denom = max(len(a), len(b)) or 1
    score = sum(item_scores) / denom
    ev = {"kind": "nested", "up_count": len(a), "prod_count": len(b), "paired": len(seen),
          "field_diffs": field_diffs[:15]}
    return CompareResult(score, "object match (paired by id, then position)", missing, extra, ev)
