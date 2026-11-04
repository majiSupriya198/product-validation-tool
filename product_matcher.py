"""Find the production record for every uploaded product.

Matching runs in batched passes, each only for products still unmatched:
  1. productNo           extract.productNo IN (...)
  2. URL                 gather.url IN (...), plus the trailing number of the URL as a productNo
  3. name                lower(trim(extract.name)) IN (...)
Per (retailer, key) only the latest record by timestamp is used; the second-latest is kept
to flag crawls that disagree. When several retailers match, the URL locale picks one.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Callable
from urllib.parse import urlparse

from config import DBXSettings, Rules
from dbx_connector import Source, SourceError
from field_resolver import get_path
from file_processor import Product
from utils.logger import get_logger
from utils.normalization import norm_strict, parse_number
from utils.query_builder import chunked

log = get_logger("match")

_LOCALE = re.compile(r"/([a-z]{2})[-_]([A-Za-z]{2})(?:/|$)")
_TRAILING_ID = re.compile(r"(\d{5,})(?:\D*)$")


@dataclass
class MatchResult:
    product: Product
    table: str | None
    pagetype: str | None
    record: dict | None = None
    method: str | None = None             # productNo | url | url_derived_productNo | name
    identifier_used: str | None = None
    retailer: str | None = None
    dbx_timestamp: str | None = None
    record_id: str | None = None
    n_records: int = 0                    # crawls of this product in the date range (chosen retailer)
    candidate_retailers: list[str] = field(default_factory=list)
    ambiguous: bool = False
    notes: list[str] = field(default_factory=list)
    attempted: list[str] = field(default_factory=list)
    not_found_reason: str | None = None

    @property
    def found(self) -> bool:
        return self.record is not None


def url_country(url: str | None) -> str | None:
    """'https://www.goflink.com/shop/en-DE/product/...' -> 'DE'. Falls back to a 2-letter TLD."""
    if not url:
        return None
    m = _LOCALE.search(urlparse(url).path)
    if m:
        return m.group(2).upper()
    host = urlparse(url).netloc.lower()
    tld = host.rsplit(".", 1)[-1] if "." in host else ""
    return tld.upper() if len(tld) == 2 else None


def url_trailing_id(url: str | None) -> str | None:
    if not url:
        return None
    m = _TRAILING_ID.search(urlparse(url).path.rstrip("/"))
    return m.group(1) if m else None


def _tables_for(pagetype: str | None, settings: DBXSettings) -> list[tuple[str, str]]:
    pt = (pagetype or "").lower()
    if pt in ("detail", "search"):
        return [(pt, settings.table_for(pt))]
    return [("detail", settings.table_for("detail")), ("search", settings.table_for("search"))]


def _choose(rows: list[dict], product: Product) -> tuple[dict | None, dict | None, list[str], bool, str | None]:
    """From latest rows (rn=1) across retailers pick one. Returns (latest, second_latest, retailers, ambiguous, note)."""
    latest = [r for r in rows if r["rn"] == 1]
    if not latest:
        return None, None, [], False, None
    retailers = sorted({r["retailer"] for r in latest})
    chosen, note, ambiguous = None, None, False
    country = url_country(product.identifiers.get("originalURL"))
    if len(latest) == 1:
        chosen = latest[0]
        rc = _retailer_country(chosen)
        if country and rc and rc != country:
            ambiguous = True
            note = (f"Only found for {chosen['retailer']} ({rc}), but the uploaded URL locale is {country}; "
                    "values may be from a different country's site")
    else:
        if country:
            hits = [r for r in latest if _retailer_country(r) == country]
            if len(hits) == 1:
                chosen = hits[0]
                note = f"Retailer {chosen['retailer']} chosen from URL locale ({country}) among {', '.join(retailers)}"
        if chosen is None:
            chosen = max(latest, key=lambda r: str(r["ts"]))
            ambiguous = True
            keys = sorted({str(r["match_key"]) for r in latest})
            note = (f"Ambiguous: {len(latest)} candidate records ({', '.join(retailers)}"
                    + (f"; keys {', '.join(keys)}" if len(keys) > 1 else "") + "); latest used")
    second = next((r for r in rows if r["rn"] == 2 and r["retailer"] == chosen["retailer"]
                   and r["match_key"] == chosen["match_key"]), None)
    return chosen, second, retailers, ambiguous, note


def _retailer_country(row: dict) -> str | None:
    meta = row["record"].get("metadata") or {}
    c = meta.get("country")
    if c and len(str(c)) == 2:
        return str(c).upper()
    m = re.search(r"[-_]([A-Z]{2})$", row["retailer"] or "")
    return m.group(1) if m else None


def _crawl_disagreement(latest: dict, second: dict | None) -> str | None:
    if not second:
        return None
    a, b = latest["record"], second["record"]
    diffs = []
    pa, pb = parse_number(get_path(a, "extract.price")), parse_number(get_path(b, "extract.price"))
    if pa is not None and pb is not None and abs(pa - pb) > 0.005:
        diffs.append(f"price {pb:g} → {pa:g}")
    na, nb = get_path(a, "extract.name"), get_path(b, "extract.name")
    if na and nb and norm_strict(na) != norm_strict(nb):
        diffs.append("name")
    if diffs:
        return f"Latest two crawls differ ({'; '.join(diffs)}); the latest ({latest['ts']}) is used"
    return None


def match_products(products: list[Product], source: Source, settings: DBXSettings, rules: Rules,
                   start: date, end: date, retailer: str | None,
                   pagetype_default: str | None = None,
                   progress: Callable[[float, str], None] | None = None) -> list[MatchResult]:
    progress = progress or (lambda f, m: None)
    results: list[MatchResult] = []
    for p in products:
        pt = p.identifiers.get("Pagetype") or pagetype_default
        results.append(MatchResult(product=p, table=None, pagetype=pt))

    # products per table; "both" / unknown page type -> try detail then search
    plan: dict[str, list[MatchResult]] = {}
    for m in results:
        if "no identifiers" in m.product.warnings:
            m.not_found_reason = "Row has no productNo, originalURL or name"
            continue
        try:
            tables = _tables_for(m.pagetype, settings)
        except ValueError as exc:
            m.not_found_reason = str(exc)
            continue
        for _, tbl in tables:
            plan.setdefault(tbl, []).append(m)

    # fixed order: detail before search, whatever the row order in the file
    order = [settings.table_for("detail"), settings.table_for("search")]
    plan = {t: plan[t] for t in order if t in plan}
    total_steps = max(1, len(plan) * 3)
    step = 0
    for table, group in plan.items():
        pagetype = "detail" if table.endswith(settings.detail_table) else "search"
        passes: list[tuple[str, str, Callable[[MatchResult], list[str]]]] = [
            ("productNo", "productNo", lambda m: [m.product.identifiers["productNo"]] if m.product.identifiers.get("productNo") else []),
            ("url", "url", lambda m: [m.product.identifiers["originalURL"]] if m.product.identifiers.get("originalURL") else []),
            ("name", "name", lambda m: [m.product.identifiers["name"].strip().lower()] if m.product.identifiers.get("name") else []),
        ]
        for method, key_type, keys_of in passes:
            step += 1
            pending = [m for m in group if not m.found]
            if not pending:
                continue
            progress(step / total_steps, f"{pagetype}: matching {len(pending)} products by {method}")
            by_key: dict[str, list[MatchResult]] = {}
            for m in pending:
                for k in keys_of(m):
                    by_key.setdefault(k, []).append(m)
                    m.attempted.append(f"{pagetype}.{method}={k}")
            if method == "url":
                # production stores API URLs, so also try the number at the end of the storefront URL
                derived: dict[str, list[MatchResult]] = {}
                for m in pending:
                    tid = url_trailing_id(m.product.identifiers.get("originalURL"))
                    if tid and tid != m.product.identifiers.get("productNo"):
                        derived.setdefault(tid, []).append(m)
                        m.attempted.append(f"{pagetype}.url_derived_productNo={tid}")
                _run_pass(source, rules, table, pagetype, "productNo", "url_derived_productNo", derived, start, end, retailer, product_key=True)
            _run_pass(source, rules, table, pagetype, key_type, method, by_key, start, end, retailer)

    unmatched = [m for m in results if not m.found and not m.not_found_reason]
    seen = _last_seen_lookup(unmatched, source, settings, rules, end, retailer, progress)
    for m in unmatched:
        base = (f"No production record in {start} to {end}"
                + (f" for retailer '{retailer}'" if retailer else ""))
        m.not_found_reason = base + ". " + _explain_not_found(m, seen, settings, start, end, rules, retailer)
    progress(1.0, "matching done")
    found = sum(1 for m in results if m.found)
    log.info("Matched %d of %d products", found, len(results))
    return results


def _candidate_pnos(m: MatchResult) -> list[str]:
    ids = [m.product.identifiers.get("productNo"), url_trailing_id(m.product.identifiers.get("originalURL"))]
    return [i for i in dict.fromkeys(ids) if i]


def _last_seen_lookup(unmatched: list[MatchResult], source: Source, settings: DBXSettings, rules: Rules,
                      end: date, retailer: str | None, progress) -> dict[str, list[dict]] | None:
    """Latest crawl per table/retailer for unmatched productNos over a longer lookback (one batched query)."""
    lookback = rules.last_seen_lookback_days
    pnos = sorted({p for m in unmatched for p in _candidate_pnos(m)})
    if lookback <= 0 or not hasattr(source, "last_seen"):
        return None
    if not pnos:
        return {}
    progress(0.95, f"checking last-seen dates for {len(pnos)} unmatched products")
    since = end - timedelta(days=lookback)
    tables = [settings.table_for("detail"), settings.table_for("search")]
    out: dict[str, list[dict]] = {}
    try:
        for chunk in chunked(pnos, rules.chunk_size):
            for r in source.last_seen(tables, chunk, since, retailer):
                out.setdefault(str(r["productNo"]), []).append(r)
    except Exception as exc:  # diagnostics are best-effort; never fail the run for them
        log.warning("Last-seen lookup failed: %s", exc)
        return None
    return out


def _explain_not_found(m: MatchResult, seen: dict[str, list[dict]] | None, settings: DBXSettings,
                       start: date, end: date, rules: Rules, retailer: str | None) -> str:
    tried = ", ".join(m.attempted) or "none"
    wanted = settings.table_for(m.pagetype) if (m.pagetype or "").lower() in ("detail", "search") else None
    if seen is None:
        return f"No match (tried {tried})."
    rows = [r for p in _candidate_pnos(m) for r in seen.get(p, [])]
    if not rows:
        since = end - timedelta(days=rules.last_seen_lookback_days)
        scope = f" for retailer '{retailer}'" if retailer else ""
        return f"Not crawled in either table{scope} since {since} (tried {tried})."
    parts = []
    for r in sorted(rows, key=lambda r: r["last_date"], reverse=True):
        tname = r["table"].rsplit(".", 1)[-1]
        where = "in range" if start.isoformat() <= r["last_date"] <= end.isoformat() else (
            "after range" if r["last_date"] > end.isoformat() else "before range")
        parts.append(f"{tname} / {r['retailer']}: last crawled {r['last_date']} ({where})")
    hint = ""
    if wanted and any(r["table"] != wanted and start.isoformat() <= r["last_date"] <= end.isoformat() for r in rows):
        hint = " Found in the other table within the range; set Pagetype to the other table or Both to compare it."
    elif all(r["last_date"] < start.isoformat() for r in rows):
        hint = " Widen the date range to compare the older record."
    return "; ".join(parts) + "." + hint + f" (tried {tried})"


def _run_pass(source: Source, rules: Rules, table: str, pagetype: str, key_type: str, method: str,
              by_key: dict[str, list[MatchResult]], start: date, end: date, retailer: str | None,
              product_key: bool = False) -> None:
    if not by_key:
        return
    keys = sorted(by_key)
    rows: list[dict] = []
    for chunk in chunked(keys, rules.chunk_size):
        try:
            rows.extend(source.fetch(table, key_type, chunk, start, end, retailer))
        except SourceError:
            raise
        except Exception as exc:
            raise SourceError(f"Fetching {pagetype} records by {method} failed: {exc}") from None
    rows_by_key: dict[str, list[dict]] = {}
    for r in rows:
        rows_by_key.setdefault(str(r["match_key"]), []).append(r)
    for k, ms in by_key.items():
        cand = rows_by_key.get(k, [])
        for m in ms:
            if m.found or not cand:
                continue
            chosen, second, retailers, ambiguous, note = _choose(cand, m.product)
            if chosen is None:
                continue
            m.record = chosen["record"]
            m.table, m.pagetype = table, pagetype
            m.method, m.identifier_used = method, k
            m.retailer, m.dbx_timestamp = chosen["retailer"], chosen["ts"]
            m.record_id, m.n_records = chosen["record_id"], chosen["n_records"]
            m.candidate_retailers, m.ambiguous = retailers, ambiguous
            if note:
                m.notes.append(note)
            if method == "name":
                pnos = {str(get_path(r["record"], "extract.productNo")) for r in cand if r["rn"] == 1}
                if len(pnos) > 1:
                    m.ambiguous = True
                    m.notes.append(f"Name matches {len(pnos)} different productNos: {', '.join(sorted(pnos))}")
                up_pno = m.product.identifiers.get("productNo")
                prod_pno = get_path(m.record, "extract.productNo")
                if up_pno and prod_pno and str(up_pno) != str(prod_pno):
                    m.notes.append(f"Matched by name, but productNo differs (uploaded {up_pno}, production {prod_pno})")
            dis = _crawl_disagreement(chosen, second)
            if dis:
                m.notes.append(dis)
