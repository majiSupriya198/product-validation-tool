"""Streamlit UI: upload → detect → transform (JSON-section CSVs) → preview → configure → validate → dashboard.

Run:  streamlit run app.py
"""
from __future__ import annotations

import hashlib
from datetime import date
from pathlib import Path

import pandas as pd
import streamlit as st

from config import ROOT, TRANSFORM_DIR, DBXSettings, Rules
from csv_transformer import SHEETS, TransformError, detect, save_transformed, transform
from dbx_connector import FixtureSource, LiveSource, SourceError
from excel_report import report_bytes
from file_processor import InputFileError, column_profile, parse_file, parse_standardized
from mock_data import build_mock
from pipeline import RunResult, default_date_range, run_validation
from utils.logger import redact
from utils.normalization import short

st.set_page_config(page_title="Product data validation", page_icon="✅", layout="wide")

FIXTURE_DIR = ROOT / "fixtures"
XLSX_MIME = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
STATUS_COLORS = {"Match": "#C6EFCE", "Partial": "#FFEB9C", "Mismatch": "#FFC7CE", "Missing in production": "#FFC7CE",
                 "Extra in production": "#DDEBF7", "N/A": "#E7E6E6", "Not in schema": "#E7E6E6"}
SECTIONS = [("extract", "Extract"), ("parsed", "Parsed"), ("feedback", "Feedback")]

st.markdown("""<style>
h1, h2, h3 { color: #210235; }
div[data-testid="stMetricValue"] { color: #1F22B2; }
</style>""", unsafe_allow_html=True)

settings = DBXSettings()
rules = Rules.load()


def pct(v) -> str:
    return f"{v}%" if v is not None else "—"


def color_status(df: pd.DataFrame, col: str = "Status"):
    if df.empty or col not in df:
        return df
    return df.style.apply(lambda c: [f"background-color: {STATUS_COLORS.get(v, '')}" for v in c], subset=[col])


# ------------------------------------------------------------------ 1. file upload
st.title("Product data validation")
st.caption("Compare extracted product data with production records in Databricks: Extract, Parsed and Feedback, "
           "attribute by attribute.")

st.header("1. File upload")
upload = st.file_uploader("Upload a CSV or Excel file", type=["csv", "xlsx", "xls"])
if not upload:
    st.info("Supported: flat CSV/Excel (one column per attribute), CSVs whose extract / parsed / feedback / "
            "metadata / gather columns hold JSON, and standardized workbooks produced by this tool.")
    st.stop()

data = upload.getvalue()
file_key = hashlib.sha1(data).hexdigest()[:12]
if st.session_state.get("file_key") != file_key:
    for k in ("run", "report", "transform", "parsed", "transformed_path"):
        st.session_state.pop(k, None)
    st.session_state["file_key"] = file_key

try:
    det = detect(data, upload.name)
except (TransformError, InputFileError) as exc:
    st.error(str(exc))
    st.stop()

c = st.columns(4)
c[0].metric("Detected format", {"flat": "Flat", "json_sections": "JSON sections", "standardized": "Standardized"}[det.format])
c[1].metric("Records", det.records)
c[2].metric("JSON sections", ", ".join(det.json_sections) if det.json_sections else "—")
transform_status = c[3].empty()
if det.malformed:
    st.warning("Some JSON cells couldn't be parsed: " + ", ".join(f"{k}: {v}" for k, v in det.malformed.items())
               + ". Their raw text is kept in the standardized workbook.")

# ------------------------------------------------------------------ 2. transform + preview
result = None
if det.format == "json_sections":
    if "transform" not in st.session_state:
        with st.spinner("Transforming CSV into the standardized workbook…"):
            try:
                st.session_state["transform"] = transform(data, upload.name)
                st.session_state["transformed_path"] = save_transformed(
                    st.session_state["transform"], TRANSFORM_DIR, Path(upload.name).stem)
            except TransformError as exc:
                transform_status.metric("Transformation", "Failed")
                st.error(str(exc))
                st.stop()
    result = st.session_state["transform"]
    transform_status.metric("Transformation", "Done")
