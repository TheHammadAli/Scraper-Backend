# Backend - City-wise Listing Collector

Collects public listings from **OLX Pakistan**, **PakWheels** and **Zameen.com**,
organised city by city, into SQLite + an Excel workbook.

This is both the scraper library and the API the frontend calls. It works
standalone as a CLI - the API is an optional layer on top.

Pipeline:

```
City -> Website -> Category/search page -> Listing URLs -> Listing details
     -> Validation -> Database -> Excel
```

---

## Quick start

```bash
cd backend
python -m venv .venv
.venv\Scripts\activate              # Windows
pip install -r requirements.txt

python run.py verify                # check the site parsers still work
python run.py run --cities Lahore --sources olx --limit 20
python run.py stats
python run.py export
```

The Excel file lands in `exports/`, the database in `data/listings.db`.

### Serving the API

```bash
uvicorn main:app --reload           # http://127.0.0.1:8000
```

| Endpoint | Purpose |
|---|---|
| `GET /api/health` | Liveness, plus the id of any running job |
| `GET /api/config` | Cities, sources and categories for the run form |
| `POST /api/jobs` | Start a collection; returns a job id |
| `GET /api/jobs/{id}` | Poll progress, stats and log lines (`?log_offset=`) |
| `POST /api/jobs/{id}/cancel` | Stop a run; collected listings are kept |
| `GET /api/listings` | Filter, search and paginate the collected data |
| `GET /api/filters` | Distinct cities / sources / categories present |
| `GET /api/stats` | Per city and source summary |
| `GET /api/export` | Download an `.xlsx` (`?city=` to narrow it) |

Environment variables: `CORS_ORIGINS` (comma separated frontend origins),
`HOST`, `PORT`, `JOB_LOG_LIMIT`, `JOB_HISTORY_LIMIT`.

Only one collection runs at a time - a second request gets HTTP 409. The
collectors are rate limited per host, so a parallel run would not be faster,
just ruder to the sites.

### Choosing categories per site

`POST /api/jobs` takes `selections`, which maps each source to the categories
wanted from it. An empty list means every category of that source:

```json
{
  "cities": ["Lahore", "Karachi"],
  "selections": {
    "olx": ["cars", "mobiles"],
    "pakwheels": ["used-cars"],
    "zameen": []
  },
  "limit": 50
}
```

**Use `selections`, not the flat `categories` list.** Category keys are not
unique across sources: `bikes` exists on both OLX (Motorcycles) and PakWheels
(Used Bikes). A flat `categories: ["bikes"]` matches both, so it cannot
express "OLX motorcycles but not PakWheels bikes". `selections` can.

The flat `sources` + `categories` fields still work and are what the CLI uses,
but they carry that ambiguity - `run.py run --categories bikes` will collect
from both sites.

### Filtering by ad date

`POST /api/jobs` also takes `date_from`, `date_to` (both `YYYY-MM-DD`,
inclusive) and `keep_undated`:

```json
{ "date_from": "2026-09-20", "date_to": "2026-09-20", "keep_undated": false }
```

From the CLI:

```bash
python run.py run --today                          # today's ads only
python run.py run --date-from 2026-09-01           # everything since
python run.py run --date-from 2026-09-01 --date-to 2026-09-07
python run.py run --today --drop-undated           # strict window
```

Two things worth knowing:

- **The filter runs after fetching.** Listing pages put featured ads first
  rather than sorting by date - a live OLX page showed ads from 2026-09-20
  interleaved with ones from 2025-10-21 - so there is no safe point to stop
  paging early. Asking for an older date means paging through everything
  newer; raise `--limit` if a narrow window returns little.
- **A window that reaches today bypasses the page cache**, because the cache
  holds responses for up to a day and a page fetched yesterday cannot contain
  today's ads.

`keep_undated` defaults to true: an ad whose date could not be parsed is kept
rather than dropped, so a parser gap never silently loses real listings. The
run summary reports `filtered` and `undated` counts separately.

### Commands

