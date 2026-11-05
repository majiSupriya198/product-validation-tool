"""Values that spreadsheets treat specially must survive the CSV → standardized workbook → read round trip."""
import io
import json

import pandas as pd
from openpyxl import load_workbook

from csv_transformer import SHEETS, transform
from file_processor import parse_standardized
from product_matcher import _crawl_gap

SPECIAL = ["- 50% off today", "+1 free", "@home edition", "=SUM(A1:A2)", "'quoted", "Tab\there", "Line1\nLine2",
           "ünïcödé ✓ 日本語", "  padded  "]


def test_special_values_round_trip_exactly():
    rows = [{"timestamp_date": "2026-10-28T10:00:00Z",
             "extract": json.dumps({"productNo": str(1000 + i), "name": f"P{i}", "description": v}, ensure_ascii=False),
             "parsed": json.dumps({"currency": v}), "feedback": json.dumps({"summary": v}, ensure_ascii=False),
             "metadata": "{}", "gather": "{}"} for i, v in enumerate(SPECIAL)]
    data = pd.DataFrame(rows).to_csv(index=False).encode()
    res = transform(data, "special.csv")
    wb = load_workbook(io.BytesIO(res.workbook_bytes))
    assert all(c.data_type != "f" for ws in wb for row in ws.iter_rows() for c in row)   # no formulas
    products = parse_standardized(res.workbook_bytes).products
    for p, v in zip(products, SPECIAL):
        want = v.strip()   # clean_cell trims surrounding whitespace for comparison
        assert p.attributes["extract.description"] == want
        assert p.attributes["parsed.currency"] == want
        assert p.attributes["feedback.summary"] == want
    # the Original Data sheet keeps the raw CSV text untouched (including surrounding spaces)
    ws = wb[SHEETS["original"]]
    hdr = [c.value for c in ws[1]]
    raw = [dict(zip(hdr, [c.value for c in r])) for r in ws.iter_rows(min_row=2)]
    assert [json.loads(r["extract"])["description"] for r in raw] == SPECIAL


def test_crawl_gap_note():
    assert _crawl_gap("2026-10-28T18:21:22.899Z", "2026-10-30T08:00:00Z") == "Production crawl 1.6 days after the uploaded crawl"
    assert _crawl_gap("2026-10-28T18:00:00Z", "2026-10-28 18:00:00.000") == "Production crawl at the same time as the uploaded crawl"
    assert _crawl_gap(None, "2026-10-30") is None
