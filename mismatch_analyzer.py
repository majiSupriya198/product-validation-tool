"""Turn comparator evidence into plain-language reasons.

Every sentence is built from what the comparison actually found. Nothing here guesses at
why a value differs (no "extraction failed" unless the production value is truly absent).
"""
from __future__ import annotations

from typing import Any

from comparators.base import CompareResult


def _fmt(n: Any) -> str:
    if isinstance(n, float):
        return f"{n:g}"
    return str(n)


def _list(items: list[str], limit: int = 8) -> str:
    items = [str(i) for i in items]
    if len(items) <= limit:
        return ", ".join(items)
    return ", ".join(items[:limit]) + f" (+{len(items) - limit} more)"


def explain(attribute: str, status: str, res: CompareResult | None, up_value: Any = None, prod_value: Any = None) -> str:
    if status == "N/A":
        return "Uploaded value is empty, so this attribute isn't scored." + (
            " Production has a value." if prod_value not in (None, "", [], {}) else "")
    if status == "Not in schema":
        return "The production table has no field that maps to this column, so it isn't scored."
    if status == "Missing in production":
        return "Uploaded has a value; the production record has this field empty or absent."
    if res is None:
        return ""
    ev = res.evidence or {}
    kind = ev.get("kind")
    pct = round(res.score * 100)
    parts: list[str] = []

    if kind == "exact":
        if ev.get("equal"):
            parts.append("Values are identical after normalization.")
        else:
            parts.append("Values differ after normalization (case, whitespace and punctuation ignored).")
    elif kind == "boolean":
        parts.append(f"Uploaded is {ev['up']}, production is {ev['prod']}." if ev["up"] != ev["prod"] else "Same boolean value.")
    elif kind == "numeric":
        if ev.get("unit_mismatch"):
            parts.append(f"Units differ: uploaded {ev['up_unit']}, production {ev['prod_unit']}.")
        if res.score >= 1:
            parts.append(f"Numbers match ({_fmt(ev['up'])} vs {_fmt(ev['prod'])}) within tolerance.")
        else:
            parts.append(f"Numbers differ: uploaded {_fmt(ev['up'])}, production {_fmt(ev['prod'])} (difference {_fmt(ev['delta'])}).")
        if ev.get("prod_candidates", 1) > 1:
            parts.append(f"Production has {ev['prod_candidates']} values here; the closest one was used.")
    elif kind in ("words", "text"):
        parts.append(f"{ev['common']} words in common; uploaded has {ev['up_count']} words, production {ev['prod_count']} ({pct}% word overlap).")
        if ev.get("containment"):
            parts.append(ev["containment"].capitalize() + ".")
        if res.missing:
            parts.append(f"Words only in uploaded: {_list(res.missing)}.")
        if res.extra:
            parts.append(f"Words only in production: {_list(res.extra)}.")
        if ev.get("missing_sentences"):
            parts.append(f"{len(ev['missing_sentences'])} of {ev['up_sentences']} uploaded sentences aren't in production.")
    elif kind == "list":
        parts.append(f"Uploaded has {ev['up_count']} items; production has {ev['prod_count']}; {ev['matched']} match.")
        if res.missing:
            parts.append(f"{len(res.missing)} missing in production: {_list(res.missing)}.")
        if res.extra:
            parts.append(f"{len(res.extra)} only in production: {_list(res.extra)}.")
        if ev.get("near_matches"):
            parts.append(f"Matched with small wording differences: {_list(ev['near_matches'], 4)}.")
    elif kind == "keyvalue":
        if ev["matched"]:
            parts.append(f"Matching: {_list(ev['matched'])}.")
        if ev["differ"]:
            parts.append(f"Different values: {_list(ev['differ'], 6)}.")
        if res.missing:
            parts.append(f"Missing in production: {_list(res.missing, 6)}.")
        if res.extra:
            parts.append(f"Only in production: {_list(res.extra, 6)}.")
        parts.append(f"{len(ev['matched'])} of {ev['keys']} entries match.")
    elif kind == "images":
        if ev.get("rehosted"):
            parts.append(f"Image URLs aren't comparable (production re-hosts images on {_list(ev['prod_hosts'], 2)}), so only counts are scored: uploaded {ev['up_count']}, production {ev['prod_count']}.")
            if ev["up_count"] > ev["prod_count"]:
                parts.append(f"{ev['up_count'] - ev['prod_count']} fewer image(s) in production.")
            elif ev["prod_count"] > ev["up_count"]:
                parts.append(f"{ev['prod_count'] - ev['up_count']} more image(s) in production.")
        else:
            parts.append(f"Uploaded has {ev['up_count']} images; production has {ev['prod_count']}; {ev['matched']} URLs match.")
            if res.missing:
                parts.append(f"{len(res.missing)} image(s) missing in production.")
            if res.extra:
                parts.append(f"{len(res.extra)} extra image(s) in production.")
    elif kind == "nested":
        parts.append(f"Uploaded has {ev['up_count']} entries, production {ev['prod_count']}; {ev['paired']} paired.")
        if res.missing:
            parts.append(f"Not found in production: {_list(res.missing, 5)}.")
        if ev.get("field_diffs"):
            parts.append(f"Fields that differ: {_list(ev['field_diffs'], 6)}.")
    if ev.get("note"):
        parts.append(f"Note: {ev['note']}.")
    return " ".join(parts)
