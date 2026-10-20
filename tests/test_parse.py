"""Offline acceptance tests for parsing, persistence, retries, and CLI output."""

import csv
import json
import sqlite3
from datetime import UTC
from pathlib import Path

import httpx
import pytest

import olx_watch as watch

FIXTURE = Path(__file__).parent / "fixtures" / "listing.html"


def ad(ad_id=1, value=100, **changes):
    """Create a representative embedded-state advertisement."""
    result = {
        "id": ad_id,
        "title": f"Keyboard {ad_id}",
        "description": "A keyboard",
        "url": f"https://www.olx.ua/d/uk/obyavlenie/test-{ad_id}.html",
        "isActive": True,
        "createdTime": "2026-10-20T10:00:00+03:00",
        "price": {
            "displayValue": f"{value} грн.",
            "regularPrice": {"value": value, "currencyCode": "UAH"},
        },
        "location": {"cityName": "Kyiv", "regionName": "Kyiv region"},
    }
    result.update(changes)
    return result


def page(ads, total=None, pages=1):
    """Encode exactly the double-JSON prerendered-state format."""
    state = {
        "listing": {
            "listing": {
                "ads": ads,
                "totalElements": len(ads) if total is None else total,
                "totalPages": pages,
                "pageNumber": 0,
            }
        }
    }
    return (
        "<script>window.__PRERENDERED_STATE__ = "
        + json.dumps(json.dumps(state))
        + ";</script>"
    )


def mock_client(handler):
    return httpx.Client(transport=httpx.MockTransport(handler))


def invoke(tmp_path, html, *args):
    with mock_client(lambda request: httpx.Response(200, text=html)) as client:
        return watch.main(["--db", str(tmp_path / "ads.sqlite3"), *args], client=client)


def test_real_fixture():
    parsed = watch.parse_page(FIXTURE.read_text(), watch.DEFAULT_URL)
    assert parsed.total == 63
    assert parsed.total_pages == 2
    assert len(parsed.ads) == len({a.id for a in parsed.ads})
    first = parsed.ads[0]
    assert first.id == 938028766
    assert first.price_value == 109000
    assert first.price_currency == "UAH"
    assert first.location == "Київ"
    assert first.description
    assert first.raw["isActive"] is True


def test_active_filter_dedup_and_escaped_strings():
    parsed = watch.parse_page(
        page(
            [
                ad(title='A "quoted" title \\ backslash'),
                ad(),
                ad(2, isActive=False),
                ad(3, isActive=1),
                ad(4, isActive=None),
            ]
        ),
        watch.DEFAULT_URL,
    )
    assert [a.id for a in parsed.ads] == [1]
    assert parsed.ads[0].title == 'A "quoted" title \\ backslash'


def test_fallback():
    html = (Path(__file__).parent / "fixtures" / "fallback.html").read_text()
    parsed = watch.parse_page(html, watch.DEFAULT_URL)
    assert parsed.total == 1
    assert parsed.total_pages == 2
    item = parsed.ads[0]
    assert item.id == 42
    assert item.title == "Wooting 60HE"
    assert item.price_value == 6500
    assert item.price_currency == "UAH"
    assert item.location == "Kyiv, Centre"
    assert item.url == "https://www.olx.ua/d/uk/obyavlenie/test.html"


@pytest.mark.parametrize(
    "html",
    [
        "<html>Blocked</html>",
        '<script>window.__PRERENDERED_STATE__ = "broken";</script>',
        page([{"id": 1, "isActive": True}]),
    ],
)
def test_bad_pages_raise(html):
    with pytest.raises(watch.ParseError):
        watch.parse_page(html, watch.DEFAULT_URL)


