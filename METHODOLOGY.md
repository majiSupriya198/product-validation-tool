# Matching and scoring methodology

## 0. Sections

Every compared attribute belongs to one of three sections, and each section is scored separately.
That separation shows which part of the pipeline causes a discrepancy.

| Section | Uploaded attributes | Compared with |
|---|---|---|
| Extract | `extract.*` (JSON-section CSV) or a flat column that maps to `extract.*` | Production `extract.<same field>` |
| Parsed | `parsed.*` | Production `parsed.<same field>` |
| Feedback | `feedback.*`, including `comments` reviewed one by one | Production `feedback.<same field>` |

- **Section-qualified uploads** (JSON-section CSVs and standardized workbooks) are only compared within the same section. `extract.price` is never compared with `parsed.price`.
- **Flat uploads:** each column belongs to the section of the production field it maps to (§2).
- **Not compared:** `metadata` and `gather`. They're used for matching (retailer, URL) and kept for traceability.

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

**Several retailers** (e.g. `Flink-APP-DE` and `Flink-APP-NL`): the tool picks one in this order:

1. The uploaded `metadata.retailer`, when the file has it.
2. The locale in the uploaded URL (`/en-DE/`), matched to the retailer's country.
3. Otherwise the newest record, with the match marked **ambiguous**.

A product found only for a different retailer or country than the upload is also marked ambiguous.

**Date range.** By default this is ±3 days around the uploaded crawl timestamps, so the comparison
uses the production crawl closest in time. Without timestamps it's the last 30 days. Reviews and
prices change over time, so a wide window adds differences that aren't extraction errors.

**URL formats differ by retailer:**
- Flink production stores API URLs (`api.goflink.com/.../pdp/<id>`).
- Walmart production stores `walmart.com/ip/random/<id>`, while the upload has `walmart.com/ip/<id>`.

In both cases the URL pass also tries the number at the end of the URL as a productNo.

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
| nested | promotions, contextRatings | Objects paired by `id`, else by position. Average share of equal fields per pair ÷ larger count. Leaves like `parsed.promotions.discount` compare as numbers |
| dict | JSON objects (`extract.meta`, `extract.wholesale`, …) | Recursive: flatten both, compare each leaf. **Score = equal leaves ÷ all leaves on either side** |
| reviews | `feedback.comments` | Review by review, see below |

**Section defaults.** A field with no configured comparator gets its section's default:

| Section | Default comparison |
|---|---|
| Extract | Numbers numerically, JSON objects key by key, everything else as text |
| Parsed | Numbers numerically, labels exactly, lists object by object |
| Feedback | Numbers numerically, review lists review by review, everything else as text |

### Review-level comparison (`feedback_comparator.py`)

**Pairing.** Order never matters. Reviews are paired by:

1. `reviewId` (also `id`, `commentId`)
2. The same normalized content, username, review date and rating
3. The same username with content word-F1 ≥ 90%

Steps 2 and 3 only pair reviews when at least one side has no ID. Two different IDs are always two different reviews.

**Per-pair score.** Each pair gets the share of equal fields: rating (numeric), subject and content (word F1), username, and date (the timestamp's day). Fields that are empty in the upload are skipped.

**Per-review status** (shown in Feedback Comparison as "Review" rows):

- Match / Partial / Mismatch, based on the pair score
- Missing in production: an uploaded review with no partner
- Extra in production: a production review with no partner

**Attribute score.** `reviews.score_basis` decides it:

| Basis | Score |
|---|---|
| `max` (default) | Sum of pair scores ÷ max(uploaded, production) review count. Missing and extra reviews lower the score |
| `uploaded` | Sum of pair scores ÷ uploaded review count |
| `paired` | Mean pair score: accuracy only |

The reason always reports both coverage and accuracy, for example: "Uploaded has 50 reviews,
production 10; 10 paired (10 by reviewId). 40 uploaded reviews aren't in production. Paired
reviews agree 100%."
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

- **Extract % / Parsed % / Feedback %** = weighted mean of the section's attribute scores over its
  Match/Partial/Mismatch/Missing attributes. Weights default to 1; set them with `scoring.weights`.
  A section with nothing comparable shows "—".
- **Overall %** depends on `scoring.overall_basis`:
  - `sections` (default): mean of the section scores that exist, so each section counts equally. Feedback has few attributes but is as important as Extract.
  - `attributes`: mean over all comparable attributes.
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

## 7. Walmart-US findings (JSON-section CSV, live run 2026-11-05)

Input: `11051976_2026-10-29_0001_fixed_….csv`, 85 product pages crawled 2026-10-28. Production was read from `dev_wh`.

**Results by window:**

| Window | Found | Extract | Parsed | Feedback | Overall |
|---|---|---|---|---|---|
| Default ±3 days (2026-10-25 to 10-31) | 50 / 85 | 67.7% | 85.0% | 30.2% | 60.8% |
| 30-day lookback (2026-09-28 to 10-31) | 61 / 85 | 67.3% | 85.9% | 29.8% | 60.6% |

**Production differences that explain most of the gap:**

| Area | What's different |
|---|---|
| Reviews | Production keeps about 10 reviews per product; the upload has 50. The reviews present on both sides agree about 99%, so the low Feedback % is mostly coverage, not accuracy. Set `reviews.score_basis: paired` to score accuracy only |
| Review counts | Production `feedback.commentCount` holds the upload's `totalRatings` (e.g. 5,286 vs 5,278). The upload's `commentCount` (3,656) counts something else. This is a field-definition difference. It isn't aliased; decide which definition is right |
| Fields production doesn't fill | `extract.gtin`, `extract.contentSpecifications`, `feedback.totalRatings` are empty for every product; `extract.outOfStock` for 47 of 50 |
| Language | Some production crawls are Spanish (name, description, review text) but tagged `metadata.language = en`, so they can't be filtered out |
| Prices | `price` differs on 22 of 50 products. Crawl times differ, so check the "Production crawl X days after" note before calling these extraction errors |
| Units and rounding | `parsed.perUnit` "EA" vs "UNIT", and display-rounded ratings (4.5 vs 4.469), are treated as equal |
