"""Streamlit UI: upload a file, validate against production, inspect products, download the Excel report.

Run:  streamlit run app.py
"""
from __future__ import annotations

from datetime import date, timedelta
from pathlib import Path

import pandas as pd
import streamlit as st

from config import ROOT, DBXSettings, Rules
from dbx_connector import FixtureSource, LiveSource, SourceError
from excel_report import report_bytes
from file_processor import InputFileError, column_profile, parse_file
from mock_data import build_mock
from pipeline import RunResult, run_validation
from utils.logger import redact
from utils.normalization import short

st.set_page_config(page_title="Product data validation", page_icon="✅", layout="wide")

FIXTURE_DIR = ROOT / "fixtures"
STATUS_COLORS = {"Match": "#C6EFCE", "Partial": "#FFEB9C", "Mismatch": "#FFC7CE",
                 "Missing in production": "#FFC7CE", "N/A": "#E7E6E6", "Not in schema": "#E7E6E6"}

st.markdown(
    """<style>
    h1, h2, h3 { color: #210235; }
    div[data-testid="stMetricValue"] { color: #1F22B2; }
    </style>""",
    unsafe_allow_html=True,
)

settings = DBXSettings()
rules = Rules.load()

# ------------------------------------------------------------------ sidebar
with st.sidebar:
    st.header("Settings")
    mode = st.radio("Production data source", ["Live (Databricks)", "Fixture (saved records)", "Mock (synthetic)"],
                    index=0 if settings.is_configured() else 1,
                    help="Fixture and mock modes let you run the full workflow without Databricks access.")
    fixture_path = None
    if mode.startswith("Fixture"):
        fixtures = sorted(FIXTURE_DIR.glob("*.json"))
        if fixtures:
            fixture_path = st.selectbox("Fixture file", fixtures, format_func=lambda p: p.name)
        else:
            st.warning("No fixture files in fixtures/.")

    retailer = st.text_input("Retailer filter (SQL LIKE)", value="", placeholder="e.g. Flink% or Flink-APP-DE",
                             help="Matches metadata.retailer. Leave empty for all retailers.")
    pagetype_choice = st.selectbox("Page type", ["From file (Pagetype column)", "Detail", "Search", "Both"],
                                   help="Detail → sku_detail_v2, Search → sku_search_v2, Both → try detail then search.")
    today = date.today()
    dr = st.date_input("Production date range", value=(today - timedelta(days=30), today), max_value=today)
    start, end = (dr if isinstance(dr, tuple) and len(dr) == 2 else (today - timedelta(days=30), today))

    st.divider()
    st.subheader("Databricks connection")
    if mode.startswith("Live"):
        if st.button("Check connection"):
            with st.spinner("Connecting…"):
                ok, msg = LiveSource(settings).status()
            st.session_state["dbx_status"] = (ok, msg)
        ok_msg = st.session_state.get("dbx_status")
        if ok_msg:
            (st.success if ok_msg[0] else st.error)(ok_msg[1])
        elif settings.is_configured():
            st.info("Configured. Click Check connection to test.")
        else:
            st.warning("Not configured. Set DBX_HOST, DBX_HTTP_PATH (or DBX_WAREHOUSE_ID) and DBX_TOKEN in .env.")
        st.caption(f"Detail table: `{settings.table_for('detail')}`  \nSearch table: `{settings.table_for('search')}`")
    else:
        st.caption("Not used in this mode.")

pagetype_override = None if pagetype_choice.startswith("From") else pagetype_choice.lower()

# ------------------------------------------------------------------ main
st.title("Product data validation")
st.caption("Compare extracted product data with production records in Databricks, attribute by attribute.")

upload = st.file_uploader("Upload a CSV or Excel file", type=["csv", "xlsx", "xls"])
if not upload:
    st.info("Upload a file to start. Identifier columns (productNo, originalURL, name, Pagetype) are detected automatically.")
    st.stop()

try:
    parsed = parse_file(upload.getvalue(), filename=upload.name, pagetype_override=pagetype_override)
except InputFileError as exc:
    st.error(str(exc))
    st.stop()

ids = parsed.identifier_columns
c1, c2, c3 = st.columns(3)
c1.metric("Rows", len(parsed.products))
c2.metric("Columns", len(parsed.columns))
pts = pd.Series([p.identifiers.get("Pagetype") or "—" for p in parsed.products]).value_counts().to_dict()
c3.metric("Page types", ", ".join(f"{k}: {v}" for k, v in pts.items()))
st.write("Identifier columns: " + " · ".join(f"**{k}** → `{v}`" if v else f"**{k}** → not found" for k, v in ids.items()))
for w in parsed.warnings:
    st.warning(w)
if not ids.get("Pagetype") and not pagetype_override:
    st.error("The file has no Pagetype column. Choose a page type in the sidebar.")
    st.stop()
with st.expander("Column preview"):
    st.dataframe(column_profile(parsed), use_container_width=True, hide_index=True)

if st.button("Start validation", type="primary"):
    bar = st.progress(0.0, text="Starting…")
    try:
        if mode.startswith("Live"):
            source = LiveSource(settings)
        elif mode.startswith("Fixture"):
            if not fixture_path:
                st.error("Choose a fixture file.")
                st.stop()
            source = FixtureSource(fixture_path)
        else:
            base = (retailer or "").replace("%", "").replace("_", "").strip("-") or "Mock"
            source, _ = build_mock(parsed, settings, rules, retailer=f"{base}-MOCK-DE", end=end)
        run = run_validation(parsed, source, settings, rules, start, end, retailer,
                             pagetype_default=pagetype_override,
                             progress=lambda f, m: bar.progress(min(max(f, 0.0), 1.0), text=m))
        st.session_state["run"] = run
        st.session_state["report"] = report_bytes(run)
        st.session_state["report_name"] = f"{Path(upload.name).stem}_validation.xlsx"
        bar.progress(1.0, text=f"Done in {run.seconds:.1f}s")
    except SourceError as exc:
        bar.empty()
        st.error(str(exc))
        st.stop()
    except Exception as exc:  # show a safe message, keep details in the log
        bar.empty()
        st.error(f"Validation failed: {redact(exc)}")
        st.stop()

