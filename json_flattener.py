"""Flatten JSON objects into dotted columns without losing information, and store them safely in Excel.

- Nested objects become dotted keys: {"meta": {"keywords": "x"}} -> {"meta.keywords": "x"}.
- Arrays are kept whole (one cell, JSON-encoded) so element order and nested review objects survive.
- Excel cells hold at most 32,767 characters. Longer values are split into continuation columns
  (`col`, `col__part2`, `col__part3`, ...) and joined back on read, so nothing is truncated.
- Control characters that XLSX can't store are written as visible escapes (\\u0007) and counted.
"""
from __future__ import annotations

import json
import re
from typing import Any, Iterable

from openpyxl.cell.cell import ILLEGAL_CHARACTERS_RE

EXCEL_CELL_LIMIT = 32_000          # keep a margin below Excel's 32,767
PART_SUFFIX = "__part"
_PART_RE = re.compile(rf"^(?P<base>.+){PART_SUFFIX}(?P<n>\d+)$")


def parse_json_cell(value: Any) -> tuple[Any, str | None]:
    """Parse a JSON string cell. Returns (value, error). Empty -> (None, None)."""
    if value is None:
        return None, None
    if not isinstance(value, str):
        return value, None
    s = value.strip()
    if not s or s.lower() in ("null", "none", "nan"):
        return None, None
    try:
        return json.loads(s), None
    except (ValueError, TypeError) as exc:
        return None, f"invalid JSON ({exc.__class__.__name__}: {str(exc)[:80]})"


def flatten(obj: Any, prefix: str = "", sep: str = ".") -> dict[str, Any]:
    """Flatten nested dicts into dotted keys. Lists and scalars are leaf values (kept as-is)."""
    out: dict[str, Any] = {}
    if isinstance(obj, dict):
        if not obj and prefix:
            out[prefix] = {}
        for k, v in obj.items():
            key = f"{prefix}{sep}{k}" if prefix else str(k)
            if isinstance(v, dict) and v:
                out.update(flatten(v, key, sep))
            else:
                out[key] = v
    elif prefix:
        out[prefix] = obj
    return out


def to_cell_text(value: Any) -> Any:
    """Excel-safe cell value: lists/dicts as JSON, numbers/bools kept, illegal control chars escaped."""
    if value is None:
        return None
    if isinstance(value, bool):
        return str(value).lower()          # keep JSON spelling so "false" round-trips
    if isinstance(value, (int, float)):
        return value
    if isinstance(value, (list, dict)):
        value = json.dumps(value, ensure_ascii=False)
    s = str(value)
    if ILLEGAL_CHARACTERS_RE.search(s):
        s = ILLEGAL_CHARACTERS_RE.sub(lambda m: f"\\u{ord(m.group(0)):04x}", s)
    return s


def force_text(cell) -> None:
    """openpyxl turns strings starting with '=' into formulas; store them as plain text instead,
    so the value round-trips exactly (no added apostrophe)."""
    if cell.data_type == "f":
        cell.data_type = "s"


def count_illegal(value: Any) -> int:
    return len(ILLEGAL_CHARACTERS_RE.findall(value)) if isinstance(value, str) else 0


def split_long_cells(rows: list[dict[str, Any]], columns: list[str]) -> tuple[list[dict[str, Any]], list[str]]:
    """Split cells longer than the Excel limit into continuation columns. Returns (rows, columns)."""
    parts_needed: dict[str, int] = {}
    for r in rows:
        for c in columns:
            v = r.get(c)
            if isinstance(v, str) and len(v) > EXCEL_CELL_LIMIT:
                n = -(-len(v) // EXCEL_CELL_LIMIT)
                parts_needed[c] = max(parts_needed.get(c, 1), n)
    if not parts_needed:
        return rows, columns
    new_cols: list[str] = []
    for c in columns:
        new_cols.append(c)
        for i in range(2, parts_needed.get(c, 1) + 1):
            new_cols.append(f"{c}{PART_SUFFIX}{i}")
    out = []
    for r in rows:
        r2 = dict(r)
        for c, n in parts_needed.items():
            v = r.get(c)
            if isinstance(v, str) and len(v) > EXCEL_CELL_LIMIT:
                chunks = [v[i:i + EXCEL_CELL_LIMIT] for i in range(0, len(v), EXCEL_CELL_LIMIT)]
                r2[c] = chunks[0]
                for i, ch in enumerate(chunks[1:], 2):
                    r2[f"{c}{PART_SUFFIX}{i}"] = ch
        out.append(r2)
    return out, new_cols


def join_long_cells(row: dict[str, Any]) -> dict[str, Any]:
    """Inverse of split_long_cells for one row read back from Excel."""
    parts: dict[str, list[tuple[int, str]]] = {}
    out: dict[str, Any] = {}
    for k, v in row.items():
        m = _PART_RE.match(str(k))
        if m:
            parts.setdefault(m.group("base"), []).append((int(m.group("n")), "" if v is None else str(v)))
        else:
            out[k] = v
    for base, chunks in parts.items():
        head = out.get(base)
        tail = "".join(ch for _, ch in sorted(chunks))
        if tail:
            out[base] = ("" if head is None else str(head)) + tail
    return out


def ordered_columns(rows: Iterable[dict[str, Any]], first: list[str] | None = None) -> list[str]:
    """Union of keys in first-seen order, with `first` columns leading (when present)."""
    seen: dict[str, None] = {}
    for r in rows:
        for k in r:
            seen.setdefault(k, None)
    lead = [c for c in (first or []) if c in seen]
    return lead + [c for c in seen if c not in lead]
