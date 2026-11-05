"""Detect an uploaded file's structure and turn JSON-section CSVs into the standardized workbook.

Supported inputs
  flat          one column per attribute (CSV or Excel), e.g. Flink_PDP.xlsx
  json_sections CSV whose columns hold JSON objects: extract, parsed, feedback, metadata, gather
                (plus plain columns such as timestamp_date)
  standardized  a workbook this module produced earlier (sheets "Product Data", "Parsed Data", ...)

Standardized workbook (the validation engine's input for JSON-section data)
  Product Data       _row + extract fields (no prefix), originalURL/Pagetype filled from gather when missing
  Parsed Data        _row, productNo + parsed.* fields
  Feedback Data      _row, productNo + feedback.* fields (comments kept whole as JSON) + review count
  Feedback Reviews   one row per review, for reading (validation uses Feedback Data)
  Metadata & Gather  _row, productNo, timestamp + metadata.* + gather.* + any other plain columns
  Original Data      the CSV rows exactly as uploaded
  Transform Log      what was detected, counts, and any problems (malformed JSON, split cells)
Values longer than Excel's cell limit are split into `col__partN` columns and joined back on read.
"""
from __future__ import annotations

import io
import json
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, BinaryIO

import pandas as pd
from openpyxl import Workbook
from openpyxl.styles import Alignment, Font, PatternFill
from openpyxl.utils import get_column_letter

from json_flattener import (count_illegal, flatten, force_text, ordered_columns, parse_json_cell, split_long_cells,
                            to_cell_text)
from utils.logger import get_logger

log = get_logger("transform")

SECTIONS = ("extract", "parsed", "feedback", "metadata", "gather")
VALIDATED_SECTIONS = ("extract", "parsed", "feedback")
SHEETS = {
    "product": "Product Data",
    "parsed": "Parsed Data",
    "feedback": "Feedback Data",
    "reviews": "Feedback Reviews",
    "meta": "Metadata & Gather",
    "original": "Original Data",
    "log": "Transform Log",
}
PRODUCT_LEAD = ["name", "productNo", "gtin", "description", "brand", "manufacturer", "price", "pricePerUnit",
                "ingredients", "allergens", "nutrition", "imageURL", "bulletPoints", "country", "instructions",
                "originalURL", "Pagetype"]
REVIEW_FIELDS = ["reviewId", "rating", "subject", "content", "username", "timestamp", "dateLabel"]
COMMENT_KEYS = ("comments", "reviews")

HEADER_FILL = PatternFill("solid", fgColor="210235")
HEADER_FONT = Font(name="DM Sans", bold=True, color="FFFFFF")


class TransformError(ValueError):
    """The file can't be transformed (message is safe to show)."""


@dataclass
class Detection:
    format: str                         # flat | json_sections | standardized
    file_type: str                      # csv | xlsx | xls
    records: int
    json_sections: list[str] = field(default_factory=list)
    plain_columns: list[str] = field(default_factory=list)
    malformed: dict[str, int] = field(default_factory=dict)

    @property
    def label(self) -> str:
        return {"flat": "Flat attributes (one column per field)",
                "json_sections": "JSON sections (" + ", ".join(self.json_sections) + ")",
                "standardized": "Standardized workbook"}[self.format]


@dataclass
class TransformResult:
    detection: Detection
    sheets: dict[str, pd.DataFrame]     # sheet name -> preview frame (long cells joined)
    workbook_bytes: bytes
    warnings: list[str]
    stats: dict[str, Any]


def _read_csv(data: bytes) -> pd.DataFrame:
    try:
        return pd.read_csv(io.BytesIO(data), dtype=str, keep_default_na=False, encoding="utf-8-sig")
    except UnicodeDecodeError:
        return pd.read_csv(io.BytesIO(data), dtype=str, keep_default_na=False, encoding="latin-1")
    except Exception as exc:
        raise TransformError(f"Couldn't read the CSV: {exc}") from exc


def _as_bytes(source: str | Path | BinaryIO | bytes) -> bytes:
    if isinstance(source, bytes):
        return source
    if isinstance(source, (str, Path)):
        return Path(source).read_bytes()
    return source.read()


def _json_object_share(series: pd.Series) -> tuple[float, int]:
    """Share of non-empty cells that parse as JSON objects, and the count that fail to parse."""
    vals = [v for v in series.head(500) if isinstance(v, str) and v.strip()]
    if not vals:
        return 0.0, 0
    ok = bad = 0
    for v in vals:
        parsed, err = parse_json_cell(v)
        if err:
            bad += 1
        elif isinstance(parsed, dict):
            ok += 1
    return ok / len(vals), bad


