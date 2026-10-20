# OLX Watch — parser spec

Goal: poll an OLX.ua search listing, collect all ads, detect newly appeared ads, store state in SQLite,
print/send a summary of new ads. Runs hourly from cron.

## Verified facts about the source (checked 2026-10-20)
- URL: https://www.olx.ua/uk/list/q-Wooting-60HE/ (63 results, 2 pages, ~40 ads/page)
- Pagination: `?page=2`, `?page=3`, ... (page 1 = no param)
- Plain `httpx` GET with a desktop UA returns 200 and full server-rendered HTML. No JS needed.
- The HTML embeds `window.__PRERENDERED_STATE__ = "<json-escaped-json-string>";`
  Decode: `json.loads(json.loads('"' + raw + '"'))` -> dict with `listing.listing.ads` (list),
  `listing.listing.totalElements`, `totalPages`, `pageNumber`.
- Each ad dict has: id (int), title, description, url, urlPath, createdTime (ISO+03:00),
  lastRefreshTime, isActive, price.displayValue, price.regularPrice.value/currencyCode,
  location.cityName, location.regionName, location.districtName, location.pathName,
  category.id, itemCondition, photos (list of urls), isBusiness, isPromoted, user.id, user.name,
  contact.name.
- Fallback if the state blob is missing: parse `div[data-cy="l-card"]` (id attr = ad id) with BeautifulSoup.

## CLI
`olx_watch.py` single-file, stdlib + httpx + beautifulsoup4 only.
- `--url` (default the Wooting 60HE URL above), `--db` (default ./olx_watch.sqlite3),
  `--max-pages` (default 10), `--once` (single run, default behavior),
  `--json` (machine readable result on stdout), `--include-description`,
  `--export new.csv` optional.
- Filter out inactive ads (`isActive is not True`), dedupe by id.
- Also detect *changed price* on already known ads? -> store price, report separately as `price_changes`
  but keep exit code semantics the same.

## Output contract (important, cron uses it)
- Human output: one block per new ad: `NEW: <title>` / price / location / createdTime / url.
  If nothing new: `No new ads (known=<N>, total=<M>).`
- `--json` -> `{"run_at": iso, "query_url": ..., "total": M, "new": [...], "price_changes": [...], "errors": []}`
- Exit code 0 on success (even with no new ads), 1 on fatal error (network/parse fail after retries),
  2 on partial success (some pages failed but at least one parsed).
- Do not print secrets; no auth needed.

## Robustness
- Retries: 3 attempts, backoff 2/4/8 s, on network errors and 5xx and 429 (honor Retry-After).
- Timeout 30 s. UA desktop Chrome. `Accept-Language: uk-UA,uk;q=0.9`.
- SQLite: table `ads(id INTEGER PRIMARY KEY, first_seen TEXT, last_seen TEXT, title, url, price_value,
  price_currency, price_display, location, region, created_time, active INTEGER, raw_json TEXT)`,
  table `runs(id INTEGER PK AUTOINCREMENT, run_at, total, new_count, error)`, table
  `price_history(ad_id, seen_at, price_value)`.
- First ever run: baseline — insert all, report `first_run` and do NOT dump 63 "NEW" lines
  (print `Baseline created: N ads stored.`). This is critical so cron doesn't spam on setup.
- Per-page failure tolerance; final `errors` list.

## Files
- `olx_watch.py` (parser + CLI, single file, typed, docstrings)
- `tests/test_parse.py` — tests against the saved fixture `tests/fixtures/listing.html`,
  plus a synthetic fixture for the BeautifulSoup fallback path.
- `tests/fixtures/listing.html` — real saved page (copy it yourself from a fetch during dev if the
  file does not exist; it is already provided at that path).
- `README.md` — usage, cron line, exit codes.
- `requirements.txt` (httpx, beautifulsoup4, pytest).
Do NOT install a cron job yourself, do not modify ~/.config, do not run network calls in tests.
Use `.venv/bin/python` for running things. Finish by running the tests and a real
`--db /tmp/olx_test.sqlite3 --once` smoke run, then report what you changed.
