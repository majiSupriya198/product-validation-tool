"""Feedback section: review-level comparison of comments/reviews.

Pairing (order never matters):
  1. reviewId (or id / commentId)
  2. fallback key: normalized content + username + review date + rating
  3. fuzzy: same username (or both empty) and content word-F1 >= 0.9
Each paired review is scored field by field (rating, subject, content, username, date); the
attribute score depends on `reviews.score_basis`:
  max       sum(paired review scores) / max(uploaded reviews, production reviews)   (default)
  uploaded  sum(paired review scores) / uploaded reviews
  paired    mean score of the paired reviews (accuracy only, ignores coverage)
"""
from __future__ import annotations

from typing import Any

from comparators.base import CompareContext, CompareResult
from comparators.exact import token_f1
from utils.normalization import is_empty, norm_strict, parse_number, short, tokens

SECTION = "feedback"
TITLE = "Feedback"
ID_KEYS = ("reviewId", "id", "commentId", "review_id")
FIELDS = ("rating", "subject", "content", "username", "timestamp")


def _rid(r: dict) -> str | None:
    for k in ID_KEYS:
        v = r.get(k)
        if not is_empty(v):
            return str(v).strip()
    return None


def _date(v: Any) -> str:
    return str(v or "").strip()[:10]


def _fallback_key(r: dict) -> tuple:
    return (norm_strict(r.get("content"))[:300], norm_strict(r.get("username")), _date(r.get("timestamp")),
            parse_number(r.get("rating")))


def _field_score(f: str, a: Any, b: Any) -> tuple[float | None, str | None]:
    """(score, diff text). None score = not comparable (uploaded empty)."""
    if is_empty(a):
        return None, None
    if is_empty(b):
        return 0.0, f"{f}: missing in production"
    if f == "rating":
        x, y = parse_number(a), parse_number(b)
        ok = x is not None and y is not None and abs(x - y) < 1e-6
        return (1.0, None) if ok else (0.0, f"rating {a} vs {b}")
    if f == "timestamp":
        return (1.0, None) if _date(a) == _date(b) else (0.0, f"date {_date(a)} vs {_date(b)}")
    if f in ("content", "subject"):
        if norm_strict(a) == norm_strict(b):
            return 1.0, None
        s, *_ = token_f1(tokens(a), tokens(b))
        return s, f"{f} {round(s * 100)}% word overlap"
    return (1.0, None) if norm_strict(a) == norm_strict(b) else (0.0, f"{f} '{a}' vs '{b}'")


def _score_pair(a: dict, b: dict) -> tuple[float, list[str], dict[str, float]]:
    scores, diffs, per = [], [], {}
    for f in FIELDS:
        s, d = _field_score(f, a.get(f), b.get(f))
        if s is None:
            continue
        scores.append(s)
        per[f] = s
        if d:
            diffs.append(d)
    return (sum(scores) / len(scores) if scores else 1.0), diffs, per


