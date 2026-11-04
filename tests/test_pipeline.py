import io
from datetime import date

import pandas as pd
import pytest
from openpyxl import load_workbook

from comparison_engine import compare_product
from config import DBXSettings, Rules
from dbx_connector import FixtureSource, MockSource, _load_snapshot
from excel_report import build_report
from field_resolver import FieldResolver
from file_processor import InputFileError, parse_file
from mock_data import build_mock
from pipeline import run_validation
from product_matcher import url_country, url_trailing_id
from scoring_engine import score_product
from utils.query_builder import assert_read_only, build_fetch_query

SETTINGS = DBXSettings()
RULES = Rules.load()
DETAIL = SETTINGS.table_for("detail")
SEARCH = SETTINGS.table_for("search")


def _csv(rows):
    return pd.DataFrame(rows).to_csv(index=False).encode()


def _rec(pno, ts, retailer="Shop-DE", **extract):
    return {"id": f"{pno}-{ts}", "timestamp": ts, "extract": {"productNo": pno, **extract},
            "parsed": {}, "feedback": {}, "gather": {"url": f"https://api.shop/pdp/{pno}"},
            "metadata": {"retailer": retailer}}


# ---------------------------------------------------------------- input


def test_parse_flink_like_csv_keeps_leading_zeros_and_blanks():
    data = _csv([{"name": "A", "productNo": "00123", "Pagetype": "detail", "instructions": " ", "imageURL": '["u1","u2"]'}])
    p = parse_file(data, "x.csv")
    prod = p.products[0]
    assert prod.identifiers["productNo"] == "00123"
    assert prod.attributes["instructions"] is None
    assert prod.attributes["imageURL"] == ["u1", "u2"]


def test_missing_identifiers_raises():
    with pytest.raises(InputFileError):
        parse_file(_csv([{"foo": 1}]), "x.csv")


def test_unsupported_extension():
    with pytest.raises(InputFileError):
        parse_file(b"abc", "x.pdf")


# ---------------------------------------------------------------- queries


def test_query_is_parameterized_and_read_only():
    q = build_fetch_query(DETAIL, SETTINGS.allowed_tables(), "productNo", ["1'; DROP TABLE x;--", "2"],
                          date(2026, 10, 1), date(2026, 11, 1), "Flink%")
    assert "DROP" not in q.sql and ":p0" in q.sql and q.params["p0"].startswith("1'")
    assert q.params["retailer"] == "Flink%"
    assert "timestamp_date BETWEEN" in q.sql


def test_query_rejects_unknown_table():
    with pytest.raises(ValueError):
        build_fetch_query("x.y.z", SETTINGS.allowed_tables(), "productNo", ["1"], date.today(), date.today(), None)
    with pytest.raises(ValueError):
        build_fetch_query("a.b; drop", SETTINGS.allowed_tables(), "productNo", ["1"], date.today(), date.today(), None)


def test_assert_read_only():
    with pytest.raises(ValueError):
        assert_read_only("DELETE FROM x")
    assert_read_only("WITH a AS (SELECT 1) SELECT * FROM a WHERE b = 'drop'")


def test_url_helpers():
    u = "https://www.goflink.com/shop/en-DE/product/funny-frisch-ungarisch-150g-122301750"
    assert url_country(u) == "DE" and url_trailing_id(u) == "122301750"


# ---------------------------------------------------------------- matching


def test_latest_record_used_and_crawl_disagreement_flagged():
    src = MockSource({DETAIL: [_rec("1", "2026-10-10T00:00:00Z", price="1.00"),
                               _rec("1", "2026-10-20T00:00:00Z", price="2.00")]})
    p = parse_file(_csv([{"productNo": "1", "price": "2.00", "Pagetype": "detail"}]), "x.csv")
    run = run_validation(p, src, SETTINGS, RULES, date(2026, 10, 1), date(2026, 10, 31), None)
    o = run.outcomes[0]
    assert o.match.dbx_timestamp.startswith("2026-10-20")
    price = next(a for a in o.attributes if a.attribute == "price")
    assert price.status == "Match"
    assert any("Latest two crawls differ" in n for n in o.match.notes)


