"""Parsed section: normalized values derived from the extract (price, currency, quantities, promotions).

Numbers are compared with the configured tolerance, labels (currency, units, promotion type)
exactly, and `promotions` object by object. Uploaded fields missing in production are flagged.
"""
from __future__ import annotations

from typing import Any

from utils.normalization import parse_number

SECTION = "parsed"
TITLE = "Parsed"


def default_type(key: str, up: Any, prod: Any) -> str:
    if isinstance(up, dict) or isinstance(prod, dict):
        return "dict"
    if isinstance(up, list) or isinstance(prod, list):
        return "nested"
    if parse_number(up) is not None and parse_number(prod) is not None:
        return "numeric"
    return "exact"


def compare(attributes: dict, record: dict | None, resolver, rules):
    from comparison_engine import compare_product
    return [r for r in compare_product(attributes, record, resolver, rules) if r.section == SECTION]
