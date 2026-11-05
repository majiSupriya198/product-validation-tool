"""CSV transformation (JSON sections), section scoring and review-level feedback comparison."""
import io
import json
from datetime import date
from pathlib import Path

import pandas as pd
import pytest
from openpyxl import load_workbook

from comparators.base import CompareContext
from config import DBXSettings, Rules
from csv_transformer import SHEETS, detect, transform
from dbx_connector import MockSource
from excel_report import build_report
from feedback_comparator import compare_reviews
from file_processor import normalize_pagetype, parse_file, parse_standardized
from json_flattener import EXCEL_CELL_LIMIT, flatten, join_long_cells
from mock_data import build_mock
from pipeline import default_date_range, run_validation

SETTINGS = DBXSettings()
RULES = Rules.load()
DETAIL = SETTINGS.table_for("detail")
SAMPLE = Path.home() / "Downloads" / "11051976_2026-10-29_0001_fixed_1793705640008.csv"
CTX = CompareContext("feedback.comments")


def _review(rid, rating=5, content="Great sound", user="ann", ts="2026-10-01T00:00:00Z", subject=None):
    return {"reviewId": rid, "rating": rating, "content": content, "username": user, "timestamp": ts, "subject": subject}


def _json_csv(rows: list[dict]) -> bytes:
    out = []
    for r in rows:
        out.append({"timestamp_date": r.get("ts", "2026-10-28T10:00:00Z"),
                    "extract": json.dumps(r["extract"], ensure_ascii=False) if r.get("extract") is not None else "",
                    "metadata": json.dumps(r.get("metadata", {"retailer": "Shop-US"})),
                    "gather": json.dumps(r.get("gather", {"url": f"https://shop.example/ip/{r['extract'].get('productNo')}"})),
                    "feedback": r["feedback"] if isinstance(r.get("feedback"), str) else json.dumps(r.get("feedback", {}), ensure_ascii=False),
                    "parsed": json.dumps(r.get("parsed", {}))})
    return pd.DataFrame(out).to_csv(index=False).encode()


SAMPLE_ROWS = [
    {"extract": {"name": "Ölmühle Grüner Tee 🍵", "productNo": "10001", "price": "$5.99", "Pagetype": "Product",
                 "description": "Line one\nLine two, with \"quotes\"", "imageURL": ["https://img.example/a.png"],
                 "meta": {"keywords": "tea"}, "wholesale": None},
     "parsed": {"price": 5.99, "currency": "USD", "promotions": [{"promotionType": "PRICE_CUT", "discount": 10.0}]},
     "feedback": {"avgRating": 4.5, "commentCount": 2, "comments": [_review("r1"), _review("r2", 3, "Too bitter", "bob")]}},
    {"extract": {"name": "Broken feedback", "productNo": "10002", "Pagetype": "Product"},
     "feedback": "{not valid json"},
    {"extract": {"name": "Long reviews", "productNo": "10003", "Pagetype": "Product"},
     "feedback": {"avgRating": 4.0, "comments": [_review(f"x{i}", content="word " * 900) for i in range(12)]}},
]


# ---------------------------------------------------------------- transformation


def test_detect_formats():
    assert detect(_json_csv(SAMPLE_ROWS), "x.csv").format == "json_sections"
    flat = pd.DataFrame([{"name": "a", "productNo": "1"}]).to_csv(index=False).encode()
    assert detect(flat, "flat.csv").format == "flat"


def test_flatten_keeps_arrays_and_nests_objects():
    f = flatten({"a": {"b": 1, "c": {"d": None}}, "l": [1, {"x": 2}], "e": {}})
    assert f == {"a.b": 1, "a.c.d": None, "l": [1, {"x": 2}], "e": {}}