def test_url_fallback_and_name_fallback():
    recs = [_rec("55501", "2026-10-20T00:00:00Z", name="Alpha Chips 150g"),
            _rec("777", "2026-10-20T00:00:00Z", name="Beta Nuts")]
    src = MockSource({DETAIL: recs})
    p = parse_file(_csv([
        {"productNo": "", "originalURL": "https://shop/en-DE/product/alpha-55501", "name": "x", "Pagetype": "detail"},
        {"productNo": "", "originalURL": "", "name": "Beta Nuts", "Pagetype": "detail"},
        {"productNo": "999", "originalURL": "", "name": "Gamma", "Pagetype": "detail"},
    ]), "x.csv")
    run = run_validation(p, src, SETTINGS, RULES, date(2026, 10, 1), date(2026, 10, 31), None)
    m = [o.match for o in run.outcomes]
    assert m[0].method == "url_derived_productNo" and m[0].identifier_used == "55501"
    assert m[1].method == "name"
    assert not m[2].found and "productNo=999" in m[2].not_found_reason


def test_retailer_chosen_from_url_locale_and_flagged_when_only_other_country():
    recs = [_rec("1", "2026-10-21T00:00:00Z", "Flink-APP-NL"), _rec("1", "2026-10-20T00:00:00Z", "Flink-APP-DE"),
            _rec("2", "2026-10-20T00:00:00Z", "Flink-APP-NL")]
    src = MockSource({DETAIL: recs})
    p = parse_file(_csv([
        {"productNo": "1", "originalURL": "https://x.com/shop/en-DE/product/a-1", "Pagetype": "detail"},
        {"productNo": "2", "originalURL": "https://x.com/shop/en-DE/product/b-2", "Pagetype": "detail"},
    ]), "x.csv")
    run = run_validation(p, src, SETTINGS, RULES, date(2026, 10, 1), date(2026, 10, 31), "Flink%")
    a, b = (o.match for o in run.outcomes)
    assert a.retailer == "Flink-APP-DE" and not a.ambiguous
    assert b.retailer == "Flink-APP-NL" and b.ambiguous


def test_search_pagetype_uses_search_table():
    src = MockSource({SEARCH: [_rec("1", "2026-10-20T00:00:00Z", name="S", rank=3)]})
    p = parse_file(_csv([{"productNo": "1", "name": "S", "Pagetype": "search"}]), "x.csv")
    run = run_validation(p, src, SETTINGS, RULES, date(2026, 10, 1), date(2026, 10, 31), None)
    assert run.outcomes[0].match.table == SEARCH


def test_both_pagetype_falls_back_to_search():
    src = MockSource({SEARCH: [_rec("1", "2026-10-20T00:00:00Z", name="S")]})
    p = parse_file(_csv([{"productNo": "1", "name": "S"}]), "x.csv", pagetype_override="both")
    run = run_validation(p, src, SETTINGS, RULES, date(2026, 10, 1), date(2026, 10, 31), None, pagetype_default="both")
    assert run.outcomes[0].match.found and run.outcomes[0].match.pagetype == "search"


# ---------------------------------------------------------------- resolution & scoring


def test_resolver_checks_extract_then_parsed_then_feedback():
    fr = FieldResolver(RULES.aliases, _load_snapshot(DETAIL))
    assert fr.candidates("price") == ["extract.price", "parsed.price"]
    assert fr.resolve("avgRating") == "feedback.avgRating"
    assert fr.resolve("coments.content") == "feedback.comments.content"
    assert fr.resolve("Storage_and_Usage") == "extract.storage"
    rec = {"extract": {"price": None}, "parsed": {"price": 1.99}}
    assert fr.lookup("price", rec) == ("parsed.price", 1.99, True)


