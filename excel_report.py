"""Validation report (7 sheets):
Overall Summary, Extract Comparison, Parsed Comparison, Feedback Comparison (attribute + review level),
Missing Attributes, Unmatched Products, DBX Raw Data.
"""
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

from json_flattener import EXCEL_CELL_LIMIT, force_text, to_cell_text
from pipeline import RunResult
from scoring_engine import methodology_text
from utils.normalization import short

HEADLINE = "210235"
BLUE = "1F22B2"
FONT = "DM Sans"
SECTION_TITLES = {"extract": "Extract", "parsed": "Parsed", "feedback": "Feedback"}

FILL = {
    "Match": PatternFill("solid", fgColor="C6EFCE"),
    "Partial": PatternFill("solid", fgColor="FFEB9C"),
    "Mismatch": PatternFill("solid", fgColor="FFC7CE"),
    "Missing in production": PatternFill("solid", fgColor="FFC7CE"),
    "Extra in production": PatternFill("solid", fgColor="DDEBF7"),
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
    v = to_cell_text(v)
    if isinstance(v, str) and len(v) > EXCEL_CELL_LIMIT:
        v = v[:EXCEL_CELL_LIMIT] + " …[truncated in report; full value in the standardized input]"
    return v


def _table(ws: Worksheet, start_row: int, headers: list[str], rows: Iterable[list[Any]],
           widths: dict[str, int] | None = None, wrap: set[str] | None = None) -> tuple[int, int]:
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
            force_text(c)
            c.font = BODY_FONT
            c.border = THIN
            c.alignment = Alignment(vertical="top", wrap_text=headers[j - 1] in wrap)
    ws.auto_filter.ref = f"A{start_row}:{get_column_letter(len(headers))}{max(r, start_row)}"
    for j, h in enumerate(headers, 1):
        ws.column_dimensions[get_column_letter(j)].width = widths.get(h, min(max(len(h) + 4, 12), 40))
    return start_row, r


def _status_colors(ws: Worksheet, status_col: str, cols: list[str], first: int, last: int) -> None:
    if last <= first:
        return
    for col in cols:
        rng = f"{col}{first + 1}:{col}{last}"
        for status, fill in FILL.items():
            ws.conditional_formatting.add(rng, FormulaRule(formula=[f'${status_col}{first + 1}="{status}"'], fill=fill, stopIfTrue=True))


def _pct_colors(ws: Worksheet, cols: list[str], first: int, last: int) -> None:
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
    for sec in ("extract", "parsed", "feedback"):
        _comparison(wb.create_sheet(f"{SECTION_TITLES[sec]} Comparison"), run, sec)
    _missing(wb.create_sheet("Missing Attributes"), run)
    _unmatched(wb.create_sheet("Unmatched Products"), run)
    _raw(wb.create_sheet("DBX Raw Data"), run)
    return wb


def _summary(ws: Worksheet, run: RunResult) -> None:
    ws.title = "Overall Summary"
    s = run.summary()
    ws["A1"] = "Product data validation report"
    ws["A1"].font = TITLE_FONT
    ws["A2"] = (f"Generated {datetime.now():%Y-%m-%d %H:%M} · Source: {run.source_name} · Retailer filter: "
                f"{run.retailer or 'any'} · Production dates {run.start} to {run.end} · Run time {run.seconds:.1f}s")
    ws["A2"].font = Font(name=FONT, size=10, italic=True, color="595959")
    kpis = [
        ("Uploaded products", s["uploaded"]), ("Found in DBX", s["found"]), ("Not found", s["not_found"]),
        ("Ambiguous matches", s["ambiguous"]), ("Avg Extract %", s["avg_extract_pct"]),
        ("Avg Parsed %", s["avg_parsed_pct"]), ("Avg Feedback %", s["avg_feedback_pct"]),
        ("Avg Overall %", s["avg_match_pct"]), ("Avg Completeness %", s["avg_completeness_pct"]),
        ("Matched attrs", s["matched"]), ("Partial", s["partial"]), ("Mismatched", s["mismatched"]),
        ("Missing in prod", s["missing_in_prod"]), ("Unavailable", s["unavailable"]),
    ]
    for j, (k, v) in enumerate(kpis, 1):
        lc = ws.cell(row=4, column=j, value=k)
        lc.font = Font(name=FONT, bold=True, size=9, color="595959")
        lc.alignment = Alignment(wrap_text=True, vertical="bottom")
        vc = ws.cell(row=5, column=j, value=v)
        vc.font = Font(name=FONT, bold=True, size=14, color=BLUE)
    ws.row_dimensions[4].height = 30
    ws.cell(row=7, column=1, value="Products").font = SUB_FONT
    headers = ["Product", "ProductNo", "Pagetype", "Retailer", "Extract %", "Parsed %", "Feedback %", "Overall %",
               "Completeness %", "Matched", "Partial", "Mismatched", "Missing in Prod", "DBX Status",
               "DBX Timestamp", "Matching Method", "Notes"]
    rows = []
    for o in run.outcomes:
        m, sc = o.match, o.score
        status = ("Found (ambiguous)" if m.ambiguous else "Found") if m.found else "Not found"
        rows.append([m.product.identifiers.get("name"), m.product.identifiers.get("productNo"), m.pagetype, m.retailer,
                     sc.section_pct("extract") if sc else None, sc.section_pct("parsed") if sc else None,
                     sc.section_pct("feedback") if sc else None, sc.match_pct if sc else None,
                     sc.completeness_pct if sc else None, sc.matched if sc else None, sc.partial if sc else None,
                     sc.mismatched if sc else None, sc.missing_in_prod if sc else None, status, m.dbx_timestamp,
                     m.method, " | ".join(m.notes) or m.not_found_reason])
    first, last = _table(ws, 8, headers, rows,
                         widths={"Product": 44, "Notes": 60, "DBX Timestamp": 24, "Matching Method": 20,
                                 "DBX Status": 16, "Retailer": 16, "ProductNo": 14}, wrap={"Notes"})
    _pct_colors(ws, ["E", "F", "G", "H", "I"], first, last)
    ws.freeze_panes = ws.cell(row=first + 1, column=2)
    row = last + 3
    if run.warnings:
        ws.cell(row=row, column=1, value="Input warnings").font = SUB_FONT
        for w in run.warnings:
            row += 1
            ws.cell(row=row, column=1, value=_cell(w)).font = BODY_FONT
        row += 2
    ws.cell(row=row, column=1, value="Methodology").font = SUB_FONT
    for k, v in methodology_text(run.rules):
        row += 1
        ws.cell(row=row, column=1, value=k).font = Font(name=FONT, bold=True, size=10)
        c = ws.cell(row=row, column=2, value=v)
        c.font = BODY_FONT
        ws.merge_cells(start_row=row, start_column=2, end_row=row, end_column=12)


COMPARISON_HEADERS = ["Product", "ProductNo", "Original URL", "Pagetype", "Level", "Attribute", "Review",
                      "Uploaded Value", "Production Value", "Match %", "Status", "Mismatch Reason",
                      "Missing Content", "Extra Content", "Production Field", "Comparison Method",
                      "DBX Timestamp", "Matching Method", "Retailer"]


def _comparison(ws: Worksheet, run: RunResult, section: str) -> None:
    headers = [h for h in COMPARISON_HEADERS if section == "feedback" or h != "Review"]
    rows = []
    for o in run.found:
        m = o.match
        ids = m.product.identifiers
        base = [ids.get("name"), ids.get("productNo"), ids.get("originalURL"), m.pagetype]
        tail = [m.dbx_timestamp, _matching_desc(m), m.retailer]
        for a in o.attributes:
            if a.section != section:
                continue
            rec = base + ["Attribute", a.field] + ([None] if section == "feedback" else []) + [
                short(a.uploaded_value, 2000), short(a.production_value, 2000), a.pct, a.status, a.reason,
                short(a.missing, 1000) if a.missing else None, short(a.extra, 1000) if a.extra else None,
                a.prod_path, a.comparator] + tail
            rows.append(rec)
            if section == "feedback":
                for d in a.details:
                    rows.append(base + ["Review", a.field, d["review"], d["uploaded"], d["production"], d["score"],
                                        d["status"], d["differences"] or "All compared fields equal.", None, None,
                                        a.prod_path, d["pairing"]] + tail)
    status_col = get_column_letter(headers.index("Status") + 1)
    pct_col = get_column_letter(headers.index("Match %") + 1)
    first, last = _table(ws, 1, headers, rows,
                         widths={"Product": 34, "Original URL": 28, "Attribute": 22, "Uploaded Value": 45,
                                 "Production Value": 45, "Mismatch Reason": 70, "Missing Content": 32,
                                 "Extra Content": 32, "Production Field": 24, "Comparison Method": 26,
                                 "DBX Timestamp": 24, "Matching Method": 28, "Status": 20, "Review": 16, "Level": 10},
                         wrap={"Uploaded Value", "Production Value", "Mismatch Reason", "Missing Content", "Extra Content"})
    _status_colors(ws, status_col, [pct_col, status_col], first, last)
    ws.freeze_panes = ws.cell(row=2, column=headers.index("Attribute") + 2)


def _missing(ws: Worksheet, run: RunResult) -> None:
    headers = ["Product", "ProductNo", "Pagetype", "Section", "Attribute", "Uploaded Value", "Production Field",
               "Reason", "DBX Timestamp", "Retailer"]
    rows = []
    for o in run.found:
        m = o.match
        for a in o.attributes:
            if a.status in ("Missing in production", "Not in schema"):
                rows.append([m.product.identifiers.get("name"), m.product.identifiers.get("productNo"), m.pagetype,
                             SECTION_TITLES.get(a.section, a.section), a.field, short(a.uploaded_value, 1000),
                             a.prod_path or "(no matching field)", a.reason, m.dbx_timestamp, m.retailer])
    _table(ws, 1, headers, rows, widths={"Product": 40, "Uploaded Value": 50, "Reason": 60, "Attribute": 22,
                                         "Production Field": 24, "DBX Timestamp": 24}, wrap={"Uploaded Value", "Reason"})
    ws.freeze_panes = "B2"


def _unmatched(ws: Worksheet, run: RunResult) -> None:
    headers = ["Product", "ProductNo", "Original URL", "Pagetype", "File Row", "Identifiers Attempted", "Reason"]
    rows = []
    for o in run.not_found:
        m = o.match
        ids = m.product.identifiers
        rows.append([ids.get("name"), ids.get("productNo"), ids.get("originalURL"), m.pagetype, m.product.row_index,
                     "\n".join(m.attempted) or "none", m.not_found_reason])
    _table(ws, 1, headers, rows, widths={"Product": 40, "Original URL": 50, "Identifiers Attempted": 55, "Reason": 70},
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
    flat_rows, keys = [], {}
    for o in run.found:
        flat = _flatten(o.match.record)
        flat_rows.append((o.match, flat))
        for k in flat:
            keys.setdefault(k, None)
    order = ["id", "timestamp", "metadata.retailer", "gather.url"]
    cols = [k for k in order if k in keys] + sorted(k for k in keys if k not in order)
    headers = ["Product (upload)", "ProductNo (upload)", "File Row", "Matching Method", "Uploaded Row (JSON)"] + cols
    rows = []
    for m, flat in flat_rows:
        ids = m.product.identifiers
        rows.append([ids.get("name"), ids.get("productNo"), m.product.row_index, _matching_desc(m),
                     json.dumps(m.product.original, ensure_ascii=False, default=str)] + [flat.get(c) for c in cols])
    _table(ws, 1, headers, rows, widths={"Product (upload)": 36, "Uploaded Row (JSON)": 40})
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