def compare_reviews(up: Any, prod: Any, ctx: CompareContext, score_basis: str = "max") -> CompareResult:
    a = [r for r in (up or []) if isinstance(r, dict)]
    b = [r for r in (prod or []) if isinstance(r, dict)]
    used_b: set[int] = set()
    pairs: list[tuple[int, int, str]] = []
    b_by_id = {}
    for j, r in enumerate(b):
        rid = _rid(r)
        if rid is not None:
            b_by_id.setdefault(rid, j)
    unpaired = []
    for i, r in enumerate(a):
        rid = _rid(r)
        j = b_by_id.get(rid) if rid is not None else None
        if j is not None and j not in used_b:
            used_b.add(j)
            pairs.append((i, j, "reviewId"))
        else:
            unpaired.append(i)
    if unpaired:
        # fallbacks only pair reviews when at least one side has no ID: two different IDs are two reviews
        def can_pair(i: int, j: int) -> bool:
            return _rid(a[i]) is None or _rid(b[j]) is None

        b_by_key: dict[tuple, list[int]] = {}
        for j, r in enumerate(b):
            if j not in used_b:
                b_by_key.setdefault(_fallback_key(r), []).append(j)
        still = []
        for i in unpaired:
            j = next((j for j in b_by_key.get(_fallback_key(a[i]), []) if j not in used_b and can_pair(i, j)), None)
            if j is not None:
                used_b.add(j)
                pairs.append((i, j, "content+user+date+rating"))
            else:
                still.append(i)
        for i in still:
            ta, ua = tokens(a[i].get("content")), norm_strict(a[i].get("username"))
            best, best_j = 0.0, None
            for j, r in enumerate(b):
                if j in used_b or norm_strict(r.get("username")) != ua or not can_pair(i, j):
                    continue
                s, *_ = token_f1(ta, tokens(r.get("content")))
                if s > best:
                    best, best_j = s, j
            if best_j is not None and best >= 0.9:
                used_b.add(best_j)
                pairs.append((i, best_j, f"fuzzy content ({round(best * 100)}%)"))
        paired_a = {i for i, _, _ in pairs}
        unpaired = [i for i in range(len(a)) if i not in paired_a]

    details: list[dict] = []
    pair_scores: list[float] = []
    field_ok: dict[str, list[float]] = {f: [] for f in FIELDS}
    by_method: dict[str, int] = {}
    for i, j, method in sorted(pairs):
        s, diffs, per = _score_pair(a[i], b[j])
        pair_scores.append(s)
        for f, v in per.items():
            field_ok[f].append(v)
        by_method[method.split(" (")[0]] = by_method.get(method.split(" (")[0], 0) + 1
        details.append({"review": _rid(a[i]) or f"uploaded #{i + 1}", "pairing": method,
                        "status": "Match" if s >= 0.98 else ("Partial" if s >= 0.5 else "Mismatch"),
                        "score": round(s * 100, 1), "differences": "; ".join(diffs),
                        "uploaded": _summary(a[i]), "production": _summary(b[j])})
    for i in unpaired:
        details.append({"review": _rid(a[i]) or f"uploaded #{i + 1}", "pairing": "-", "status": "Missing in production",
                        "score": 0.0, "differences": "review not in production", "uploaded": _summary(a[i]), "production": ""})
    extra_idx = [j for j in range(len(b)) if j not in used_b]
    for j in extra_idx:
        details.append({"review": _rid(b[j]) or f"production #{j + 1}", "pairing": "-", "status": "Extra in production",
                        "score": None, "differences": "review not in upload", "uploaded": "", "production": _summary(b[j])})

    total = sum(pair_scores)
    if score_basis == "uploaded":
        score = total / len(a) if a else 1.0
    elif score_basis == "paired":
        score = total / len(pair_scores) if pair_scores else 0.0
    else:
        score = total / (max(len(a), len(b)) or 1)
    ev = {
        "kind": "reviews", "up_count": len(a), "prod_count": len(b), "paired": len(pairs), "by_method": by_method,
        "paired_accuracy": round(100 * total / len(pair_scores), 1) if pair_scores else None,
        "field_accuracy": {f: (round(100 * sum(v) / len(v)), len(v)) for f, v in field_ok.items() if v},
        "score_basis": score_basis,
        "differing": sum(1 for s in pair_scores if s < 0.98),
    }
    missing = [_rid(a[i]) or f"#{i + 1}" for i in unpaired]
    extra = [_rid(b[j]) or f"#{j + 1}" for j in extra_idx]
    return CompareResult(score, f"review match ({score_basis})", missing, extra, ev, details)


def default_type(key: str, up: Any, prod: Any) -> str:
    """Ratings/counts as numbers, review lists review by review, summaries as text."""
    lists = [v for v in (up, prod) if isinstance(v, list)]
    if any(isinstance(x, dict) for v in lists for x in v):
        return "reviews" if key.rsplit(".", 1)[-1].lower() in ("comments", "reviews", "coments") else "nested"
    if isinstance(up, dict) or isinstance(prod, dict):
        return "dict"
    if parse_number(up) is not None and parse_number(prod) is not None:
        return "numeric"
    return "text"


def compare(attributes: dict, record: dict | None, resolver, rules):
    from comparison_engine import compare_product
    return [r for r in compare_product(attributes, record, resolver, rules) if r.section == SECTION]


def _summary(r: dict) -> str:
    bits = [f"★{r.get('rating')}" if r.get("rating") is not None else "", str(r.get("username") or ""),
            _date(r.get("timestamp")), short(r.get("subject") or "", 60), short(r.get("content") or "", 160)]
    return " | ".join(b for b in bits if b)