else:
    transform_status.metric("Transformation", "Not needed")

if "parsed" not in st.session_state:
    try:
        if result is not None:
            parsed = parse_standardized(result.workbook_bytes)
            parsed.warnings = result.warnings + parsed.warnings
        else:
            parsed = parse_file(data, filename=upload.name)
        st.session_state["parsed"] = parsed
    except InputFileError as exc:
        st.error(str(exc))
        st.stop()
parsed = st.session_state["parsed"]
for w in parsed.warnings:
    st.warning(w)

if result is not None:
    st.header("2. Transformation preview")
    s = result.stats
    st.caption(f"{s['records']} records · {s['extract_fields']} extract fields · {s['parsed_fields']} parsed fields · "
               f"{s['feedback_fields']} feedback fields · {s['reviews']} reviews · "
               f"{s['malformed_json_cells']} malformed JSON cells")
    tabs = st.tabs([SHEETS[k] for k in ("product", "parsed", "feedback", "reviews", "meta", "original")])
    for tab, key in zip(tabs, ("product", "parsed", "feedback", "reviews", "meta", "original")):
        with tab:
            df = result.sheets[SHEETS[key]]
            st.caption(f"{len(df)} rows × {len(df.columns)} columns")
            st.dataframe(df.head(200).astype(str).map(lambda v: short(v, 300)), use_container_width=True, hide_index=True)
    st.download_button("Download standardized Excel", data=result.workbook_bytes, mime=XLSX_MIME,
                       file_name=f"{Path(upload.name).stem}_standardized.xlsx")
    if st.session_state.get("transformed_path"):
        st.caption(f"Also saved to `{st.session_state['transformed_path'].relative_to(ROOT)}`")
else:
    with st.expander("Column preview"):
        st.dataframe(column_profile(parsed), use_container_width=True, hide_index=True)

# ------------------------------------------------------------------ 3. configuration
st.header("3. Validation configuration")
d_start, d_end = default_date_range(parsed)
uploaded_retailers = parsed.uploaded_retailers
cfg = st.columns([1.2, 1, 1, 1.4])
mode = cfg[0].radio("Production data source", ["Live (Databricks)", "Fixture (saved records)", "Mock (synthetic)"],
                    index=0 if settings.is_configured() else 2, key=f"mode_{file_key}",
                    help="Fixture and mock let you run the whole workflow without Databricks access.")
fixture_path = None
if mode.startswith("Fixture"):
    fixtures = sorted(FIXTURE_DIR.glob("*.json"))
    fixture_path = cfg[0].selectbox("Fixture file", fixtures, format_func=lambda p: p.name) if fixtures else None
retailer = cfg[1].text_input("Retailer (SQL LIKE)", value=uploaded_retailers[0] if len(uploaded_retailers) == 1 else "",
                             placeholder="e.g. Walmart-US or Flink%", key=f"ret_{file_key}",
                             help="Matches metadata.retailer. Prefilled from the uploaded metadata when there is one retailer.")
pt_choice = cfg[2].selectbox("Page type", ["From file", "Detail", "Search", "Both"], key=f"pt_{file_key}",
                             help="Detail → sku_detail_v2, Search → sku_search_v2, Both → detail then search. "
                                  "'Product' / 'PDP' in the file count as Detail.")
dr = cfg[3].date_input("Production date range", value=(d_start, d_end), max_value=date.today(), key=f"dr_{file_key}",
                       help="Defaults to ±3 days around the uploaded crawl timestamps, so the closest production crawl is used.")
start, end = dr if isinstance(dr, tuple) and len(dr) == 2 else (d_start, d_end)
pagetype_override = None if pt_choice == "From file" else pt_choice.lower()
pts = pd.Series([p.identifiers.get("Pagetype") or "—" for p in parsed.products]).value_counts().to_dict()
st.caption("Page types in file: " + ", ".join(f"{k}: {v}" for k, v in pts.items()))