def test_scoring_excludes_na_and_counts_missing():
    fr = FieldResolver(RULES.aliases, _load_snapshot(DETAIL))
    rec = {"extract": {"name": "A", "price": "1.99 €", "ingredients": "a, b, c"}, "parsed": {}, "feedback": {}}
    attrs = {"name": "A", "price": "1.99", "ingredients": "a, b, c, d", "brand": "X", "allergens": None, "fooBar": "1"}
    res = {r.attribute: r for r in compare_product(attrs, rec, fr, RULES)}
    assert res["allergens"].status == "N/A"
    assert res["brand"].status == "Missing in production"
    assert res["fooBar"].status == "Not in schema"
    assert res["ingredients"].pct == 75.0
    sc = score_product(list(res.values()), RULES)
    # (1 + 1 + 0.75 + 0) / 4
    assert sc.match_pct == pytest.approx(68.8, abs=0.1)
    assert sc.completeness_pct == 75.0
    assert (sc.matched, sc.partial, sc.missing_in_prod, sc.unavailable) == (2, 1, 1, 2)


# ---------------------------------------------------------------- end to end


def test_mock_perturbations_end_to_end():
    rows = []
    for i in range(10):
        rows.append({"name": f"P{i}", "productNo": str(1000 + i), "Pagetype": "detail", "price": "1.99",
                     "ingredients": "A, B, C, D, E, F, G, H",
                     "nutrition": "Energie: 500 kcal | Fett: 10 g | Zucker: 2 g | Salz: 1 g",
                     "imageURL": '["https://cdn.x/1.png"]', "description": "one two three four five six",
                     "manufacturer": "Maker GmbH"})
    p = parse_file(_csv(rows), "m.csv")
    src, applied = build_mock(p, SETTINGS, RULES, retailer="Mock-DE", end=date(2026, 11, 4))
    run = run_validation(p, src, SETTINGS, RULES, date(2026, 10, 1), date(2026, 11, 4), "Mock%")
    by = {o.match.product.identifiers["productNo"]: {a.attribute: a for a in o.attributes} for o in run.outcomes}
    assert not run.outcomes[0].match.found
    assert by["1001"]["ingredients"].pct == 75.0
    assert by["1002"]["price"].status == "Mismatch"
    assert by["1003"]["nutrition"].pct == 75.0
    assert by["1004"]["imageURL"].pct == 50.0
    assert by["1005"]["description"].pct == pytest.approx(66.7, abs=0.1)
    assert by["1006"]["manufacturer"].status == "Missing in production"
    assert all(a.status in ("Match", "N/A", "Not in schema") for a in by["1007"].values())
    # stale older crawl has a different price; the latest must win
    assert by["1007"]["price"].status == "Match"

    wb = build_report(run)
    assert wb.sheetnames == ["Summary", "Attribute Comparison", "Missing Attributes", "Unmatched Products", "Raw DBX Data"]
    buf = io.BytesIO()
    wb.save(buf)
    wb2 = load_workbook(io.BytesIO(buf.getvalue()))
    assert wb2["Attribute Comparison"].auto_filter.ref and wb2["Attribute Comparison"].freeze_panes == "F2"
    assert wb2["Unmatched Products"].max_row == 2


def test_fixture_source_real_flink_records():
    import json
    from config import ROOT
    path = ROOT / "fixtures" / "flink_detail_sample.json"
    data = json.loads(path.read_text())
    rec = next(r for r in data[DETAIL] if r["extract"].get("productNo") == "122301750")
    p = parse_file(_csv([{
        "name": "Funny-frisch Chipsfrisch ungarisch 150g", "productNo": "122301750", "Pagetype": "detail",
        "price": "1.99", "pricePerUnit": "13.27 € / 1KG",
        "nutrition": "Nährwerte (je 100 g): " + rec["extract"]["nutrition"].replace(", ", " | ").replace("2 | 223", "2,223"),
        "Storage_and_Usage": rec["extract"]["storage"],
    }]), "f.csv")
    run = run_validation(p, FixtureSource(path), SETTINGS, RULES, date(2026, 10, 5), date(2026, 11, 4), "Flink%")
    o = run.outcomes[0]
    assert o.match.found and o.match.retailer == "Flink-APP-DE"
    st = {a.attribute: a.status for a in o.attributes}
    assert st["price"] == st["pricePerUnit"] == st["Storage_and_Usage"] == st["name"] == "Match"
