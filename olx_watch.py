"""Poll public OLX listings and track new advertisements and price changes."""

from __future__ import annotations

import argparse
import csv
import json
import re
import sqlite3
import sys
import time
from collections.abc import Callable, Sequence
from contextlib import closing
from dataclasses import dataclass
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl, urlencode, urljoin, urlsplit, urlunsplit

import httpx
from bs4 import BeautifulSoup, Tag

DEFAULT_URL = "https://www.olx.ua/uk/list/q-Wooting-60HE/"
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "uk-UA,uk;q=0.9",
}
STATE_PATTERN = re.compile(
    r'window\.__PRERENDERED_STATE__\s*=\s*("(?:\\.|[^"\\])*")', re.DOTALL
)
CSV_FIELDS = [
    "id",
    "title",
    "url",
    "price_value",
    "price_currency",
    "price_display",
    "location",
    "region",
    "created_time",
    "promoted",
]


class ParseError(ValueError):
    """The response does not contain a recognizable listing."""


class FetchError(RuntimeError):
    """A page could not be fetched and parsed after retries."""


@dataclass(frozen=True)
class Ad:
    """Normalized advertisement, retaining the complete source payload."""

    id: int
    title: str
    url: str
    price_value: float | None
    price_currency: str
    price_display: str
    location: str
    region: str
    created_time: str
    promoted: bool
    description: str
    raw: dict[str, Any]

    def public(self, include_description: bool = False) -> dict[str, Any]:
        """Return fields suitable for JSON or CSV output."""
        result = {field: getattr(self, field) for field in CSV_FIELDS}
        if include_description:
            result["description"] = self.description
        return result


@dataclass(frozen=True)
class ListingPage:
    """An independently parsed listing page and its pagination metadata."""

    ads: list[Ad]
    total: int
    total_pages: int


def normalize_ad(raw: dict[str, Any], query_url: str) -> Ad:
    """Validate core identity fields and normalize the embedded source schema."""
    try:
        ad_id = int(raw["id"])
        title = raw["title"]
        link = raw.get("url") or raw.get("urlPath")
        if ad_id <= 0 or not isinstance(title, str) or not title.strip():
            raise ValueError("invalid id or title")
        if not isinstance(link, str) or not link:
            raise ValueError("missing ad URL")
        price = raw.get("price") or {}
        regular = price.get("regularPrice") or {}
        value = regular.get("value")
        amount = None if value is None else float(value)
        location = raw.get("location") or {}
        return Ad(
            id=ad_id,
            title=title,
            url=urljoin(query_url, link),
            price_value=amount,
            price_currency=str(regular.get("currencyCode") or ""),
            price_display=str(price.get("displayValue") or ""),
            location=str(location.get("cityName") or ""),
            region=str(location.get("regionName") or ""),
            created_time=str(raw.get("createdTime") or ""),
            promoted=raw.get("isPromoted") is True,
            description=str(raw.get("description") or ""),
            raw=raw,
        )
    except (KeyError, TypeError, ValueError, AttributeError) as exc:
        raise ParseError("Invalid advertisement data") from exc


def _text(tag: Tag, selector: str) -> str:
    """Extract optional card text without relying on generated CSS classes."""
    found = tag.select_one(selector)
    return found.get_text(" ", strip=True) if found else ""


