"""Synthetic production records built from the upload, with known, deterministic perturbations.

Used to test the scoring end to end without Databricks. Perturbation by row position (i % 10):
  0 -> no production record (unmatched)
  1 -> last 2 ingredients dropped
  2 -> price +0.30
  3 -> last nutrition entry removed
  4 -> production has one extra image
  5 -> description: last 3 words removed
  6 -> manufacturer missing in production
  7 -> identical copy
  8 -> feedback reviews: last 2 dropped, first review's rating +1, order reversed
  9 -> parsed.price +1 and feedback.avgRating -0.1
Two crawls are generated per product; the older one has a different price so the latest-record
logic is exercised.
"""
from __future__ import annotations

import copy
from datetime import date, datetime, timedelta, timezone
from typing import Any

from config import DBXSettings, Rules
from field_resolver import FieldResolver
from file_processor import ParsedFile
from dbx_connector import MockSource, _load_snapshot
from utils.normalization import as_list, parse_number, split_items

PERTURBATIONS = {0: "unmatched", 1: "ingredients -2", 2: "price +0.30", 3: "nutrition -1 entry",
                 4: "imageURL +1", 5: "description -3 words", 6: "manufacturer missing",
                 8: "reviews: 2 missing, 1 rating changed, order reversed", 9: "parsed.price +1, feedback.avgRating -0.1"}


def _set_path(rec: dict, path: str, value: Any) -> None:
    parts = path.split(".")
    cur = rec
    for p in parts[:-1]:
        nxt = cur.get(p)
        if isinstance(nxt, list):  # nested list path (comments.content); store at first element
            if not nxt:
                nxt.append({})
            cur = nxt[0]
            continue
        cur = cur.setdefault(p, {})
    cur[parts[-1]] = value


def build_mock(parsed: ParsedFile, settings: DBXSettings | None = None, rules: Rules | None = None,
               retailer: str = "Mock-Retailer-DE", end: date | None = None) -> tuple[MockSource, dict[int, str]]:
    settings = settings or DBXSettings()
    rules = rules or Rules.load()
    end = end or date.today()
    by_table: dict[str, list[dict]] = {}
    applied: dict[int, str] = {}
    resolvers: dict[str, FieldResolver] = {}
    for i, p in enumerate(parsed.products):
        kind = i % 10
        applied[p.row_index] = PERTURBATIONS.get(kind, "identical")
        if kind == 0:
            continue
        pt = p.identifiers.get("Pagetype") or "detail"
        try:
            table = settings.table_for(pt)
        except ValueError:
            table = settings.table_for("detail")
        if table not in resolvers:
            resolvers[table] = FieldResolver(rules.aliases, _load_snapshot(table))
        res = resolvers[table]
        rec: dict[str, Any] = {"extract": {}, "parsed": {}, "feedback": {}, "gather": {},
                               "metadata": {"retailer": p.identifiers.get("retailer") or retailer,
                                            "country": None if p.identifiers.get("retailer") else "DE"}}
        for col, val in p.attributes.items():
            if val is None:
                continue
            path = res.resolve(col)
            if path and not path.startswith("gather."):
                _set_path(rec, path, copy.deepcopy(val))
        pno = p.identifiers.get("productNo") or f"mock{i}"
        rec["extract"]["productNo"] = pno
        rec["gather"]["url"] = p.identifiers.get("originalURL") or f"https://example.invalid/{pno}"

        ex = rec["extract"]
        if kind == 1 and ex.get("ingredients"):
            items = split_items(ex["ingredients"])
            if len(items) > 2:
                ex["ingredients"] = ", ".join(items[:-2])
        elif kind == 2 and ex.get("price") is not None:
            n = parse_number(ex["price"])
            if n is not None:
                ex["price"] = f"{n + 0.30:.2f}"
        elif kind == 3 and ex.get("nutrition"):
            parts = [s for s in str(ex["nutrition"]).split("|")]
            if len(parts) > 2:
                ex["nutrition"] = "|".join(parts[:-1])
        elif kind == 4:
            imgs = as_list(ex.get("imageURL"))
            base = imgs[0] if imgs else "https://example.invalid/img.png"
            ex["imageURL"] = imgs + [str(base).rsplit("/", 1)[0] + "/extra-mock.png"]
        elif kind == 5 and ex.get("description"):
            words = str(ex["description"]).split()
            if len(words) > 4:
                ex["description"] = " ".join(words[:-3])
        elif kind == 6:
            ex.pop("manufacturer", None)
        elif kind == 8:
            fb = rec["feedback"]
            reviews = [dict(r) for r in (fb.get("comments") or []) if isinstance(r, dict)]
            if len(reviews) >= 3:
                reviews = reviews[:-2]                       # 2 reviews missing in production
                reviews[0]["rating"] = (parse_number(reviews[0].get("rating")) or 0) + 1   # 1 rating differs
                fb["comments"] = list(reversed(reviews))     # order must not matter
        elif kind == 9:
            pa, fb = rec["parsed"], rec["feedback"]
            if parse_number(pa.get("price")) is not None:
                pa["price"] = parse_number(pa["price"]) + 1.0
            if parse_number(fb.get("avgRating")) is not None:
                fb["avgRating"] = round(parse_number(fb["avgRating"]) - 0.1, 2)

        now = datetime.combine(end, datetime.min.time(), tzinfo=timezone.utc) - timedelta(hours=i % 24)
        latest = dict(rec, id=f"mock-{i}-b", timestamp=now.isoformat())
        older = copy.deepcopy(rec)
        older["id"], older["timestamp"] = f"mock-{i}-a", (now - timedelta(days=3)).isoformat()
        if older["extract"].get("price") is not None:
            n = parse_number(older["extract"]["price"])
            if n is not None:
                older["extract"]["price"] = f"{n + 1:.2f}"   # stale price: must never be the one compared
        by_table.setdefault(table, []).extend([latest, older])
    return MockSource(by_table), applied