| Command | What it does |
|---|---|
| `run.py run` | Collect listings, then export to Excel |
| `run.py export` | Re-export the existing database |
| `run.py stats` | Per-city / per-source counts |
| `run.py verify` | Fetch one page per source and report what parsed |
| `run.py init-db` | Create the database and indexes |

Useful flags on `run`:

```bash
--cities Lahore,Karachi        # default: every enabled city
--sources olx,zameen           # default: every enabled source
--categories cars,used-cars    # default: every category for each source
--limit 50                     # max listings per city+category
--no-export                    # skip the Excel step
-v                             # debug logging
```

---

## Layout

```
backend/
├── config/
│   ├── cities.yml         # the city list - nothing is hard-coded
│   └── settings.yml       # rate limits, categories, caps, storage paths
├── core/
│   ├── models.py          # the Listing record + validation + dedup key
│   ├── normalize.py       # price / date / phone parsing
│   ├── config.py          # config loading
│   ├── http.py            # robots.txt, rate limiting, retries, cache
│   ├── db.py              # schema, indexes, upserts
│   └── pipeline.py        # orchestration
├── scrapers/
│   ├── base.py            # shared collector machinery + parse helpers
│   ├── olx/
│   ├── pakwheels/
│   └── zameen/
├── export/excel.py        # Summary + All Listings + one sheet per city
├── tests/test_offline.py  # 39 tests, no network needed
├── run.py                 # CLI
│
├── main.py                # FastAPI app - the routes
├── jobs.py                # background scrape jobs + log capture
└── settings.py            # API settings (CORS, host, port)
```

The API layer (`main.py`, `jobs.py`, `settings.py`) is a thin wrapper: it
imports the same `core/` and `scrapers/` the CLI uses, so anything the UI can
do is also available from the command line.

---

## Configuring cities

Cities live in `config/cities.yml` only. Add or remove a block and the whole
system follows — no code change:

```yaml
cities:
  - name: Sahiwal
    enabled: true
    province: Punjab
    olx_location_id: null      # auto-resolved on first run, then cached
    zameen_slug: null          # auto-resolved on first run, then cached
    pakwheels_slug: null       # derived from the name
```

Each site identifies cities differently, so the collectors resolve them live
and cache the result in `.cache/locations.json`:

- **OLX** — reads the published locations sitemap
  (`/sitemap/searches/locations.xml`) and matches `<slug>_g<id>`, e.g.
  Lahore → `4060673`.
- **Zameen** — reads Zameen's own city links to find `<City>-<id>`, e.g.
  Lahore → `Lahore-1`.
- **PakWheels** — uses the lowercased city name directly (`ct_lahore`).

If a lookup fails the city is **skipped with a warning** rather than guessed —
a wrong location id would quietly collect a different city's listings. Set the
value manually in `cities.yml` to override.

---

## Configuring categories

Categories live per source in `config/settings.yml`. OLX covers the general
marketplace with 14 verified categories:

| Group | Categories |
|---|---|
| Vehicles | Cars, Motorcycles, Car Parts & Accessories |
| Mobiles & computing | Mobile Phones, Tablets, Mobile Accessories, Computers & Accessories |
| Electronics | Electronics & Home Appliances, TV/Video/Audio, Cameras & Accessories |
| General | Furniture & Home Decor, Fashion & Beauty, Watches, Games & Entertainment |

PakWheels adds Used Cars and Used Bikes; Zameen covers property (Homes, Plots,
Commercial, Rentals).

**Never hand-write an OLX category slug.** A wrong category id returns an
empty page rather than an error, so a typo fails silently. (This bit us once:
`electronics-home-appliances` is `_c99`, not `_c136`.) Sync the full list from
OLX's own sitemap instead:

```bash
python run.py sync-categories
```

That reads OLX's own category tree out of the homepage's
`window.state.categories.data` - the same tree its menu renders - and writes
it to `config/olx_categories.json`, which the config loader merges in
automatically. The result is **894 categories across 14 sections**:

```
Furniture & Home Decor              172
Electronics & Home Appliances       141
Business, Industrial & Agriculture  110
Vehicles                             92
Books, Sports & Hobbies              71
Bikes                                70
Fashion & Beauty                     53
Animals                              51
Services                             46
Jobs                                 28
Mobiles                              26
Kids                                 19
Property for Rent                     9
Property for Sale                     6
```