def _fallback_page(html: str, query_url: str) -> ListingPage:
    """Parse visible cards when the prerendered-state assignment is absent."""
    soup = BeautifulSoup(html, "html.parser")
    cards = soup.select('div[data-cy="l-card"]')
    if not cards:
        # An empty state blob is authoritative; arbitrary cardless HTML is not.
        raise ParseError("No prerendered state or listing cards found")
    ads: dict[int, Ad] = {}
    for card in cards:
        link = card.select_one('[data-testid="card-title-link"]')
        if link is None:
            link = card.select_one('a[href*="/obyavlenie/"]')
        if link is None:
            raise ParseError("Listing card has no advertisement link")
        title = _text(card, "h4, h6") or link.get_text(" ", strip=True)
        display = _text(card, '[data-testid="ad-price"]')
        amount_match = re.search(r"\d[\d\s\u00a0\u202f]*(?:[.,]\d{1,2})?", display)
        amount = (
            float(re.sub(r"\s", "", amount_match.group()).replace(",", "."))
            if amount_match
            else None
        )
        currency = next(
            (
                code
                for marker, code in (
                    ("грн", "UAH"),
                    ("$", "USD"),
                    ("€", "EUR"),
                    ("UAH", "UAH"),
                    ("USD", "USD"),
                    ("EUR", "EUR"),
                )
                if marker in display
            ),
            "",
        )
        location_date = _text(card, '[data-testid="location-date"]')
        location, _, created = location_date.partition(" - ")
        raw = {
            "id": card.get("id"),
            "title": title,
            "url": link.get("href"),
            "isActive": True,
            "price": {
                "displayValue": display,
                "regularPrice": {
                    "value": amount,
                    "currencyCode": currency,
                },
            },
            "location": {"cityName": location},
            "createdTime": created,
        }
        item = normalize_ad(raw, query_url)
        ads.setdefault(item.id, item)
    pages = 1
    for anchor in soup.select("a[href]"):
        for key, value in parse_qsl(urlsplit(str(anchor.get("href"))).query):
            if key == "page" and value.isdigit():
                pages = max(pages, int(value))
    return ListingPage(list(ads.values()), len(ads), pages)


def parse_page(html: str, query_url: str = DEFAULT_URL) -> ListingPage:
    """Decode OLX's double-JSON state or fall back to visible listing cards."""
    match = STATE_PATTERN.search(html)
    if match is None:
        if "window.__PRERENDERED_STATE__" in html:
            raise ParseError("Malformed prerendered-state assignment")
        return _fallback_page(html, query_url)
    try:
        state = json.loads(json.loads(match.group(1)))
        listing = state["listing"]["listing"]
        rows = listing["ads"]
        total = int(listing["totalElements"])
        pages = int(listing["totalPages"])
        if not isinstance(rows, list) or total < 0 or pages < 0:
            raise ValueError("invalid listing metadata")
        ads: dict[int, Ad] = {}
        for raw in rows:
            if not isinstance(raw, dict):
                raise TypeError("invalid ad object")
            if raw.get("isActive") is True:
                item = normalize_ad(raw, query_url)
                ads.setdefault(item.id, item)
        return ListingPage(list(ads.values()), total, max(1, pages))
    except (ValueError, KeyError, TypeError) as exc:
        raise ParseError("Invalid prerendered listing data") from exc


def page_url(query_url: str, number: int) -> str:
    """Preserve search filters, replacing any existing page parameter."""
    parts = urlsplit(query_url)
    params = [
        (key, value)
        for key, value in parse_qsl(parts.query, keep_blank_values=True)
        if key != "page"
    ]
    if number > 1:
        params.append(("page", str(number)))
    return urlunsplit(parts._replace(query=urlencode(params), fragment=""))


def retry_delay(response: httpx.Response | None, attempt: int) -> float:
    """Use exponential backoff, respecting a longer Retry-After delay."""
    delay = float(2 ** (attempt + 1))
    header = response.headers.get("Retry-After") if response is not None else None
    if header:
        try:
            seconds = float(header)
        except ValueError:
            try:
                date = parsedate_to_datetime(header)
                seconds = (date - datetime.now(timezone.utc)).total_seconds()
            except (TypeError, ValueError, OverflowError):
                seconds = 0
        delay = max(delay, seconds)
    return delay