run: RunResult | None = st.session_state.get("run")
if not run:
    st.stop()

# ------------------------------------------------------------------ results
st.header("Summary")
s = run.summary()
m = st.columns(6)
m[0].metric("Uploaded", s["uploaded"])
m[1].metric("Found in DBX", s["found"])
m[2].metric("Not found", s["not_found"])
m[3].metric("Ambiguous", s["ambiguous"])
m[4].metric("Avg match %", f"{s['avg_match_pct']}%" if s["avg_match_pct"] is not None else "—")
m[5].metric("Avg completeness %", f"{s['avg_completeness_pct']}%" if s["avg_completeness_pct"] is not None else "—")
m = st.columns(5)
m[0].metric("Matched attrs", s["matched"])
m[1].metric("Partial", s["partial"])
m[2].metric("Mismatched", s["mismatched"])
m[3].metric("Missing in prod", s["missing_in_prod"])
m[4].metric("Unavailable", s["unavailable"])

st.download_button("Download Excel report", data=st.session_state["report"], file_name=st.session_state["report_name"],
                   mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", type="primary")

rows = []
for i, o in enumerate(run.outcomes):
    mt, sc = o.match, o.score
    rows.append({
        "#": i, "Product": mt.product.label, "ProductNo": mt.product.identifiers.get("productNo"),
        "Pagetype": mt.pagetype,
        "DBX status": ("Found (ambiguous)" if mt.ambiguous else "Found") if mt.found else "Not found",
        "Match %": sc.match_pct if sc else None, "Completeness %": sc.completeness_pct if sc else None,
        "Matched": sc.matched if sc else None, "Partial": sc.partial if sc else None,
        "Mismatched": sc.mismatched if sc else None, "Missing": sc.missing_in_prod if sc else None,
        "Method": mt.method, "Notes": " | ".join(mt.notes) or (mt.not_found_reason or ""),
    })
df = pd.DataFrame(rows)

st.header("Products")
f1, f2, f3 = st.columns([2, 1, 1])
q = f1.text_input("Search by name or productNo", "")
status_filter = f2.multiselect("DBX status", sorted(df["DBX status"].unique()), default=[])
max_pct = f3.slider("Show match % up to", 0, 100, 100)
view = df.copy()
if q:
    ql = q.lower()
    view = view[view["Product"].str.lower().str.contains(ql, na=False) | view["ProductNo"].astype(str).str.contains(ql, na=False)]
if status_filter:
    view = view[view["DBX status"].isin(status_filter)]
view = view[(view["Match %"].isna()) | (view["Match %"] <= max_pct)]
st.dataframe(
    view.drop(columns=["#"]), use_container_width=True, hide_index=True,
    column_config={
        "Match %": st.column_config.ProgressColumn("Match %", min_value=0, max_value=100, format="%.1f%%"),
        "Completeness %": st.column_config.ProgressColumn("Completeness %", min_value=0, max_value=100, format="%.1f%%"),
    },
)

st.header("Product detail")
options = view["#"].tolist()
if not options:
    st.info("No products match the filters.")
    st.stop()
sel = st.selectbox("Product", options, format_func=lambda i: f"{df.loc[i, 'Product']} ({df.loc[i, 'ProductNo']}) · {df.loc[i, 'DBX status']}")
o = run.outcomes[sel]
mt = o.match
if not mt.found:
    st.error(mt.not_found_reason)
    st.write("Identifiers tried:", mt.attempted or "none")
    st.stop()

d = st.columns(4)
d[0].metric("Match %", f"{o.score.match_pct}%" if o.score.match_pct is not None else "—")
d[1].metric("Completeness %", f"{o.score.completeness_pct}%" if o.score.completeness_pct is not None else "—")
d[2].metric("Retailer", mt.retailer or "—")
d[3].metric("DBX timestamp", (mt.dbx_timestamp or "—")[:19])
st.caption(f"Matched by **{mt.method}** = `{mt.identifier_used}` in `{mt.table}` · {mt.n_records} crawl(s) in range")
for n in mt.notes:
    st.warning(n)

show_na = st.toggle("Show N/A and not-in-schema attributes", value=False)
arows = []
for a in o.attributes:
    if not show_na and a.status in ("N/A", "Not in schema"):
        continue
    arows.append({"Attribute": a.attribute, "Status": a.status, "Match %": a.pct,
                  "Uploaded": short(a.uploaded_value, 400), "Production": short(a.production_value, 400),
                  "Reason": a.reason, "Production field": a.prod_path, "Method": a.comparator})
adf = pd.DataFrame(arows)
if not adf.empty:
    styled = adf.style.apply(lambda col: [f"background-color: {STATUS_COLORS.get(v, '')}" for v in col], subset=["Status"])
    st.dataframe(styled, use_container_width=True, hide_index=True,
                 column_config={"Match %": st.column_config.NumberColumn(format="%.1f%%"),
                                "Reason": st.column_config.TextColumn(width="large"),
                                "Uploaded": st.column_config.TextColumn(width="medium"),
                                "Production": st.column_config.TextColumn(width="medium")})
with st.expander("Raw production record"):
    st.json(mt.record, expanded=False)
with st.expander("Original uploaded row"):
    st.json({k: v for k, v in mt.product.original.items() if str(v).strip()}, expanded=False)
