"""End-to-end validation run shared by the CLI and the Streamlit app."""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Callable

from comparison_engine import AttributeResult, compare_product
from config import DBXSettings, Rules
from dbx_connector import Source
from field_resolver import FieldResolver
from file_processor import ParsedFile
from product_matcher import MatchResult, match_products
from scoring_engine import ProductScore, score_product
from utils.logger import get_logger

log = get_logger("pipeline")


@dataclass
class ProductOutcome:
    match: MatchResult
    attributes: list[AttributeResult]
    score: ProductScore | None


@dataclass
class RunResult:
    outcomes: list[ProductOutcome]
    parsed: ParsedFile
    source_name: str
    retailer: str | None
    start: date
    end: date
    rules: Rules
    seconds: float
    warnings: list[str] = field(default_factory=list)

    @property
    def found(self) -> list[ProductOutcome]:
        return [o for o in self.outcomes if o.match.found]

    @property
    def not_found(self) -> list[ProductOutcome]:
        return [o for o in self.outcomes if not o.match.found]

    def summary(self) -> dict[str, float | int | None]:
        found = self.found
        scores = [o.score for o in found if o.score]
        mp = [s.match_pct for s in scores if s.match_pct is not None]
        cp = [s.completeness_pct for s in scores if s.completeness_pct is not None]

        def avg_section(sec: str) -> float | None:
            v = [s.section_pct(sec) for s in scores if s.section_pct(sec) is not None]
            return round(sum(v) / len(v), 1) if v else None

        return {
            "avg_extract_pct": avg_section("extract"),
            "avg_parsed_pct": avg_section("parsed"),
            "avg_feedback_pct": avg_section("feedback"),
            "uploaded": len(self.outcomes),
            "found": len(found),
            "not_found": len(self.not_found),
            "ambiguous": sum(1 for o in found if o.match.ambiguous),
            "avg_match_pct": round(sum(mp) / len(mp), 1) if mp else None,
            "avg_completeness_pct": round(sum(cp) / len(cp), 1) if cp else None,
            "matched": sum(s.matched for s in scores),
            "partial": sum(s.partial for s in scores),
            "mismatched": sum(s.mismatched for s in scores),
            "missing_in_prod": sum(s.missing_in_prod for s in scores),
            "unavailable": sum(s.unavailable for s in scores),
        }


def default_date_range(parsed: ParsedFile, today: date | None = None, pad_days: int = 3,
                       fallback_days: int = 30) -> tuple[date, date]:
    """Production window around the uploaded crawl timestamps (± pad_days, never past today),
    so the comparison uses the production crawl closest in time. Without timestamps: last 30 days."""
    today = today or date.today()
    rng = parsed.timestamp_range
    if rng:
        try:
            lo, hi = date.fromisoformat(rng[0]), date.fromisoformat(rng[1])
            return lo - timedelta(days=pad_days), min(hi + timedelta(days=pad_days), today)
        except ValueError:
            pass
    return today - timedelta(days=fallback_days), today


def run_validation(parsed: ParsedFile, source: Source, settings: DBXSettings, rules: Rules,
                   start: date, end: date, retailer: str | None, pagetype_default: str | None = None,
                   progress: Callable[[float, str], None] | None = None) -> RunResult:
    progress = progress or (lambda f, m: None)
    t0 = time.time()
    retailer = (retailer or "").strip() or None
    matches = match_products(parsed.products, source, settings, rules, start, end, retailer, pagetype_default,
                             progress=lambda f, m: progress(0.1 + 0.6 * f, m))

    resolvers: dict[str, FieldResolver] = {}
    outcomes: list[ProductOutcome] = []
    n = len(matches)
    for i, m in enumerate(matches):
        if i % 50 == 0:
            progress(0.7 + 0.3 * i / max(n, 1), f"comparing {i}/{n}")
        if not m.found:
            outcomes.append(ProductOutcome(m, [], None))
            continue
        if m.table not in resolvers:
            resolvers[m.table] = FieldResolver(rules.aliases, source.schema_paths(m.table))
        attrs = compare_product(m.product.attributes, m.record, resolvers[m.table], rules)
        outcomes.append(ProductOutcome(m, attrs, score_product(attrs, rules)))
    progress(1.0, "done")
    secs = time.time() - t0
    log.info("Run finished in %.1fs: %d products", secs, n)
    return RunResult(outcomes, parsed, source.name, retailer, start, end, rules, secs, list(parsed.warnings))
