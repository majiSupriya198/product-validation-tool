"""Read an uploaded CSV/XLSX/XLS file into product records without assuming a fixed layout."""
from __future__ import annotations

import io
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, BinaryIO

import pandas as pd

from utils.logger import get_logger
from utils.normalization import clean_cell, is_empty

log = get_logger("file")

IDENTIFIER_ALIASES = {
    "productNo": ["productno", "product_no", "productnumber", "productid", "product_id", "sku", "skuid", "itemid", "asin"],
    "originalURL": ["originalurl", "original_url", "url", "producturl", "product_url", "pageurl", "link"],
    "name": ["name", "productname", "product_name", "title"],
    "Pagetype": ["pagetype", "page_type", "pagetyp", "type"],
}


PAGETYPE_ALIASES = {
    "detail": {"detail", "details", "product", "products", "pdp", "productpage", "product_page", "item", "itempage"},
    "search": {"search", "searches", "listing", "plp", "category", "searchpage", "search_page", "serp"},
}


def normalize_pagetype(value: str | None) -> str | None:
    """'Product' / 'PDP' -> 'detail', 'Listing' / 'PLP' -> 'search'. Unknown values pass through lowercased."""
    if not value:
        return None
    v = str(value).strip().lower()
    for canon, names in PAGETYPE_ALIASES.items():
        if v in names:
            return canon
    return v


class InputFileError(ValueError):
    """Raised for unreadable or unusable input files."""


@dataclass
class Product:
    row_index: int                       # 1-based row in the original file (header = row 1 -> first data row = 2)
    identifiers: dict[str, str | None]   # productNo / originalURL / name / Pagetype (canonical keys)
    attributes: dict[str, Any]           # cleaned values keyed by original column name (dotted names kept)
    original: dict[str, Any]             # untouched cell values for traceability
    warnings: list[str] = field(default_factory=list)

    @property
    def label(self) -> str:
        return self.identifiers.get("name") or self.identifiers.get("productNo") or f"row {self.row_index}"


@dataclass
class ParsedFile:
    products: list[Product]
    columns: list[str]
    identifier_columns: dict[str, str | None]
    dropped_columns: list[str]
    warnings: list[str]
    format: str = "flat"                  # flat | standardized (JSON sections)

    @property
    def uploaded_retailers(self) -> list[str]:
        return sorted({p.identifiers["retailer"] for p in self.products if p.identifiers.get("retailer")})

    @property
    def timestamp_range(self) -> tuple[str, str] | None:
        ts = sorted(p.identifiers["timestamp"][:10] for p in self.products if p.identifiers.get("timestamp"))
        return (ts[0], ts[-1]) if ts else None


def _norm_col(c: str) -> str:
    return re.sub(r"[^a-z0-9_]", "", str(c).strip().lower())


def detect_identifier_columns(columns: list[str]) -> dict[str, str | None]:
    lookup = {_norm_col(c): c for c in columns}
    found: dict[str, str | None] = {}
    for canon, aliases in IDENTIFIER_ALIASES.items():
        found[canon] = None
        for a in aliases:
            if a in lookup:
                found[canon] = lookup[a]
                break
            a2 = a.replace("_", "")
            if a2 in lookup:
                found[canon] = lookup[a2]
                break
    return found


def read_table(source: str | Path | BinaryIO | bytes, filename: str | None = None) -> pd.DataFrame:
    """Read the file as all-string columns so identifiers keep leading zeros."""
    name = filename or (str(source) if isinstance(source, (str, Path)) else "upload")
    ext = Path(name).suffix.lower()
    if isinstance(source, bytes):
        source = io.BytesIO(source)
    try:
        if ext == ".csv" or ext == ".txt":
            df = pd.read_csv(source, dtype=str, keep_default_na=False, encoding_errors="replace", sep=None, engine="python")
        elif ext in (".xlsx", ".xlsm"):
            df = pd.read_excel(source, dtype=str, keep_default_na=False, engine="openpyxl")
        elif ext == ".xls":
            df = pd.read_excel(source, dtype=str, keep_default_na=False, engine="xlrd")
        else:
            raise InputFileError(f"Unsupported file type '{ext}'. Upload a .csv, .xlsx or .xls file.")
    except InputFileError:
        raise
    except Exception as exc:  # pandas raises many types here
        raise InputFileError(f"Could not read {Path(name).name}: {exc}") from exc
    if df.empty:
        raise InputFileError("The file has no data rows.")
    return df


