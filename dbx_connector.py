"""Production data sources.

LiveSource    Databricks SQL warehouse (read-only SELECTs, parameter binding).
FixtureSource real production records saved as JSON (lets the tool run without credentials).
MockSource    synthetic records derived from the upload with known perturbations (for testing scores).

All sources return rows in one shape:
    {"record_id", "ts", "retailer", "match_key", "rn", "n_records",
     "record": {"extract": {...}, "parsed": {...}, "feedback": {...}, "gather": {...}, "metadata": {...}}}
"""
from __future__ import annotations

import fnmatch
import json
import time
from datetime import date, datetime
from pathlib import Path
from typing import Any, Protocol

from config import ROOT, DBXSettings
from field_resolver import get_path, schema_paths_from_records
from utils.logger import get_logger, redact
from utils.query_builder import build_fetch_query, build_last_seen_query, build_ping_query, build_schema_query

log = get_logger("dbx")

SCHEMA_SNAPSHOT = ROOT / "config" / "schema_snapshot.json"


class SourceError(RuntimeError):
    """A data source failed. Message is safe to show (no credentials)."""


class Source(Protocol):
    name: str

    def fetch(self, table: str, key_type: str, values: list[str], start: date, end: date,
              retailer: str | None) -> list[dict]: ...

    def schema_paths(self, table: str) -> set[str]: ...

    def last_seen(self, tables: list[str], product_nos: list[str], since: date,
                  retailer: str | None) -> list[dict]: ...

    def status(self) -> tuple[bool, str]: ...


def _load_snapshot(table: str) -> set[str]:
    try:
        data = json.loads(SCHEMA_SNAPSHOT.read_text(encoding="utf-8"))
        return {p.lower() for p in data.get(table, [])}
    except (OSError, ValueError):
        return set()


def _row_from_db(r: dict) -> dict:
    def j(x: Any) -> Any:
        if x is None:
            return {}
        return json.loads(x) if isinstance(x, str) else x

    ts = r.get("ts")
    record = {
        "id": r.get("record_id"),
        "timestamp": ts.isoformat() if isinstance(ts, datetime) else ts,
        "extract": j(r.get("extract_json")),
        "parsed": j(r.get("parsed_json")),
        "feedback": j(r.get("feedback_json")),
        "gather": j(r.get("gather_json")),
        "metadata": j(r.get("metadata_json")),
    }
    return {"record_id": r.get("record_id"), "ts": record["timestamp"], "retailer": r.get("retailer"),
            "match_key": r.get("match_key"), "rn": int(r.get("rn") or 1),
            "n_records": int(r.get("n_records") or 1), "record": record}


class LiveSource:
    name = "Live (Databricks)"

    def __init__(self, settings: DBXSettings | None = None):
        self.settings = settings or DBXSettings()
        self._schema_cache: dict[str, set[str]] = {}

    def _connect(self):
        if not self.settings.is_configured():
            raise SourceError("Databricks isn't configured. Set DBX_HOST, DBX_HTTP_PATH (or DBX_WAREHOUSE_ID) and DBX_TOKEN in .env.")
        from databricks import sql as dbsql

        import certifi

        kwargs: dict[str, Any] = {
            "server_hostname": self.settings.host,
            "http_path": self.settings.resolved_http_path(),
            "_socket_timeout": self.settings.query_timeout,
            "_tls_trusted_ca_file": self.settings.ca_bundle or certifi.where(),
            # fail fast on connection errors instead of the connector's 15-minute retry window
            "_retry_stop_after_attempts_count": 3,
            "_retry_stop_after_attempts_duration": 120,
        }
        if self.settings.auth_type == "databricks-oauth" and not self.settings.token:
            kwargs["auth_type"] = "databricks-oauth"
        else:
            kwargs["access_token"] = self.settings.token
        try:
            return dbsql.connect(**kwargs)
        except Exception as exc:
            raise SourceError(f"Couldn't connect to Databricks: {redact(exc)}") from None

    def _run(self, sql: str, params: dict, retries: int = 1) -> list[dict]:
        last: Exception | None = None
        for attempt in range(retries + 1):
            started = time.time()
            try:
                with self._connect() as conn, conn.cursor() as cur:
                    cur.execute(sql, params or None)
                    cols = [c[0] for c in cur.description] if cur.description else []
                    rows = [dict(zip(cols, row)) for row in cur.fetchall()]
                log.info("Query ok: %d rows in %.1fs (%d params)", len(rows), time.time() - started, len(params))
                return rows
            except SourceError:
                raise
            except Exception as exc:
                last = exc
                log.warning("Query failed (attempt %d): %s", attempt + 1, redact(exc))
                time.sleep(1.5 * (attempt + 1))
        raise SourceError(f"Databricks query failed: {redact(last)}")

    def fetch(self, table, key_type, values, start, end, retailer):
        q = build_fetch_query(table, self.settings.allowed_tables(), key_type, values, start, end, retailer)
        return [_row_from_db(r) for r in self._run(q.sql, q.params)]

    def last_seen(self, tables, product_nos, since, retailer):
        q = build_last_seen_query(tables, self.settings.allowed_tables(), product_nos, since, retailer)
        return [{"table": r["tbl"], "productNo": r["product_no"], "retailer": r["retailer"], "last_date": r["last_date"]}
                for r in self._run(q.sql, q.params)]

    def schema_paths(self, table: str) -> set[str]:
        if table in self._schema_cache:
            return self._schema_cache[table]
        paths: set[str] = set()
        try:
            q = build_schema_query(table, self.settings.allowed_tables())
            with self._connect() as conn, conn.cursor() as cur:
                cur.execute(q.sql)
                schema = cur.fetchall_arrow().schema
            paths = _arrow_paths(schema)
        except Exception as exc:
            log.warning("Couldn't read live schema for %s (%s); using snapshot", table, redact(exc))
        if not paths:
            paths = _load_snapshot(table)
        self._schema_cache[table] = paths
        return paths

    def status(self) -> tuple[bool, str]:
        if not self.settings.is_configured():
            return False, "Not configured (set DBX_HOST, DBX_HTTP_PATH/DBX_WAREHOUSE_ID, DBX_TOKEN in .env)"
        try:
            q = build_ping_query()
            self._run(q.sql, q.params, retries=0)
            return True, "Connected"
        except SourceError as exc:
            return False, str(exc)