def load_cookies(path: str | None) -> str:
    """Read a browser cookie export into a single Cookie request header."""
    if not path:
        return ""
    pairs: list[str] = []
    for line in Path(path).read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        if line.startswith("#HttpOnly_"):
            line = line[len("#HttpOnly_") :]
        elif line.startswith("#"):
            continue
        fields = line.split("\t")
        if len(fields) >= 7:
            pairs.append(f"{fields[5]}={fields[6]}")
        elif "=" in line:
            pairs.append(line.rstrip(";"))
    return "; ".join(pairs)


def fetch_page(
    client: httpx.Client,
    url: str,
    *,
    headers: dict[str, str] | None = None,
    sleep: Callable[[float], Any] = time.sleep,
) -> ListingPage:
    """Fetch and parse with three total attempts; retry transient failures."""
    reason = "unknown error"
    for attempt in range(3):
        response = None
        try:
            response = client.get(
                url,
                headers=headers or HEADERS,
                timeout=30,
                follow_redirects=True,
            )
            response.raise_for_status()
            return parse_page(response.text, url)
        except httpx.HTTPStatusError as exc:
            status = exc.response.status_code
            reason = f"HTTP {status}"
            if status != 429 and status < 500:
                break
        except httpx.RequestError:
            # Transport exceptions can include credentials from a supplied URL.
            reason = "network request failed"
        except ParseError as exc:
            reason = str(exc)
        if attempt < 2:
            sleep(retry_delay(response, attempt))
    raise FetchError(f"{reason} (up to 3 attempts)")


def initialize_database(db: sqlite3.Connection) -> None:
    """Create persistent state without changing existing advertisements."""
    db.executescript("""
        CREATE TABLE IF NOT EXISTS ads (
            id INTEGER PRIMARY KEY, first_seen TEXT, last_seen TEXT,
            title TEXT, url TEXT, price_value REAL, price_currency TEXT,
            price_display TEXT, location TEXT, region TEXT, created_time TEXT,
            active INTEGER, promoted INTEGER DEFAULT 0, raw_json TEXT
        );
        CREATE TABLE IF NOT EXISTS runs (
            id INTEGER PRIMARY KEY AUTOINCREMENT, run_at TEXT, total INTEGER,
            new_count INTEGER, error TEXT
        );
        CREATE TABLE IF NOT EXISTS price_history (
            ad_id INTEGER, seen_at TEXT, price_value REAL
        );
        CREATE TABLE IF NOT EXISTS metadata (
            key TEXT PRIMARY KEY, value TEXT
        );
    """)


WOOTING_KEYBOARD = re.compile(
    r"wooting|вутинг|вутінг", re.IGNORECASE
)
NON_KEYBOARD = re.compile(
    r"свитч|світч|switch|комплект|моддинг|моддінг|моддинг|wrist|keycap|кейкап|"
    r"кабель|консультац|прокладк|стабилиз|стабіліз|кейс|case",
    re.IGNORECASE,
)
MODEL_60HE = re.compile(
    r"60\s*-?\s*he|60he", re.IGNORECASE
)


def is_wooting_60he_keyboard(title: str) -> bool:
    """True when a title names a Wooting 60HE board rather than an accessory."""
    if not WOOTING_KEYBOARD.search(title):
        return False
    if NON_KEYBOARD.search(title):
        return False
    return bool(MODEL_60HE.search(title))


