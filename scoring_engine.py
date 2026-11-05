"""Product metrics: Extract / Parsed / Feedback scores, overall score and completeness, kept separate."""
from __future__ import annotations

from dataclasses import dataclass, field

from comparison_engine import SECTIONS, AttributeResult
from config import Rules, norm_key


@dataclass
class SectionScore:
    match_pct: float | None          # weighted mean of attribute scores (comparable attributes only)
    completeness_pct: float | None   # share of uploaded, in-schema attributes that production fills
    matched: int = 0
    partial: int = 0
    mismatched: int = 0
    missing_in_prod: int = 0
    unavailable: int = 0             # N/A (uploaded empty) + not in production schema
    comparable: int = 0


@dataclass
class ProductScore(SectionScore):
    sections: dict[str, SectionScore] = field(default_factory=dict)

    def section_pct(self, section: str) -> float | None:
        s = self.sections.get(section)
        return s.match_pct if s else None


def _score(results: list[AttributeResult], rules: Rules) -> SectionScore:
    counts = {s: 0 for s in ("Match", "Partial", "Mismatch", "Missing in production", "N/A", "Not in schema")}
    for r in results:
        counts[r.status] = counts.get(r.status, 0) + 1
    num = den = 0.0
    for r in results:
        if r.status in ("N/A", "Not in schema"):
            continue
        if r.status == "Missing in production" and not rules.missing_counts_as_zero:
            continue
        w = rules.weights.get(norm_key(r.attribute), rules.weights.get(norm_key(r.field), 1.0))
        if w <= 0:
            continue
        num += w * (r.score or 0.0)
        den += w
    expected = sum(1 for r in results if r.status not in ("N/A", "Not in schema"))
    present = expected - counts["Missing in production"]
    return SectionScore(
        match_pct=round(100 * num / den, 1) if den else None,
        completeness_pct=round(100 * present / expected, 1) if expected else None,
        matched=counts["Match"], partial=counts["Partial"], mismatched=counts["Mismatch"],
        missing_in_prod=counts["Missing in production"],
        unavailable=counts["N/A"] + counts["Not in schema"], comparable=expected,
    )


def score_product(results: list[AttributeResult], rules: Rules) -> ProductScore:
    total = _score(results, rules)
    sections = {s: _score([r for r in results if r.section == s], rules) for s in SECTIONS}
    sections = {s: v for s, v in sections.items() if v.comparable or v.unavailable}
    match_pct = total.match_pct
    if rules.overall_basis == "sections":
        vals = [v.match_pct for v in sections.values() if v.match_pct is not None]
        match_pct = round(sum(vals) / len(vals), 1) if vals else None
    return ProductScore(match_pct=match_pct, completeness_pct=total.completeness_pct, matched=total.matched,
                        partial=total.partial, mismatched=total.mismatched, missing_in_prod=total.missing_in_prod,
                        unavailable=total.unavailable, comparable=total.comparable, sections=sections)


def methodology_text(rules: Rules) -> list[tuple[str, str]]:
    """Rows describing the scoring rules actually used, for the report's Summary sheet."""
    overall = ("mean of the Extract, Parsed and Feedback scores that exist (each section counts equally)"
               if rules.overall_basis == "sections" else "weighted mean over all comparable attributes")
    reviews = {"max": "sum of paired review scores ÷ the larger of uploaded and production review counts",
               "uploaded": "sum of paired review scores ÷ uploaded review count",
               "paired": "mean score of reviews found on both sides"}[rules.review_score_basis]
    return [
        ("Sections", "Extract = extract.*, Parsed = parsed.*, Feedback = feedback.*. Each section is scored separately."),
        ("Match status", f"Match: score ≥ {rules.match_threshold:.0%}. Partial: {rules.partial_threshold:.0%} – {rules.match_threshold:.0%}. Mismatch: below {rules.partial_threshold:.0%}."),
        ("Missing in production", "Uploaded has a value, production field is empty: scored 0%" + ("" if rules.missing_counts_as_zero else " (excluded from match %)") + "."),
        ("N/A / Not in schema", "Uploaded value empty, or the production table has no matching field: not scored, not counted in completeness."),
        ("Section match %", "Weighted mean of attribute scores over the section's comparable attributes (default weight 1)."),
        ("Overall match %", overall.capitalize() + "."),
        ("Completeness %", "Attributes production fills ÷ attributes that are non-empty in the upload and exist in the production table."),
        ("Numeric", f"Equal within ±{rules.abs_tol} or ±{rules.rel_tol:.1%}; currency symbols and decimal commas normalized; different units = mismatch."),
        ("Text", "Word-level F1: 2 × common words ÷ (uploaded words + production words), after lowercasing and removing punctuation/HTML."),
        ("Lists (ingredients, bullet points)", f"Split on top-level commas; items match if equal or word-Jaccard ≥ {rules.item_threshold}. Score = matched ÷ larger list size."),
        ("Nutrition / JSON objects", "Key by key; score = keys with equal values ÷ all keys on either side."),
        ("Images", "Same host: URL set match (query strings ignored). Re-hosted (different hosts): score = smaller count ÷ larger count."),
        ("Reviews", "Paired by reviewId, then content+username+date+rating, then fuzzy content (≥90%) for the same user; order ignored. "
                    "Each pair scores rating, subject, content, username and date. Attribute score = " + reviews + "."),
        ("Record choice", "Latest production record by timestamp within the date range; retailer from the uploaded metadata or URL locale when several match."),
    ]