def _arrow_paths(schema) -> set[str]:
    import pyarrow as pa

    out: set[str] = set()

    def visit(t, prefix):
        if pa.types.is_struct(t):
            for f in t:
                p = f"{prefix}.{f.name}"
                out.add(p.lower())
                visit(f.type, p)
        elif pa.types.is_list(t) or pa.types.is_large_list(t):
            visit(t.value_type, prefix)

    for f in schema:
        out.add(f.name.lower())
        visit(f.type, f.name)
    return out


# ---------------------------------------------------------------- offline sources

def _key_of(record: dict, key_type: str) -> str | None:
    if key_type == "productNo":
        v = get_path(record, "extract.productNo")
    elif key_type == "url":
        v = get_path(record, "gather.url")
    else:
        v = get_path(record, "extract.name")
        return str(v).strip().lower() if v else None  # same as SQL lower(trim(...))
    return str(v) if v is not None else None


def _like(value: str, pattern: str) -> bool:
    return fnmatch.fnmatchcase(value or "", pattern.replace("%", "*").replace("_", "?"))


def emulate_fetch(records: list[dict], key_type: str, values: list[str], start: date, end: date,
                  retailer: str | None, keep_latest: int = 2) -> list[dict]:
    """Python version of build_fetch_query's semantics for fixture/mock records."""
    wanted = set(values)
    groups: dict[tuple[str, str], list[dict]] = {}
    for rec in records:
        ts = str(rec.get("timestamp") or "")
        d = ts[:10]
        if d and not (start.isoformat() <= d <= end.isoformat()):
            continue
        ret = get_path(rec, "metadata.retailer") or ""
        if retailer and not _like(ret, retailer):
            continue
        k = _key_of(rec, key_type)
        if k is None or k not in wanted:
            continue
        groups.setdefault((ret, k), []).append(rec)
    rows = []
    for (ret, k), recs in groups.items():
        recs.sort(key=lambda r: (str(r.get("timestamp")), str(r.get("id"))), reverse=True)
        for i, rec in enumerate(recs[:keep_latest]):
            rows.append({"record_id": rec.get("id"), "ts": rec.get("timestamp"), "retailer": ret,
                         "match_key": k, "rn": i + 1, "n_records": len(recs), "record": rec})
    return rows


class FixtureSource:
    """Production records saved to JSON: {"table": [record, ...]} or a flat list (any table)."""
    name = "Fixture (saved production records)"

    def __init__(self, path: str | Path):
        self.path = Path(path)
        data = json.loads(self.path.read_text(encoding="utf-8"))
        data = data if isinstance(data, dict) else {"*": data}
        self.last_seen_rows: list[dict] = (data.get("_last_seen") or {}).get("rows", [])
        self.by_table: dict[str, list[dict]] = {k: v for k, v in data.items() if not k.startswith("_")}

    def _records(self, table: str) -> list[dict]:
        return self.by_table.get(table, []) + self.by_table.get("*", [])

    def fetch(self, table, key_type, values, start, end, retailer):
        return emulate_fetch(self._records(table), key_type, values, start, end, retailer)

    def last_seen(self, tables, product_nos, since, retailer):
        """Saved diagnostics plus whatever the saved records show."""
        wanted, out = set(product_nos), {}
        rows = list(getattr(self, "last_seen_rows", []))
        for t in tables:
            for rec in self._records(t):
                rows.append({"table": t, "productNo": str(get_path(rec, "extract.productNo")),
                             "retailer": get_path(rec, "metadata.retailer") or "",
                             "last_date": str(rec.get("timestamp") or "")[:10]})
        for r in rows:
            if r["table"] not in tables or r["productNo"] not in wanted or r["last_date"] < since.isoformat():
                continue
            if retailer and not _like(r["retailer"], retailer):
                continue
            k = (r["table"], r["productNo"], r["retailer"])
            if k not in out or r["last_date"] > out[k]["last_date"]:
                out[k] = dict(r)
        return list(out.values())

    def schema_paths(self, table: str) -> set[str]:
        return _load_snapshot(table) or schema_paths_from_records(self._records(table))

    def status(self) -> tuple[bool, str]:
        n = sum(len(v) for v in self.by_table.values())
        return True, f"{n} saved records from {self.path.name}"


class MockSource(FixtureSource):
    """In-memory synthetic records (see mock_data.py)."""
    name = "Mock (synthetic)"

    def __init__(self, records_by_table: dict[str, list[dict]]):
        self.path = Path("mock")
        self.by_table = records_by_table
        self.last_seen_rows = []

    def status(self) -> tuple[bool, str]:
        n = sum(len(v) for v in self.by_table.values())
        return True, f"{n} synthetic records"
