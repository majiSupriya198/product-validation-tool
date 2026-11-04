"""Five-sheet Excel report: Summary, Attribute Comparison, Missing Attributes, Unmatched Products, Raw DBX Data."""
from __future__ import annotations

import json
from datetime import datetime
from io import BytesIO
from pathlib import Path
from typing import Any, Iterable

from openpyxl import Workbook
from openpyxl.formatting.rule import FormulaRule
from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
from openpyxl.utils import get_column_letter
from openpyxl.worksheet.worksheet import Worksheet

from pipeline import RunResult
from scoring_engine import methodology_text
from utils.normalization import short

EXCEL_CELL_LIMIT = 32_000

HEADLINE = "210235"
ACCENT = "C231FF"
BLUE = "1F22B2"
FONT = "DM Sans"

FILL = {
    "Match": PatternFill("solid", fgColor="C6EFCE"),
    "Partial": PatternFill("solid", fgColor="FFEB9C"),
    "Mismatch": PatternFill("solid", fgColor="FFC7CE"),
    "Missing in production": PatternFill("solid", fgColor="FFC7CE"),
    "N/A": PatternFill("solid", fgColor="E7E6E6"),
    "Not in schema": PatternFill("solid", fgColor="E7E6E6"),
}
HEADER_FILL = PatternFill("solid", fgColor=HEADLINE)
HEADER_FONT = Font(name=FONT, bold=True, color="FFFFFF")
BODY_FONT = Font(name=FONT, size=10)
TITLE_FONT = Font(name=FONT, size=16, bold=True, color=HEADLINE)
SUB_FONT = Font(name=FONT, size=11, bold=True, color=BLUE)
THIN = Border(bottom=Side(style="thin", color="D9D9D9"))


def _cell(v: Any) -> Any:
    if v is None:
        return None
    if isinstance(v, (int, float)) and not isinstance(v, bool):
        return v
    if isinstance(v, (list, dict)):
        v = json.dumps(v, ensure_ascii=False, default=str)
    s = str(v)
    if s and s[0] in "=+-@":  # never let a value become a formula
        s = "'" + s
    if len(s) > EXCEL_CELL_LIMIT:
        s = s[:EXCEL_CELL_LIMIT] + " …[truncated]"
    return s


def _table(ws: Worksheet, start_row: int, headers: list[str], rows: Iterable[list[Any]],
           widths: dict[str, int] | None = None, wrap: set[str] | None = None) -> tuple[int, int]:
    """Write a header + rows block with filter. Returns (first_row, last_row)."""
    widths, wrap = widths or {}, wrap or set()
    for j, h in enumerate(headers, 1):
        c = ws.cell(row=start_row, column=j, value=h)
        c.fill, c.font = HEADER_FILL, HEADER_FONT
        c.alignment = Alignment(vertical="center", wrap_text=True)
    r = start_row
    for r_i, row in enumerate(rows, start_row + 1):
        r = r_i
        for j, v in enumerate(row, 1):
            c = ws.cell(row=r, column=j, value=_cell(v))
            c.font = BODY_FONT
            c.border = THIN
            c.alignment = Alignment(vertical="top", wrap_text=headers[j - 1] in wrap)
    last_col = get_column_letter(len(headers))
    ws.auto_filter.ref = f"A{start_row}:{last_col}{max(r, start_row)}"
    for j, h in enumerate(headers, 1):
        ws.column_dimensions[get_column_letter(j)].width = widths.get(h, min(max(len(h) + 4, 12), 40))
    return start_row, r


def _status_colors(ws: Worksheet, status_col: str, cols: list[str], first: int, last: int) -> None:
    """Colour the given columns by the status text in status_col (conditional formatting, so filters keep it)."""
    if last <= first:
        return
    for col in cols:
        rng = f"{col}{first + 1}:{col}{last}"
        for status, fill in FILL.items():
            ws.conditional_formatting.add(rng, FormulaRule(formula=[f'${status_col}{first + 1}="{status}"'], fill=fill, stopIfTrue=True))


def _pct_colors(ws: Worksheet, cols: list[str], first: int, last: int) -> None:
    """Colour percentage cells: 100 green, 50-99 yellow, <50 red, blank grey."""
    if last <= first:
        return
    for col in cols:
        rng = f"{col}{first + 1}:{col}{last}"
        c0 = f"{col}{first + 1}"
        ws.conditional_formatting.add(rng, FormulaRule(formula=[f'ISBLANK({c0})'], fill=FILL["N/A"], stopIfTrue=True))
        ws.conditional_formatting.add(rng, FormulaRule(formula=[f'{c0}>=100'], fill=FILL["Match"], stopIfTrue=True))
        ws.conditional_formatting.add(rng, FormulaRule(formula=[f'{c0}>=50'], fill=FILL["Partial"], stopIfTrue=True))
        ws.conditional_formatting.add(rng, FormulaRule(formula=[f'{c0}<50'], fill=FILL["Mismatch"], stopIfTrue=True))


