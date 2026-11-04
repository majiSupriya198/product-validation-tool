"""Settings: Databricks connection from env/.env, validation rules from config/validation.yaml.

Credentials are only ever read from the environment. Nothing here logs or prints them.
"""
from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent
load_dotenv(ROOT / ".env", override=False)

DEFAULT_RULES_PATH = ROOT / "config" / "validation.yaml"
REPORT_DIR = ROOT / "output" / "validation_reports"
LOG_DIR = ROOT / "output" / "logs"

_IDENT = re.compile(r"^[A-Za-z0-9_]+$")


def _env(name: str, default: str = "") -> str:
    val = os.environ.get(name, default)
    # strip inline comments some editors leave in .env values
    return val.split(" #")[0].strip() if val else default


@dataclass
class DBXSettings:
    host: str = field(default_factory=lambda: _env("DBX_HOST"))
    http_path: str = field(default_factory=lambda: _env("DBX_HTTP_PATH"))
    warehouse_id: str = field(default_factory=lambda: _env("DBX_WAREHOUSE_ID"))
    token: str = field(default_factory=lambda: _env("DBX_TOKEN"), repr=False)
    auth_type: str = field(default_factory=lambda: _env("DBX_AUTH_TYPE", "token"))
    catalog: str = field(default_factory=lambda: _env("DBX_CATALOG", "common_catalog"))
    schema: str = field(default_factory=lambda: _env("DBX_SCHEMA", "gather_data"))
    detail_table: str = field(default_factory=lambda: _env("DBX_DETAIL_TABLE", "sku_detail_v2"))
    search_table: str = field(default_factory=lambda: _env("DBX_SEARCH_TABLE", "sku_search_v2"))
    query_timeout: int = field(default_factory=lambda: int(_env("DBX_QUERY_TIMEOUT", "300") or 300))

    def resolved_http_path(self) -> str:
        if self.http_path:
            return self.http_path
        if self.warehouse_id:
            return f"/sql/1.0/warehouses/{self.warehouse_id}"
        return ""

    def is_configured(self) -> bool:
        has_auth = bool(self.token) or self.auth_type == "databricks-oauth"
        return bool(self.host and self.resolved_http_path() and has_auth)

    def table_for(self, pagetype: str) -> str:
        """Fully qualified table for a page type. Raises on unknown page types."""
        pt = (pagetype or "").strip().lower()
        if pt == "detail":
            tbl = self.detail_table
        elif pt == "search":
            tbl = self.search_table
        else:
            raise ValueError(f"Unsupported page type: {pagetype!r} (expected detail or search)")
        return self.qualify(tbl)

    def qualify(self, table: str) -> str:
        parts = table.split(".")
        if len(parts) == 1:
            parts = [self.catalog, self.schema, table]
        if len(parts) != 3 or not all(_IDENT.match(p) for p in parts):
            raise ValueError(f"Invalid table identifier: {table!r}")
        return ".".join(parts)

    def allowed_tables(self) -> set[str]:
        return {self.qualify(self.detail_table), self.qualify(self.search_table)}


@dataclass
class Rules:
    raw: dict[str, Any]

    @classmethod
    def load(cls, path: Path | str | None = None) -> "Rules":
        p = Path(path) if path else DEFAULT_RULES_PATH
        data: dict[str, Any] = {}
        if p.exists():
            with open(p, encoding="utf-8") as fh:
                data = yaml.safe_load(fh) or {}
        return cls(data)

    def get(self, *keys: str, default: Any = None) -> Any:
        cur: Any = self.raw
        for k in keys:
            if not isinstance(cur, dict) or k not in cur:
                return default
            cur = cur[k]
        return cur

    @property
    def aliases(self) -> dict[str, str]:
        return {norm_key(k): v for k, v in (self.get("aliases", default={}) or {}).items()}

    @property
    def skip(self) -> set[str]:
        return {norm_key(k) for k in (self.get("skip_attributes", default=[]) or [])}

    @property
    def comparators(self) -> list[dict[str, str]]:
        return self.get("comparators", default=[]) or []

    @property
    def abs_tol(self) -> float:
        return float(self.get("numeric", "abs_tolerance", default=0.01))

    @property
    def rel_tol(self) -> float:
        return float(self.get("numeric", "rel_tolerance", default=0.005))

    @property
    def item_threshold(self) -> float:
        return float(self.get("text", "item_match_threshold", default=0.8))

    @property
    def match_threshold(self) -> float:
        return float(self.get("scoring", "match_threshold", default=0.98))

    @property
    def partial_threshold(self) -> float:
        return float(self.get("scoring", "partial_threshold", default=0.5))

    @property
    def weights(self) -> dict[str, float]:
        return {norm_key(k): float(v) for k, v in (self.get("scoring", "weights", default={}) or {}).items()}

    @property
    def missing_counts_as_zero(self) -> bool:
        return bool(self.get("scoring", "missing_counts_as_zero", default=True))

    @property
    def false_equals_empty(self) -> bool:
        return bool(self.get("scoring", "false_equals_empty", default=True))

    @property
    def last_seen_lookback_days(self) -> int:
        return int(self.get("matching", "last_seen_lookback_days", default=180))

    @property
    def chunk_size(self) -> int:
        return int(self.get("matching", "chunk_size", default=500))


def norm_key(name: str) -> str:
    """Normalized attribute key: lowercase, dots kept, other non-alphanumerics dropped."""
    return re.sub(r"[^a-z0-9.]", "", str(name).lower())