with st.container(border=True):
    if mode.startswith("Live"):
        cc = st.columns([1, 4])
        if cc[0].button("Check DBX connection"):
            with st.spinner("Connecting…"):
                st.session_state["dbx_status"] = LiveSource(settings).status()
        ok_msg = st.session_state.get("dbx_status")
        if ok_msg:
            (cc[1].success if ok_msg[0] else cc[1].error)(ok_msg[1])
        elif settings.is_configured():
            cc[1].info("Configured. Click Check DBX connection to test.")
        else:
            cc[1].warning("Not configured. Set DBX_HOST, DBX_HTTP_PATH and DBX_TOKEN in .env.")
    else:
        st.caption(f"DBX connection not used in {mode.split(' ')[0].lower()} mode.")

needs_pt = any((p.identifiers.get("Pagetype") or "") not in ("detail", "search") for p in parsed.products)
if needs_pt and not pagetype_override:
    st.warning("Some rows have no recognizable page type; choose Detail, Search or Both.")

if st.button("Start validation", type="primary", disabled=needs_pt and not pagetype_override):
    bar = st.progress(0.0, text="Starting…")
    try:
        run_parsed = parse_standardized(result.workbook_bytes, pagetype_override) if result is not None else (
            parse_file(data, filename=upload.name, pagetype_override=pagetype_override) if pagetype_override else parsed)
        if result is not None:
            run_parsed.warnings = result.warnings + run_parsed.warnings
        if mode.startswith("Live"):
            source = LiveSource(settings)
        elif mode.startswith("Fixture"):
            if not fixture_path:
                st.error("Choose a fixture file.")
                st.stop()
            source = FixtureSource(fixture_path)
        else:
            base = (retailer or "").replace("%", "").replace("_", "").strip("-") or "Mock"
            source, _ = build_mock(run_parsed, settings, rules, retailer=f"{base}-MOCK", end=end)
        run = run_validation(run_parsed, source, settings, rules, start, end, retailer,
                             pagetype_default=pagetype_override,
                             progress=lambda f, m: bar.progress(min(max(f, 0.0), 1.0), text=m))
        st.session_state["run"] = run
        st.session_state["report"] = report_bytes(run)
        bar.progress(1.0, text=f"Done in {run.seconds:.1f}s")
    except SourceError as exc:
        bar.empty()
        st.error(str(exc))
        st.stop()
    except Exception as exc:  # safe message; details are in the log
        bar.empty()
        st.error(f"Validation failed: {redact(exc)}")
        st.stop()

run: RunResult | None = st.session_state.get("run")
if not run:
    st.stop()

# ------------------------------------------------------------------ 4. dashboard
st.header("4. Validation dashboard")
s = run.summary()
m = st.columns(6)
m[0].metric("Extract accuracy", pct(s["avg_extract_pct"]))
m[1].metric("Parsed accuracy", pct(s["avg_parsed_pct"]))
m[2].metric("Feedback accuracy", pct(s["avg_feedback_pct"]))
m[3].metric("Overall accuracy", pct(s["avg_match_pct"]))
m[4].metric("Production completeness", pct(s["avg_completeness_pct"]))
m[5].metric("Products not found", f"{s['not_found']} of {s['uploaded']}")
st.caption(f"{s['found']} found ({s['ambiguous']} ambiguous) · attributes: {s['matched']} matched, {s['partial']} partial, "
           f"{s['mismatched']} mismatched, {s['missing_in_prod']} missing in production, {s['unavailable']} unavailable · "
           f"production dates {run.start} to {run.end} · {run.source_name}")
st.download_button("Download Excel validation report", data=st.session_state["report"], mime=XLSX_MIME, type="primary",
                   file_name=f"{Path(upload.name).stem}_validation.xlsx")

rows = []
for i, o in enumerate(run.outcomes):
    mt, sc = o.match, o.score
    rows.append({"#": i, "Product": mt.product.label, "ProductNo": mt.product.identifiers.get("productNo"),
                 "DBX status": ("Found (ambiguous)" if mt.ambiguous else "Found") if mt.found else "Not found",
                 "Extract %": sc.section_pct("extract") if sc else None, "Parsed %": sc.section_pct("parsed") if sc else None,
                 "Feedback %": sc.section_pct("feedback") if sc else None, "Overall %": sc.match_pct if sc else None,
                 "Completeness %": sc.completeness_pct if sc else None,
                 "Notes": " | ".join(mt.notes) or (mt.not_found_reason or "")})
