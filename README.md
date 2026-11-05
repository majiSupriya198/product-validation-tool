# Product data validation tool

Compares extracted product data with production records in Databricks (`sku_detail_v2` /
`sku_search_v2`). It scores three sections separately, attribute by attribute, and explains every
difference:

- **Extract**: product content such as name, description, ingredients, images and bullet points
- **Parsed**: normalized values such as price, currency, quantities and promotions
- **Feedback**: ratings, counts and each individual review

The output is an Excel report and an interactive Streamlit view. The tool only reads from
Databricks: every statement is a parameterized `SELECT`, and anything else is refused.

## Supported inputs

The format is detected automatically.

| Format | Example | What happens |
|---|---|---|
| Flat CSV / XLSX / XLS | `Flink_PDP.xlsx` (one column per attribute) | Validated directly |
| JSON-section CSV | `11051976_…_fixed_….csv`: `extract`, `parsed`, `feedback`, `metadata` and `gather` columns holding JSON strings | Transformed into a standardized workbook first, then validated |
| Standardized workbook | Output of the step above | Validated directly |

### Standardized workbook (`csv_transformer.py`)

| Sheet | Contents |
|---|---|
| Product Data | `extract` fields flattened (`meta.keywords`, …). Arrays such as `imageURL` and `bulletPoints` are kept whole as JSON. If `originalURL` is missing, it's filled from `gather.url` and logged |
| Parsed Data | `parsed.*` fields. `promotions` is kept as JSON |
| Feedback Data | `feedback.*` fields. `comments` is kept as JSON, plus a review count |
| Feedback Reviews | One row per review (reviewId, rating, subject, content, username, timestamp) |
| Metadata & Gather | `metadata.*`, `gather.*` and other plain columns such as `timestamp_date` |
| Original Data | The CSV rows exactly as uploaded |
| Transform Log | Detected format, counts and warnings |

No data is lost in the transformation:

- **Long values:** values longer than Excel's 32,767-character cell limit are split into `col__part2`, `col__part3`, … and joined back on read. For example, 9 feedback cells in the Walmart sample are longer than 55,000 characters.
- **Malformed JSON:** the raw text is kept in a `__raw__` column and listed in the log.
- **Control characters:** characters Excel can't store become visible `\uXXXX` escapes.

## Setup

For a step-by-step guide (prerequisites, Databricks token and permissions, first run, troubleshooting),
see **[SETUP.md](SETUP.md)**. In short:

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
| `DBX_CA_BUNDLE` | Optional CA bundle (.pem) for TLS-inspecting proxies. The default is certifi's bundle |
| `DBX_CATALOG`, `DBX_SCHEMA` | Default `common_catalog` / `gather_data` |
| `DBX_DETAIL_TABLE`, `DBX_SEARCH_TABLE` | Default `sku_detail_v2` / `sku_search_v2` |
| `DBX_QUERY_TIMEOUT` | Seconds, default 300 |

You need **Can use** on the warehouse and SELECT on both tables. Credentials come only from the
environment and are never logged.

## Run

**UI**

```bash
.venv/bin/streamlit run app.py
```

1. **Upload** a file. The app shows the detected format, record count, JSON sections found and transformation status.
2. **Preview** the transformed data (JSON-section CSVs only): one tab per sheet, plus **Download standardized Excel**.
3. **Configure** the data source (Live / Fixture / Mock), retailer, page type and production date range.
   - The retailer is prefilled from the uploaded `metadata.retailer`.
   - The date range defaults to ±3 days around the uploaded crawl timestamps.
4. **Validate.** The dashboard shows Extract, Parsed, Feedback and Overall accuracy, completeness and products not found.
   - You can filter the product table and drill into one product, with a tab per section and a review-level table.
   - **Download Excel validation report** gets the full report.

**CLI**

```bash
.venv/bin/python cli.py walmart_export.csv --transform-only                 # standardized workbook only
.venv/bin/python cli.py walmart_export.csv --mode live --retailer 'Walmart-US'
.venv/bin/python cli.py ~/Downloads/Flink_PDP.xlsx --mode live --retailer 'Flink%' --start 2026-10-03 --end 2026-11-04
.venv/bin/python cli.py any_file.csv --mode mock
```