def store_ads(
    db: sqlite3.Connection,
    ads: list[Ad],
    result: dict[str, Any],
    include_description: bool,
    export_path: str | None = None,
) -> None:
    """Atomically update observations, price history, and the successful run."""
    run_at = result["run_at"]
    with db:
        db.execute("BEGIN IMMEDIATE")
        initialized = db.execute(
            "SELECT 1 FROM metadata WHERE key='baseline'"
        ).fetchone()
        has_ads = db.execute("SELECT 1 FROM ads LIMIT 1").fetchone()
        first_run = not initialized and not has_ads
        result["first_run"] = first_run
        for item in ads:
            if result.get("exclude_promoted") and item.promoted:
                continue
            if result.get("only_wooting_60he") and not is_wooting_60he_keyboard(
                item.title
            ):
                continue
            previous = db.execute(
                "SELECT price_value, price_currency FROM ads WHERE id=?", (item.id,)
            ).fetchone()
            public = item.public(include_description)
            if previous is None and not first_run:
                result["new"].append(public)
            changed = previous is not None and previous != (
                item.price_value,
                item.price_currency,
            )
            if changed and previous is not None:
                result["price_changes"].append(
                    {
                        **public,
                        "old_price_value": previous[0],
                        "old_price_currency": previous[1],
                    }
                )
            if previous is None or changed:
                db.execute(
                    "INSERT INTO price_history VALUES (?, ?, ?)",
                    (item.id, run_at, item.price_value),
                )
            db.execute(
                """
                INSERT INTO ads VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    last_seen=excluded.last_seen, title=excluded.title,
                    url=excluded.url, price_value=excluded.price_value,
                    price_currency=excluded.price_currency,
                    price_display=excluded.price_display, location=excluded.location,
                    region=excluded.region, created_time=excluded.created_time,
                    active=excluded.active, promoted=excluded.promoted,
                    raw_json=excluded.raw_json
            """,
                (
                    item.id,
                    run_at,
                    run_at,
                    item.title,
                    item.url,
                    item.price_value,
                    item.price_currency,
                    item.price_display,
                    item.location,
                    item.region,
                    item.created_time,
                    1,
                    int(item.promoted),
                    json.dumps(item.raw, ensure_ascii=False),
                ),
            )
        db.execute("INSERT OR IGNORE INTO metadata VALUES ('baseline', ?)", (run_at,))
        result["known"] = db.execute("SELECT COUNT(*) FROM ads").fetchone()[0]
        if export_path:
            export_csv(export_path, result["new"], include_description)
        record_run(db, result)


def record_run(db: sqlite3.Connection, result: dict[str, Any]) -> None:
    """Record successful, partial, and failed polling attempts."""
    db.execute(
        "INSERT INTO runs(run_at,total,new_count,error) VALUES (?, ?, ?, ?)",
        (
            result["run_at"],
            result["total"],
            len(result["new"]),
            json.dumps(result["errors"], ensure_ascii=False)
            if result["errors"]
            else None,
        ),
    )


def collect(
    client: httpx.Client,
    url: str,
    max_pages: int,
    headers: dict[str, str] | None = None,
) -> tuple[list[Ad], int, list[str], int]:
    """Visit available pages, tolerating failures and deduplicating across pages."""
    ads: dict[int, Ad] = {}
    total = 0
    errors: list[str] = []
    parsed_count = 0
    limit = max_pages
    number = 1
    while number <= limit:
        try:
            parsed = fetch_page(client, page_url(url, number), headers=headers)
        except FetchError as exc:
            errors.append(f"Page {number}: {exc}")
        else:
            parsed_count += 1
            total = max(total, parsed.total)
            limit = min(max_pages, max(number, parsed.total_pages))
            for item in parsed.ads:
                ads.setdefault(item.id, item)
        number += 1
    return list(ads.values()), total, errors, parsed_count


def export_csv(path: str, ads: list[dict[str, Any]], include_description: bool) -> None:
    """Export newly reported advertisements, including a header for empty runs."""
    fields = CSV_FIELDS + (["description"] if include_description else [])
    with Path(path).open("w", newline="", encoding="utf-8") as stream:
        writer = csv.DictWriter(stream, fieldnames=fields)
        writer.writeheader()
        writer.writerows(ads)


