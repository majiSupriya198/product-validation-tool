# Product data validation tool

Compares extracted product data (CSV/XLSX/XLS) with production records in Databricks
(`sku_detail_v2` / `sku_search_v2`), attribute by attribute, and explains every difference.
The output is an Excel report and an interactive Streamlit view.

It only reads from Databricks: every statement is a parameterized `SELECT`, and anything else is refused.

## Setup

```bash
cd product-validation-tool
python3.11 -m venv .venv
.venv/bin/pip install -r requirements.txt
cp .env.example .env    # then fill in DBX_* values
```

### Databricks access (`.env`)

| Variable | What it is |
|---|---|
| `DBX_HOST` | Workspace host, without `https://` |
| `DBX_HTTP_PATH` or `DBX_WAREHOUSE_ID` | SQL warehouse HTTP path, or just the warehouse id |
| `DBX_TOKEN` | Personal access token. Leave empty and set `DBX_AUTH_TYPE=databricks-oauth` to sign in through the browser |
| `DBX_CATALOG`, `DBX_SCHEMA` | Default `common_catalog` / `gather_data` |
| `DBX_DETAIL_TABLE`, `DBX_SEARCH_TABLE` | Default `sku_detail_v2` / `sku_search_v2` (a bare name or `catalog.schema.table`) |
| `DBX_QUERY_TIMEOUT` | Seconds, default 300 |

Credentials come only from the environment. They're never logged: a redaction filter removes
tokens, hosts and warehouse paths from log lines and error messages.

## Run

**UI**

```bash
.venv/bin/streamlit run app.py
```

Upload a file, then pick the data source (Live / Fixture / Mock), the retailer filter (SQL `LIKE`,
e.g. `Flink%`), the page type and the production date range. Click **Start validation**. You get
summary tiles, a filterable product table, a per-product attribute drill-down with reasons, and
**Download Excel report**.

**CLI**

```bash
.venv/bin/python cli.py ~/Downloads/Flink_PDP.xlsx --mode live --retailer 'Flink%' --start 2026-10-05 --end 2026-11-04
.venv/bin/python cli.py ~/Downloads/Flink_PDP.xlsx --mode fixture --retailer 'Flink%' --start 2026-10-05 --end 2026-11-04
.venv/bin/python cli.py any_file.csv --mode mock
```

Reports are written to `output/validation_reports/`. Logs go to `output/logs/validation.log`.

### Data sources

- **live**: queries Databricks.
- **fixture**: uses saved production records (JSON). `fixtures/flink_detail_sample.json` holds the
  real latest two crawls for every product in `Flink_PDP.xlsx`, pulled read-only from
  `sku_detail_v2` (2026-10-05 to 2026-11-04, `retailer LIKE 'Flink%'`), plus last-seen dates for
  the unmatched products. The full workflow runs without a token. Use
  `--start 2026-10-05 --end 2026-11-04` with it. The file contains production data, so keep it
  internal.
- **mock**: synthetic records built from the upload, with known changes (dropped ingredients,
  changed price, missing nutrition entry, extra image, shortened description, missing
  manufacturer, unmatched rows). Use it to check the scoring.

## Tests

```bash
.venv/bin/python -m pytest -q
```

## Configuration (`config/validation.yaml`)

- `aliases`: uploaded column → production path, e.g. `Storage_and_Usage: extract.storage`,
  `coments: feedback.comments`.
- `skip_attributes`: columns used only for matching.
- `comparators`: comparator per attribute pattern (exact, name, numeric, text, list, images,
  keyvalue, nested, boolean).
- `numeric`, `text`, `scoring`, `matching`: tolerances, thresholds, weights, chunk size and
  last-seen lookback.

`matching.chunk_size` (default 500) sets how many identifiers go into one query, one bound
parameter each. It hasn't been tested at 500 against the live warehouse yet. If a query fails on
parameter count or size, lower it.

`config/schema_snapshot.json` lists every field path in both production tables. It's used when
the live schema can't be read, and in fixture/mock mode.

See [METHODOLOGY.md](METHODOLOGY.md) for how matching and scoring work.

## Layout

```
app.py               Streamlit UI
cli.py               command-line runner
pipeline.py          orchestration (match → compare → score)
config.py            env settings + validation rules
dbx_connector.py     Live / Fixture / Mock sources
file_processor.py    file reading, cleaning, identifier detection
product_matcher.py   batched multi-pass matching, latest record, ambiguity flags
field_resolver.py    uploaded column → production field (extract → parsed → feedback)
comparison_engine.py comparator dispatch, attribute status
comparators/         exact, numeric, text, list/images, key/value, nested
scoring_engine.py    product match %, completeness %
mismatch_analyzer.py reasons built from comparison evidence
excel_report.py      5-sheet formatted workbook
mock_data.py         synthetic records for testing
utils/               normalization, query builder, logger
```
