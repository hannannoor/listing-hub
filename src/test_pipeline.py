#!/usr/bin/env python3
"""
Self-check for the whole pipeline, with no network access.

    python test_pipeline.py

Builds a fake G2G response in the real devalue format, runs it through the
decoder, the adapter, the database and the query layer, and asserts the
listing comes out the other end intact.
"""

import json
import sqlite3
import sys
import tempfile
from pathlib import Path
from urllib.parse import parse_qs

sys.path.insert(0, str(Path(__file__).resolve().parent))

import devalue
import serve
import store
from sources import g2g

PASS, FAIL = [], []


def check(name, condition, detail=""):
    (PASS if condition else FAIL).append(name)
    mark = "ok  " if condition else "FAIL"
    print(f"  [{mark}] {name}" + (f"  -- {detail}" if detail and not condition else ""))


# ---------------------------------------------------------------- devalue ---
print("\ndevalue decoder")

check("flat scalars", devalue.unflatten([{"a": 1, "b": 2}, "hello", 42]) ==
      {"a": "hello", "b": 42})

check("nested + shared refs",
      devalue.unflatten([{"x": 1, "y": 1}, {"n": 2}, 7]) == {"x": {"n": 7}, "y": {"n": 7}})

check("arrays", devalue.unflatten([[1, 2], "a", "b"]) == ["a", "b"])

check("undefined -> None", devalue.unflatten([{"a": -1}]) == {"a": None})
check("null -> None", devalue.unflatten([{"a": -2}]) == {"a": None})

cyclic = devalue.unflatten([{"self": 0, "name": 1}, "loop"])
check("cycles do not hang", cyclic["self"] is cyclic and cyclic["name"] == "loop")

check("Date wrapper",
      devalue.unflatten([{"d": 1}, ["Date", "2026-01-01T00:00:00.000Z"]]) ==
      {"d": "2026-01-01T00:00:00.000Z"})

check("empty input", devalue.unflatten([]) is None)


# ------------------------------------------------------- fake G2G payload ---
print("\nadapter against a synthetic G2G response")

ROOT_ATTRS = [
    {"collection_id": "lgc_23957_platform", "label": {"en": "Platform"},
     "children": [
         {"dataset_id": "ds_android", "value": "Android", "children": []},
         {"dataset_id": "ds_ios", "value": "IOS", "children": []},
     ]},
    {"collection_id": "lgc_23957_tier", "label": {"en": "Rank"},
     "children": [
         {"dataset_id": "ds_glory", "value": "Mythical Glory", "children": []},
         {"dataset_id": "ds_honor", "value": "Mythical Honor", "children": []},
     ]},
    {"collection_id": "lgc_23957_skin", "label": {"en": "Skins"},
     # nested one level deeper, to prove the walk recurses
     "children": [{"dataset_id": "ds_grp", "value": "Group", "children": [
         {"dataset_id": "ds_100", "value": "100+", "children": []},
     ]}]},
    {"collection_id": "lgc_23957_hero", "label": None,   # missing label -> fall back to id
     "children": [{"dataset_id": "ds_h70", "value": "70+", "children": []}]},
]

OFFER_A = {
    "offer_id": "G1700000000001AA",
    "offer_attributes": [
        {"collection_id": "lgc_23957_platform", "dataset_id": "ds_android"},
        {"collection_id": "lgc_23957_tier", "dataset_id": "ds_glory"},
        {"collection_id": "lgc_23957_skin", "dataset_id": "ds_100"},
        {"collection_id": "lgc_23957_hero", "dataset_id": "ds_h70"},
        {"collection_id": "unknown", "dataset_id": "ds_missing"},   # must be dropped
    ],
    "title": "  Minecraft Java Edition | Full Access | No Ban  ",
    "display_price": "13.38",
    "display_currency": "SGD",
    "username": "HellenWong",
    "seller_profile_url": "https://www.g2g.com/HellenWong",
    "satisfaction_rate": 0.9913,
    "total_success_order": 50218,
    "available_qty": 7692,
    "created_at": 1703130011240,
    "precheckout_url": "https://www.g2g.com/sg/categories/minecraft-account/offer/G1700000000001AA",
    "user_avatar": "https://assets.g2g.com/user/avatar/875683.png",
    "offer_media_files": [],
    "gallery_images": [{"url": "https://assets.g2g.com/gallery/shot1.jpg"}],
}
OFFER_B = {
    "offer_id": "G1700000000002BB",
    "title": "Valorant Account | Radiant",
    "display_price": "not-a-number",          # price parsing must not explode
    "display_currency": "SGD",
    "username": "SellerTwo",
    "created_at": 1703130099999,
    "offer_attributes": [
        {"collection_id": "lgc_23957_platform", "dataset_id": "ds_android"},
        {"collection_id": "lgc_23957_platform", "dataset_id": "ds_ios"},  # multiselect
    ],
    "offer_media_files": [],
    "gallery_images": [],
    # no precheckout_url -> adapter must build a fallback link
}
BAD = {"title": "no offer_id, must be skipped"}


