"""Headless validation run.

Examples:
  python cli.py ~/Downloads/Flink_PDP.xlsx --mode fixture --retailer 'Flink%'
  python cli.py data.csv --mode live --retailer 'Flink-APP-DE' --start 2026-10-01 --end 2026-11-04
  python cli.py data.xlsx --mode mock
  python cli.py walmart_export.csv --transform-only          # JSON-section CSV -> standardized workbook
  python cli.py walmart_export.csv --mode live --retailer 'Walmart-US'
"""
from __future__ import annotations

import argparse
import sys
from datetime import date
from pathlib import Path

from config import REPORT_DIR, ROOT, TRANSFORM_DIR, DBXSettings, Rules
from csv_transformer import TransformError, detect, save_transformed, transform
from dbx_connector import FixtureSource, LiveSource, SourceError
from excel_report import save_report
from file_processor import InputFileError, parse_file, parse_standardized
from mock_data import build_mock
from pipeline import default_date_range, run_validation

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
    ap.add_argument("--start", type=date.fromisoformat, default=None,
                    help="YYYY-MM-DD (default: 3 days before the uploaded timestamps, else end - 30 days)")
    ap.add_argument("--end", type=date.fromisoformat, default=None,
                    help="YYYY-MM-DD (default: 3 days after the uploaded timestamps, else today)")
    ap.add_argument("--rules", default=None, help="Path to a validation.yaml")
    ap.add_argument("--out", default=str(REPORT_DIR))
    ap.add_argument("--transform-only", action="store_true",
                    help="Only convert a JSON-section CSV to the standardized workbook, then stop")
    args = ap.parse_args(argv)
    settings, rules = DBXSettings(), Rules.load(args.rules)

    try:
        det = detect(Path(args.file).read_bytes(), args.file)
        print(f"Detected: {det.label}, {det.records} records", file=sys.stderr)
        if det.format == "json_sections":
            result = transform(args.file, args.file)
            tpath = save_transformed(result, TRANSFORM_DIR, Path(args.file).stem)
            print(f"Standardized workbook: {tpath}", file=sys.stderr)
            for w in result.warnings:
                print(f"  note: {w}", file=sys.stderr)
            if args.transform_only:
                return 0
            parsed = parse_standardized(result.workbook_bytes, args.pagetype)
            parsed.warnings = result.warnings + parsed.warnings
        elif args.transform_only:
            print(f"Nothing to transform: {det.label}", file=sys.stderr)
            return 0
        else:
            parsed = parse_file(args.file, pagetype_override=args.pagetype)
    except (InputFileError, TransformError) as exc:
        print(f"Input error: {exc}", file=sys.stderr)
        return 2

    d_start, d_end = default_date_range(parsed)
    end = args.end or d_end
    start = args.start or d_start
    if end < start:
        start, end = end, start

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
    print(f"Production dates: {start} to {end} · retailer filter: {args.retailer or 'any'}", file=sys.stderr)

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
    print(f"Average match: Extract {s['avg_extract_pct']}%  ·  Parsed {s['avg_parsed_pct']}%  ·  "
          f"Feedback {s['avg_feedback_pct']}%  ·  Overall {s['avg_match_pct']}%  ·  completeness {s['avg_completeness_pct']}%")
    print(f"Attributes: {s['matched']} matched, {s['partial']} partial, {s['mismatched']} mismatched, "
          f"{s['missing_in_prod']} missing in production, {s['unavailable']} unavailable")
    print(f"Report: {path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
