"""List comparators: ingredients/bullet points/badges, and image URLs."""
from __future__ import annotations

from typing import Any
from urllib.parse import urlparse

from comparators.base import CompareContext, CompareResult
from utils.normalization import as_list, jaccard, norm_strict, split_items, tokens


def to_items(value: Any) -> list[str]:
    """List-like value -> list of item strings. A single string is split on top-level separators."""
    items = as_list(value)
    if len(items) == 1 and isinstance(items[0], str):
        return split_items(items[0])
    out: list[str] = []
    for it in items:
        if isinstance(it, (dict, list)):
            out.append(str(it))
        else:
            out.append(str(it).strip())
    return [i for i in out if i]


def match_items(a: list[str], b: list[str], threshold: float) -> tuple[list[tuple[str, str, float]], list[str], list[str]]:
    """Greedy one-to-one matching: exact normalized first, then best token-Jaccard >= threshold."""
    na = [norm_strict(x) for x in a]
    nb = [norm_strict(x) for x in b]
    used_b: set[int] = set()
    pairs: list[tuple[str, str, float]] = []
    unmatched_a: list[int] = []
    for i, x in enumerate(na):
        j = next((j for j, y in enumerate(nb) if j not in used_b and y == x), None)
        if j is not None:
            used_b.add(j)
            pairs.append((a[i], b[j], 1.0))
        else:
            unmatched_a.append(i)
    still: list[int] = []
    tb = [tokens(y) for y in b]
    for i in unmatched_a:
        ta = tokens(a[i])
        best, best_j = 0.0, None
        for j in range(len(b)):
            if j in used_b:
                continue
            s = jaccard(ta, tb[j])
            if s > best:
                best, best_j = s, j
        if best_j is not None and best >= threshold:
            used_b.add(best_j)
            pairs.append((a[i], b[best_j], best))
        else:
            still.append(i)
    missing = [a[i] for i in still]
    extra = [b[j] for j in range(len(b)) if j not in used_b]
    return pairs, missing, extra


def compare_list(up: Any, prod: Any, ctx: CompareContext) -> CompareResult:
    a, b = to_items(up), to_items(prod)
    pairs, missing, extra = match_items(a, b, ctx.item_threshold)
    denom = max(len(a), len(b)) or 1
    score = len(pairs) / denom
    near = [(x, y) for x, y, s in pairs if s < 1.0]
    ev = {"kind": "list", "up_count": len(a), "prod_count": len(b), "matched": len(pairs),
          "near_matches": [f"{x} ≈ {y}" for x, y in near][:10]}
    return CompareResult(score, "item match (matched / max count)", missing, extra, ev)


def _url_key(u: str) -> str:
    p = urlparse(str(u).strip())
    return (p.netloc.lower() + p.path).rstrip("/")


def compare_images(up: Any, prod: Any, ctx: CompareContext) -> CompareResult:
    a = [str(x) for x in as_list(up) if isinstance(x, str)]
    b = [str(x) for x in as_list(prod) if isinstance(x, str)]
    hosts_a = {urlparse(x).netloc.lower() for x in a}
    hosts_b = {urlparse(x).netloc.lower() for x in b}
    denom = max(len(a), len(b)) or 1
    if hosts_a & hosts_b:
        ka, kb = [_url_key(x) for x in a], [_url_key(x) for x in b]
        matched = len(set(ka) & set(kb))
        missing = [x for x, k in zip(a, ka) if k not in set(kb)]
        extra = [x for x, k in zip(b, kb) if k not in set(ka)]
        ev = {"kind": "images", "rehosted": False, "up_count": len(a), "prod_count": len(b), "matched": matched}
        return CompareResult(matched / denom, "image URL set match", missing, extra, ev)
    # production re-hosts images on its own asset store, so URLs can't be compared: count only
    ev = {"kind": "images", "rehosted": True, "up_count": len(a), "prod_count": len(b),
          "up_hosts": sorted(hosts_a), "prod_hosts": sorted(hosts_b)}
    score = min(len(a), len(b)) / denom
    return CompareResult(score, "image count (URLs re-hosted)", evidence=ev)