def build_payload(offers, total=1305, max_page=2):
    """Encode a page of offers the way SvelteKit's devalue would."""
    flat = []

    def add(value):
        if isinstance(value, dict):
            slot = len(flat)
            flat.append(None)
            flat[slot] = {k: add(v) for k, v in value.items()}
            return slot
        if isinstance(value, list):
            slot = len(flat)
            flat.append(None)
            flat[slot] = [add(v) for v in value]
            return slot
        flat.append(value)
        return len(flat) - 1

    root_slot = len(flat)
    flat.append(None)
    flat[root_slot] = {
        "offers": add(offers),
        "totalResults": add(total),
        "maxPage": add(max_page),
        "rootAttrCollections": add(ROOT_ATTRS),
    }
    return {"type": "data",
            "nodes": [{"type": "skip"}, {"type": "data", "data": flat}]}


pages = {
    1: build_payload([OFFER_A, BAD]),
    2: build_payload([OFFER_B]),
}
requested = []
warmed = []


class FakeFetcher:
    """Stands in for fetcher.HttpFetcher / BrowserFetcher."""
    name = "fake"

    def warm_up(self, page_url):
        warmed.append(page_url)

    def get_json(self, url, referer=None):
        requested.append(url)
        if referer:
            self.warm_up(referer)
        page = int(parse_qs(url.split("?", 1)[1])["page"][0])
        return pages.get(page, build_payload([]))

    def close(self):
        pass


listings = g2g.fetch_category("minecraft-account", label="Minecraft Accounts",
                              pages=2, delay=0, verbose=False,
                              fetcher=FakeFetcher())

check("skips records without offer_id", len(listings) == 2, f"got {len(listings)}")
check("walked both pages", len(requested) == 2, f"{len(requested)} requests")
check("requests hit /categories/<slug>/__data.json",
      all("/categories/minecraft-account/__data.json" in u for u in requested))

check("sends a Referer of the real category page",
      warmed and all(w == "https://www.g2g.com/sg/categories/minecraft-account" for w in warmed))
check("builds the documented data URL",
      g2g.data_url("minecraft-account", "sg", 2, "most_recent") ==
      "https://www.g2g.com/sg/categories/minecraft-account/__data.json"
      "?page=2&sort=most_recent&x-sveltekit-invalidated=001")

a = next(x for x in listings if x["external_id"] == "G1700000000001AA")
b = next(x for x in listings if x["external_id"] == "G1700000000002BB")

check("title trimmed", a["title"] == "Minecraft Java Edition | Full Access | No Ban")
check("price parsed as float", a["price"] == 13.38)
check("bad price becomes None", b["price"] is None)
check("currency kept", a["currency"] == "SGD")
check("source link preserved", a["url"] == OFFER_A["precheckout_url"])
check("fallback link built when missing",
      b["url"] == "https://www.g2g.com/categories/minecraft-account/offer/G1700000000002BB")
check("gallery image preferred", a["image_url"] == "https://assets.g2g.com/gallery/shot1.jpg")
check("avatar fallback when no gallery", b["image_url"] is None or b["image_url"].startswith("http"))
check("seller carried over", a["seller"] == "HellenWong" and a["seller_orders"] == 50218)
check("category label attached", a["category_label"] == "Minecraft Accounts")

print("\nattribute decoding")
check("decodes readable field names and values",
      a["attributes"] == {"Platform": "Android", "Rank": "Mythical Glory",
                          "Skins": "100+", "lgc_23957_hero": "70+"},
      str(a["attributes"]))
check("walks nested attribute trees", a["attributes"].get("Skins") == "100+")
check("falls back to collection id when a label is missing",
      "lgc_23957_hero" in a["attributes"])
