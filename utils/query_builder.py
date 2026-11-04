"""Parameterized, read-only SQL for fetching production records.

Values are always bound as named parameters (:p0, :start_date, ...). The table name can't be
bound, so it must be a validated three-part identifier from the configured allow-list.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date
from typing import Iterable

_TABLE = re.compile(r"^[A-Za-z0-9_]+\.[A-Za-z0-9_]+\.[A-Za-z0-9_]+$")

KEY_EXPR = {
    "productNo": "d.extract.productNo",
    "url": "d.gather.url",
    "name": "lower(trim(d.extract.name))",
}


@dataclass
class Query:
    sql: str
    params: dict[str, object]


def assert_read_only(sql: str) -> None:
    head = sql.lstrip().split(None, 1)[0].upper() if sql.strip() else ""
    if head not in ("SELECT", "WITH"):
        raise ValueError("Only SELECT/WITH statements are allowed")
    stripped = re.sub(r"'[^']*'", "''", sql)  # ignore string literals
    if re.search(r"\b(INSERT|UPDATE|DELETE|MERGE|DROP|ALTER|CREATE|TRUNCATE|GRANT|REVOKE|OPTIMIZE|VACUUM)\b", stripped, re.I):
        raise ValueError("Write/DDL keywords are not allowed")
    if ";" in stripped.rstrip().rstrip(";"):
        raise ValueError("Multiple statements are not allowed")


def validate_table(table: str, allowed: Iterable[str]) -> str:
    if not _TABLE.match(table):
        raise ValueError(f"Invalid table identifier: {table!r}")
    if table not in set(allowed):
        raise ValueError(f"Table {table!r} isn't in the configured allow-list")
    return table


def chunked(values: list[str], size: int) -> Iterable[list[str]]:
    for i in range(0, len(values), size):
        yield values[i:i + size]


def build_fetch_query(table: str, allowed: Iterable[str], key_type: str, values: list[str],
                      start_date: date, end_date: date, retailer: str | None,
                      keep_latest: int = 2) -> Query:
    """Latest `keep_latest` records per (retailer, key) for the given identifier values.

    keep_latest=2 lets the matcher see whether the two most recent crawls disagree.
    """
    validate_table(table, allowed)
    if key_type not in KEY_EXPR:
        raise ValueError(f"Unknown key type {key_type!r}")
    if not values:
        raise ValueError("No identifier values to query")
    key = KEY_EXPR[key_type]
    params: dict[str, object] = {"start_date": start_date.isoformat(), "end_date": end_date.isoformat()}
    names = []
    for i, v in enumerate(values):
        params[f"p{i}"] = v
        names.append(f":p{i}")
    retailer_clause = ""
    if retailer:
        params["retailer"] = retailer
        retailer_clause = "AND d.metadata.retailer LIKE :retailer"
    sql = f"""
WITH candidates AS (
  SELECT
    d.id AS record_id,
    d.timestamp AS ts,
    d.metadata.retailer AS retailer,
    {key} AS match_key,
    to_json(d.extract) AS extract_json,
    to_json(d.parsed) AS parsed_json,
    to_json(d.feedback) AS feedback_json,
    to_json(d.gather) AS gather_json,
    to_json(d.metadata) AS metadata_json,
    ROW_NUMBER() OVER (PARTITION BY d.metadata.retailer, {key} ORDER BY d.timestamp DESC, d.id DESC) AS rn,
    COUNT(*) OVER (PARTITION BY d.metadata.retailer, {key}) AS n_records
  FROM {table} d
  WHERE d.timestamp_date BETWEEN CAST(:start_date AS DATE) AND CAST(:end_date AS DATE)
    {retailer_clause}
    AND {key} IN ({", ".join(names)})
)
SELECT * FROM candidates WHERE rn <= {int(keep_latest)}
""".strip()
    assert_read_only(sql)
    return Query(sql, params)


def build_last_seen_query(tables: list[str], allowed: Iterable[str], product_nos: list[str],
                          since: date, retailer: str | None) -> Query:
    """Latest crawl date per table/retailer/productNo since `since` (diagnostics for unmatched products)."""
    if not product_nos:
        raise ValueError("No identifier values to query")
    params: dict[str, object] = {"since": since.isoformat()}
    names = []
    for i, v in enumerate(product_nos):
        params[f"p{i}"] = v
        names.append(f":p{i}")
    retailer_clause = ""
    if retailer:
        params["retailer"] = retailer
        retailer_clause = "AND d.metadata.retailer LIKE :retailer"
    parts = []
    for t in tables:
        validate_table(t, allowed)
        parts.append(f"""SELECT '{t}' AS tbl, d.extract.productNo AS product_no, d.metadata.retailer AS retailer,
       CAST(MAX(d.timestamp_date) AS STRING) AS last_date
FROM {t} d
WHERE d.timestamp_date >= CAST(:since AS DATE) {retailer_clause}
  AND d.extract.productNo IN ({", ".join(names)})
GROUP BY 1, 2, 3""")
    sql = "\nUNION ALL\n".join(parts)
    assert_read_only(sql)
    return Query(sql, params)


def build_ping_query() -> Query:
    return Query("SELECT 1 AS ok", {})


def build_schema_query(table: str, allowed: Iterable[str]) -> Query:
    validate_table(table, allowed)
    return Query(f"SELECT * FROM {table} LIMIT 0", {})
