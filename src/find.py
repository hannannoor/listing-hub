#!/usr/bin/env python3
"""
Query the scraped listings. No web server -- results go to your terminal,
or to a CSV / JSON file.

    python find.py                          # defaults from config.json
    python find.py --max-price 30 --limit 15
    python find.py --attr Rank="Mythical Glory" --attr Skins=500+
    python find.py --search skin
    python find.py --csv cheap.csv
    python find.py --links                  # just the URLs, one per line

Filters are applied when you read, not when you scrape, so the database keeps
everything and you can change your mind without re-fetching.
"""

import argparse
import csv
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

import store

HERE = Path(__file__).parent
CONFIG_PATH = HERE / "config.json"

SORTS = {
    "price_asc":  "price IS NULL, price ASC",
    "price_desc": "price IS NULL, price DESC",
    "newest":     "COALESCE(posted_at, first_seen) DESC",
    "oldest":     "COALESCE(posted_at, first_seen) ASC",
    "rating":     "seller_rating DESC",
}

FIELDS = ["title", "price", "currency", "seller", "seller_rating", "seller_orders",
          "available_qty", "category_label", "source", "posted_at", "attributes", "url"]


def load_filters():
    """Default filters live in config.json under "filters"."""
    defaults = {"max_price": None, "min_price": None, "limit": 15,
                "sort": "price_asc", "search": None, "category": None,
                "source": None, "attrs": {}}
    if CONFIG_PATH.exists():
        try:
            with open(CONFIG_PATH, encoding="utf-8") as fh:
                defaults.update((json.load(fh).get("filters") or {}))
        except json.JSONDecodeError as exc:
            sys.exit(f"config.json has a syntax error on line {exc.lineno}: {exc.msg}")
    return defaults


def parse_attrs(pairs):
    """Turn ['Rank=Mythical Glory', 'Skins=500+'] into a dict."""
    out = {}
    for pair in pairs or []:
        if "=" not in pair:
            sys.exit(f"--attr needs KEY=VALUE, got: {pair}")
        key, value = pair.split("=", 1)
        out[key.strip()] = value.strip()
    return out


def query(conn, f):
    where, args = ["is_active = 1"], []

    if f.get("search"):
        where.append("(title LIKE ? OR seller LIKE ?)")
        args += [f"%{f['search']}%"] * 2
    if f.get("category"):
        where.append("category = ?")
        args.append(f["category"])
    if f.get("source"):
        where.append("source = ?")
        args.append(f["source"])
    if f.get("max_price") is not None:
        where.append("price IS NOT NULL AND price <= ?")
        args.append(float(f["max_price"]))
    if f.get("min_price") is not None:
        where.append("price IS NOT NULL AND price >= ?")
        args.append(float(f["min_price"]))

    clause = " AND ".join(where)
    order = SORTS.get(f.get("sort") or "price_asc", SORTS["price_asc"])
    limit = int(f.get("limit") or 15)
    attrs = f.get("attrs") or {}

    rows = conn.execute(
        f"SELECT {', '.join(FIELDS)} FROM listings WHERE {clause} ORDER BY {order}",
        args,
    ).fetchall()

    # Attribute matching happens here rather than in SQL: the values live in a
    # JSON blob, and the result sets are small enough that it costs nothing.
    kept = []
    for row in rows:
        item = dict(row)
        item["attributes"] = json.loads(item.get("attributes") or "{}")
        if all(want.lower() in str(item["attributes"].get(key, "")).lower()
               for key, want in attrs.items()):
            kept.append(item)

    return len(kept), kept[:limit]


def when(ms):
    if not ms:
        return ""
    try:
        return datetime.fromtimestamp(ms / 1000, timezone.utc).strftime("%Y-%m-%d")
    except (ValueError, OSError, OverflowError):
        return ""


def clip(text, width):
    text = (text or "").replace("\n", " ").strip()
    return text if len(text) <= width else text[: width - 1] + "~"