def test_baseline_new_and_price_changes(tmp_path, capsys):
    assert invoke(tmp_path, page([ad()]), "--json") == 0
    baseline = json.loads(capsys.readouterr().out)
    assert baseline["first_run"] is True
    assert baseline["new"] == []
    assert baseline["price_changes"] == []
    assert (
        invoke(
            tmp_path,
            page([ad(value=90), ad(2)]),
            "--json",
            "--include-description",
            "--export",
            str(tmp_path / "new.csv"),
        )
        == 0
    )
    changed = json.loads(capsys.readouterr().out)
    assert changed["first_run"] is False
    assert [a["id"] for a in changed["new"]] == [2]
    assert changed["new"][0]["description"] == "A keyboard"
    assert changed["price_changes"][0]["old_price_value"] == 100
    assert changed["price_changes"][0]["price_value"] == 90
    with (tmp_path / "new.csv").open() as stream:
        rows = list(csv.DictReader(stream))
    assert [r["id"] for r in rows] == ["2"]
    with sqlite3.connect(tmp_path / "ads.sqlite3") as db:
        assert db.execute("SELECT COUNT(*) FROM ads").fetchone()[0] == 2
        assert db.execute(
            "SELECT price_value FROM price_history WHERE ad_id=1 ORDER BY rowid"
        ).fetchall() == [(100,), (90,)]
        assert db.execute("SELECT new_count FROM runs ORDER BY id").fetchall() == [
            (0,),
            (1,),
        ]
        assert (
            json.loads(db.execute("SELECT raw_json FROM ads WHERE id=1").fetchone()[0])[
                "price"
            ]["regularPrice"]["value"]
            == 90
        )
    assert invoke(tmp_path, page([ad(value=90), ad(2)]), "--json") == 0
    unchanged = json.loads(capsys.readouterr().out)
    assert unchanged["new"] == unchanged["price_changes"] == []


def test_empty_baseline_and_human_output(tmp_path, capsys):
    assert invoke(tmp_path, page([])) == 0
    assert capsys.readouterr().out == "Baseline created: 0 ads stored.\n"
    assert invoke(tmp_path, page([ad()]), "--include-description") == 0
    output = capsys.readouterr().out
    assert "NEW: Keyboard 1" in output
    assert "A keyboard" in output
    assert "2026-10-20T10:00:00+03:00" in output
    assert invoke(tmp_path, page([ad()])) == 0
    assert capsys.readouterr().out == "No new ads (known=1, total=1).\n"


def test_description_opt_in(tmp_path, capsys):
    invoke(tmp_path, page([]), "--json")
    capsys.readouterr()
    invoke(tmp_path, page([ad()]), "--json")
    assert "description" not in json.loads(capsys.readouterr().out)["new"][0]


def test_partial_pages_and_query_preserved(tmp_path, capsys):
    requests = []

    def handler(request):
        requests.append(request.url)
        number = request.url.params.get("page", "1")
        if number == "2":
            return httpx.Response(404)
        return httpx.Response(200, text=page([ad(int(number))], total=3, pages=3))

    with mock_client(handler) as client:
        code = watch.main(
            [
                "--db",
                str(tmp_path / "ads.sqlite3"),
                "--json",
                "--url",
                watch.DEFAULT_URL + "?sort_by=created_at:desc&page=9",
            ],
            client=client,
        )
    result = json.loads(capsys.readouterr().out)
    assert code == 2
    assert len(requests) == 3
    assert all(u.params["sort_by"] == "created_at:desc" for u in requests)
    assert requests[0].params.get("page") is None
    assert result["known"] == 2
    assert result["errors"]


def test_first_page_failure_can_recover(tmp_path, capsys):
    def handler(request):
        if "page" not in request.url.params:
            return httpx.Response(404)
        return httpx.Response(200, text=page([ad(2)], pages=2, total=2))

    with mock_client(handler) as client:
        code = watch.main(
            ["--db", str(tmp_path / "ads.sqlite3"), "--max-pages", "2", "--json"],
            client=client,
        )
    assert code == 2
    assert json.loads(capsys.readouterr().out)["known"] == 1


def test_fatal_run_does_not_create_baseline(tmp_path, capsys):
    assert invoke(tmp_path, "<html>Blocked</html>", "--max-pages", "1", "--json") == 1
    assert json.loads(capsys.readouterr().out)["errors"]
    assert invoke(tmp_path, page([ad()]), "--json") == 0
    assert json.loads(capsys.readouterr().out)["first_run"] is True


def test_max_pages(tmp_path, capsys):
    requests = []

    def handler(request):
        requests.append(request)
        return httpx.Response(200, text=page([ad()], total=1000, pages=50))

    with mock_client(handler) as client:
        assert (
            watch.main(
                ["--db", str(tmp_path / "ads.sqlite3"), "--max-pages", "2", "--json"],
                client=client,
            )
            == 0
        )
    assert len(requests) == 2
    assert json.loads(capsys.readouterr().out)["total"] == 1000


@pytest.mark.parametrize("failure", ["network", "500", "429", "parse"])
def test_retries(failure):
    count = 0
    sleeps = []

    def handler(request):
        nonlocal count
        count += 1
        if count < 3:
            if failure == "network":
                raise httpx.ConnectError("offline", request=request)
            if failure == "parse":
                return httpx.Response(200, text="invalid")
            return httpx.Response(int(failure), headers={"Retry-After": "5"})
        return httpx.Response(200, text=page([ad()]))

    with mock_client(handler) as client:
        assert watch.fetch_page(client, watch.DEFAULT_URL, sleep=sleeps.append).ads
    assert count == 3
    assert sleeps == ([5, 5] if failure in {"500", "429"} else [2, 4])