def _matching_desc(m) -> str:
    if not m.found:
        return ""
    s = f"{m.method} = {m.identifier_used}"
    if m.notes:
        s += " | " + " | ".join(m.notes)
    return s


def build_report(run: RunResult) -> Workbook:
    wb = Workbook()
    _summary(wb.active, run)
    _comparison(wb.create_sheet("Attribute Comparison"), run)
    _missing(wb.create_sheet("Missing Attributes"), run)
    _unmatched(wb.create_sheet("Unmatched Products"), run)
    _raw(wb.create_sheet("Raw DBX Data"), run)
    return wb


def _summary(ws: Worksheet, run: RunResult) -> None:
    ws.title = "Summary"
    s = run.summary()
    ws["A1"] = "Product data validation report"
    ws["A1"].font = TITLE_FONT
    ws["A2"] = (f"Generated {datetime.now():%Y-%m-%d %H:%M} · Source: {run.source_name} · Retailer filter: "
                f"{run.retailer or 'any'} · Production dates {run.start} to {run.end} · Run time {run.seconds:.1f}s")
    ws["A2"].font = Font(name=FONT, size=10, italic=True, color="595959")

    kpis = [
        ("Total uploaded products", s["uploaded"]),
        ("Products found in DBX", s["found"]),
        ("Products not found in DBX", s["not_found"]),
        ("Ambiguous matches (flagged)", s["ambiguous"]),
        ("Average product match %", s["avg_match_pct"]),
        ("Average production completeness %", s["avg_completeness_pct"]),
        ("Matched attributes", s["matched"]),
        ("Partially matched attributes", s["partial"]),
        ("Mismatched attributes", s["mismatched"]),
        ("Missing in production (attributes)", s["missing_in_prod"]),
        ("Unavailable attributes (N/A or not in schema)", s["unavailable"]),
    ]
    # KPIs laid out horizontally (label row + value row) so the product table starts near the top
    for j, (k, v) in enumerate(kpis, 1):
        lc = ws.cell(row=4, column=j, value=k)
        lc.font = Font(name=FONT, bold=True, size=9, color="595959")
        lc.alignment = Alignment(wrap_text=True, vertical="bottom")
        vc = ws.cell(row=5, column=j, value=v)
        vc.font = Font(name=FONT, bold=True, size=14, color=BLUE)
    ws.row_dimensions[4].height = 42
    row = 7
    ws.cell(row=row, column=1, value="Products").font = SUB_FONT
    row += 1
    headers = ["Product Name", "ProductNo", "Pagetype", "DBX Status", "Overall Match %", "Completeness %",
               "Matched", "Partial", "Mismatched", "Missing in Prod", "Unavailable", "Retailer",
               "DBX Timestamp", "Matching Method", "Notes"]
    rows = []
    for o in run.outcomes:
        m, sc = o.match, o.score
        status = ("Found (ambiguous)" if m.ambiguous else "Found") if m.found else "Not found"
        rows.append([m.product.identifiers.get("name"), m.product.identifiers.get("productNo"), m.pagetype, status,
                     sc.match_pct if sc else None, sc.completeness_pct if sc else None,
                     sc.matched if sc else None, sc.partial if sc else None, sc.mismatched if sc else None,
                     sc.missing_in_prod if sc else None, sc.unavailable if sc else None,
                     m.retailer, m.dbx_timestamp, m.method, " | ".join(m.notes) or m.not_found_reason])
    first, last = _table(ws, row, headers, rows, widths={"Product Name": 42, "Notes": 60, "DBX Timestamp": 24,
                                                          "Matching Method": 22, "DBX Status": 16, "Retailer": 16},
                         wrap={"Notes"})
    _pct_colors(ws, ["E", "F"], first, last)
    ws.freeze_panes = ws.cell(row=first + 1, column=2)
    ws.column_dimensions["A"].width = 46

    row = last + 3
    if run.warnings:
        ws.cell(row=row, column=1, value="Input warnings").font = SUB_FONT
        for w in run.warnings:
            row += 1
            ws.cell(row=row, column=1, value=w).font = BODY_FONT
        row += 2
    ws.cell(row=row, column=1, value="Methodology").font = SUB_FONT
    for k, v in methodology_text(run.rules):
        row += 1
        ws.cell(row=row, column=1, value=k).font = Font(name=FONT, bold=True, size=10)
        c = ws.cell(row=row, column=2, value=v)
        c.font = BODY_FONT
        ws.merge_cells(start_row=row, start_column=2, end_row=row, end_column=10)