| Output | Location |
|---|---|
| Standardized workbooks | `output/transformed_files/` |
| Validation reports | `output/validation_reports/` |
| Logs | `output/logs/validation.log` |

### Page types

| `Pagetype` value | Table used |
|---|---|
| `detail`, `Product`, `PDP`, `item` | `sku_detail_v2` |
| `search`, `listing`, `PLP`, `category` | `sku_search_v2` |
| Anything else | Choose Detail / Search / Both in the UI, or pass `--pagetype` |

### Data sources

- **live**: queries Databricks.
- **fixture**: saved production records (JSON).
  - `fixtures/flink_detail_sample.json` holds real Flink records for 2026-10-05 to 2026-11-04.
  - It contains production data, so keep it internal.
- **mock**: synthetic records built from the upload, with known changes for checking the scoring:
  - extract: ingredients, price, nutrition, images, description, manufacturer
  - feedback reviews: 2 missing, 1 rating changed, order reversed
  - other: parsed.price and feedback.avgRating changed, plus some unmatched rows

## Report (7 sheets)

| Sheet | Contents |
|---|---|
| Overall Summary | Key figures, then one row per product: Extract %, Parsed %, Feedback %, Overall %, completeness, counts, DBX status, timestamp and notes. Ends with the methodology |
| Extract Comparison | One row per product and extract attribute |
| Parsed Comparison | One row per product and parsed attribute |
| Feedback Comparison | Attribute rows (avgRating, commentCount, …), plus one Review row per review: matched / partial / missing / extra, with the field differences |
| Missing Attributes | Uploaded attributes that production lacks |
| Unmatched Products | Identifiers tried and the reason, with last-seen dates |
| DBX Raw Data | The production record and the original uploaded row |

Every comparison row has the uploaded value, production value, match %, status, mismatch reason,
missing and extra content, production field and comparison method.

## Tests

```bash
.venv/bin/python -m pytest -q
```

## Configuration (`config/validation.yaml`)

| Setting | What it controls |
|---|---|
| `aliases` | Uploaded column → production path, e.g. `Storage_and_Usage: extract.storage` |
| `comparators` | Comparator per attribute pattern: exact, name, numeric, text, list, images, keyvalue, nested, dict, reviews, boolean. Unlisted fields use their section's default |
| `scoring.overall_basis` | `sections` (default): mean of the section scores. `attributes`: mean over all attributes |
| `reviews.score_basis` | `max` (default): paired ÷ larger review count. `uploaded`: paired ÷ uploaded reviews. `paired`: accuracy of paired reviews only |
| `numeric`, `text`, `scoring`, `matching` | Tolerances, thresholds, weights, chunk size and last-seen lookback |

See [METHODOLOGY.md](METHODOLOGY.md) for how matching and scoring work.

## Layout

```
app.py                 Streamlit UI (upload → transform → preview → configure → validate → dashboard)
cli.py                 command-line runner
pipeline.py            orchestration (match → compare → score), default date range
config.py              env settings + validation rules
csv_transformer.py     format detection, JSON-section CSV → standardized workbook
json_flattener.py      JSON flattening, Excel-safe cells, long-cell split/join
file_processor.py      reads flat files and standardized workbooks, page-type aliases
dbx_connector.py       Live / Fixture / Mock sources
product_matcher.py     batched multi-pass matching, latest record, retailer choice, ambiguity flags
field_resolver.py      uploaded column → production field
comparison_engine.py   shared engine: section tagging, comparator dispatch, attribute status
extract_comparator.py  extract-section defaults
parsed_comparator.py   parsed-section defaults
feedback_comparator.py feedback-section defaults + review-level comparison
comparators/           exact, numeric, text, list/images, key/value, nested/dict
scoring_engine.py      section and overall scores, completeness
mismatch_analyzer.py   reasons built from comparison evidence
excel_report.py        7-sheet formatted report
mock_data.py           synthetic records for testing
utils/                 normalization, query builder, logger
```
