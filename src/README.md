# Listing Hub

Pulls listings from marketplaces into one local database and shows them on a
single page. Every card links straight back to the original listing on the
source site.

Right now there is one source, **G2G**. The whole point of the layout is that
adding a second one is a small, self-contained job — see *Adding another site*.

No dependencies. Python 3.9+ and the standard library, nothing to `pip install`.

---

## Quick start

```bash
cd listing-hub

python scrape.py             # fetch listings into listings.db
python find.py               # show the filtered results
```

`find.py` is the data tool: it prints matching listings and their links, and
can write CSV or JSON. `serve.py` is an optional browser view of the same data
-- ignore or delete it if you do not want it.

```bash
python find.py --max-price 30 --limit 15   # what config.json already defaults to
python find.py --csv cheap.csv             # for Excel
python find.py --json cheap.json           # for feeding something else
python find.py --links                     # just URLs, one per line
python find.py --search "skin" --sort rating
python find.py --show-attrs                # every attribute field + value, with counts
python find.py --attr Rank="Mythical Glory" --attr Skins=500+
python test_pipeline.py                    # 44 self-checks, no network needed
```

First scrape takes about a minute for the three categories in `config.json`
(deliberately slow — see *Being a good guest*).

---

## Choosing what to track

`config.json` drives everything:

```json
{
  "region": "sg",
  "pages_per_category": 3,
  "sort": "most_recent",
  "delay_seconds": 1.5,
  "categories": [
    { "source": "g2g", "slug": "mobile-legends-account", "label": "ML Accounts" }
  ],
  "filters": { "max_price": 30, "limit": 15, "sort": "price_asc" }
}
```

`categories` controls what gets **scraped**; `filters` sets the defaults
`find.py` uses when it **reads**. They are separate on purpose -- the database
keeps everything it fetched, so you can change filters without re-scraping.

If you only ever want cheap listings, set the scrape `sort` to `lowest_price`.
Then even one page is full of things under your cap, instead of hoping enough
cheap ones show up among the most recent.

To find a `slug`, open a category on g2g.com and copy the last part of the URL:

```
https://www.g2g.com/sg/categories/minecraft-account
                                  ^^^^^^^^^^^^^^^^^ this
```

`python scrape.py --list-categories` prints G2G's top-level tree to get you
started.

Each page is 48 listings, so `pages_per_category: 3` gives ~144 per category.
G2G stops paginating at page 32, so about 1,500 listings per category is the
practical ceiling.

Useful flags:

```bash
python scrape.py --pages 10                # deeper pull, this run only
python scrape.py --only minecraft-account  # refresh one category
python serve.py --port 9000 --no-browser
```

---

## Listing attributes

Each category defines its own structured fields -- for Mobile Legends those are
Platform, Rank, Heros and Skins. G2G stores them on an offer as opaque id pairs,
and ships the schema that decodes them (`rootAttrCollections`) in the *same*
response as the listings, so capturing them costs no extra requests.

They land in the `attributes` column as JSON, and show up automatically:

```bash
python find.py --show-attrs        # what fields exist, and every value, with counts
python find.py --attr Rank="Mythical Glory"
python find.py --attr Skins=500+ --attr Platform=Android --max-price 30
```

Attribute matching is case-insensitive and matches on substrings, so
`--attr Rank=glory` finds "Mythical Glory". In CSV exports each attribute
becomes its own column; in JSON they stay nested under `attributes`.

Fields differ per category, which is why nothing here is hardcoded -- scrape a
different game and `--show-attrs` will show you whatever *that* category uses.

