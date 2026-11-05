"""Compare one uploaded product against its production record, attribute by attribute.

Every attribute belongs to one section, which drives scoring and the report:
  extract   product content (extract.*)
  parsed    normalized values (parsed.*)
  feedback  ratings and reviews (feedback.*)
Section-qualified uploads (standardized workbooks) carry the section in the key
("parsed.price"); for flat uploads (Flink_PDP.xlsx) the section comes from the production
field the column maps to.
"""
from __future__ import annotations

import fnmatch
from dataclasses import dataclass, field
from functools import partial
from typing import Any, Callable

import extract_comparator
import feedback_comparator
import parsed_comparator
from comparators.base import CompareContext, CompareResult
from comparators.exact import compare_boolean, compare_exact, compare_name, compare_unit
from comparators.keyvalue import compare_keyvalue
from comparators.listset import compare_images, compare_list
from comparators.nested import compare_dict, compare_nested
from comparators.numeric import compare_numeric
from comparators.text import compare_text
from config import Rules, norm_key
from field_resolver import FieldResolver
from mismatch_analyzer import explain
from utils.normalization import as_list, clean_cell, is_empty, parse_bool, parse_number

SECTIONS = ("extract", "parsed", "feedback")
SECTION_MODULES = {m.SECTION: m for m in (extract_comparator, parsed_comparator, feedback_comparator)}

COMPARATORS: dict[str, Callable[[Any, Any, CompareContext], CompareResult]] = {
    "exact": compare_exact,
    "unit": compare_unit,
    "name": compare_name,
    "boolean": compare_boolean,
    "numeric": compare_numeric,
    "text": compare_text,
    "list": compare_list,
    "images": compare_images,
    "keyvalue": compare_keyvalue,
    "nested": compare_nested,
    "dict": compare_dict,
}
WHOLE_VALUE_TYPES = ("list", "images", "nested", "keyvalue", "dict", "reviews")

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
    section: str = "extract"
    details: list[dict] = field(default_factory=list)   # review-level rows for feedback comments

    @property
    def pct(self) -> float | None:
        return None if self.score is None else round(self.score * 100, 1)

    @property
    def field(self) -> str:
        """Attribute name without the section prefix (for display)."""
        head, _, rest = self.attribute.partition(".")
        return rest if head in SECTIONS and rest else self.attribute


def section_of(attribute: str, prod_path: str | None) -> str:
    head = attribute.split(".", 1)[0].lower()
    if head in SECTIONS and "." in attribute:
        return head
    root = (prod_path or "").split(".", 1)[0].lower()
    return root if root in SECTIONS else "extract"


def _strip_section(key: str) -> str:
    head, _, rest = key.partition(".")
    return rest if head in SECTIONS and rest else key


def pick_comparator(attribute: str, rules: Rules, section: str | None = None,
                    up: Any = None, prod: Any = None) -> str:
    """Configured comparator for the attribute (full key, then key without section prefix);
    otherwise the section's default for these values."""
    full = norm_key(attribute)
    stripped = norm_key(_strip_section(attribute))
    for rule in rules.comparators:
        pat = str(rule.get("pattern", "")).lower()
        if pat == "*":
            continue
        if fnmatch.fnmatch(full, pat) or fnmatch.fnmatch(stripped, pat):
            return str(rule.get("type", "text"))
    mod = SECTION_MODULES.get(section or "extract", extract_comparator)
    return mod.default_type(attribute, up, prod)


def _infer(attr_type: str, up: Any, prod: Any) -> str:
    """Promote scalar comparators to list/nested when the data is list-shaped."""
    if attr_type == "reviews":
        return attr_type
    if isinstance(up, dict) and isinstance(prod, dict):
        return "dict"
    lists = [v for v in (up, prod) if isinstance(v, list)]
    has_dicts = any(isinstance(x, dict) for v in lists for x in v)
    if attr_type == "nested" and not has_dicts:
        # a leaf of a nested list (parsed.promotions.discount -> [10.05]) compares as numbers when it can
        vals = [x for v in (up, prod) for x in (v if isinstance(v, list) else [v])]
        return "numeric" if vals and all(parse_number(x) is not None for x in vals) else "list"
    if attr_type in ("list", "images", "keyvalue", "nested", "dict"):
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


def _skipped(col: str, skip: set[str]) -> bool:
    return norm_key(col) in skip or norm_key(_strip_section(col)) in skip


def compare_attribute(col: str, up: Any, record: dict | None, resolver: FieldResolver | None,
                      rules: Rules) -> AttributeResult:
    path, prod, fell_back = (resolver.lookup(col, record) if (resolver and record is not None)
                             else (None, None, False))
    prod = clean_cell(prod)
    if isinstance(prod, list):
        prod = [clean_cell(x) if not isinstance(x, (dict, list)) else x for x in prod]
        prod = [x for x in prod if not is_empty(x)] or None
    section = section_of(col, path or (resolver.resolve(col) if resolver else None))
    ctype = pick_comparator(col, rules, section, up, prod)

    false_vs_empty = (ctype == "boolean" and rules.false_equals_empty
                      and parse_bool(up) is False and is_empty(prod))
    res: CompareResult | None = None
    if is_empty(up) or false_vs_empty:
        status = "N/A"
    elif record is not None and path is None:
        status = "Not in schema"
    elif is_empty(prod):
        status = "Missing in production"
    else:
        ctype = _infer(ctype, up, prod)
        ctx = CompareContext(col, rules.abs_tol, rules.rel_tol, rules.item_threshold, rules.round_to_uploaded)
        if ctype == "reviews":
            fn = partial(feedback_comparator.compare_reviews, score_basis=rules.review_score_basis)
        else:
            fn = COMPARATORS.get(ctype, compare_text)
        a, b = (up, prod) if ctype in WHOLE_VALUE_TYPES else (_unwrap(up), _unwrap(prod))
        try:
            res = fn(a, b, ctx)
        except Exception as exc:  # a bad value must not abort the whole run
            res = CompareResult(0.0, f"{ctype} (error)", evidence={"kind": "error", "note": f"comparison error: {exc}"})
        status = status_for(res.score, rules)

    if res is not None and fell_back and resolver:
        primary = resolver.resolve(col)
        res.evidence["note"] = (res.evidence.get("note", "") + "; " if res.evidence.get("note") else "") + \
            f"{primary} is empty in production, so {path} was used"
    score = res.score if res is not None else (0.0 if status == "Missing in production" else None)
    return AttributeResult(
        attribute=col, prod_path=path, uploaded_value=up, production_value=prod,
        comparator=(res.method if res else ctype), score=score, status=status,
        reason=_reason(col, status, res, up, prod, false_vs_empty, resolver),
        missing=(res.missing if res else []), extra=(res.extra if res else []),
        section=section, details=(res.details if res else []),
    )


def compare_product(attributes: dict[str, Any], record: dict | None, resolver: FieldResolver | None,
                    rules: Rules) -> list[AttributeResult]:
    """Every uploaded attribute produces exactly one AttributeResult (nothing is silently skipped)."""
    skip = rules.skip
    return [compare_attribute(col, attributes[col], record, resolver, rules)
            for col in attributes if not _skipped(col, skip)]
