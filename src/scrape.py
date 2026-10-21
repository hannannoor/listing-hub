#!/usr/bin/env python3
"""
Pull listings from every configured category into listings.db.

    python scrape.py                 # use config.json
    python scrape.py --pages 5       # override page depth
    python scrape.py --only mobile-legends-account
    python scrape.py --diagnose      # find out what the site will let through
    python scrape.py --list-categories

Standard library only, unless the site forces the browser fallback --
see --diagnose.
"""

import argparse
import json
import sys
import time
from pathlib import Path

import store
from fetcher import BlockedError, FetchError, get_fetcher
from sources import g2g

HERE = Path(__file__).parent
CONFIG_PATH = HERE / "config.json"

ADAPTERS = {
    "g2g": g2g,          # add more sources here as you write them
}


def load_config():
    if not CONFIG_PATH.exists():
        sys.exit(f"Missing {CONFIG_PATH}. Copy the sample config back in.")
    try:
        with open(CONFIG_PATH, encoding="utf-8") as fh:
            return json.load(fh)
    except json.JSONDecodeError as exc:
        sys.exit(f"config.json has a syntax error on line {exc.lineno}: {exc.msg}\n"
                 "Usually a missing or extra comma.")


def diagnose(config):
    """Try each strategy against a real category and report what works."""
    cats = config.get("categories") or [{"slug": "mobile-legends-account"}]
    slug = cats[0]["slug"]
    region = config.get("region", "sg")
    url = g2g.data_url(slug, region, 1, config.get("sort", "most_recent"))
    referer = g2g.category_url(slug, region)

    print(f"Testing against: {referer}\n")
    verdict = []

    for mode, blurb in (("http", "plain HTTP with a warmed-up session"),
                        ("browser", "real browser via Playwright")):
        print(f"[{mode}] {blurb}")
        fetcher = None
        try:
            fetcher = get_fetcher(mode)
            payload = fetcher.get_json(url, referer=referer)
            data = g2g.parse_sveltekit_data(payload)
            n = len(data.get("offers") or [])
            print(f"  WORKS -- {n} offers, ~{data.get('totalResults')} total\n")
            verdict.append(mode)
        except BlockedError as exc:
            print(f"  blocked: {exc}\n")
        except FetchError as exc:
            print(f"  unavailable: {exc}\n")
        except Exception as exc:                       # noqa: BLE001
            print(f"  error: {type(exc).__name__}: {exc}\n")
        finally:
            if fetcher:
                fetcher.close()

    print("-" * 60)
    if "http" in verdict:
        print("Plain HTTP works. Run:  python scrape.py --mode http")
    elif "browser" in verdict:
        print("The browser route works. Run:  python scrape.py")
        print("(--mode auto is the default and will use the browser by itself.)")
    else:
        print("Neither route got through. Most likely fixes:")
        print("  1. pip install playwright")
        print("     python -m playwright install chromium")
        print("  2. Open the category in Chrome first, then retry")
        print("  3. Wait a while -- you may be temporarily rate-limited")


def list_categories(config):
    """Print G2G's category tree so you can pick slugs for config.json."""
    fetcher = get_fetcher("auto")
    try:
        data = fetcher.get_json(
            f"{g2g.BASE}/api/get-mega-menu?country=SG&language=en&region=sg",
            referer=f"{g2g.BASE}/",
        )
    finally:
        fetcher.close()

    def walk(nodes, depth=0):
        for node in nodes:
            print("  " * depth + f"- {node.get('cat_name','?')}  [{node.get('seo_term','')}]")
            walk(node.get("subCategories") or [], depth + 1)

    walk((data.get("payload") or {}).get("results") or [])
    print("\nThese are sections, not listing pages. For a real slug, open a section "
          "on g2g.com, click a game, and copy the last part of the URL:\n"
          "  https://www.g2g.com/sg/categories/<slug>   <-- that <slug>")


def run(config, only=None, pages=None, mode="auto"):
    conn = store.connect()
    region = config.get("region", "sg")
    sort = config.get("sort", "most_recent")
    delay = config.get("delay_seconds", 1.5)
    default_pages = pages or config.get("pages_per_category", 3)

    categories = config.get("categories", [])
    if only:
        categories = [c for c in categories if c["slug"] == only]
        if not categories:
            sys.exit(f"No category with slug '{only}' in config.json")

    # One fetcher for the whole run: the session (or browser) is reused, which
    # is both faster and gentler on the site than starting over per category.
    fetcher = get_fetcher(mode)
    grand_added = grand_updated = grand_found = 0

    try:
        for entry in categories:
            source_name = entry.get("source", "g2g")
            adapter = ADAPTERS.get(source_name)
            if adapter is None:
                print(f"! Skipping unknown source '{source_name}'")
                continue

            slug = entry["slug"]
            label = entry.get("label")
            print(f"\n{source_name} / {label or slug}")

            run_id = store.start_run(conn, source_name, slug)
            run_started = int(time.time() * 1000)

            try:
                listings = adapter.fetch_category(
                    slug, label=label, region=region,
                    pages=entry.get("pages", default_pages),
                    sort=sort, delay=delay, fetcher=fetcher,
                )
            except BlockedError as exc:
                print(f"  BLOCKED: {exc}")
                print("  Try:  python scrape.py --diagnose")
                store.finish_run(conn, run_id, error=str(exc))
                continue
            except Exception as exc:                   # noqa: BLE001
                print(f"  FAILED: {exc}")
                store.finish_run(conn, run_id, error=str(exc))
                continue

            added, updated = store.save_listings(conn, listings)
            gone = store.mark_stale(conn, source_name, slug, run_started)
            store.finish_run(conn, run_id, len(listings), added, updated)

            grand_found += len(listings)
            grand_added += added
            grand_updated += updated
            print(f"  saved {len(listings)}: {added} new, {updated} refreshed, "
                  f"{gone} no longer listed")
            time.sleep(delay)
    finally:
        fetcher.close()

    total, by_source = store.stats(conn)
    print(f"\nDone. {grand_found} fetched this run ({grand_added} new, {grand_updated} refreshed).")
    print(f"Database now holds {total} active listings:")
    for row in by_source:
        print(f"  {row['source']}: {row['c']}")
    if total:
        print("\nNext: python serve.py")
    conn.close()


def main():
    parser = argparse.ArgumentParser(description="Scrape marketplace listings into listings.db")
    parser.add_argument("--pages", type=int, help="pages per category (48 listings each)")
    parser.add_argument("--only", help="scrape a single category slug")
    parser.add_argument("--mode", choices=["auto", "http", "browser"], default="auto",
                        help="how to fetch: auto (default), plain http, or a real browser")
    parser.add_argument("--diagnose", action="store_true",
                        help="test which fetch strategies the site allows")
    parser.add_argument("--list-categories", action="store_true",
                        help="show G2G's category tree and exit")
    args = parser.parse_args()

    config = load_config()
    if args.diagnose:
        diagnose(config)
    elif args.list_categories:
        list_categories(config)
    else:
        run(config, only=args.only, pages=args.pages, mode=args.mode)


if __name__ == "__main__":
    main()