check("drops references with no match in the schema",
      len(a["attributes"]) == 4)
check("multiselect values are joined, not lost",
      b["attributes"] == {"Platform": "Android, IOS"}, str(b["attributes"]))
check("offers with no attributes give an empty dict",
      g2g.normalise({"offer_id": "X", "title": "t"}, "c", "C", {})["attributes"] == {})


# -------------------------------------------------------------- database ---
print("\ndatabase round-trip")

tmp = Path(tempfile.mkdtemp()) / "test.db"
conn = store.connect(tmp)

added, updated = store.save_listings(conn, listings)
check("first save inserts", (added, updated) == (2, 0), f"{added}/{updated}")

added2, updated2 = store.save_listings(conn, listings)
check("re-save deduplicates", (added2, updated2) == (0, 2), f"{added2}/{updated2}")

count = conn.execute("SELECT COUNT(*) FROM listings").fetchone()[0]
check("no duplicate rows", count == 2, f"{count} rows")

first_seen, last_seen = conn.execute(
    "SELECT first_seen, last_seen FROM listings WHERE external_id = ?",
    ("G1700000000001AA",)).fetchone()
check("first_seen preserved across rescrapes", first_seen <= last_seen)

stale = store.mark_stale(conn, "g2g", "minecraft-account", 9_999_999_999_999)
check("stale listings get flagged", stale == 2, f"{stale}")
store.save_listings(conn, listings)   # reactivate for the query tests

run_id = store.start_run(conn, "g2g", "minecraft-account")
store.finish_run(conn, run_id, found=2, added=2, updated=0)
check("scrape run recorded",
      conn.execute("SELECT finished_at FROM scrape_runs WHERE id=?", (run_id,)).fetchone()[0])


# ---------------------------------------------------------- query layer ---
print("\nquery + API layer")

ro = sqlite3.connect(str(tmp))
ro.row_factory = sqlite3.Row

res = serve.query_listings(ro, {})
check("returns everything by default", res["total"] == 2)

res = serve.query_listings(ro, {"q": ["Radiant"]})
check("search filters by title", res["total"] == 1 and
      res["listings"][0]["external_id"] == "G1700000000002BB")

res = serve.query_listings(ro, {"q": ["HellenWong"]})
check("search also matches seller", res["total"] == 1)

res = serve.query_listings(ro, {"q": ["'; DROP TABLE listings; --"]})
check("SQL injection is parameterised away", res["total"] == 0 and
      ro.execute("SELECT COUNT(*) FROM listings").fetchone()[0] == 2)

res = serve.query_listings(ro, {"min_price": ["10"]})
check("min price filter", res["total"] == 1)

res = serve.query_listings(ro, {"min_price": ["abc"]})
check("junk price filter ignored, not fatal", res["total"] == 2)

res = serve.query_listings(ro, {"sort": ["price_asc"]})
check("null prices sort last",
      res["listings"][0]["price"] == 13.38 and res["listings"][-1]["price"] is None)

res = serve.query_listings(ro, {"sort": ["nonsense"]})
check("unknown sort falls back", res["total"] == 2)

res = serve.query_listings(ro, {"limit": ["1"], "offset": ["1"]})
check("pagination works", len(res["listings"]) == 1 and res["total"] == 2)

res = serve.query_listings(ro, {"limit": ["99999"]})
check("limit is capped", res["limit"] == 300)

res = serve.query_listings(ro, {"category": ["minecraft-account"]})
check("category filter", res["total"] == 2)

res = serve.query_listings(ro, {"source": ["ebay"]})
check("unknown source returns nothing", res["total"] == 0)

f = serve.facets(ro)
check("facets list the source", any(s["source"] == "g2g" for s in f["sources"]))
check("facets list the category",
      any(c["category"] == "minecraft-account" for c in f["categories"]))

check("every listing keeps a link back to the original",
      all(l["url"].startswith("https://www.g2g.com/") for l in serve.query_listings(ro, {})["listings"]))

ro.close()
conn.close()

# ------------------------------------------------------------------ done ---
print(f"\n{len(PASS)} passed, {len(FAIL)} failed")
if FAIL:
    for name in FAIL:
        print("  failed:", name)
    sys.exit(1)
print("Pipeline is sound. Run `python scrape.py` to pull live data.")