def parse_file(source: str | Path | BinaryIO | bytes, filename: str | None = None,
               pagetype_override: str | None = None) -> ParsedFile:
    """Read any supported upload. JSON-section CSVs are transformed to the standardized workbook first."""
    from csv_transformer import TransformError, detect, transform

    name = filename or (str(source) if isinstance(source, (str, Path)) else "upload")
    data = source if isinstance(source, bytes) else (Path(source).read_bytes() if isinstance(source, (str, Path)) else source.read())
    try:
        det = detect(data, name)
    except TransformError as exc:
        raise InputFileError(str(exc)) from exc
    except Exception as exc:
        raise InputFileError(f"Could not read {Path(name).name}: {exc}") from exc
    if det.format == "json_sections":
        try:
            result = transform(data, name)
        except TransformError as exc:
            raise InputFileError(str(exc)) from exc
        parsed = parse_standardized(result.workbook_bytes, pagetype_override)
        parsed.warnings = result.warnings + parsed.warnings
        return parsed
    if det.format == "standardized":
        return parse_standardized(data, pagetype_override)
    return _parse_flat(data, name, pagetype_override)


def parse_standardized(data: bytes, pagetype_override: str | None = None) -> ParsedFile:
    """Read a standardized workbook (see csv_transformer). Attribute keys are section-qualified:
    extract.<field>, parsed.<field>, feedback.<field>."""
    from csv_transformer import SHEETS
    from json_flattener import join_long_cells

    try:
        xl = pd.ExcelFile(io.BytesIO(data), engine="openpyxl")
    except Exception as exc:
        raise InputFileError(f"Could not read the standardized workbook: {exc}") from exc

    def sheet(name: str) -> dict[int, dict]:
        if name not in xl.sheet_names:
            return {}
        df = xl.parse(name, dtype=str, keep_default_na=False)
        out = {}
        for r in df.to_dict(orient="records"):
            r = join_long_cells(r)
            try:
                out[int(float(r.pop("_row")))] = r
            except (KeyError, ValueError):
                continue
        return out

    product, parsed_s, feedback, meta, original = (sheet(SHEETS[k]) for k in ("product", "parsed", "feedback", "meta", "original"))
    if not product:
        raise InputFileError("The standardized workbook has no rows in 'Product Data'.")
    warnings: list[str] = []
    products: list[Product] = []
    skip_feedback = {"feedback.review_count"}
    for row_no in sorted(product):
        ex = product[row_no]
        attrs: dict[str, Any] = {}
        for k, v in ex.items():
            if k.startswith("__"):
                continue
            attrs[f"extract.{k}"] = clean_cell(v)
        for k, v in parsed_s.get(row_no, {}).items():
            if k.startswith("parsed."):
                attrs[k] = clean_cell(v)
        for k, v in feedback.get(row_no, {}).items():
            if k.startswith("feedback.") and k not in skip_feedback:
                attrs[k] = clean_cell(v)
        m = meta.get(row_no, {})
        idv = {
            "productNo": _s(ex.get("productNo")),
            "originalURL": _s(ex.get("originalURL")) or _s(m.get("gather.url")),
            "name": _s(ex.get("name")),
            "Pagetype": normalize_pagetype(pagetype_override if pagetype_override and pagetype_override.lower() != "both"
                                           else _s(ex.get("Pagetype"))),
            "retailer": _s(m.get("metadata.retailer")),
            "timestamp": _s(m.get("timestamp_date") or m.get("timestamp")),
        }
        p = Product(row_index=row_no, identifiers=idv, attributes=attrs,
                    original=original.get(row_no) or {**ex, **m})
        if all(is_empty(idv.get(k)) for k in ("productNo", "originalURL", "name")):
            p.warnings.append("no identifiers")
        products.append(p)
    unknown_pt = sorted({p.identifiers["Pagetype"] for p in products if p.identifiers.get("Pagetype") not in (None, "detail", "search")})
    if unknown_pt:
        warnings.append(f"Unrecognized page types {unknown_pt}; choose Detail/Search/Both in the settings.")
    columns = list(products[0].attributes) if products else []
    ids = {"productNo": "extract.productNo", "originalURL": "extract.originalURL", "name": "extract.name",
           "Pagetype": "extract.Pagetype", "retailer": "metadata.retailer"}
    log.info("Parsed standardized workbook: %d products", len(products))
    return ParsedFile(products, columns, ids, [], warnings, format="standardized")