def _comparison(ws: Worksheet, run: RunResult) -> None:
    headers = ["Product Name", "ProductNo", "Original URL", "Pagetype", "Attribute Name", "Uploaded Value",
               "Production Value", "Match Percentage", "Match Status", "Mismatch Reason", "Missing Content",
               "Extra Content", "Production Field", "Comparison Method", "DBX Timestamp", "Matching Method", "Retailer"]
    rows = []
    for o in run.found:
        m = o.match
        ids = m.product.identifiers
        for a in o.attributes:
            rows.append([ids.get("name"), ids.get("productNo"), ids.get("originalURL"), m.pagetype, a.attribute,
                         short(a.uploaded_value, 2000), short(a.production_value, 2000), a.pct, a.status,
                         a.reason, short(a.missing, 1000) if a.missing else None,
                         short(a.extra, 1000) if a.extra else None, a.prod_path, a.comparator,
                         m.dbx_timestamp, _matching_desc(m), m.retailer])
    first, last = _table(ws, 1, headers, rows,
                         widths={"Product Name": 36, "Original URL": 30, "Attribute Name": 22, "Uploaded Value": 45,
                                 "Production Value": 45, "Mismatch Reason": 70, "Missing Content": 35,
                                 "Extra Content": 35, "Production Field": 24, "Comparison Method": 26,
                                 "DBX Timestamp": 24, "Matching Method": 30, "Match Status": 20},
                         wrap={"Uploaded Value", "Production Value", "Mismatch Reason", "Missing Content", "Extra Content"})
    _status_colors(ws, "I", ["H", "I"], first, last)
    ws.freeze_panes = "F2"


def _missing(ws: Worksheet, run: RunResult) -> None:
    headers = ["Product Name", "ProductNo", "Pagetype", "Attribute Name", "Uploaded Value", "Production Field",
               "Reason", "DBX Timestamp", "Retailer"]
    rows = []
    for o in run.found:
        m = o.match
        for a in o.attributes:
            if a.status in ("Missing in production", "Not in schema"):
                rows.append([m.product.identifiers.get("name"), m.product.identifiers.get("productNo"), m.pagetype,
                             a.attribute, short(a.uploaded_value, 1000), a.prod_path or "(no matching field)",
                             a.reason, m.dbx_timestamp, m.retailer])
    first, last = _table(ws, 1, headers, rows, widths={"Product Name": 40, "Uploaded Value": 50, "Reason": 60,
                                                       "Attribute Name": 22, "Production Field": 24, "DBX Timestamp": 24},
                         wrap={"Uploaded Value", "Reason"})
    ws.freeze_panes = "B2"


def _unmatched(ws: Worksheet, run: RunResult) -> None:
    headers = ["Product Name", "ProductNo", "Original URL", "Pagetype", "File Row", "Identifiers Attempted", "Reason"]
    rows = []
    for o in run.not_found:
        m = o.match
        ids = m.product.identifiers
        rows.append([ids.get("name"), ids.get("productNo"), ids.get("originalURL"), m.pagetype, m.product.row_index,
                     "\n".join(m.attempted) or "none", m.not_found_reason])
    _table(ws, 1, headers, rows, widths={"Product Name": 40, "Original URL": 50, "Identifiers Attempted": 55, "Reason": 70},
           wrap={"Identifiers Attempted", "Reason"})
    ws.freeze_panes = "B2"


def _flatten(obj: Any, prefix: str = "") -> dict[str, Any]:
    out: dict[str, Any] = {}
    if isinstance(obj, dict):
        for k, v in obj.items():
            p = f"{prefix}.{k}" if prefix else k
            if isinstance(v, dict):
                out.update(_flatten(v, p))
            else:
                out[p] = v
    return out


def _raw(ws: Worksheet, run: RunResult) -> None:
    flat_rows = []
    keys: dict[str, None] = {}
    for o in run.found:
        m = o.match
        flat = _flatten(m.record)
        flat_rows.append((m, flat))
        for k in flat:
            keys.setdefault(k, None)
    order = ["id", "timestamp", "metadata.retailer", "gather.url"]
    cols = [k for k in order if k in keys] + sorted(k for k in keys if k not in order)
    headers = ["Product Name (upload)", "ProductNo (upload)", "File Row", "Matching Method", "Uploaded Row (JSON)"] + cols
    rows = []
    for m, flat in flat_rows:
        ids = m.product.identifiers
        rows.append([ids.get("name"), ids.get("productNo"), m.product.row_index, _matching_desc(m),
                     json.dumps(m.product.original, ensure_ascii=False, default=str)] + [flat.get(c) for c in cols])
    _table(ws, 1, headers, rows, widths={"Product Name (upload)": 36, "Uploaded Row (JSON)": 40})
    ws.freeze_panes = "C2"


def report_bytes(run: RunResult) -> bytes:
    buf = BytesIO()
    build_report(run).save(buf)
    return buf.getvalue()


def save_report(run: RunResult, out_dir: Path, stem: str) -> Path:
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / f"{stem}_validation_{datetime.now():%Y%m%d_%H%M%S}.xlsx"
    build_report(run).save(path)
    return path