def test_transform_sheets_unicode_multiline_malformed_and_long_cells():
    res = transform(_json_csv(SAMPLE_ROWS), "sample.csv")
    wb = load_workbook(io.BytesIO(res.workbook_bytes))
    for key in ("product", "parsed", "feedback", "reviews", "meta", "original"):
        assert SHEETS[key] in wb.sheetnames
    assert res.stats["malformed_json_cells"] == 1 and any("couldn't be parsed" in w for w in res.warnings)
    assert any("continuation columns" in w for w in res.warnings)
    fb = wb[SHEETS["feedback"]]
    hdr = [c.value for c in fb[1]]
    assert "feedback.comments__part2" in hdr and "feedback.__raw__" in hdr
    assert all(len(str(c.value or "")) <= EXCEL_CELL_LIMIT for row in fb.iter_rows() for c in row)

    parsed = parse_standardized(res.workbook_bytes)
    p1, p2, p3 = parsed.products
    assert p1.attributes["extract.name"] == "Ölmühle Grüner Tee 🍵"
    assert p1.attributes["extract.description"] == "Line one\nLine two, with \"quotes\""
    assert p1.attributes["extract.meta.keywords"] == "tea"
    assert p1.attributes["parsed.promotions"] == [{"promotionType": "PRICE_CUT", "discount": 10.0}]
    assert p1.identifiers["Pagetype"] == "detail" and p1.identifiers["retailer"] == "Shop-US"
    assert p2.attributes["feedback.__raw__"] == "{not valid json"            # nothing lost
    assert len(p3.attributes["feedback.comments"]) == 12                    # long cell joined back
    assert p3.attributes["feedback.comments"][0]["content"].startswith("word word")


def test_original_data_round_trips_exactly():
    data = _json_csv(SAMPLE_ROWS)
    res = transform(data, "sample.csv")
    ws = load_workbook(io.BytesIO(res.workbook_bytes))[SHEETS["original"]]
    hdr = [c.value for c in ws[1]]
    back = [join_long_cells(dict(zip(hdr, [c.value for c in r]))) for r in ws.iter_rows(min_row=2)]
    src = pd.read_csv(io.BytesIO(data), dtype=str, keep_default_na=False).to_dict(orient="records")
    assert all((b.get(k) or "") == v for b, s in zip(back, src) for k, v in s.items())


@pytest.mark.parametrize("raw,expected", [("Product", "detail"), ("PDP", "detail"), ("detail", "detail"),
                                          ("Search", "search"), ("PLP", "search"), ("odd", "odd"), (None, None)])
def test_pagetype_aliases(raw, expected):
    assert normalize_pagetype(raw) == expected


def test_default_date_range_from_uploaded_timestamps():
    parsed = parse_file(_json_csv(SAMPLE_ROWS), "s.csv")
    assert default_date_range(parsed, today=date(2026, 11, 5)) == (date(2026, 10, 25), date(2026, 10, 31))


@pytest.mark.skipif(not SAMPLE.exists(), reason="sample CSV not on this machine")
def test_real_walmart_sample_transforms_losslessly():
    res = transform(SAMPLE, SAMPLE.name)
    assert res.stats["records"] == 85 and res.stats["malformed_json_cells"] == 0
    parsed = parse_standardized(res.workbook_bytes)
    src = pd.read_csv(SAMPLE, dtype=str, keep_default_na=False)
    for prod, fb in zip(parsed.products, src["feedback"]):
        assert (json.loads(fb).get("comments") or None) == prod.attributes.get("feedback.comments")


# ---------------------------------------------------------------- feedback reviews


def test_reviews_order_does_not_matter():
    up = [_review("1"), _review("2", 4)]
    assert compare_reviews(up, list(reversed(up)), CTX).score == 1


def test_reviews_missing_extra_and_changed_rating():
    up = [_review("1"), _review("2", 4), _review("3")]
    prod = [_review("1"), _review("2", 2), _review("9")]
    r = compare_reviews(up, prod, CTX)
    assert r.missing == ["3"] and r.extra == ["9"]
    # pair 1 = 1.0, pair 2: rating wrong, content/username/date right -> 0.75; divided by max(3, 3)
    assert r.score == pytest.approx((1 + 0.75) / 3)
    statuses = {d["review"]: d["status"] for d in r.details}
    assert statuses == {"1": "Match", "2": "Partial", "3": "Missing in production", "9": "Extra in production"}
    assert "rating 4 vs 2" in next(d for d in r.details if d["review"] == "2")["differences"]


def test_reviews_fallback_pairing_without_ids():
    up = [{"rating": 5, "content": "Lovely tea, would buy again", "username": "ann", "timestamp": "2026-10-01"}]
    prod = [{"rating": 5, "content": "Lovely tea, would buy again!", "username": "Ann", "timestamp": "2026-10-01T08:00:00Z"}]
    r = compare_reviews(up, prod, CTX)
    assert r.score == 1 and r.evidence["by_method"] == {"content+user+date+rating": 1}


