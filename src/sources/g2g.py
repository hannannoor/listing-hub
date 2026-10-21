"""
G2G.com source adapter.

G2G's site is a SvelteKit app, and SvelteKit publishes every page's data as JSON
at <page-url>/__data.json -- the same data the page renders. So this reads their
own structured feed instead of parsing HTML that changes with every restyle.

G2G sits behind Cloudflare, which refuses plain scripted requests. The `fetcher`
module handles that: it warms up a session on the real category page and, if
that is still blocked, drives an actual browser. See fetcher.py.

g2g.com/robots.txt explicitly Allows /categories/ and /offer/ paths (and names
ClaudeBot among permitted agents). We stay inside that and keep the rate low.
"""

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from devalue import parse_sveltekit_data  # noqa: E402
from fetcher import get_fetcher           # noqa: E402

SOURCE = "g2g"
BASE = "https://www.g2g.com"

PAGE_SIZE = 48          # what G2G returns per page
MAX_PAGE_CAP = 32       # G2G stops paginating past this
DELAY_SECONDS = 1.5     # gap between requests


def category_url(seo_term, region="sg"):
    return f"{BASE}/{region}/categories/{seo_term}"


def data_url(seo_term, region="sg", page=1, sort="most_recent"):
    return (f"{category_url(seo_term, region)}/__data.json"
            f"?page={page}&sort={sort}&x-sveltekit-invalidated=001")


def build_attr_lookup(data, lookup=None):
    """
    Map G2G's opaque dataset_ids to readable name/value pairs.

    Each category response carries `rootAttrCollections`: the schema for that
    category's attributes. A collection is one field ("Rank"), and its children
    are the possible values ("Mythical Glory"), each with a dataset_id. Offers
    then reference those ids. This builds {dataset_id: (field, value)}.
    """
    lookup = {} if lookup is None else lookup

    def walk(nodes, label):
        for node in nodes or []:
            if not isinstance(node, dict):
                continue
            dataset_id = node.get("dataset_id")
            value = node.get("value")
            if dataset_id and value is not None:
                lookup[dataset_id] = (label, value)
            walk(node.get("children"), label)

    for collection in data.get("rootAttrCollections") or []:
        if not isinstance(collection, dict):
            continue
        label = collection.get("label")
        label = (label or {}).get("en") if isinstance(label, dict) else label
        label = label or collection.get("collection_id") or "Attribute"
        walk(collection.get("children"), label)

    return lookup


def decode_attributes(offer, lookup):
    """Turn an offer's attribute references into {"Rank": "Mythical Glory", ...}."""
    out = {}
    for attr in offer.get("offer_attributes") or []:
        if not isinstance(attr, dict):
            continue
        hit = lookup.get(attr.get("dataset_id"))
        if not hit:
            continue
        label, value = hit
        # A multi-select field can appear more than once; keep every value.
        out[label] = f"{out[label]}, {value}" if label in out else value
    return out


def _pick_image(offer):
    for key in ("gallery_images", "offer_media_files"):
        for entry in offer.get(key) or []:
            if isinstance(entry, str) and entry.startswith("http"):
                return entry
            if isinstance(entry, dict):
                for field in ("url", "image_url", "file_path", "path", "src"):
                    val = entry.get(field)
                    if isinstance(val, str) and val.startswith("http"):
                        return val
    # Many G2G list views carry no product image at all. Returning None lets the
    # viewer render a compact text card instead of a large empty box.
    return None


def _to_float(value):
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def normalise(offer, category_key, category_label, attr_lookup=None):
    """Turn one raw G2G offer into our shared listing shape."""
    offer_id = offer.get("offer_id")
    if not offer_id:
        return None

    url = offer.get("precheckout_url") or \
        f"{BASE}/categories/{category_key}/offer/{offer_id}"

    return {
        "source": SOURCE,
        "external_id": str(offer_id),
        "category": category_key,
        "category_label": category_label,
        "title": (offer.get("title") or "Untitled listing").strip(),
        "url": url,
        "price": _to_float(offer.get("display_price")),
        "currency": offer.get("display_currency"),
        "image_url": _pick_image(offer),
        "seller": offer.get("username"),
        "seller_url": offer.get("seller_profile_url"),
        "seller_rating": _to_float(offer.get("satisfaction_rate")),
        "seller_orders": offer.get("total_success_order"),
        "available_qty": offer.get("available_qty"),
        "posted_at": offer.get("created_at"),
        "attributes": decode_attributes(offer, attr_lookup or {}),
        "raw": offer,
    }


def fetch_category(seo_term, label=None, region="sg", pages=3,
                   sort="most_recent", delay=DELAY_SECONDS, verbose=True,
                   fetcher=None):
    """
    Pull listings for one G2G category.

    seo_term -- the slug from a category URL, e.g. "mobile-legends-account"
    pages    -- how many pages of 48 to walk. G2G caps pagination at 32.
    sort     -- most_recent | lowest_price | highest_price | recommended_v4
    fetcher  -- a fetcher from fetcher.py; one is created if not supplied.
    """
    label = label or seo_term.replace("-", " ").title()
    own_fetcher = fetcher is None
    fetcher = fetcher or get_fetcher("auto")
    referer = category_url(seo_term, region)
    results = []
    attr_lookup = {}
    pages = min(pages, MAX_PAGE_CAP)

    try:
        for page in range(1, pages + 1):
            if verbose:
                print(f"  page {page}/{pages} ...", end=" ", flush=True)

            payload = fetcher.get_json(
                data_url(seo_term, region, page, sort), referer=referer
            )
            data = parse_sveltekit_data(payload)
            build_attr_lookup(data, attr_lookup)   # schema ships with every page
            offers = data.get("offers") or []

            if not offers:
                if verbose:
                    print("no offers, stopping")
                break

            for offer in offers:
                if isinstance(offer, dict):
                    item = normalise(offer, seo_term, label, attr_lookup)
                    if item:
                        results.append(item)

            if verbose:
                total = data.get("totalResults")
                print(f"{len(offers)} offers" + (f" (of ~{total} total)" if total else ""))

            max_page = data.get("maxPage")
            if isinstance(max_page, int) and page >= max_page:
                if verbose:
                    print(f"  reached last page ({max_page})")
                break

            if page < pages:
                time.sleep(delay)
    finally:
        if own_fetcher:
            fetcher.close()

    return results