Each entry carries `group` (its top-level section), `level` (0 for the section
itself, 1+ for nesting) and `priority` (OLX's own ordering), which is what lets
the picker show real sections instead of one flat list. Labels come from OLX,
so they read properly - "Mobile Phones", not a guess made from the slug.

The 14 curated entries in `settings.yml` keep their hand-written labels but
adopt the section the sync found, so they sort with their siblings.

Two details the sync handles:

- A slug is reused across two different ids - `houses` is both c1719 (for
  sale) and c1721 (for rent). Those keys get the id appended
  (`houses-c1719`) so a selection is never ambiguous.
- If the tree ever moves, the sync falls back to the categories sitemap. That
  still yields every category id, but flat - no sections.

Re-run `sync-categories` whenever OLX adds categories. Confirm a new one
returns data before relying on it:

```bash
python run.py run --sources olx --categories <key> --cities Lahore --limit 5
```

Zero listings means the slug is wrong.

## Data model

Every collector returns the same record, regardless of source:

```python
Listing(
    source, source_listing_id, city, category,
    title, description, price, price_currency, price_raw,
    phone, ad_date, url, scraped_at,
)
```

Stored in the `listings` table:

| Column | Notes |
|---|---|
| `id` | autoincrement PK |
| `source` | `olx` / `pakwheels` / `zameen` |
| `source_listing_id` | the site's own ad id |
| `fingerprint` | dedup key (see below) |
| `city`, `category` | |
| `title`, `description` | |
| `price`, `price_currency`, `price_raw` | `price_raw` keeps the text as shown |
| `phone` | see the note below |
| `ad_date` | |
| `url`, `url_canonical` | canonical form has tracking params stripped |
| `first_seen_at` | set once, never overwritten |
| `last_seen_at` | refreshed every time the ad is seen again |
| `scraped_at` | |

Indexes: unique on `fingerprint`, unique on `(source, source_listing_id)`,
unique on `(source, url_canonical)`, plus lookups on `city`,
`(source, city)`, `(city, category)`, `ad_date`, `scraped_at`, `last_seen_at`.

Two audit tables come along: `scrape_runs` (what ran, what it produced) and
`rejected_listings` (records that failed validation, with the reasons).

### Duplicate prevention

The `fingerprint` is, in order of preference:

1. `source:id:<source_listing_id>` — used whenever the site exposes an ad id
2. `source:url:<canonical url>` — when it does not
3. `source:hash:<content hash>` — last resort

Before inserting, the database also looks the record up by
`(source, url_canonical)`. That second lookup is what stops an ad from being
stored twice when it was first seen without a visible id and later with one —
the existing row is upgraded in place instead.

On re-scrape the row is refreshed, but a thinner scrape never erases richer
data: a shorter description, or a newly-missing price or phone, leaves the
stored value intact.

---

## About the phone number field

Coverage differs sharply by site, because the sites themselves differ in what
they publish. Measured on a live sample:

| Source | Phone coverage | Why |
|---|---|---|
| **Zameen** | 60/60 (100%) | Serves the contact number in the public page payload |
| **OLX** | 0% | Number is not in the payload at all |
| **PakWheels** | 0% | Number is behind a logged-in reveal call |

Zameen was measured across all four configured categories (Homes, Plots,
Commercial, Rentals) in Lahore, Karachi and Islamabad — 100% in every one of
the twelve city/category combinations, collected fully automatically.

**Zameen** renders the number to every visitor, and its payload carries a
`requiresLoginForContact` flag saying so. The collector reads
`property.data.primaryPhoneNumber` / `phoneNumber.mobileNumbers[]`, and
**honours that flag** — if a listing says a login is required, the number is
left empty rather than reached for.

**OLX** genuinely does not include the number. Its payload carries only:

```json
"contactInfo": {"roles": ["show_phone_number"], "name": "Tahir Javed"}
```

That is a capability flag and a display name — the number itself is served by a
separate authenticated endpoint. **PakWheels** is the same: a logged-in
"Show Phone Number" call.

This project does not log in and drive those reveal endpoints. Bulk-harvesting
seller contact details is prohibited by all three sites' terms, implicates
PECA 2016, and in practice gets the account and IP banned quickly. So for OLX
and PakWheels the only number that lands in the table is one the seller typed
into their own ad description — which is uncommon, since both sites strip
contact details out of ad text.

If you need OLX or PakWheels numbers at volume, the route is commercial, not
technical: OLX Group / Dubizzle Pakistan and PakWheels both run dealer and data
partnership programmes that provide lead access under contract.

Set `collection.collect_phone: false` in `settings.yml` to drop the field
entirely.

---

## Being a good citizen

`config/settings.yml` controls this and the defaults are deliberate:

```yaml
http:
  user_agent: "..."        # put a real contact URL/email in here
  delay_seconds: 3.0       # minimum gap between requests to the same host
  jitter_seconds: 2.0      # randomised on top
  respect_robots: true     # skip anything robots.txt disallows
  cache_responses: true    # re-parse without re-fetching
```

Two things follow from `respect_robots: true`:

- OLX's `robots.txt` disallows `/api/`, so its JSON search endpoint is not
  used. The collector reads the `window.state` blob embedded in the HTML
  category pages instead — which happens to be better anyway: one request
  returns ~25 complete ads.
- PakWheels and Zameen need a detail fetch per listing for the full
  description, so they are meaningfully slower. Budget roughly 4-5 seconds
  per listing.

`collection.max_pages_per_city_category`, `max_listings_per_city_category` and
`max_listings_per_run` are hard ceilings so an unattended run cannot spiral.

---

## When a site changes its markup

Classified sites reshuffle their HTML regularly. Run this first:

```bash
python run.py verify --city Lahore
```

It fetches one index page per source and prints exactly what resolved — the
location id, how many listings parsed, and a fully-parsed sample row:

```
--- pakwheels / Lahore ---
  location   : lahore
  parsed     : 25 listings
  sample id  : 12017889
  price      : 6150000
  ad_date    : 2026-09-19
  desc chars : 992
  validation : OK
```

`parsed: 0` means that source's index selectors need updating. Each collector
keeps its selectors in named constants at the top of its `collector.py`, and
every field has fallbacks — structured data (JSON-LD or the site's embedded
state blob) first, CSS selectors second — so one dead selector degrades a
single field instead of breaking the run.

Extraction strategy per site:

| Site | Index | Detail |
|---|---|---|
| OLX | `window.state.algolia.content.hits[]` → anchor fallback | `window.state.ad.data` → JSON-LD fallback |
| PakWheels | `a.car-name` + JSON-LD `Product` prices | JSON-LD `Car`/`Product` → CSS |
| Zameen | `/Property/…-<id>-…html` links | JSON-LD → `window.state` → CSS |

---

## Tests

```bash
python -m unittest discover -s tests
```

39 offline tests, no network. They cover price parsing (including `4.5 Crore`
and `85 Lakh`), relative and absolute dates, phone normalisation and its false
positives, Zameen's published-contact shapes and its login gate, the dedup key,
validation rules, and the Excel export — including that a description beginning
with `=` is neutralised rather than evaluated as a formula when the workbook is
opened.

---

## Excel output

`exports/listings_<timestamp>.xlsx`:

- **Summary** — listings / with-price / with-phone / average price / date range,
  broken down by city and source
- **All Listings** — every row, filterable
- **One sheet per city** — the city-wise view

Prices are numeric and formatted, URLs are clickable, headers are frozen and
auto-filtered.

---

## Known limits

- `ad_date` is the posted-or-updated date as each site reports it; PakWheels
  exposes "Last Updated" rather than a true posted date.
- OLX leaves its top-level `price` at `0` for vehicles and property; the real
  figure is read from `extraFields.price`. A genuinely price-less ad stores
  `NULL` and keeps its original text in `price_raw`.
- Categories are configured per site in `settings.yml` and do not map 1:1
  across sites — `cars` on OLX and `used-cars` on PakWheels are different
  category keys covering the same market.
- Only listings reachable from the configured category pages are collected;
  there is no keyword search mode.
