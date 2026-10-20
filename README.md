# OLX Watch

A single-file Python CLI that polls an OLX.ua search, stores advertisements in
SQLite, and reports new ads and changed prices. The default search is
<https://www.olx.ua/uk/list/q-Wooting-60HE/>. No authentication or JavaScript
browser is needed.

## Setup

Python 3.10+ is required. From the project directory:

```bash
uv venv .venv
uv pip install --python .venv/bin/python -r requirements.txt
```

Runtime dependencies are `httpx` and `beautifulsoup4`; `pytest` is included for
offline tests. No paid API is used.

## Usage

```bash
.venv/bin/python olx_watch.py --once
.venv/bin/python olx_watch.py --db ./olx_watch.sqlite3 --json
.venv/bin/python olx_watch.py --include-description --export new.csv
.venv/bin/python olx_watch.py --url 'https://www.olx.ua/uk/list/q-Wooting-60HE/' --max-pages 2
```

| Option | Default | Purpose |
| --- | --- | --- |
| `--url` | Wooting 60HE search above | Public search URL; existing filters are preserved |
| `--db` | `./olx_watch.sqlite3` | Persistent SQLite state |
| `--max-pages` | `10` | Positive upper limit on pages fetched |
| `--once` | Enabled implicitly | Perform one poll, then exit |
| `--json` | Off | Write one JSON object to stdout |
| `--include-description` | Off | Include descriptions in new-ad output and CSV |
| `--export NEW.csv` | Disabled | Overwrite a UTF-8 CSV containing this run's new ads |

The first successful poll creates a baseline and prints:

```text
Baseline created: 63 ads stored.
```

It stores all observed active ads without reporting them as new. An empty
successful listing also establishes a baseline. Fatal polls do not establish
one. A partial first poll establishes a baseline for the pages that succeeded;
ads first observed on later polls are then reported as new.

Subsequent polls print one block per new ad:

```text
NEW: Wooting 60HE
6 500 грн.
Kyiv
2026-10-20T10:00:00+03:00
https://www.olx.ua/d/uk/obyavlenie/example.html
```

When nothing is new, output is `No new ads (known=N, total=M).` Price changes
are printed separately as `PRICE CHANGED` blocks and do not affect the exit
code. A change in numeric amount or currency counts as a price change; a
display-format change alone does not. Descriptions are omitted unless requested.
CSV contains only newly reported ads, not baseline ads or price changes; an
empty result produces a header-only file.
If CSV writing fails, database updates are rolled back so the next poll can
report those new advertisements again.

JSON always contains `run_at` (UTC ISO timestamp), `query_url`, `total`, `new`,
`price_changes`, and `errors`. Additional fields are `first_run`, `known`, and
`parsed_pages`. Each ad has `id`, `title`, `url`, `price_value`, `price_currency`,
`price_display`, `location`, `region`, and `created_time`; descriptions are
optional. Price changes add `old_price_value` and `old_price_currency` alongside
the current price fields. For example, an empty baseline returns:

```json
{
  "run_at": "2026-10-20T10:00:00+00:00",
  "query_url": "https://www.olx.ua/uk/list/q-Wooting-60HE/",
  "total": 0,
  "new": [],
  "price_changes": [],
  "errors": [],
  "first_run": true,
  "known": 0,
  "parsed_pages": 1
}
```

## Persistence and failures

- `ads` keeps first/last observation times, the latest normalized fields, and
  the full source advertisement in `raw_json`. Ad IDs are deduplicated both
  within and across pages. Embedded ads are included only when `isActive` is
  exactly `true`.
- `price_history` records the initial observed price and every amount/currency
  change. `runs` records each polling attempt, total, new count, and errors.
  `metadata` distinguishes an empty baseline from an uninitialized database.
- Missing ads are retained: disappearing from a search or a failed page is not
  sufficient evidence that an advertisement was deactivated. `known` counts all
  ads ever stored in this database. Use separate databases for separate searches.
- Requests use a desktop Chrome user agent, Ukrainian language preferences,
  redirects, and a 30-second timeout. Network errors, HTTP 429/5xx, and parsing
  failures get three total attempts. Backoff starts at 2 seconds and doubles
  (2/4/8 schedule); with three attempts only the 2- and 4-second waits occur.
  A longer `Retry-After`, expressed as seconds or an HTTP date, takes precedence.
  Other HTTP errors fail that page immediately.
- A failed page does not prevent later pages from being attempted. If page 1
  fails, the watcher probes later pages up to the limit until pagination metadata
  becomes available. Errors are listed in JSON or printed to stderr in human mode.
- The preferred parser decodes the double-JSON prerendered state. If it is
  absent, BeautifulSoup reads visible listing cards and pagination links.
  Fallback data may lack descriptions, region, exact timestamps, or numeric
  prices; its total is an estimate from visible cards. Unrecognized HTML and
  malformed embedded state are errors rather than silently empty baselines.
- `--max-pages` intentionally caps traversal; reaching the cap alone is not an
  error. `total` from embedded state is the server's count, not the number of
  stored or fetched ads.

| Exit code | Meaning |
| --- | --- |
| `0` | All requested pages succeeded, including baseline or no new ads |
| `1` | Fatal polling failure (no page parsed), or local storage/output failure |
| `2` | Partial poll: at least one parsed page and at least one failed page |

Invalid command-line syntax is rejected by argparse with exit code 2 before
polling. Keep SQLite state between runs; deleting it intentionally creates a new
baseline on the next successful run.

## Scheduling example

This is an example for manual configuration only; the watcher never creates or
modifies a crontab. Replace the paths with your installation:

```cron
0 * * * * cd /absolute/path/to/olx-watch && /absolute/path/to/olx-watch/.venv/bin/python olx_watch.py --db /absolute/path/to/olx-watch/olx_watch.sqlite3 --once >> /absolute/path/to/olx-watch/olx_watch.log 2>&1
```

Each invocation runs once. Do not start overlapping polls using the same
database. Human stdout can be consumed by an external notification system; the
watcher itself does not send email or messages.

## Verification

The tests use the saved real listing and a synthetic fallback fixture. HTTP
requests are mocked; tests never call the network.

```bash
.venv/bin/python -m pytest
uvx ruff check .
uvx ruff format --check .
uvx pyright
.venv/bin/python olx_watch.py --db /tmp/olx_test.sqlite3 --once
```

The last command is a real network smoke check and changes only the selected
SQLite database. Repeating it against the same database checks steady-state
behavior. The Pyright configuration points at `.venv` for installed dependencies.
