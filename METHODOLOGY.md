# Matching and scoring methodology

## 1. Finding the production record

Products are grouped by page type: `detail` → `sku_detail_v2`, `search` → `sku_search_v2`.
For `both` or an unknown page type, detail is tried first, then search. Matching runs in
**batched passes** (up to 500 identifiers per query). Each pass covers only the products still
unmatched, so most products need a single query.

| Pass | Production field | Notes |
|---|---|---|
| 1. productNo | `extract.productNo IN (…)` | Primary key |
| 2. URL | `gather.url IN (…)` | Production stores API URLs (`api.goflink.com/.../pdp/<id>`), not storefront URLs, so this pass also tries the trailing number of the uploaded URL (5+ digits) as a productNo (`url_derived_productNo`) |
| 3. name | `lower(trim(extract.name)) IN (…)` | Exact normalized name. Flagged if it maps to several productNos, or if the productNo differs from the upload |

Every query filters on the `timestamp_date` partition (the date range) and
`metadata.retailer LIKE :retailer`. All values are bound parameters.

**Latest record.** Per retailer and identifier, `ROW_NUMBER() OVER (… ORDER BY timestamp DESC)`
keeps the newest crawl. The second-newest crawl is fetched too. If the two disagree on price or
name, the product gets the note "Latest two crawls differ", and the newest crawl is still the one
compared.

**Several retailers** (e.g. `Flink-APP-DE` and `Flink-APP-NL`): the locale in the uploaded URL
(`/en-DE/`) picks the retailer whose country matches. If that doesn't settle it, the newest record
is used and the match is marked **ambiguous**. A product found only for another country's retailer
(URL says DE, record is NL) is also marked ambiguous.

Each result keeps: method, identifier value, table, retailer, DBX timestamp, record id, crawl count
in range, and notes.

**Unmatched products.** One extra batched query looks up the latest crawl date per table and
retailer over a longer lookback (`matching.last_seen_lookback_days`, default 180). Each reason is
built from that lookup:

- *last crawled 2026-09-30 (before range)*: an older record exists, so widen the date range
- *found in sku_search_v2 within the range*: set Pagetype to search or Both
- *not crawled in either table since …*: no production record at all

Every identifier tried is listed as well.

**`duplicate` column.** For Flink between 2026-10-05 and 2026-11-04, `duplicate` is null on every
row, so it isn't used to filter. Revisit this if another retailer populates it.

## 2. Which production field an uploaded column maps to

Candidates are tried in this order. The first one that **exists in the table and has a value in
the record** is used:

1. a configured alias (`Storage_and_Usage` → `extract.storage`, `coments.*` → `feedback.comments.*`,
   `avgRating`/`commentCount` → `feedback.*`)
2. `extract.<column>`
3. `<column>` as a top-level path (`parsed.price`, `parsed.promotions.discount`)
4. `parsed.<column>`
5. `feedback.<column>`, only when `extract` has no field of that name (so `extract.summary` never
   falls back to the unrelated `feedback.summary`)
6. a case-insensitive name match within `extract` / `parsed` / `feedback` (same condition)

So if `extract.price` is empty and `parsed.price` has a value, `parsed.price` is compared, and the
reason says so. A column with no matching field in the table is **Not in schema**. It isn't scored
and doesn't count toward completeness.

## 3. Comparators

Values are normalized first: HTML entities decoded, NFKC Unicode, lowercase, whitespace collapsed.
Whitespace-only cells, `[]`, `{}`, `null` and `nan` all count as empty.

| Type | Used for | Score |
|---|---|---|
| exact | productNo, gtin, brand, country, currency, units, outOfStock | 1 if equal after normalization (punctuation ignored), else 0 |
| name | name, manufacturer | 1 if equal after normalization, else word-level F1 |
| numeric | price, pricePerUnit, netQuantity, parsed.\*, ratings, counts | 1 if within ±0.01 or ±0.5%, else 0. Currency symbols and decimal commas are handled (`13,27 € / 1kg` = 13.27/kg). Different units (l vs piece) = 0 |
| text | description, allergens, instructions, storage, features | Word-level F1 = 2·common ÷ (uploaded words + production words). Lists words only on one side, and uploaded sentences missing from production |
| list | ingredients, bulletPoints, badge | Split on top-level commas/semicolons (not inside brackets, not decimal commas). Items match if equal or word-Jaccard ≥ 0.8. **Score = matched ÷ larger list** |
| keyvalue | nutrition | Header (`Nährwerte (je 100 g):`) dropped, split into `key: value`. A key matches when all its numbers are equal. **Score = matching keys ÷ all keys on either side** |
| images | imageURL | Same host: URL set match (matched ÷ larger count). Production re-hosts images (`gather-assets-prod…`), so with different hosts **score = smaller count ÷ larger count**, and URLs are reported as not comparable |
| nested | comments, promotions | Objects paired by `reviewId`/`id`, else by position. Average share of equal fields per pair ÷ larger count. Leaves like `parsed.promotions.discount` compare as numbers |
| boolean | video, sponsored, hasRichContent | Equal booleans. Uploaded `False` with an empty production value counts as N/A (`scoring.false_equals_empty`) |

## 4. Attribute status

| Status | Rule | In match %? | In completeness? |
|---|---|---|---|
| Match | score ≥ 98% | yes | yes, present |
| Partial | 50% ≤ score < 98% | yes | yes, present |
| Mismatch | score < 50% | yes | yes, present |
| Missing in production | uploaded has a value, production empty | yes, as 0% (configurable) | yes, absent |
| N/A | uploaded value empty | no | no |
| Not in schema | table has no such field | no | no |

## 5. Product metrics (kept separate)

- **Match %** = weighted mean of attribute scores over Match/Partial/Mismatch/Missing attributes
  (weights default to 1; set `scoring.weights`).
- **Completeness %** = attributes production fills ÷ attributes that are non-empty in the upload
  and exist in the table.
- Counts: matched, partial, mismatched, missing in production, unavailable (N/A + not in schema).

## 6. Worked example (real record, productNo 122301750)

| Attribute | Uploaded | Production | Score | Why |
|---|---|---|---|---|
| price | `1.99` | `1.99 €` | 100% | numbers equal |
| pricePerUnit | `13.27 € / 1KG` | `13,27 € / 1kg` | 100% | 13.27 = 13.27, kg = kg |
| nutrition | `Nährwerte (je 100 g): Energie: … \| Fett: 33 g \| …` | `Energie: …, Fett: 33 g, …` | 100% | 8 of 8 keys equal |
| Storage_and_Usage | `Vor Wärme geschützt…` | `extract.storage` same text | 100% | alias |
| imageURL | 1 cloudinary URL | 1 gather-assets URL | 100% | re-hosted, counts equal |
| country | `Deutschland` | empty | 0% (Missing) | not in production |
| parsed.netQuantityUnit | `g` | `KG` | 0% | values differ |

Reasons are built only from these comparison results. The tool never states a cause (such as an
extraction failure) that the data doesn't show.