def detect(source: str | Path | BinaryIO | bytes, filename: str) -> Detection:
    ext = Path(filename).suffix.lower().lstrip(".")
    data = _as_bytes(source)
    if ext in ("xlsx", "xlsm", "xls"):
        xl = pd.ExcelFile(io.BytesIO(data), engine="xlrd" if ext == "xls" else "openpyxl")
        if SHEETS["product"] in xl.sheet_names and (SHEETS["parsed"] in xl.sheet_names or SHEETS["feedback"] in xl.sheet_names):
            n = len(xl.parse(SHEETS["product"], dtype=str))
            secs = [s for s, sh in (("extract", "product"), ("parsed", "parsed"), ("feedback", "feedback"),
                                    ("metadata", "meta")) if SHEETS[sh] in xl.sheet_names]
            return Detection("standardized", ext, n, secs)
        df = xl.parse(xl.sheet_names[0], dtype=str, keep_default_na=False)
        return Detection("flat", ext, len(df), plain_columns=list(map(str, df.columns)))
    if ext not in ("csv", "txt"):
        raise TransformError(f"Unsupported file type '.{ext}'. Upload a .csv, .xlsx or .xls file.")
    df = _read_csv(data)
    found, malformed = [], {}
    for c in df.columns:
        share, bad = _json_object_share(df[c])
        if str(c).strip().lower() in SECTIONS and share >= 0.5:
            found.append(str(c))
            if bad:
                malformed[str(c)] = bad
    if any(c.lower() in VALIDATED_SECTIONS for c in found):
        plain = [str(c) for c in df.columns if str(c) not in found]
        return Detection("json_sections", ext, len(df), found, plain, malformed)
    return Detection("flat", ext, len(df), plain_columns=list(map(str, df.columns)))


def _reviews_of(feedback: dict) -> list[dict]:
    for k in COMMENT_KEYS:
        v = feedback.get(k)
        if isinstance(v, list):
            return [x for x in v if isinstance(x, dict)]
    return []


def transform(source: str | Path | BinaryIO | bytes, filename: str) -> TransformResult:
    """JSON-section CSV -> standardized workbook. Raises TransformError for other formats."""
    data = _as_bytes(source)
    det = detect(data, filename)
    if det.format != "json_sections":
        raise TransformError(f"Nothing to transform: detected {det.label}.")
    df = _read_csv(data)
    if df.empty:
        raise TransformError("The CSV has no data rows.")
    colmap = {c.lower(): c for c in det.json_sections}
    warnings: list[str] = []
    malformed_rows: list[str] = []
    illegal = filled_url = 0

    product_rows, parsed_rows, feedback_rows, review_rows, meta_rows, original_rows = [], [], [], [], [], []
    for i, raw in enumerate(df.to_dict(orient="records")):
        row_no = i + 2  # header is row 1 in the CSV
        sec: dict[str, dict] = {}
        for s in SECTIONS:
            col = colmap.get(s)
            if not col:
                sec[s] = {}
                continue
            val, err = parse_json_cell(raw.get(col))
            if err:
                malformed_rows.append(f"row {row_no} {col}: {err}")
                sec[s] = {"__raw__": raw.get(col)}          # keep the text so nothing is lost
            elif val is None:
                sec[s] = {}
            elif isinstance(val, dict):
                sec[s] = val
            else:
                sec[s] = {"__value__": val}
        ex, pa, fb, me, ga = (sec[s] for s in SECTIONS)
        product_no = ex.get("productNo")

        prod = {"_row": row_no, **flatten(ex)}
        if not prod.get("originalURL") and ga.get("url"):
            prod["originalURL"] = ga.get("url")             # fallback, noted in the log
            filled_url += 1
        product_rows.append(prod)

        parsed_rows.append({"_row": row_no, "productNo": product_no, **flatten(pa, "parsed")})

        reviews = _reviews_of(fb)
        frow = {"_row": row_no, "productNo": product_no, **flatten(fb, "feedback")}
        frow["feedback.review_count"] = len(reviews)
        feedback_rows.append(frow)
        for j, rv in enumerate(reviews, 1):
            review_rows.append({"_row": row_no, "productNo": product_no, "review_index": j,
                                **{k: rv.get(k) for k in REVIEW_FIELDS},
                                **{k: v for k, v in rv.items() if k not in REVIEW_FIELDS}})

        plain = {c: raw.get(c) for c in det.plain_columns}
        meta_rows.append({"_row": row_no, "productNo": product_no, **plain,
                          **flatten(me, "metadata"), **flatten(ga, "gather")})
        original_rows.append({"_row": row_no, **raw})
        illegal += sum(count_illegal(v) for v in raw.values())

    if malformed_rows:
        warnings.append(f"{len(malformed_rows)} JSON cells couldn't be parsed; their raw text is kept in a "
                        f"'__raw__' column: " + "; ".join(malformed_rows[:5]) + (" …" if len(malformed_rows) > 5 else ""))
    if filled_url:
        warnings.append(f"originalURL taken from gather.url for {filled_url} rows that had none in extract")
    if illegal:
        warnings.append(f"{illegal} control characters Excel can't store were written as \\uXXXX escapes")

    sheets_rows = {
        SHEETS["product"]: (product_rows, ["_row"] + PRODUCT_LEAD),
        SHEETS["parsed"]: (parsed_rows, ["_row", "productNo"]),
        SHEETS["feedback"]: (feedback_rows, ["_row", "productNo", "feedback.avgRating", "feedback.commentCount",
                                            "feedback.totalRatings", "feedback.review_count"]),
        SHEETS["reviews"]: (review_rows, ["_row", "productNo", "review_index"] + REVIEW_FIELDS),
        SHEETS["meta"]: (meta_rows, ["_row", "productNo"] + det.plain_columns),
        SHEETS["original"]: (original_rows, ["_row"] + list(map(str, df.columns))),
    }
    stats = {
        "records": len(df),
        "json_sections": det.json_sections,
        "extract_fields": len(ordered_columns(product_rows)) - 1,
        "parsed_fields": len(ordered_columns(parsed_rows)) - 2,
        "feedback_fields": len(ordered_columns(feedback_rows)) - 2,
        "reviews": len(review_rows),
        "malformed_json_cells": len(malformed_rows),
    }
    wb, split_cols = _build_workbook(sheets_rows, det, stats, warnings)
    if split_cols:
        warnings.append("Long values split across continuation columns (joined again on read): "
                        + ", ".join(f"{s}: {', '.join(c)}" for s, c in split_cols.items()))
        _write_log(wb, det, stats, warnings)
    buf = io.BytesIO()
    wb.save(buf)
    previews = {name: pd.DataFrame(rows, columns=ordered_columns(rows, lead)) for name, (rows, lead) in sheets_rows.items()}
    log.info("Transformed %s: %s", filename, stats)
    return TransformResult(det, previews, buf.getvalue(), warnings, stats)


