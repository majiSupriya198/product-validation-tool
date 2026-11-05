"""Value normalization shared by the file reader and comparators."""
from __future__ import annotations

import html
import json
import math
import re
import unicodedata
from typing import Any

EMPTY_STRINGS = {"", "null", "none", "nan", "n/a", "na", "[]", "{}", "undefined", "-"}

_WS = re.compile(r"\s+")
_TAGS = re.compile(r"<[^>]+>")
_TOKEN = re.compile(r"[0-9]+(?:[.,][0-9]+)?|[^\W\d_]+", re.UNICODE)
_NUM = re.compile(r"[-+]?\d+(?:[.,]\d+)*")


def is_empty(value: Any) -> bool:
    """True for None, NaN, whitespace-only strings, placeholder strings and empty containers."""
    if value is None:
        return True
    if isinstance(value, float) and math.isnan(value):
        return True
    if isinstance(value, (list, tuple, set, dict)):
        if not value:
            return True
        if isinstance(value, dict):
            return all(is_empty(v) for v in value.values())
        return all(is_empty(v) for v in value)
    if isinstance(value, str):
        return value.strip().lower() in EMPTY_STRINGS
    return False


def clean_cell(value: Any) -> Any:
    """Strip, decode HTML entities, parse JSON-looking lists/objects. Empty -> None."""
    if is_empty(value):
        return None
    if isinstance(value, str):
        s = html.unescape(value).strip()
        if is_empty(s):
            return None
        if (s.startswith("[") and s.endswith("]")) or (s.startswith("{") and s.endswith("}")):
            try:
                parsed = json.loads(s)
                return None if is_empty(parsed) else parsed
            except (ValueError, TypeError):
                pass
        return s
    return value


def norm_text(value: Any) -> str:
    """Lowercase, NFKC, HTML/tags removed, whitespace collapsed."""
    if value is None:
        return ""
    if isinstance(value, (list, tuple)):
        value = " ".join(str(v) for v in value if v is not None)
    s = html.unescape(str(value))
    s = _TAGS.sub(" ", s)
    s = unicodedata.normalize("NFKC", s).lower()
    s = s.replace(" ", " ")
    return _WS.sub(" ", s).strip()


def norm_strict(value: Any) -> str:
    """norm_text plus punctuation removed: for exact-style identity comparison."""
    s = norm_text(value)
    s = re.sub(r"[^\w\s%]", " ", s, flags=re.UNICODE)
    return _WS.sub(" ", s).strip()


def tokens(value: Any) -> list[str]:
    """Word and number tokens. Decimal commas normalized to dots so 2,5 == 2.5."""
    return [t.replace(",", ".") for t in _TOKEN.findall(norm_text(value))]


def parse_number(value: Any) -> float | None:
    """Parse the first number in a value. Handles '1.99 €', '13,27', '1.234,56', '2,223'."""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return None if (isinstance(value, float) and math.isnan(value)) else float(value)
    m = _NUM.search(str(value))
    if not m:
        return None
    raw = m.group(0)
    if "," in raw and "." in raw:
        # whichever separator comes last is the decimal separator
        if raw.rfind(",") > raw.rfind("."):
            raw = raw.replace(".", "").replace(",", ".")
        else:
            raw = raw.replace(",", "")
    elif "," in raw:
        raw = raw.replace(",", ".") if raw.count(",") == 1 else raw.replace(",", "")
    elif raw.count(".") > 1:
        raw = raw.replace(".", "")
    try:
        return float(raw)
    except ValueError:
        return None


_UNIT = re.compile(r"(?:/\s*\d*\s*)?(kilogram|kg|gram|g|mg|milliliter|ml|cl|liter|litre|l|stk|stück|st|pcs|pce|pc|piece|pieces|each|ea|ct|count|units?|lb|oz|m|cm)\b", re.I)
_UNIT_CANON = {"stück": "stk", "st": "stk", "pcs": "stk", "piece": "stk", "pieces": "stk", "each": "stk", "ea": "stk",
               "unit": "stk", "units": "stk", "ea": "stk", "pc": "stk", "pce": "stk", "ct": "stk", "count": "stk", "liter": "l", "litre": "l", "kilogram": "kg", "gram": "g", "milliliter": "ml"}


def parse_unit(value: Any) -> str | None:
    """Unit label in a quantity/price string: '13.27 € / 1KG' -> 'kg', '0.15 KG' -> 'kg'."""
    if value is None or isinstance(value, (int, float)):
        return None
    m = _UNIT.search(str(value))
    if not m:
        return None
    u = m.group(1).lower()
    return _UNIT_CANON.get(u, u)


def parse_bool(value: Any) -> bool | None:
    if isinstance(value, bool):
        return value
    if value is None:
        return None
    s = str(value).strip().lower()
    if s in {"true", "1", "yes", "y", "t"}:
        return True
    if s in {"false", "0", "no", "n", "f"}:
        return False
    return None


def as_list(value: Any) -> list[Any]:
    """Coerce to a list: JSON strings parsed, scalars wrapped, empties removed."""
    if value is None:
        return []
    if isinstance(value, str):
        cleaned = clean_cell(value)
        if cleaned is None:
            return []
        if not isinstance(cleaned, str):
            return as_list(cleaned)
        return [cleaned]
    if isinstance(value, dict):
        return [value]
    if isinstance(value, (list, tuple)):
        return [v for v in value if not is_empty(v)]
    return [value]


def split_items(text: str) -> list[str]:
    """Split an ingredient-style list on top-level commas/semicolons/pipes.

    Commas inside brackets are kept, and decimal commas ('13, 5%' or '2,5 g') are not split.
    """
    if not text:
        return []
    s = str(text)
    # repair "13, 5%" style broken decimals before splitting
    s = re.sub(r"(\d),\s+(\d+\s*%)", r"\1,\2", s)
    items, buf, depth = [], [], 0
    for i, ch in enumerate(s):
        if ch in "([{":
            depth += 1
        elif ch in ")]}":
            depth = max(0, depth - 1)
        if depth == 0 and ch in ",;|":
            prev_digit = i > 0 and s[i - 1].isdigit()
            next_digit = i + 1 < len(s) and s[i + 1].isdigit()
            if ch == "," and prev_digit and next_digit:
                buf.append(ch)
                continue
            items.append("".join(buf))
            buf = []
            continue
        buf.append(ch)
    items.append("".join(buf))
    out = []
    for it in items:
        it = it.strip().strip(".").strip()
        if it and not is_empty(it):
            out.append(it)
    return out


def jaccard(a: list[str], b: list[str]) -> float:
    sa, sb = set(a), set(b)
    if not sa and not sb:
        return 1.0
    return len(sa & sb) / len(sa | sb)


def short(value: Any, limit: int = 300) -> str:
    """Readable single-line rendering for reports."""
    if value is None:
        return ""
    if isinstance(value, (list, dict)):
        s = json.dumps(value, ensure_ascii=False, default=str)
    else:
        s = str(value)
    s = _WS.sub(" ", s).strip()
    return s if len(s) <= limit else s[: limit - 1] + "…"
