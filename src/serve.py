#!/usr/bin/env python3
"""
Local viewer for the aggregated listings.

    python serve.py            -> http://localhost:8000
    python serve.py --port 9000

Standard library only. Reads listings.db; never writes to it.
"""

import argparse
import json
import sqlite3
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import store

HERE = Path(__file__).parent
INDEX_HTML = HERE / "index.html"

SORTS = {
    "newest": "COALESCE(posted_at, first_seen) DESC",
    "oldest": "COALESCE(posted_at, first_seen) ASC",
    "price_asc": "price IS NULL, price ASC",
    "price_desc": "price IS NULL, price DESC",
    "title": "title COLLATE NOCASE ASC",
}


def query_listings(conn, params):
    def one(key, default=None):
        vals = params.get(key)
        return vals[0] if vals else default

    where = ["is_active = 1"]
    args = []

    search = (one("q") or "").strip()
    if search:
        where.append("(title LIKE ? OR seller LIKE ?)")
        args += [f"%{search}%", f"%{search}%"]

    for key, column in (("source", "source"), ("category", "category")):
        value = one(key)
        if value and value != "all":
            where.append(f"{column} = ?")
            args.append(value)

    for key, op in (("min_price", ">="), ("max_price", "<=")):
        value = one(key)
        if value not in (None, ""):
            try:
                args.append(float(value))
                where.append(f"price {op} ?")
            except ValueError:
                pass

    order = SORTS.get(one("sort", "newest"), SORTS["newest"])

    try:
        limit = max(1, min(int(one("limit", 60)), 300))
        offset = max(0, int(one("offset", 0)))
    except ValueError:
        limit, offset = 60, 0

    clause = " AND ".join(where)
    total = conn.execute(
        f"SELECT COUNT(*) FROM listings WHERE {clause}", args
    ).fetchone()[0]

    rows = conn.execute(
        f"""SELECT uid, source, external_id, category, category_label, title, url, price, currency,
                   image_url, seller, seller_url, seller_rating, seller_orders,
                   available_qty, posted_at, first_seen
            FROM listings WHERE {clause}
            ORDER BY {order} LIMIT ? OFFSET ?""",
        args + [limit, offset],
    ).fetchall()

    return {
        "total": total,
        "offset": offset,
        "limit": limit,
        "listings": [dict(r) for r in rows],
    }


def facets(conn):
    sources = conn.execute(
        "SELECT source, COUNT(*) c FROM listings WHERE is_active = 1 "
        "GROUP BY source ORDER BY c DESC"
    ).fetchall()
    categories = conn.execute(
        "SELECT category, COALESCE(category_label, category) label, COUNT(*) c "
        "FROM listings WHERE is_active = 1 GROUP BY category ORDER BY c DESC"
    ).fetchall()
    last = conn.execute(
        "SELECT MAX(finished_at) FROM scrape_runs WHERE error IS NULL"
    ).fetchone()[0]
    return {
        "sources": [dict(r) for r in sources],
        "categories": [dict(r) for r in categories],
        "last_scrape": last,
    }


class Handler(BaseHTTPRequestHandler):
    db_path = store.DB_PATH

    def log_message(self, fmt, *args):
        pass  # keep the console quiet

    def _send(self, code, body, content_type):
        if isinstance(body, str):
            body = body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        parsed = urlparse(self.path)
        route = parsed.path
        params = parse_qs(parsed.query)

        if route in ("/", "/index.html"):
            if not INDEX_HTML.exists():
                self._send(500, "index.html is missing", "text/plain; charset=utf-8")
                return
            self._send(200, INDEX_HTML.read_text(encoding="utf-8"),
                       "text/html; charset=utf-8")
            return

        if route in ("/api/listings", "/api/facets"):
            conn = sqlite3.connect(f"file:{self.db_path}?mode=ro", uri=True)
            conn.row_factory = sqlite3.Row
            try:
                payload = (query_listings(conn, params) if route == "/api/listings"
                           else facets(conn))
            except sqlite3.Error as exc:
                self._send(500, json.dumps({"error": str(exc)}),
                           "application/json; charset=utf-8")
                return
            finally:
                conn.close()
            self._send(200, json.dumps(payload, ensure_ascii=False),
                       "application/json; charset=utf-8")
            return

        self._send(404, "Not found", "text/plain; charset=utf-8")


def main():
    parser = argparse.ArgumentParser(description="Serve the local listings viewer")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--no-browser", action="store_true")
    args = parser.parse_args()

    if not store.DB_PATH.exists():
        print("No listings.db yet -- run `python scrape.py` first.")
        return

    url = f"http://localhost:{args.port}"
    server = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
    print(f"Listing Hub running at {url}   (Ctrl+C to stop)")
    if not args.no_browser:
        try:
            webbrowser.open(url)
        except Exception:  # noqa: BLE001
            pass
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopped.")
        server.server_close()


if __name__ == "__main__":
    main()