def _build_workbook(sheets_rows, det: Detection, stats, warnings):
    wb = Workbook()
    wb.remove(wb.active)
    split_cols: dict[str, list[str]] = {}
    for name, (rows, lead) in sheets_rows.items():
        ws = wb.create_sheet(name)
        cols = ordered_columns(rows, lead) or list(lead)
        cells = [{c: to_cell_text(r.get(c)) for c in cols} for r in rows]
        cells, cols2 = split_long_cells(cells, cols)
        extra = [c for c in cols2 if c not in cols]
        if extra:
            split_cols[name] = sorted({c.split("__part")[0] for c in extra})
        ws.append(cols2)
        for r in cells:
            ws.append([r.get(c) for c in cols2])
            for cell in ws[ws.max_row]:
                force_text(cell)
        for j, c in enumerate(cols2, 1):
            cell = ws.cell(row=1, column=j)
            cell.fill, cell.font = HEADER_FILL, HEADER_FONT
            ws.column_dimensions[get_column_letter(j)].width = min(max(len(str(c)) + 2, 10), 45)
        ws.freeze_panes = "B2"
        if cols2:
            ws.auto_filter.ref = f"A1:{get_column_letter(len(cols2))}{max(ws.max_row, 1)}"
    _write_log(wb, det, stats, warnings)
    return wb, split_cols


def _write_log(wb: Workbook, det: Detection, stats: dict, warnings: list[str]) -> None:
    name = SHEETS["log"]
    if name in wb.sheetnames:
        del wb[name]
    ws = wb.create_sheet(name)
    ws["A1"] = "Transform log"
    ws["A1"].font = Font(name="DM Sans", size=14, bold=True, color="210235")
    rows = [("Generated", datetime.now().strftime("%Y-%m-%d %H:%M")), ("Detected format", det.label),
            *[(k.replace("_", " ").capitalize(), ", ".join(v) if isinstance(v, list) else v) for k, v in stats.items()]]
    for i, (k, v) in enumerate(rows, 3):
        ws.cell(row=i, column=1, value=k).font = Font(name="DM Sans", bold=True)
        ws.cell(row=i, column=2, value=str(v))
    r = len(rows) + 4
    ws.cell(row=r, column=1, value="Warnings").font = Font(name="DM Sans", bold=True)
    for w in warnings or ["none"]:
        r += 1
        c = ws.cell(row=r, column=1, value=to_cell_text(w))
        c.alignment = Alignment(wrap_text=True)
    ws.column_dimensions["A"].width = 28
    ws.column_dimensions["B"].width = 80


def save_transformed(result: TransformResult, out_dir: Path, stem: str) -> Path:
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / f"{stem}_standardized_{datetime.now():%Y%m%d_%H%M%S}.xlsx"
    path.write_bytes(result.workbook_bytes)
    return path