What is **not** captured is the free-text description on an individual offer
page (the seller's own blurb, screenshots, delivery notes). Those live on each
offer's own page, so collecting them would mean one extra request per listing.
Say the word if you want that -- it is a small addition, just a slower scrape.

---

## How it works

```
config.json ──> scrape.py ──> sources/g2g.py ──> devalue.py
                    │              (fetch)        (decode)
                    v
                 store.py ──> listings.db
                                   │
                              serve.py ──> index.html
```

G2G's site is built with SvelteKit, and SvelteKit publishes each page's data as
JSON at `<page-url>/__data.json`. That is the same data the page renders — so
instead of scraping HTML (which breaks whenever they restyle anything), the
adapter reads their own structured feed. It comes back in SvelteKit's compact
"devalue" encoding, which `devalue.py` unpacks.

**`listings.db`** keeps one row per listing, keyed on `source:external_id`.
Re-running the scraper updates existing rows rather than duplicating them, and
tracks `first_seen` / `last_seen`. Listings that disappear from the source get
`is_active = 0` rather than being deleted, so you keep the history.

---

## Adding another site

Everything site-specific lives in one file. To add, say, PlayerAuctions:

1. Write `sources/playerauctions.py` with a `fetch_category(...)` that returns
   a list of dicts in the shape below.
2. Register it in `scrape.py`: `ADAPTERS = {"g2g": g2g, "playerauctions": pa}`.
3. Add entries to `config.json` with `"source": "playerauctions"`.

The viewer, the database, the filters and the source dropdown all pick it up
with no further changes. The shared shape:

```python
{
  "source": "playerauctions",      # required
  "external_id": "12345",          # required, unique within the source
  "category": "minecraft-accounts",# required, your own slug
  "category_label": "Minecraft Accounts",
  "title": "...",                  # required
  "url": "https://...",            # required — link back to the original
  "price": 13.38, "currency": "USD",
  "image_url": None,
  "seller": "...", "seller_url": "...",
  "seller_rating": 0.99,           # 0..1
  "seller_orders": 50218,
  "available_qty": 12,
  "posted_at": 1703130011240,      # unix ms
  "raw": {...},                    # original record, stored for debugging
}
```

Only the first five are required; everything else degrades gracefully in the UI.

---

## Being a good guest

This pulls public listing pages that `g2g.com/robots.txt` explicitly allows
crawlers to read, and it links every listing back to the source rather than
replacing it. It is deliberately conservative:

- 1.5 seconds between requests, with exponential backoff on 429/5xx
- an honest User-Agent that says what the tool is (edit it in `sources/g2g.py`)
- no login, no checkout pages, no user account paths — those are all `Disallow`ed
- reads their own JSON feed instead of hammering rendered pages

Please keep it that way. If you crank `delay_seconds` down and run it in a loop,
you will get blocked, and reasonably so.

**One thing to know before this ever becomes a public site.** Right now it runs
on your machine for your own use, which is the low-risk end of things. Putting
it on a public URL is a different question: `robots.txt` governs crawling, not
republishing, and G2G's Terms of Service are the document that matters for
showing their listing data to other people. Worth actually reading that page —
and considering reaching out to them — before you make it public. Aggregators
that drive traffic *to* a marketplace are often welcome; it is much better to
ask first than to find out by takedown notice.

---

## Files

| File | What it does |
|---|---|
| `config.json` | Which categories to track |
| `scrape.py` | Runner — walks config, calls adapters, saves results |
| `sources/g2g.py` | G2G adapter (the only site-specific code) |
| `devalue.py` | Decoder for SvelteKit's flat-JSON format |
| `store.py` | SQLite schema, upsert, dedupe, run history |
| `find.py` | Query the database: table, CSV, JSON, or links |
| `serve.py` | Optional browser view (not needed if you use find.py) |
| `index.html` | The viewer — search, filter, sort, load more |
| `fetcher.py` | Getting past Cloudflare (HTTP session, then real browser) |
| `test_pipeline.py` | 44 offline self-checks |
| `listings.db` | Created on first scrape |

---

## Troubleshooting

**`No listings.db yet`** — run `python scrape.py` first.

**Scrape returns 0 offers** — the slug is probably wrong. Open the category on
g2g.com and re-copy the last URL segment. `--list-categories` helps.

**`HTTP 403`** — G2G sits behind Cloudflare, which refuses plain scripted
requests. Find out what your machine can do:

```bash
python scrape.py --diagnose
```

It tries each route and tells you which works. Two possible answers:

- *Plain HTTP works* — run `python scrape.py --mode http`, no install needed.
- *Only the browser route works* — install it once:

  ```bash
  pip install playwright
  python -m playwright install chromium
  ```

  Then plain `python scrape.py` handles it automatically. It drives a real
  headless Chromium and lets the page fetch its own data, which is exactly what
  happens when you click around the site yourself.

`--mode` lets you force either route: `auto` (default, tries HTTP then falls
back to the browser), `http`, or `browser`.

If **both** routes fail, you are probably rate-limited. Raise `delay_seconds`,
pull fewer pages, and wait a while before retrying.

**Everything breaks after a G2G redesign** — most likely the field names in
`normalise()` in `sources/g2g.py` moved. Print one raw offer to see the new
shape; `raw` is stored in the database for exactly this reason:

```bash
python -c "import store,json; c=store.connect(); print(json.dumps(json.loads(c.execute('select raw from listings limit 1').fetchone()[0]),indent=2))"
```