def test_retry_exhaustion():
    requests = []
    sleeps = []

    def handler(request):
        requests.append(request)
        return httpx.Response(503)

    with mock_client(handler) as client, pytest.raises(watch.FetchError):
        watch.fetch_page(client, watch.DEFAULT_URL, sleep=sleeps.append)
    assert len(requests) == 3
    assert sleeps == [2, 4]


def test_currency_change(tmp_path, capsys):
    invoke(tmp_path, page([ad()]), "--json")
    capsys.readouterr()
    changed = ad(
        price={
            "displayValue": "$100",
            "regularPrice": {"value": 100, "currencyCode": "USD"},
        }
    )
    invoke(tmp_path, page([changed]), "--json")
    change = json.loads(capsys.readouterr().out)["price_changes"][0]
    assert change["old_price_currency"] == "UAH"
    assert change["price_currency"] == "USD"


def test_database_failure_is_json(tmp_path, capsys):
    with mock_client(lambda request: httpx.Response(200, text=page([]))) as client:
        code = watch.main(
            ["--db", str(tmp_path / "absent" / "db"), "--json"], client=client
        )
    assert code == 1
    assert json.loads(capsys.readouterr().out)["errors"]


def test_export_failure_does_not_consume_new_ads(tmp_path, capsys):
    invoke(tmp_path, page([]), "--json")
    capsys.readouterr()
    assert (
        invoke(
            tmp_path,
            page([ad()]),
            "--json",
            "--export",
            str(tmp_path / "absent" / "new.csv"),
        )
        == 1
    )
    failure = json.loads(capsys.readouterr().out)
    assert failure["errors"]
    assert failure["new"] == []
    assert invoke(tmp_path, page([ad()]), "--json") == 0
    assert [item["id"] for item in json.loads(capsys.readouterr().out)["new"]] == [1]


def test_retry_after_http_date():
    from datetime import datetime, timedelta
    from email.utils import format_datetime

    retry_at = datetime.now(UTC) + timedelta(seconds=60)
    response = httpx.Response(429, headers={"Retry-After": format_datetime(retry_at)})
    assert 58 < watch.retry_delay(response, 0) <= 60
    assert (
        watch.retry_delay(httpx.Response(429, headers={"Retry-After": "bad"}), 1) == 4
    )


def test_non_retryable_status():
    sleeps = []
    with (
        mock_client(lambda request: httpx.Response(403)) as client,
        pytest.raises(watch.FetchError),
    ):
        watch.fetch_page(client, watch.DEFAULT_URL, sleep=sleeps.append)
    assert sleeps == []


def test_request_headers_timeout_and_baseline_human(tmp_path, capsys):
    def handler(request):
        assert "Chrome/" in request.headers["User-Agent"]
        assert request.headers["Accept-Language"] == "uk-UA,uk;q=0.9"
        assert request.extensions["timeout"]["read"] == 30
        return httpx.Response(200, text=page([ad()]))

    with mock_client(handler) as client:
        assert watch.main(["--db", str(tmp_path / "ads.sqlite3")], client=client) == 0
    assert capsys.readouterr().out == "Baseline created: 1 ads stored.\n"


def test_promoted_flag_and_cookie_loader(tmp_path):
    """Promoted ads are flagged and cookie exports become a Cookie header."""
    from olx_watch import load_cookies, normalize_ad

    ad = normalize_ad(
        {
            "id": 1,
            "title": "Wooting 60HE",
            "url": "https://www.olx.ua/d/uk/obyavlenie/x.html",
            "isPromoted": True,
            "price": {"regularPrice": {"value": 5499, "currencyCode": "UAH"}},
            "location": {"cityName": "Київ", "regionName": "Київська область"},
        },
        "https://www.olx.ua/uk/list/q-Wooting-60HE/",
    )
    assert ad.promoted is True
    assert ad.public()["promoted"] is True

    jar = tmp_path / "cookies.txt"
    jar.write_text(
        "# Netscape HTTP Cookie File\n"
        "#HttpOnly_.olx.ua\tTRUE\t/\tTRUE\t0\tdatadome\tabc\n"
        ".olx.ua\tTRUE\t/\tTRUE\t0\tlang\tuk\n",
        encoding="utf-8",
    )
    assert load_cookies(str(jar)) == "datadome=abc; lang=uk"
    assert load_cookies(None) == ""
