"""Map an uploaded column name to a value in a production record.

Production records are dicts shaped like the table row:
    {"extract": {...}, "parsed": {...}, "feedback": {...}, "gather": {...}, "metadata": {...}, ...}

Resolution order for an uploaded column C (first path that exists in the table schema wins):
    1. configured alias for C, or for C's first dotted segment (coments.content -> feedback.comments.content)
    2. extract.C
    3. C as a top-level path (parsed.price -> parsed.price)
    4. feedback.C
    5. case-insensitive match of C against extract/feedback/parsed field names
"""
from __future__ import annotations

from typing import Any, Iterable

from config import norm_key


def _walk(obj: Any, parts: list[str]) -> Any:
    """Follow a dotted path; lists fan out (promotions.discount -> [d1, d2]). Missing -> None."""
    cur = obj
    for i, p in enumerate(parts):
        if cur is None:
            return None
        if isinstance(cur, list):
            vals = [_walk(x, parts[i:]) for x in cur]
            vals = [v for v in vals if v is not None]
            flat: list[Any] = []
            for v in vals:
                flat.extend(v if isinstance(v, list) else [v])
            return flat or None
        if not isinstance(cur, dict):
            return None
        if p in cur:
            cur = cur[p]
        else:
            low = {k.lower(): k for k in cur}
            k = low.get(p.lower())
            if k is None:
                return None
            cur = cur[k]
    return cur


def get_path(record: dict, path: str) -> Any:
    return _walk(record, path.split("."))


def schema_paths_from_records(records: Iterable[dict]) -> set[str]:
    """Union of all dotted paths seen in records (fallback when no table schema is available)."""
    paths: set[str] = set()

    def visit(obj: Any, prefix: str) -> None:
        if isinstance(obj, dict):
            for k, v in obj.items():
                p = f"{prefix}.{k}" if prefix else k
                paths.add(p.lower())
                visit(v, p)
        elif isinstance(obj, list):
            for x in obj:
                visit(x, prefix)

    for r in records:
        visit(r, "")
    return paths


class FieldResolver:
    def __init__(self, aliases: dict[str, str], schema_paths: set[str]):
        self.aliases = aliases                       # normalized upload key -> production path
        self.schema = {p.lower() for p in schema_paths}
        self._cache: dict[str, str | None] = {}
        # index of leaf name -> full path, for case/format-insensitive fallback
        self._leaf: dict[str, str] = {}
        for p in sorted(self.schema, key=len):
            root = p.split(".")[0]
            if root in ("extract", "feedback", "parsed"):
                self._leaf.setdefault(norm_key(p.split(".", 1)[1]) if "." in p else norm_key(p), p)

    def _exists(self, path: str) -> bool:
        return path.lower() in self.schema

    def candidates(self, column: str) -> list[str]:
        """All production paths (in priority order) that exist in the table for an uploaded column.

        Order: alias → extract.C → C (top-level, e.g. parsed.price) → parsed.C → feedback.C → leaf-name match.
        The comparison uses the first one that has a value in the record, so an empty extract field
        falls back to the same field under parsed/feedback.
        """
        if column in self._cache:
            return self._cache[column]
        key = norm_key(column)
        raw: list[str] = []
        if key in self.aliases:
            raw.append(self.aliases[key])
        head, _, rest = column.partition(".")
        if rest and norm_key(head) in self.aliases:
            raw.append(f"{self.aliases[norm_key(head)]}.{rest}")
        raw += [f"extract.{column}", column, f"parsed.{column}"]
        # feedback.<col> only when extract has no such field: names like "summary" mean different
        # things under extract and feedback, so don't fall back across them on empty values
        if not self._exists(f"extract.{column}"):
            raw.append(f"feedback.{column}")
            if key in self._leaf:
                raw.append(self._leaf[key])
        found: list[str] = []
        for c in raw:
            if self._exists(c) and c.lower() not in {f.lower() for f in found}:
                found.append(c)
        self._cache[column] = found
        return found

    def resolve(self, column: str) -> str | None:
        """Primary production path for an uploaded column, or None when the table has no such field."""
        c = self.candidates(column)
        return c[0] if c else None

    def lookup(self, column: str, record: dict) -> tuple[str | None, object, bool]:
        """(path used, value, fell_back). Takes the first candidate path with a non-empty value."""
        from utils.normalization import is_empty

        cands = self.candidates(column)
        for i, path in enumerate(cands):
            val = get_path(record, path)
            if not is_empty(val):
                return path, val, i > 0
        return (cands[0] if cands else None), None, False