def print_results(rows, matched, f):
    if not rows:
        print("\nNothing matched.")
        print("  - Database empty?   python scrape.py")
        print("  - Filters too tight? python find.py --max-price 100")
        if f.get("attrs"):
            print("  - Check your --attr spelling: python find.py --show-attrs")
        return

    print()
    for i, r in enumerate(rows, 1):
        price = f"{r['price']:.2f} {r['currency'] or ''}".strip() if r["price"] is not None else "no price"
        rating = f"{r['seller_rating']*100:.1f}%" if r["seller_rating"] is not None else "-"
        orders = f"{r['seller_orders']:,} sold" if r["seller_orders"] else ""

        print(f"{i:>2}. {price:<12} {clip(r['title'], 84)}")
        detail = "  |  ".join(f"{k}: {v}" for k, v in (r["attributes"] or {}).items())
        if detail:
            print(f"    {clip(detail, 96)}")
        print(f"    {clip(r['seller'], 20)}  {rating}  {orders}  posted {when(r['posted_at'])}")
        print(f"    {r['url']}")
        print()

    bits = []
    if f.get("max_price") is not None:
        bits.append(f"under {f['max_price']}")
    if f.get("min_price") is not None:
        bits.append(f"over {f['min_price']}")
    if f.get("search"):
        bits.append(f"matching '{f['search']}'")
    for key, value in (f.get("attrs") or {}).items():
        bits.append(f"{key}~{value}")
    suffix = (" " + ", ".join(bits)) if bits else ""
    print(f"Showing {len(rows)} of {matched} listings{suffix}.")
    if matched > len(rows):
        print("Raise the cap with --limit to see more.")


def show_attrs(conn):
    """List every attribute field and value in the database, with counts."""
    rows = conn.execute(
        "SELECT attributes FROM listings WHERE is_active = 1 AND attributes IS NOT NULL"
    ).fetchall()
    tally = {}
    for (blob,) in rows:
        for key, value in (json.loads(blob or "{}") or {}).items():
            tally.setdefault(key, {}).setdefault(str(value), 0)
            tally[key][str(value)] += 1

    if not tally:
        print("No attributes recorded yet. Run `python scrape.py` to refresh.")
        return
    print()
    for key, values in tally.items():
        print(f"{key}")
        for value, count in sorted(values.items(), key=lambda kv: -kv[1]):
            print(f"    {value:<28} {count}")
        print()
    print('Filter with, for example:  python find.py --attr Rank="Mythical Glory"')


def write_csv(path, rows):
    # Each attribute becomes its own column, so the file is easy to sort.
    attr_keys = []
    for r in rows:
        for key in (r["attributes"] or {}):
            if key not in attr_keys:
                attr_keys.append(key)

    base = [c for c in FIELDS if c != "attributes"]
    with open(path, "w", newline="", encoding="utf-8-sig") as fh:
        writer = csv.DictWriter(fh, fieldnames=base + ["posted_date"] + attr_keys)
        writer.writeheader()
        for r in rows:
            row = {k: r[k] for k in base}
            row["posted_date"] = when(r["posted_at"])
            row.update({k: (r["attributes"] or {}).get(k, "") for k in attr_keys})
            writer.writerow(row)


def main():
    p = argparse.ArgumentParser(description="Query scraped listings")
    p.add_argument("--max-price", type=float, help="only listings at or below this price")
    p.add_argument("--min-price", type=float)
    p.add_argument("--limit", type=int, help="how many to show")
    p.add_argument("--sort", choices=list(SORTS), help="default: price_asc")
    p.add_argument("--search", help="match text in the title or seller name")
    p.add_argument("--attr", action="append", metavar="KEY=VALUE",
                   help='filter on a listing attribute, e.g. --attr Rank="Mythical Glory". Repeatable.')
    p.add_argument("--show-attrs", action="store_true",
                   help="list every attribute field and value in the database")
    p.add_argument("--category", help="restrict to one category slug")
    p.add_argument("--source", help="restrict to one site")
    p.add_argument("--csv", metavar="FILE", help="write results to a CSV file")
    p.add_argument("--json", metavar="FILE", help="write results to a JSON file")
    p.add_argument("--links", action="store_true", help="print only URLs")
    args = p.parse_args()

    if not store.DB_PATH.exists():
        sys.exit("No listings.db yet -- run `python scrape.py` first.")

    conn = store.connect()
    try:
        if args.show_attrs:
            show_attrs(conn)
            return

        f = load_filters()
        for key in ("max_price", "min_price", "limit", "sort", "search", "category", "source"):
            val = getattr(args, key, None)
            if val is not None:
                f[key] = val
        if args.attr:
            f["attrs"] = {**(f.get("attrs") or {}), **parse_attrs(args.attr)}

        matched, rows = query(conn, f)
    finally:
        conn.close()

    if args.links:
        for r in rows:
            print(r["url"])
    else:
        print_results(rows, matched, f)

    if args.csv:
        write_csv(args.csv, rows)
        print(f"\nWrote {len(rows)} rows to {args.csv}")
    if args.json:
        with open(args.json, "w", encoding="utf-8") as fh:
            json.dump(rows, fh, indent=2, ensure_ascii=False)
        print(f"\nWrote {len(rows)} rows to {args.json}")


if __name__ == "__main__":
    main()
