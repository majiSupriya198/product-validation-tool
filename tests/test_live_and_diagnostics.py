import json
from datetime import date

import pandas as pd

from config import DBXSettings, Rules
from dbx_connector import LiveSource, MockSource
from file_processor import parse_file
from pipeline import run_validation
from utils.query_builder import build_last_seen_query

SETTINGS = DBXSettings(host="h.example", http_path="/sql/1.0/warehouses/x", token="t")
RULES = Rules.load()
DETAIL = SETTINGS.table_for("detail")
SEARCH = SETTINGS.table_for("search")


def _csv(rows):
    return pd.DataFrame(rows).to_csv(index=False).encode()


def _rec(pno, ts, retailer="Shop-DE", **extract):
    return {"id": f"{pno}-{ts}", "timestamp": ts, "extract": {"productNo": pno, **extract},
            "parsed": {}, "feedback": {}, "gather": {"url": f"https://api.shop/pdp/{pno}"},
            "metadata": {"retailer": retailer}}


class FakeCursor:
    def __init__(self, log, rows):
        self.log, self.rows, self.description = log, rows, None

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def execute(self, sql, params=None):
        self.log.append((sql, params))
        self.description = [(c,) for c in (self.rows[0].keys() if self.rows else [])]

    def fetchall(self):
        return [tuple(r.values()) for r in self.rows]


class FakeConn:
    def __init__(self, log, rows):
        self.log, self.rows = log, rows

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def cursor(self):
        return FakeCursor(self.log, self.rows)


def test_live_source_binds_named_parameters(monkeypatch):
    log = []
    row = {"record_id": "r1", "ts": "2026-10-20T00:00:00Z", "retailer": "Shop-DE", "match_key": "1", "rn": 1,
           "n_records": 3, "extract_json": json.dumps({"productNo": "1", "price": "1.99 €"}),
           "parsed_json": json.dumps({"price": 1.99}), "feedback_json": None,
           "gather_json": json.dumps({"url": "u"}), "metadata_json": json.dumps({"retailer": "Shop-DE"})}
    src = LiveSource(SETTINGS)
    monkeypatch.setattr(src, "_connect", lambda: FakeConn(log, [row]))
    rows = src.fetch(DETAIL, "productNo", ["1", "2"], date(2026, 10, 1), date(2026, 10, 31), "Shop%")
    sql, params = log[0]
    assert ":p0" in sql and ":p1" in sql and ":retailer" in sql and "'1'" not in sql
    assert params == {"start_date": "2026-10-01", "end_date": "2026-10-31", "p0": "1", "p1": "2", "retailer": "Shop%"}
    r = rows[0]
    assert r["record"]["extract"]["price"] == "1.99 €" and r["record"]["parsed"]["price"] == 1.99
    assert r["record"]["feedback"] == {} and r["n_records"] == 3


def test_last_seen_query_shape():
    q = build_last_seen_query([DETAIL, SEARCH], SETTINGS.allowed_tables(), ["1", "2"], date(2026, 5, 1), "Flink%")
    assert q.sql.count("UNION ALL") == 1 and q.params["p1"] == "2" and "MAX(d.timestamp_date)" in q.sql


def test_both_pagetype_prefers_detail_even_if_first_row_is_search():
    src = MockSource({DETAIL: [_rec("2", "2026-10-20T00:00:00Z", name="D")],
                      SEARCH: [_rec("2", "2026-10-21T00:00:00Z", name="S"), _rec("1", "2026-10-20T00:00:00Z", name="S1")]})
    p = parse_file(_csv([{"productNo": "1", "name": "S1", "Pagetype": "search"},
                         {"productNo": "2", "name": "D", "Pagetype": ""}]), "x.csv")
    run = run_validation(p, src, SETTINGS, RULES, date(2026, 10, 1), date(2026, 10, 31), None, pagetype_default="both")
    a, b = (o.match for o in run.outcomes)
    assert a.table == SEARCH
    assert b.table == DETAIL


def test_unmatched_reasons_from_last_seen():
    src = MockSource({DETAIL: [_rec("10001", "2026-09-01T00:00:00Z")],
                      SEARCH: [_rec("10002", "2026-10-20T00:00:00Z")]})
    p = parse_file(_csv([{"productNo": "10001", "Pagetype": "detail"},
                         {"productNo": "10002", "Pagetype": "detail"},
                         {"productNo": "10003", "Pagetype": "detail"}]), "x.csv")
    run = run_validation(p, src, SETTINGS, RULES, date(2026, 10, 1), date(2026, 10, 31), None)
    r1, r2, r3 = (o.match.not_found_reason for o in run.outcomes)
    assert "last crawled 2026-09-01 (before range)" in r1 and "Widen the date range" in r1
    assert "sku_search_v2" in r2 and "other table" in r2
    assert "Not crawled in either table" in r3
