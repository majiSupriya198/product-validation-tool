"""Product-level metrics. Data match % and production completeness % are kept separate."""
from __future__ import annotations

from dataclasses import dataclass

from comparison_engine import AttributeResult
from config import Rules, norm_key


@dataclass
class ProductScore:
    match_pct: float | None          # weighted mean of attribute scores (comparable attributes only)
    completeness_pct: float | None   # share of uploaded, in-schema attributes that production fills
    matched: int
    partial: int
    mismatched: int
    missing_in_prod: int
    unavailable: int                 # N/A (uploaded empty) + not in production schema
    comparable: int


def score_product(results: list[AttributeResult], rules: Rules) -> ProductScore:
    counts = {s: 0 for s in ("Match", "Partial", "Mismatch", "Missing in production", "N/A", "Not in schema")}
    for r in results:
        counts[r.status] = counts.get(r.status, 0) + 1

    weights = rules.weights
    num = den = 0.0
    for r in results:
        if r.status in ("N/A", "Not in schema"):
            continue
        if r.status == "Missing in production" and not rules.missing_counts_as_zero:
            continue
        w = weights.get(norm_key(r.attribute), 1.0)
        if w <= 0:
            continue
        num += w * (r.score or 0.0)
        den += w
    match_pct = round(100 * num / den, 1) if den else None

    expected = sum(1 for r in results if r.status not in ("N/A", "Not in schema"))
    present = expected - counts["Missing in production"]
    completeness = round(100 * present / expected, 1) if expected else None

    return ProductScore(
        match_pct=match_pct,
        completeness_pct=completeness,
        matched=counts["Match"],
        partial=counts["Partial"],
        mismatched=counts["Mismatch"],
        missing_in_prod=counts["Missing in production"],
        unavailable=counts["N/A"] + counts["Not in schema"],
        comparable=expected,
    )


def methodology_text(rules: Rules) -> list[tuple[str, str]]:
    """Rows describing the scoring rules actually used, for the report's Summary sheet."""
    return [
        ("Match status", f"Match: score ≥ {rules.match_threshold:.0%}. Partial: {rules.partial_threshold:.0%} – {rules.match_threshold:.0%}. Mismatch: below {rules.partial_threshold:.0%}."),
        ("Missing in production", "Uploaded has a value, production field is empty: scored 0%" + ("" if rules.missing_counts_as_zero else " (excluded from match %)") + "."),
        ("N/A / Not in schema", "Uploaded value empty, or the production table has no matching field: not scored, not counted in completeness."),
        ("Product match %", "Weighted mean of attribute scores over comparable attributes (default weight 1)."),
        ("Completeness %", "Attributes production fills ÷ attributes that are non-empty in the upload and exist in the production table."),
        ("Numeric", f"Equal within ±{rules.abs_tol} or ±{rules.rel_tol:.1%}; currency symbols and decimal commas normalized; different units = mismatch."),
        ("Text", "Word-level F1: 2 × common words ÷ (uploaded words + production words), after lowercasing and removing punctuation/HTML."),
        ("Lists (ingredients, bullet points)", f"Split on top-level commas; items match if equal or word-Jaccard ≥ {rules.item_threshold}. Score = matched ÷ larger list size."),
        ("Nutrition", "Parsed into key: value pairs; score = keys with equal numbers ÷ all keys on either side."),
        ("Images", "Same host: URL set match. Re-hosted (different hosts): score = smaller count ÷ larger count."),
        ("Record choice", "Latest production record by timestamp within the date range; retailer inferred from the URL locale when several match."),
    ]
