# Local setup guide

This guide gets the product data validation tool running on your machine, from a fresh clone to
your first validation report. It takes about 10 minutes. The commands are for macOS and Linux;
Windows notes are at the end.

**On this page**

1. [Before you start](#1-before-you-start)
2. [Get the code](#2-get-the-code)
3. [Create the Python environment](#3-create-the-python-environment)
4. [Check the install](#4-check-the-install)
5. [Try it without Databricks](#5-try-it-without-databricks)
6. [Connect to Databricks](#6-connect-to-databricks)
7. [Run a live validation](#7-run-a-live-validation)
8. [Where things are saved](#8-where-things-are-saved)
9. [Updating](#9-updating)
10. [Troubleshooting](#10-troubleshooting)

---

## 1. Before you start

| You need | Check with | Notes |
| --- | --- | --- |
| Python 3.10 or newer (3.11 recommended) | `python3 --version` | Install from [python.org](https://www.python.org/downloads/) or with `brew install python@3.11` |
| Git | `git --version` | Preinstalled on most Macs (`xcode-select --install` if not) |
| Access to the GitHub repo | Open <https://github.com/majiSupriya198/product-validation-tool> | The repo is private. Ask the owner (Supriya Maji) to add you |
| Databricks access (only for live mode) | See [step 6](#6-connect-to-databricks) | You can try the tool without it using mock or fixture mode |

## 2. Get the code

If you haven't signed in to GitHub from the command line yet, do that first. The GitHub CLI is
the easiest way:

```bash
gh auth login
```

Then clone the repo and go into it:

```bash
git clone https://github.com/majiSupriya198/product-validation-tool.git
```

```bash
cd product-validation-tool
```

All the commands below run from this folder.

## 3. Create the Python environment

The tool uses its own virtual environment (`.venv`), so it doesn't touch your system Python.

Create it:

```bash
python3 -m venv .venv
```

Install the dependencies (about 1–2 minutes):

```bash
.venv/bin/pip install -r requirements.txt
```

The main packages:

- **pandas, openpyxl and xlrd** read and write files.
- **databricks-sql-connector** queries Databricks.
- **streamlit** runs the UI.
- **pytest** runs the tests.

> **Tip:** you can run `source .venv/bin/activate` once per terminal and then type `python`,
> `streamlit` and `pytest` without the `.venv/bin/` prefix. This guide keeps the prefix so each
> command works on its own.

## 4. Check the install

Run the test suite:

```bash
.venv/bin/python -m pytest -q
```

You should see something like `78 passed`.

- **"skipped" results** are normal. One test uses a sample Walmart CSV from the owner's Downloads folder and skips itself when the file isn't there.
- **Failures** mean something is wrong with the install. See [Troubleshooting](#10-troubleshooting).

## 5. Try it without Databricks

**Mock** mode builds synthetic "production" records from your own file, with known differences
added. Use it to see the full workflow and report before you have Databricks access.

### In the UI

```bash
.venv/bin/streamlit run app.py
```

Your browser opens <http://localhost:8501>. If it doesn't, open that address yourself.

1. **Upload** any product file: a flat CSV/XLSX like `Flink_PDP.xlsx`, or a JSON-section CSV like the Walmart export.
2. **Check the detected format.** For JSON-section CSVs, look at the **Transformation preview** tabs, and use **Download standardized Excel** if you need the converted file.
3. **Configure:** under **Validation configuration**, choose **Mock (synthetic)**.
4. **Start validation**, then look through the dashboard and the per-product drill-down, and click **Download Excel validation report**.

To stop the app, press `Ctrl+C` in the terminal.

### From the command line

```bash
.venv/bin/python cli.py path/to/your_file.csv --mode mock
```

The last line of the output gives the report's path.

### With real production records saved in the repo (fixture mode)

`fixtures/flink_detail_sample.json` holds real Flink production records from 2026-10-05 to
2026-11-04. If you have `Flink_PDP.xlsx`:

```bash
.venv/bin/python cli.py path/to/Flink_PDP.xlsx --mode fixture --retailer 'Flink%' --start 2026-10-05 --end 2026-11-04
```

> The fixture contains production data. Keep it internal and don't copy it outside the company.

## 6. Connect to Databricks

### 6.1 Find the warehouse connection details

1. In Databricks, go to **SQL Warehouses** and open the warehouse you'll use. `dev_wh` works for ad hoc runs.
2. Open the **Connection details** tab and copy:
   - **Server hostname**, for example `dbc-xxxx.cloud.databricks.com`, without `https://`
   - **HTTP path**, for example `/sql/1.0/warehouses/abc123…`

### 6.2 Create a personal access token

1. In Databricks, click your profile picture, then **Settings → Developer → Access tokens → Manage**.
2. Click **Generate new token**, add a comment such as "product validation tool" and a lifetime (for example 90 days), then click **Generate**.
3. Copy the token right away. Databricks only shows it once.

### 6.3 Check your permissions

Your account needs:

- **Can use** on the SQL warehouse
- **SELECT** on `common_catalog.gather_data.sku_detail_v2` and `sku_search_v2`

To confirm, run `SELECT 1 FROM common_catalog.gather_data.sku_detail_v2 LIMIT 1` in the
Databricks SQL editor, on the same warehouse.

### 6.4 Create your `.env` file

Copy the template:

```bash
cp .env.example .env
```

Restrict it to your user:

```bash
chmod 600 .env
```

Open `.env` in an editor and fill in:

```
DBX_HOST=dbc-xxxx.cloud.databricks.com
DBX_HTTP_PATH=/sql/1.0/warehouses/abc123
DBX_TOKEN=<paste your token here>
DBX_AUTH_TYPE=token
```

Leave the other values as they are, unless you use a different catalog, schema or tables.

| Variable | What it does |
| --- | --- |
| `DBX_HOST` | Workspace hostname |
| `DBX_HTTP_PATH` | Warehouse HTTP path (or set `DBX_WAREHOUSE_ID` to just the ID) |
| `DBX_TOKEN` | Your personal access token |
| `DBX_AUTH_TYPE` | `token`, or `databricks-oauth` to sign in through the browser with no token |
| `DBX_CATALOG`, `DBX_SCHEMA` | Default `common_catalog` / `gather_data` |
| `DBX_DETAIL_TABLE`, `DBX_SEARCH_TABLE` | Default `sku_detail_v2` / `sku_search_v2` |
| `DBX_CA_BUNDLE` | Optional path to a `.pem` file if a corporate proxy inspects TLS traffic |
| `DBX_QUERY_TIMEOUT` | Seconds before a query is cancelled (default 300) |

> **Keep your token private.**
> - Put it only in `.env`, which git ignores.
> - Never put it in `.env.example`, code, chat, Jira or Confluence.
> - If it leaks, revoke it in Databricks and create a new one.

### 6.5 Test the connection

```bash
.venv/bin/python -c "from dbx_connector import LiveSource; print(LiveSource().status())"
```

`(True, 'Connected')` means you're ready.

- The first connection can take a minute or two if the warehouse was stopped and has to start.
- In the UI, choose **Live (Databricks)** and click **Check DBX connection**.

## 7. Run a live validation

### In the UI

Start the app with `.venv/bin/streamlit run app.py`, upload your file and choose **Live (Databricks)**.

| Setting | What to enter |
| --- | --- |
| Retailer | SQL `LIKE` pattern, e.g. `Walmart-US` or `Flink%`. Prefilled from the file's metadata when it has one retailer |
| Page type | **From file**, or Detail / Search / Both. `Product` and `PDP` in the file count as Detail |
| Production date range | Defaults to ±3 days around the file's crawl timestamps, or the last 30 days if it has none |

Then click **Start validation**.

### From the command line

```bash
.venv/bin/python cli.py path/to/walmart_export.csv --mode live --retailer 'Walmart-US'
```

```bash
.venv/bin/python cli.py path/to/Flink_PDP.xlsx --mode live --retailer 'Flink%' --start 2026-10-03 --end 2026-11-04
```

To only convert a JSON-section CSV into the standardized workbook, without validating:

```bash
.venv/bin/python cli.py path/to/walmart_export.csv --transform-only
```

All CLI options:

```bash
.venv/bin/python cli.py --help
```

## 8. Where things are saved

| What | Where |
| --- | --- |
| Standardized workbooks (converted CSVs) | `output/transformed_files/` |
| Validation reports | `output/validation_reports/` |
| Logs (tokens and hosts blanked out) | `output/logs/validation.log` |
| Your Databricks settings | `.env` |
| Scoring rules, tolerances, aliases | `config/validation.yaml` |

Everything under `output/` is git-ignored, because reports contain production data.

For how the report is scored, see [METHODOLOGY.md](METHODOLOGY.md). For an overview, see
[README.md](README.md).

## 9. Updating

Pull the latest code:

```bash
git pull
```

Reinstall dependencies in case they changed:

```bash
.venv/bin/pip install -r requirements.txt
```

Check that everything still works:

```bash
.venv/bin/python -m pytest -q
```

Your `.env` and `output/` folder aren't touched by updates.

## 10. Troubleshooting

| Problem | Fix |
| --- | --- |
| `python3: command not found`, or the version is below 3.10 | Install Python 3.11 (see [step 1](#1-before-you-start)) and use `python3.11 -m venv .venv` |
| `git clone` asks for a password or says "Repository not found" | Run `gh auth login`, and check you've been added to the private repo |
| `ModuleNotFoundError` | Run `.venv/bin/pip install -r requirements.txt` again, and make sure you run commands with `.venv/bin/…` from the repo folder |
| `Not configured (set DBX_HOST, …)` or `Databricks isn't configured` | `.env` is missing a value, or isn't in the repo folder next to `app.py` |
| `403 … You do not have permission to use the SQL Warehouse` | Your token works, but you lack **Can use** on that warehouse. Ask an admin, or switch `DBX_HTTP_PATH` to a warehouse you can use (e.g. `dev_wh`) |
| `401` / invalid token | The token expired or was mistyped. Create a new one ([6.2](#62-create-a-personal-access-token)) |
| `CERTIFICATE_VERIFY_FAILED` / self-signed certificate | The tool already uses certifi's CA bundle. Behind a TLS-inspecting corporate proxy, set `DBX_CA_BUNDLE` to your company's CA `.pem` file |
| The connection test hangs | The warehouse may be starting, so wait up to 2 minutes. Failed connections give up after about 2 minutes |
| Query timeout on large files | Increase `DBX_QUERY_TIMEOUT` in `.env`, or lower `matching.chunk_size` in `config/validation.yaml` |
| "Unrecognized page types" | Choose Detail / Search / Both in the UI, or pass `--pagetype detail` |
| Many products "not found" | Read the reason in the **Unmatched Products** sheet. It says whether the product was crawled before or after your date range, or only in the search table. Adjust the date range or page type |
| Port 8501 already in use | `.venv/bin/streamlit run app.py --server.port 8502` |
| Upload too large in the UI | `.venv/bin/streamlit run app.py --server.maxUploadSize 500` (MB) |

For more detail, check `output/logs/validation.log`.

### Windows notes

- Create the environment with `py -3.11 -m venv .venv`.
- Use `.venv\Scripts\python` and `.venv\Scripts\streamlit` in place of `.venv/bin/…`.
- Copy the template with `copy .env.example .env`. Skip `chmod`.
- In PowerShell, quote patterns with double quotes: `--retailer "Flink%"`.