df = pd.DataFrame(rows)
f1, f2, f3 = st.columns([2, 1, 1])
q = f1.text_input("Search by name or productNo", "")
status_filter = f2.multiselect("DBX status", sorted(df["DBX status"].unique()))
max_pct = f3.slider("Show overall % up to", 0, 100, 100)
view = df.copy()
if q:
    ql = q.lower()
    view = view[view["Product"].str.lower().str.contains(ql, na=False) | view["ProductNo"].astype(str).str.contains(ql, na=False)]
if status_filter:
    view = view[view["DBX status"].isin(status_filter)]
view = view[view["Overall %"].isna() | (view["Overall %"] <= max_pct)]
pcol = lambda name: st.column_config.ProgressColumn(name, min_value=0, max_value=100, format="%.1f%%")
st.dataframe(view.drop(columns=["#"]), use_container_width=True, hide_index=True,
             column_config={k: pcol(k) for k in ("Extract %", "Parsed %", "Feedback %", "Overall %", "Completeness %")})

st.subheader("Product detail")
options = view["#"].tolist()
if not options:
    st.info("No products match the filters.")
    st.stop()
sel = st.selectbox("Product", options, format_func=lambda i: f"{df.loc[i, 'Product']} ({df.loc[i, 'ProductNo']}) · {df.loc[i, 'DBX status']}")
o = run.outcomes[sel]
mt = o.match
if not mt.found:
    st.error(mt.not_found_reason)
    st.stop()
d = st.columns(6)
d[0].metric("Extract", pct(o.score.section_pct("extract")))
d[1].metric("Parsed", pct(o.score.section_pct("parsed")))
d[2].metric("Feedback", pct(o.score.section_pct("feedback")))
d[3].metric("Overall", pct(o.score.match_pct))
d[4].metric("Completeness", pct(o.score.completeness_pct))
d[5].metric("DBX timestamp", (mt.dbx_timestamp or "—")[:16])
st.caption(f"Matched by **{mt.method}** = `{mt.identifier_used}` in `{mt.table}` · retailer {mt.retailer} · "
           f"{mt.n_records} crawl(s) in range")
for n in mt.notes:
    st.warning(n)
show_na = st.toggle("Show N/A and not-in-schema attributes", value=False)
tabs = st.tabs([t for _, t in SECTIONS])
for tab, (sec, title) in zip(tabs, SECTIONS):
    with tab:
        attrs = [a for a in o.attributes if a.section == sec and (show_na or a.status not in ("N/A", "Not in schema"))]
        if not attrs:
            st.info(f"No comparable {title.lower()} attributes for this product.")
            continue
        adf = pd.DataFrame([{"Attribute": a.field, "Status": a.status, "Match %": a.pct,
                             "Uploaded": short(a.uploaded_value, 300), "Production": short(a.production_value, 300),
                             "Reason": a.reason, "Production field": a.prod_path, "Method": a.comparator} for a in attrs])
        st.dataframe(color_status(adf), use_container_width=True, hide_index=True,
                     column_config={"Match %": st.column_config.NumberColumn(format="%.1f%%"),
                                    "Reason": st.column_config.TextColumn(width="large")})
        for a in attrs:
            if a.details:
                st.markdown(f"**Review-level comparison: `{a.field}`** ({a.pct}%)")
                rdf = pd.DataFrame(a.details).rename(columns=str.capitalize)
                st.dataframe(color_status(rdf), use_container_width=True, hide_index=True,
                             column_config={"Score": st.column_config.NumberColumn(format="%.1f%%")})
with st.expander("Raw production record"):
    st.json(mt.record, expanded=False)
with st.expander("Original uploaded row"):
    st.json({k: v for k, v in mt.product.original.items() if str(v).strip()}, expanded=False)
