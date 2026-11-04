"""Key/value comparator for nutrition-style fields ("Energie: 2,223 kJ | Fett: 33 g | ...")."""
from __future__ import annotations

import re
from typing import Any

from comparators.base import CompareContext, CompareResult
from comparators.text import compare_text
from utils.normalization import as_list, norm_strict, parse_number, split_items

_NUMS = re.compile(r"[-+]?\d+(?:[.,]\d+)?")
_KV_NOCOLON = re.compile(r"^(?P<k>[^\d<>~]+?)\s+(?P<v>[<>~]?\s*\d.*)$")
# table headers such as "Nährwerte (je 100 g):", "Nährwerte bei 100 g", "Nutrition facts per 100ml"
_HEADER = re.compile(
    r"^\s*(?:durchschnittliche\s+)?(?:nährwerte|nährwertangaben|nährwertinformationen|voedingswaarden(?:\s*\(?gemiddeld\)?)?|"
    r"valeurs nutritionnelles|nutrition(?:al)?(?:\s+(?:facts|information|values|declaration))?|typical values)"
    r"\s*\(?\s*(?:(?:je|pro|bei|per|pour|for|par)\s*)?(?:100\s*(?:g|ml)|portion|serving)?\s*\)?\s*:?\s*",
    re.I,
)


def parse_pairs(value: Any) -> dict[str, tuple[str, str]]:
    """Return {normalized key: (display key, raw value)}. Header prefixes like 'Nährwerte (je 100 g):' are dropped."""
    if isinstance(value, dict):
        return {norm_strict(k): (str(k), str(v)) for k, v in value.items() if norm_strict(k)}
    items = as_list(value)
    text = " | ".join(str(i) for i in items) if len(items) > 1 else (str(items[0]) if items else "")
    pairs: dict[str, tuple[str, str]] = {}
    text = _HEADER.sub("", text, count=1)
    for seg in split_items(text):
        if ":" in seg:
            parts = seg.split(":")
            key, val = parts[-2].strip(), parts[-1].strip()
        else:
            m = _KV_NOCOLON.match(seg.strip())
            if not m:
                continue
            key, val = m.group("k").strip(), m.group("v").strip()
        nk = norm_strict(key)
        if nk:  # labels without a value ("Fett:") are kept so label-only tables still compare
            pairs.setdefault(nk, (key.lstrip("- ").strip(), val))
    return pairs


def _numbers(v: str) -> list[float]:
    out = []
    for m in _NUMS.findall(v):
        n = parse_number(m)
        if n is not None:
            out.append(n)
    return out


def _values_equal(a: str, b: str, ctx: CompareContext) -> bool:
    na, nb = _numbers(a), _numbers(b)
    if na and nb:
        return len(na) == len(nb) and all(abs(x - y) <= max(ctx.abs_tol, ctx.rel_tol * max(abs(x), abs(y))) + 1e-9 for x, y in zip(na, nb))
    return norm_strict(a) == norm_strict(b)


def compare_keyvalue(up: Any, prod: Any, ctx: CompareContext) -> CompareResult:
    pa, pb = parse_pairs(up), parse_pairs(prod)
    if not pa or not pb:
        res = compare_text(up, prod, ctx)
        res.evidence["note"] = "could not parse key/value pairs on both sides; compared as text"
        return res
    # a key with leftover header words ("je portion energie") still pairs with "energie" by word suffix
    for k in [k for k in pa if k not in pb]:
        hit = next((kb for kb in pb if kb not in pa and (k.endswith(" " + kb) or kb.endswith(" " + k))), None)
        if hit:
            pa[hit] = pa.pop(k)
    keys = list(dict.fromkeys(list(pa) + list(pb)))
    matched, differ, missing, extra = [], [], [], []
    for k in keys:
        if k in pa and k in pb:
            if _values_equal(pa[k][1], pb[k][1], ctx):
                matched.append(pa[k][0])
            else:
                differ.append(f"{pa[k][0]}: {pa[k][1]} vs {pb[k][1]}")
        elif k in pa:
            missing.append(f"{pa[k][0]}: {pa[k][1]}")
        else:
            extra.append(f"{pb[k][0]}: {pb[k][1]}")
    score = len(matched) / len(keys) if keys else 1.0
    ev = {"kind": "keyvalue", "keys": len(keys), "matched": matched, "differ": differ,
          "up_count": len(pa), "prod_count": len(pb)}
    return CompareResult(score, "per-key match (matched / all keys)", missing, extra, ev)