def _s(v: Any) -> str | None:
    if v is None:
        return None
    s = str(v).strip()
    return None if is_empty(s) else s


def _parse_flat(source: bytes, filename: str, pagetype_override: str | None = None) -> ParsedFile:
    df = read_table(source, filename)
    warnings: list[str] = []

    dropped = [c for c in df.columns if str(c).startswith("Unnamed:") or not str(c).strip()]
    for c in dropped:
        non_empty = int((df[c].astype(str).str.strip() != "").sum())
        msg = f"Column without a header dropped ({c}, {non_empty} non-empty cells)"
        warnings.append(msg)
        log.warning(msg)
    df = df.drop(columns=dropped)

    dup = df.columns[df.columns.duplicated()].tolist()
    if dup:
        warnings.append(f"Duplicate column names (first kept): {dup}")
        df = df.loc[:, ~df.columns.duplicated()]

    columns = [str(c) for c in df.columns]
    ids = detect_identifier_columns(columns)
    if not any(ids[k] for k in ("productNo", "originalURL", "name")):
        raise InputFileError(
            "No identifier columns found. The file needs at least one of: productNo, originalURL, name "
            f"(columns found: {', '.join(columns[:20])})"
        )
    if not ids["Pagetype"] and not pagetype_override:
        warnings.append("No Pagetype column; choose a page type in the UI or pass --pagetype.")

    products: list[Product] = []
    for i, row in enumerate(df.to_dict(orient="records")):
        original = dict(row)
        attrs = {c: clean_cell(v) for c, v in row.items()}
        idv: dict[str, str | None] = {}
        for canon, col in ids.items():
            v = attrs.get(col) if col else None
            idv[canon] = str(v).strip() if v is not None and not isinstance(v, (list, dict)) else None
        if pagetype_override and pagetype_override.lower() != "both":
            idv["Pagetype"] = pagetype_override.lower()
        elif idv.get("Pagetype"):
            idv["Pagetype"] = normalize_pagetype(idv["Pagetype"])
        p = Product(row_index=i + 2, identifiers=idv, attributes=attrs, original=original)
        if all(is_empty(idv.get(k)) for k in ("productNo", "originalURL", "name")):
            p.warnings.append("no identifiers")
        if all(v is None for v in attrs.values()):
            p.warnings.append("empty row")
        products.append(p)

    empty_rows = sum(1 for p in products if "empty row" in p.warnings)
    if empty_rows:
        warnings.append(f"{empty_rows} empty rows kept and reported as unmatched")
    log.info("Parsed %s: %d rows, %d columns, identifiers=%s", filename or source, len(products), len(columns), ids)
    return ParsedFile(products, columns, ids, [str(c) for c in dropped], warnings)


def column_profile(parsed: ParsedFile) -> pd.DataFrame:
    """Non-empty counts per column (for the UI preview)."""
    rows = []
    for c in parsed.columns:
        n = sum(1 for p in parsed.products if p.attributes.get(c) is not None)
        rows.append({"column": c, "non_empty": n, "empty": len(parsed.products) - n})
    return pd.DataFrame(rows)
