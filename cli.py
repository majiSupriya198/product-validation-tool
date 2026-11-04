"""Headless validation run.

Examples:
  python cli.py ~/Downloads/Flink_PDP.xlsx --mode fixture --retailer 'Flink%'
  python cli.py data.csv --mode live --retailer 'Flink-APP-DE' --start 2026-10-01 --end 2026-11-04
  python cli.py data.xlsx --mode mock
"""
from __future__ import annotations

import argparse
import sys
from datetime import date, timedelta
from pathlib import Path

from config import REPORT_DIR, ROOT, DBXSettings, Rules
from dbx_connector import FixtureSource, LiveSource, SourceError
from excel_report import save_report
from file_processor import InputFileError, parse_file
from mock_data import build_mock
from pipeline import run_validation

DEFAULT_FIXTURE = ROOT / "fixtures" / "flink_detail_sample.json"


def mock_retailer_name(pattern: str | None) -> str:
    base = (pattern or "").replace("%", "").replace("_", "").strip("-") or "Mock"
    return f"{base}-MOCK-DE"


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Validate extracted product data against production (Databricks).")
    ap.add_argument("file", help="CSV, XLSX or XLS file")
    ap.add_argument("--mode", choices=["live", "fixture", "mock"], default="live")
    ap.add_argument("--fixture", default=str(DEFAULT_FIXTURE), help="JSON of saved production records (fixture mode)")
    ap.add_argument("--retailer", default="", help="metadata.retailer LIKE pattern, e.g. 'Flink%%'")
    ap.add_argument("--pagetype", choices=["detail", "search", "both"], default=None,
                    help="Override the file's Pagetype column")
    ap.add_argument("--start", type=date.fromisoformat, default=None, help="YYYY-MM-DD (default: end - 30 days)")
    ap.add_argument("--end", type=date.fromisoformat, default=None, help="YYYY-MM-DD (default: today)")
    ap.add_argument("--rules", default=None, help="Path to a validation.yaml")
    ap.add_argument("--out", default=str(REPORT_DIR))
    args = ap.parse_args(argv)

    end = args.end or date.today()
    start = args.start or end - timedelta(days=30)
    settings, rules = DBXSettings(), Rules.load(args.rules)

    try:
        parsed = parse_file(args.file, pagetype_override=args.pagetype)
    except InputFileError as exc:
        print(f"Input error: {exc}", file=sys.stderr)
        return 2

    if args.mode == "live":
        source = LiveSource(settings)
        ok, msg = source.status()
        if not ok:
            print(f"Databricks: {msg}", file=sys.stderr)
            return 3
    elif args.mode == "fixture":
        source = FixtureSource(args.fixture)
    else:
        source, _ = build_mock(parsed, settings, rules, retailer=mock_retailer_name(args.retailer), end=end)

    def progress(f: float, msg: str) -> None:
        print(f"\r[{f:5.0%}] {msg:<60}", end="", file=sys.stderr, flush=True)

    try:
        run = run_validation(parsed, source, settings, rules, start, end, args.retailer,
                             pagetype_default=args.pagetype, progress=progress)
    except SourceError as exc:
        print(f"\nDatabricks error: {exc}", file=sys.stderr)
        return 4
    print(file=sys.stderr)
    path = save_report(run, Path(args.out), Path(args.file).stem)
    s = run.summary()
    print(f"Products: {s['uploaded']} uploaded, {s['found']} found, {s['not_found']} not found, {s['ambiguous']} ambiguous")
    print(f"Average match: {s['avg_match_pct']}%  ·  average completeness: {s['avg_completeness_pct']}%")
    print(f"Attributes: {s['matched']} matched, {s['partial']} partial, {s['mismatched']} mismatched, "
          f"{s['missing_in_prod']} missing in production, {s['unavailable']} unavailable")
    print(f"Report: {path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