def print_result(
    result: dict[str, Any], as_json: bool, include_description: bool
) -> None:
    """Keep JSON stdout clean and print errors to stderr in human mode."""
    if as_json:
        print(json.dumps(result, ensure_ascii=False))
        return
    if result["first_run"]:
        print(f"Baseline created: {result['known']} ads stored.")
    elif result["new"]:
        for item in result["new"]:
            print(
                f"NEW: {item['title']}\n{item['price_display']}\n"
                f"{item['location']}\n{item['created_time']}\n{item['url']}"
            )
            if include_description and item.get("description"):
                print(item["description"])
    elif result["parsed_pages"]:
        print(f"No new ads (known={result['known']}, total={result['total']}).")
    for item in result["price_changes"]:
        print(
            f"PRICE CHANGED: {item['title']}\n"
            f"{item['old_price_value']} {item['old_price_currency']} -> "
            f"{item['price_display']}\n{item['url']}"
        )
    for error in result["errors"]:
        print(error, file=sys.stderr)


def arguments(argv: Sequence[str] | None) -> argparse.Namespace:
    """Define the single-run command line interface."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", default=DEFAULT_URL)
    parser.add_argument("--db", default="./olx_watch.sqlite3")
    parser.add_argument("--max-pages", type=int, default=10)
    parser.add_argument("--once", action="store_true", help="Run once (the default)")
    parser.add_argument("--json", action="store_true", dest="as_json")
    parser.add_argument("--include-description", action="store_true")
    parser.add_argument("--export", metavar="NEW.csv")
    parser.add_argument(
        "--cookies-file",
        metavar="PATH",
        help="Netscape/header cookie file sent with each request (keeps sessions stable)",
    )
    parser.add_argument(
        "--only-wooting-60he",
        action="store_true",
        help="Keep only Wooting 60HE/60HE+/v2 keyboards, dropping accessories",
    )
    parser.add_argument(
        "--exclude-promoted",
        action="store_true",
        help="Drop promoted (paid) ads so output matches the organic search result",
    )
    args = parser.parse_args(argv)
    if args.max_pages < 1:
        parser.error("--max-pages must be at least 1")
    parts = urlsplit(args.url)
    if parts.scheme not in {"https", "http"} or not parts.netloc:
        parser.error("--url must be an absolute HTTP(S) URL")
    if parts.username or parts.password:
        parser.error("--url must not contain credentials")
    if args.cookies_file and not Path(args.cookies_file).is_file():
        parser.error(f"--cookies-file does not exist: {args.cookies_file}")
    return args


def main(
    argv: Sequence[str] | None = None, *, client: httpx.Client | None = None
) -> int:
    """Run one poll and return 0 (success), 1 (fatal), or 2 (partial)."""
    args = arguments(argv)
    result: dict[str, Any] = {
        "run_at": datetime.now(timezone.utc).isoformat(),
        "query_url": args.url,
        "total": 0,
        "new": [],
        "price_changes": [],
        "errors": [],
        "first_run": False,
        "known": 0,
        "parsed_pages": 0,
        "exclude_promoted": args.exclude_promoted,
        "only_wooting_60he": args.only_wooting_60he,
    }
    code = 1
    try:
        with closing(sqlite3.connect(args.db, timeout=30)) as db:
            initialize_database(db)
            if client is None:
                headers = dict(HEADERS)
                cookies = load_cookies(args.cookies_file)
                if cookies:
                    headers["Cookie"] = cookies
                with httpx.Client() as owned_client:
                    ads, total, errors, parsed = collect(
                        owned_client, args.url, args.max_pages, headers=headers
                    )
            else:
                ads, total, errors, parsed = collect(client, args.url, args.max_pages)
            result.update(total=total, errors=errors, parsed_pages=parsed)
            if parsed:
                try:
                    store_ads(db, ads, result, args.include_description, args.export)
                except (sqlite3.Error, OSError):
                    result.update(new=[], price_changes=[], first_run=False)
                    result["known"] = db.execute("SELECT COUNT(*) FROM ads").fetchone()[
                        0
                    ]
                    raise
                code = 2 if errors else 0
            else:
                with db:
                    record_run(db, result)
    except (sqlite3.Error, OSError) as exc:
        result["errors"].append(f"Local storage/output failure: {type(exc).__name__}")
        code = 1
    print_result(result, args.as_json, args.include_description)
    return code


if __name__ == "__main__":
    raise SystemExit(main())