@pytest.mark.parametrize("basis,expected", [("max", 10 / 50), ("uploaded", 10 / 50), ("paired", 1.0)])
def test_review_score_basis(basis, expected):
    up = [_review(str(i)) for i in range(50)]
    prod = up[:10]
    assert compare_reviews(up, prod, CTX, score_basis=basis).score == pytest.approx(expected)


# ---------------------------------------------------------------- sections end to end


def test_section_scores_and_report_sheets():
    parsed = parse_file(_json_csv(SAMPLE_ROWS[:1]), "s.csv")
    rec = {"id": "a", "timestamp": "2026-10-28T12:00:00Z",
           "extract": {"name": "Ölmühle Grüner Tee 🍵", "productNo": "10001", "price": "$5.99",
                       "description": "Line one Line two, with \"quotes\"", "imageURL": ["https://img.example/a.png"],
                       "meta": {"keywords": "tea"}},
           "parsed": {"price": 6.49, "currency": "USD", "promotions": [{"promotionType": "PRICE_CUT", "discount": 10.0}]},
           "feedback": {"avgRating": 4.5, "commentCount": 2, "comments": [_review("r2", 3, "Too bitter", "bob")]},
           "gather": {"url": "https://shop.example/ip/10001"}, "metadata": {"retailer": "Shop-US"}}
    src = MockSource({DETAIL: [rec]})
    run = run_validation(parsed, src, SETTINGS, RULES, date(2026, 10, 25), date(2026, 10, 31), "Shop%")
    o = run.outcomes[0]
    assert o.match.found and o.match.pagetype == "detail"
    by = {a.attribute: a for a in o.attributes}
    assert {a.section for a in o.attributes} == {"extract", "parsed", "feedback"}
    assert by["extract.name"].status == "Match" and by["extract.meta.keywords"].status == "Match"
    assert by["parsed.price"].status == "Mismatch" and by["parsed.currency"].status == "Match"
    assert by["parsed.promotions"].status == "Match"
    assert by["feedback.comments"].pct == 50.0 and by["feedback.comments"].missing == ["r1"]
    sc = o.score
    assert sc.section_pct("extract") == 100.0
    assert sc.section_pct("parsed") == pytest.approx(66.7, abs=0.1)
    assert sc.section_pct("feedback") == pytest.approx(83.3, abs=0.1)      # (1 + 1 + 0.5) / 3
    assert sc.match_pct == pytest.approx((100 + 66.67 + 83.33) / 3, abs=0.1)
    wb = build_report(run)
    assert wb.sheetnames == ["Overall Summary", "Extract Comparison", "Parsed Comparison", "Feedback Comparison",
                             "Missing Attributes", "Unmatched Products", "DBX Raw Data"]
    fb = wb["Feedback Comparison"]
    levels = [r[4] for r in fb.iter_rows(min_row=2, values_only=True)]
    assert "Review" in levels and "Attribute" in levels


def test_mock_review_perturbation_end_to_end():
    rows = [{"extract": {"name": f"P{i}", "productNo": str(20000 + i), "Pagetype": "Product"},
             "feedback": {"avgRating": 4.0, "comments": [_review(f"{i}-{j}", content=f"text {j}") for j in range(5)]}}
            for i in range(10)]
    parsed = parse_file(_json_csv(rows), "m.csv")
    src, _ = build_mock(parsed, SETTINGS, RULES, retailer="Shop-US", end=date(2026, 10, 30))
    run = run_validation(parsed, src, SETTINGS, RULES, date(2026, 10, 25), date(2026, 10, 31), None)
    o = next(x for x in run.outcomes if x.match.product.identifiers["productNo"] == "20008")
    c = next(a for a in o.attributes if a.attribute == "feedback.comments")
    # 5 uploaded, 3 in production (one rating changed): (1 + 1 + 0.75) / 5
    assert c.pct == pytest.approx(55.0) and len(c.missing) == 2
    o9 = next(x for x in run.outcomes if x.match.product.identifiers["productNo"] == "20009")
    assert next(a for a in o9.attributes if a.attribute == "feedback.avgRating").status == "Mismatch"
