"""
Ways of getting JSON out of a site that does not want plain scripts talking to it.

G2G sits behind Cloudflare. A bare urllib request gets HTTP 403 even with a
convincing User-Agent, because the block is partly on TLS fingerprint -- headers
alone cannot fix that. So there are two strategies here:

  HttpFetcher     cheap: a cookie-carrying session that loads the normal
                  category page first, then asks for its JSON the way the page
                  itself would. Works on some setups, no dependencies.

  BrowserFetcher  reliable: drives a real Chromium via Playwright, and asks the
                  page to fetch its own JSON. This is exactly what your browser
                  does when you click through the site, so it passes.

`get_fetcher("auto")` tries the cheap one and falls back to the real browser.
"""

import gzip
import http.cookiejar
import json
import random
import time
import urllib.error
import urllib.parse
import urllib.request

CHROME_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36"
)


class FetchError(Exception):
    pass


class BlockedError(FetchError):
    """The site refused us -- a different strategy is needed, not a retry."""


# --------------------------------------------------------------------------
# Strategy 1: plain HTTP with a warmed-up cookie jar
# --------------------------------------------------------------------------
class HttpFetcher:
    name = "http"

    def __init__(self, max_retries=3):
        self.jar = http.cookiejar.CookieJar()
        self.opener = urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(self.jar)
        )
        self.max_retries = max_retries
        self._warmed = set()

    def _open(self, url, headers, timeout=30):
        req = urllib.request.Request(url, headers=headers)
        with self.opener.open(req, timeout=timeout) as resp:
            data = resp.read()
            if resp.headers.get("Content-Encoding") == "gzip":
                data = gzip.decompress(data)
            return data

    def warm_up(self, page_url):
        """Load the human-facing page once so we pick up its cookies."""
        if page_url in self._warmed:
            return
        headers = {
            "User-Agent": CHROME_UA,
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.9",
            "Accept-Encoding": "gzip",
            "Upgrade-Insecure-Requests": "1",
            "Sec-Fetch-Dest": "document",
            "Sec-Fetch-Mode": "navigate",
            "Sec-Fetch-Site": "none",
            "Sec-Fetch-User": "?1",
            "sec-ch-ua": '"Chromium";v="125", "Not.A/Brand";v="24"',
            "sec-ch-ua-mobile": "?0",
            "sec-ch-ua-platform": '"Windows"',
        }
        try:
            self._open(page_url, headers)
            self._warmed.add(page_url)
            time.sleep(1.0)
        except Exception as exc:  # noqa: BLE001
            raise BlockedError(f"could not load {page_url}: {exc}") from exc

    def get_json(self, url, referer=None):
        if referer:
            self.warm_up(referer)

        headers = {
            "User-Agent": CHROME_UA,
            "Accept": "*/*",
            "Accept-Language": "en-US,en;q=0.9",
            "Accept-Encoding": "gzip",
            "Sec-Fetch-Dest": "empty",
            "Sec-Fetch-Mode": "cors",
            "Sec-Fetch-Site": "same-origin",
            "sec-ch-ua": '"Chromium";v="125", "Not.A/Brand";v="24"',
            "sec-ch-ua-mobile": "?0",
            "sec-ch-ua-platform": '"Windows"',
        }
        if referer:
            headers["Referer"] = referer

        last = None
        for attempt in range(1, self.max_retries + 1):
            try:
                return json.loads(self._open(url, headers).decode("utf-8"))
            except urllib.error.HTTPError as exc:
                if exc.code in (401, 403, 429):
                    raise BlockedError(f"HTTP {exc.code} from {url}") from exc
                last = f"HTTP {exc.code}"
                if exc.code not in (500, 502, 503, 504):
                    raise FetchError(f"{url} -> HTTP {exc.code}") from exc
            except urllib.error.URLError as exc:
                reason = str(exc.reason)
                if "403" in reason or "Forbidden" in reason:
                    raise BlockedError(f"blocked reaching {url}: {reason}") from exc
                last = reason
            except (TimeoutError, json.JSONDecodeError) as exc:
                last = str(exc)

            if attempt < self.max_retries:
                time.sleep(2 ** attempt + random.uniform(0, 1))

        raise FetchError(f"{url} failed after {self.max_retries} tries: {last}")

    def close(self):
        pass


