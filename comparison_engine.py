"""Compare one uploaded product against its production record, attribute by attribute."""
from __future__ import annotations

import fnmatch
from dataclasses import dataclass, field
from typing import Any, Callable

from comparators.base import CompareContext, CompareResult
from comparators.exact import compare_boolean, compare_exact, compare_name
from comparators.keyvalue import compare_keyvalue
from comparators.listset import compare_images, compare_list
from comparators.nested import compare_nested
from comparators.numeric import compare_numeric
from comparators.text import compare_text
from config import Rules, norm_key
from field_resolver import FieldResolver, get_path
from mismatch_analyzer import explain
from utils.normalization import as_list, clean_cell, is_empty, parse_bool, parse_number

COMPARATORS: dict[str, Callable[[Any, Any, CompareContext], CompareResult]] = {
    "exact": compare_exact,
    "name": compare_name,
    "boolean": compare_boolean,
    "numeric": compare_numeric,
    "text": compare_text,
    "list": compare_list,
    "images": compare_images,
    "keyvalue": compare_keyvalue,
    "nested": compare_nested,
}

STATUSES = ("Match", "Partial", "Mismatch", "Missing in production", "N/A", "Not in schema")


@dataclass
class AttributeResult:
    attribute: str
    prod_path: str | None
    uploaded_value: Any
    production_value: Any
    comparator: str
    score: float | None        # 0..1, None when not scored
    status: str
    reason: str
    missing: list[str] = field(default_factory=list)
    extra: list[str] = field(default_factory=list)

    @property
    def pct(self) -> float | None:
        return None if self.score is None else round(self.score * 100, 1)


def pick_comparator(attribute: str, rules: Rules) -> str:
    key = norm_key(attribute)
    for rule in rules.comparators:
        if fnmatch.fnmatch(key, str(rule.get("pattern", "")).lower()):
            return str(rule.get("type", "text"))
    return "text"


def _infer(attr_type: str, up: Any, prod: Any) -> str:
    """Promote scalar comparators to list/nested when the data is list-shaped."""
    lists = [v for v in (up, prod) if isinstance(v, list)]
    has_dicts = any(isinstance(x, dict) for v in lists for x in v)
    if attr_type == "nested" and not has_dicts:
        # a leaf of a nested list (parsed.promotions.discount -> [10.05]) compares as numbers when it can
        vals = [x for v in (up, prod) for x in (v if isinstance(v, list) else [v])]
        return "numeric" if vals and all(parse_number(x) is not None for x in vals) else "list"
    if attr_type in ("list", "images", "keyvalue", "nested"):
        return attr_type
    if has_dicts:
        return "nested"
    if attr_type == "numeric":
        return attr_type  # numeric handles list-valued production paths itself
    if any(len(as_list(v)) > 1 for v in lists):
        return "list"
    return attr_type


def _unwrap(v: Any) -> Any:
    """Single-element lists compare as their element (prod ['Best price'] vs uploaded 'Best price')."""
    if isinstance(v, list):
        items = as_list(v)
        if len(items) == 1 and not isinstance(items[0], (dict, list)):
            return items[0]
    return v


def status_for(score: float, rules: Rules) -> str:
    if score >= rules.match_threshold:
        return "Match"
    if score >= rules.partial_threshold:
        return "Partial"
    return "Mismatch"


def _reason(col: str, status: str, res: CompareResult | None, up: Any, prod: Any, false_vs_empty: bool,
            resolver: FieldResolver | None) -> str:
    if false_vs_empty:
        return "Uploaded is False and production has no value; treated as no value (scoring.false_equals_empty)."
    if status == "Missing in production" and resolver:
        checked = resolver.candidates(col)
        return f"Uploaded has a value; production is empty or absent in {', '.join(checked)}."
    return explain(col, status, res, up, prod)


def compare_product(attributes: dict[str, Any], record: dict | None, resolver: FieldResolver | None,
                    rules: Rules) -> list[AttributeResult]:
    """Every uploaded column produces exactly one AttributeResult (nothing is silently skipped)."""
    results: list[AttributeResult] = []
    skip = rules.skip
    for col in attributes:
        if norm_key(col) in skip:
            continue
        up = attributes[col]
        ctype = pick_comparator(col, rules)
        path, prod, fell_back = (resolver.lookup(col, record) if (resolver and record is not None)
                                 else (None, None, False))
        prod = clean_cell(prod)
        if isinstance(prod, list):
            prod = [clean_cell(x) if not isinstance(x, (dict, list)) else x for x in prod]
            prod = [x for x in prod if not is_empty(x)] or None

        false_vs_empty = (ctype == "boolean" and rules.false_equals_empty
                          and parse_bool(up) is False and is_empty(prod))
        if is_empty(up) or false_vs_empty:
            status = "N/A"
            res = None
        elif record is not None and path is None:
            status = "Not in schema"
            res = None
        elif is_empty(prod):
            status = "Missing in production"
            res = None
        else:
            ctype = _infer(ctype, up, prod)
            fn = COMPARATORS.get(ctype, compare_text)
            ctx = CompareContext(col, rules.abs_tol, rules.rel_tol, rules.item_threshold)
            a, b = (up, prod) if ctype in ("list", "images", "nested", "keyvalue") else (_unwrap(up), _unwrap(prod))
            try:
                res = fn(a, b, ctx)
            except Exception as exc:  # a bad value must not abort the whole run
                res = CompareResult(0.0, f"{ctype} (error)", evidence={"kind": "error", "note": f"comparison error: {exc}"})
            status = status_for(res.score, rules)

        if res is not None and fell_back and resolver:
            primary = resolver.resolve(col)
            res.evidence["note"] = (res.evidence.get("note", "") + "; " if res.evidence.get("note") else "") + \
                f"{primary} is empty in production, so {path} was used"
        score = None
        if res is not None:
            score = res.score
        elif status == "Missing in production":
            score = 0.0
        results.append(AttributeResult(
            attribute=col, prod_path=path, uploaded_value=up, production_value=prod,
            comparator=(res.method if res else ctype), score=score, status=status,
            reason=_reason(col, status, res, up, prod, false_vs_empty, resolver),
            missing=(res.missing if res else []), extra=(res.extra if res else []),
        ))
    return results
