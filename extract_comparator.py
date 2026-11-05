"""Extract section: product content attributes (name, description, ingredients, images, ...).

Every field the upload has under `extract` is compared with production `extract.<field>`.
Type-specific comparators come from config/validation.yaml; anything not listed there is
compared as normalized text, or as numbers when both values are numeric.
"""
from __future__ import annotations

from typing import Any

from utils.normalization import parse_number

SECTION = "extract"
TITLE = "Extract"


def default_type(key: str, up: Any, prod: Any) -> str:
    if isinstance(up, dict) or isinstance(prod, dict):
        return "dict"
    if parse_number(up) is not None and parse_number(prod) is not None and _plain_number(up) and _plain_number(prod):
        return "numeric"
    return "text"


def _plain_number(v: Any) -> bool:
    if isinstance(v, (int, float)) and not isinstance(v, bool):
        return True
    return isinstance(v, str) and v.strip().replace(".", "", 1).replace(",", "", 1).lstrip("-").isdigit()


def compare(attributes: dict, record: dict | None, resolver, rules):
    from comparison_engine import compare_product
    return [r for r in compare_product(attributes, record, resolver, rules) if r.section == SECTION]
