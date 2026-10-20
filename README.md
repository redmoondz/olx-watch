# OLX Watch

A single-file Python CLI that polls an OLX.ua search, stores advertisements in
SQLite, and reports new ads and changed prices. The default search is
<https://www.olx.ua/uk/list/q-Wooting-60HE/>. No authentication or JavaScript
browser is needed.

## TL;DR

```bash
git clone https://github.com/redmoondz/olx-watch.git
cd olx-watch
uv venv .venv && uv pip install --python .venv/bin/python -r requirements.txt
.venv/bin/python olx_watch.py --once                      # one poll
.venv/bin/python olx_watch.py --once --json               # machine-readable
```

Watching real Wooting 60HE keyboards only, ignoring paid placements:

```bash
.venv/bin/python olx_watch.py --once \
  --cookies-file cookies.txt --exclude-promoted --only-wooting-60he
```

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
| `--cookies-file PATH` | Disabled | Netscape or `name=value` cookie export sent with every request |
| `--exclude-promoted` | Off | Skip paid ("promoted") ads so counts match organic results |
| `--only-wooting-60he` | Off | Keep only Wooting 60HE/60HE+/v2 boards, drop switches and accessories |

The first successful poll creates a baseline and prints:

```text
Baseline created: 63 ads stored.
```

It stores all observed active ads (minus promoted ads when
`--exclude-promoted` is set) without reporting them as new. An empty
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

## Filters

Three independent filters, all off by default so the CLI stays a plain watcher.

### `--only-wooting-60he`

The search `q-Wooting-60HE` returns more than keyboards: switch samples,
modding kits, wrist rests, consultations, and other brands pulled in by seller
keywords. This filter keeps an ad only when the title names Wooting
(`wooting`, `вутинг`, `вутінг`) **and** a 60HE model, while rejecting titles
that match accessory words (`свитч`, `switch`, `комплект`, `моддинг`, `wrist`,
`keycap`, `кабель`, `консультац`, `кейс`).

On a live run it cut 63 results to 16 boards. It is intentionally
title-based — a listing that mentions 60HE only in the description is dropped.
80HE-only boards are dropped; a title mixing both (`80HE | 60HE+ Zinc`) passes.

### `--exclude-promoted`

OLX mixes `isPromoted: true` placements into the first page and duplicates
them. Dropping them makes the count match the organic result a human sees.
Promoted ads are then neither stored nor reported, so they never appear as
"new" later.

### `--cookies-file PATH`

Anonymous responses come back with an empty `location` object and a slightly
different ad mix. A logged-in cookie export fixes the cities/regions and
stabilizes the session. Accepts Netscape (`#HttpOnly_` lines included) and
plain `name=value` exports.

```bash
# export once from the browser, then
chmod 600 cookies.txt
.venv/bin/python olx_watch.py --once --cookies-file cookies.txt
```

Cookies expire; when cities go blank or requests start returning 403, export
again. `cookies.txt` is git-ignored — never commit it.

## Hourly cron wrapper

`run_hourly.sh` is a ready-made wrapper: it polls with all filters, keeps
`logs/poll.log` under 2 MB, and delivers new ads to Telegram through the
channel API — no LLM and no model provider in the loop.

```bash
# crontab -e
5 * * * * /path/to/olx-watch/run_hourly.sh >> /path/to/olx-watch/logs/cron.out 2>&1
```

Notifications are sent only when something new appears or the poll fails, so a
quiet hour produces no message. The send path is:

```bash
openclaw message send --channel telegram --target <chat_id> --message "..."
```

If you do not use OpenClaw, replace that block with `curl` against the Telegram
Bot API or any notifier; the script's contract is simply "stdout contains
`NEW:` blocks when there is news".