# --------------------------------------------------------------------------
# Strategy 2: a real browser
# --------------------------------------------------------------------------
class BrowserFetcher:
    name = "browser"

    def __init__(self, headless=True):
        try:
            from playwright.sync_api import sync_playwright
        except ImportError as exc:
            raise FetchError(
                "Playwright is not installed. Run:\n"
                "    pip install playwright\n"
                "    python -m playwright install chromium"
            ) from exc

        self._pw = sync_playwright().start()
        try:
            self._browser = self._pw.chromium.launch(headless=headless)
        except Exception as exc:  # noqa: BLE001
            self._pw.stop()
            raise FetchError(
                f"Could not start Chromium ({exc}).\n"
                "Run:  python -m playwright install chromium"
            ) from exc

        self._ctx = self._browser.new_context(
            user_agent=CHROME_UA,
            viewport={"width": 1366, "height": 900},
            locale="en-US",
        )
        self._page = self._ctx.new_page()
        self._current = None

    def warm_up(self, page_url):
        """Navigate to the real page, so later fetches run in its own context."""
        if self._current == page_url:
            return
        self._page.goto(page_url, wait_until="domcontentloaded", timeout=60000)
        self._page.wait_for_timeout(1500)
        self._current = page_url

    def get_json(self, url, referer=None):
        if referer:
            self.warm_up(referer)
        elif self._current is None:
            parts = urllib.parse.urlsplit(url)
            self.warm_up(f"{parts.scheme}://{parts.netloc}/")

        # Ask the page to fetch its own data: same origin, same cookies,
        # same TLS session the site already accepted.
        result = self._page.evaluate(
            """async (u) => {
                 const r = await fetch(u, { headers: { accept: 'application/json' } });
                 const text = await r.text();
                 return { status: r.status, text };
               }""",
            url,
        )
        if result["status"] == 200:
            try:
                return json.loads(result["text"])
            except json.JSONDecodeError as exc:
                raise FetchError(f"{url} returned non-JSON") from exc
        if result["status"] in (401, 403, 429):
            raise BlockedError(f"HTTP {result['status']} from {url} (even in a browser)")
        raise FetchError(f"{url} -> HTTP {result['status']}")

    def close(self):
        for closer in (getattr(self, "_ctx", None), getattr(self, "_browser", None)):
            try:
                closer and closer.close()
            except Exception:  # noqa: BLE001
                pass
        try:
            self._pw.stop()
        except Exception:  # noqa: BLE001
            pass


# --------------------------------------------------------------------------
class AutoFetcher:
    """Try plain HTTP; the first time we are blocked, switch to a browser."""

    name = "auto"

    def __init__(self):
        self._impl = HttpFetcher()
        self._switched = False

    def _switch(self, why):
        print(f"    plain HTTP was blocked ({why}) -- switching to a real browser")
        self._impl.close()
        self._impl = BrowserFetcher()
        self._switched = True
        print(f"    browser ready")

    def warm_up(self, page_url):
        try:
            self._impl.warm_up(page_url)
        except BlockedError as exc:
            if self._switched:
                raise
            self._switch(exc)
            self._impl.warm_up(page_url)

    def get_json(self, url, referer=None):
        try:
            return self._impl.get_json(url, referer)
        except BlockedError as exc:
            if self._switched:
                raise
            self._switch(exc)
            return self._impl.get_json(url, referer)

    def close(self):
        self._impl.close()


def get_fetcher(mode="auto"):
    if mode == "http":
        return HttpFetcher()
    if mode == "browser":
        return BrowserFetcher()
    return AutoFetcher()
