"""Проверка объявления по ссылке: «Поделиться» → Сигнал.kg на телефоне."""
import asyncio
import json

import httpx
import pytest
from fastapi.testclient import TestClient

from server.app import create_app
from server.listing import (
    FetchError, HttpFetcher, Page, canonical_url, extract_coords, extract_title, find_listing_url,
)

HOUSE_URL = "https://www.house.kg/details/1629026afdba7fe86e26-56591156"
LALAFO_URL = "https://lalafo.kg/bishkek/ads/sdau-2h-kom-id-48196321"


def house_page(lat=42.8687, lon=74.604, title="2-комн. кв., Раззакова / Боконбаева"):
    ld = {"@type": "Product", "contentLocation": {"geo": {"@type": "GeoCoordinates", "latitude": lat, "longitude": lon}}}
    return (f'<html><head><meta property="og:title" content="{title}">'
            f'<script type="application/ld+json">{json.dumps(ld)}</script></head></html>')


def lalafo_page(ad_id=48196321, lat=42.85625521708996, lng=74.58782376781599):
    data = {"props": {"pageProps": {"similar": [{"id": 1, "lat": 42.80, "lng": 74.50}],
                                    "ad": {"id": ad_id, "lat": lat, "lng": lng}}}}
    return f'<script id="__NEXT_DATA__" type="application/json">{json.dumps(data)}</script>'


class FakeFetcher:
    def __init__(self, pages):
        self.pages = pages
        self.calls = []

    async def fetch(self, url):
        self.calls.append(url)
        result = self.pages.get(url)
        if isinstance(result, Exception):
            raise result
        if result is None:
            return Page(404, url, "")
        return Page(200, url, result)


@pytest.fixture
def make_client(tmp_path):
    def make(pages):
        fetcher = FakeFetcher(pages)
        app = create_app(complaints_file=tmp_path / "c.jsonl", dgis_key="", listing_fetcher=fetcher)
        return TestClient(app), fetcher
    return make


# --- ссылка из текста «Поделиться» ---------------------------------------------

@pytest.mark.parametrize("text, expected", [
    (HOUSE_URL, HOUSE_URL),
    (f"Сдаётся 2-комн. квартира {HOUSE_URL}?utm_source=app.", HOUSE_URL),
    ("https://m.house.kg/details/abc-1", "https://www.house.kg/details/abc-1"),
    ("https://house.kg/details/abc-1#photos", "https://www.house.kg/details/abc-1"),
    (f"Смотри: {LALAFO_URL}", LALAFO_URL),
    ("https://m.lalafo.kg/bishkek/ads/x-id-5", "https://lalafo.kg/bishkek/ads/x-id-5"),
    ("https://www.lalafo.kg/bishkek/ads/x-id-5?ref=share", "https://lalafo.kg/bishkek/ads/x-id-5"),
])
def test_find_listing_url(text, expected):
    assert find_listing_url(text) == expected


@pytest.mark.parametrize("text", [
    "", "просто текст без ссылки",
    "https://evil.com/house.kg/details/abc-1",
    "https://house.kg.evil.com/details/abc-1",
    "https://www.house.kg/snyat-kvartiru",             # список, а не объявление
    "https://lalafo.kg/bishkek/kvartiry",               # список, а не объявление
    "ftp://house.kg/details/abc-1",
    "https://user@house.kg/details/abc-1",
    "https://house.kg:8443/details/abc-1",
])
def test_find_listing_url_rejects(text):
    assert find_listing_url(text) is None


def test_canonical_url_only_for_allowed_sites():
    assert canonical_url("https://m.house.kg/details/a-1") == "https://www.house.kg/details/a-1"
    assert canonical_url("https://example.com/details/a-1") is None


# --- разбор страницы ---------------------------------------------------------

def test_extract_house_coords_and_title():
    html = house_page(title="2-комн. кв. &amp; балкон")
    assert extract_coords(html, HOUSE_URL) == (42.8687, 74.604)
    assert extract_title(html) == "2-комн. кв. & балкон"


def test_extract_lalafo_takes_ad_with_id_from_url():
    assert extract_coords(lalafo_page(), LALAFO_URL) == (42.85625521708996, 74.58782376781599)


def test_extract_rejects_placeholder_and_foreign_points():
    assert extract_coords(lalafo_page(lat=42.8746212, lng=74.5697617), LALAFO_URL) is None
    assert extract_coords(house_page(lat=55.75, lon=37.6), HOUSE_URL) is None
    assert extract_coords("<html></html>", HOUSE_URL) is None
    assert extract_coords('<script type="application/ld+json">{oops</script>', HOUSE_URL) is None


# --- API /api/listing ----------------------------------------------------------

def test_listing_inside_area(make_client):
    client, _ = make_client({HOUSE_URL: house_page()})
    r = client.get("/api/listing", params={"url": HOUSE_URL})
    assert r.status_code == 200
    body = r.json()
    assert body["site"] == "house.kg" and body["url"] == HOUSE_URL
    assert body["title"] == "2-комн. кв., Раззакова / Боконбаева"
    assert (body["lat"], body["lon"]) == (42.8687, 74.604)
    assert body["in_area"] is True
    assert body["signal"]["status"] == "ok" and body["signal"]["cell_id"] == "X017_Y015"


def test_listing_link_inside_shared_text(make_client):
    client, _ = make_client({LALAFO_URL: lalafo_page()})
    r = client.get("/api/listing", params={"text": f"Сдаю 2х ком. полдома {LALAFO_URL}", "title": "lalafo"})
    assert r.status_code == 200 and r.json()["site"] == "lalafo.kg"


def test_listing_outside_measured_area(make_client):
    client, _ = make_client({HOUSE_URL: house_page(lat=42.90, lon=74.62)})
    body = client.get("/api/listing", params={"url": HOUSE_URL}).json()
    assert body["in_area"] is False and body["signal"] is None


@pytest.mark.parametrize("pages, code, error", [
    ({HOUSE_URL: "<html>нет координат</html>"}, 422, "no_coordinates"),
    ({}, 404, "not_found"),
    ({HOUSE_URL: FetchError("timeout")}, 502, "site_unavailable"),
])
def test_listing_errors(make_client, pages, code, error):
    client, _ = make_client(pages)
    r = client.get("/api/listing", params={"url": HOUSE_URL})
    assert r.status_code == code and r.json()["error"] == error


def test_listing_removed_redirects_to_home_is_not_found(make_client):
    # house.kg отвечает на снятое объявление редиректом 308 на главную страницу.
    client, fetcher = make_client({})
    fetcher.fetch = lambda url: _page(Page(200, "https://www.house.kg/", "<html>главная</html>"))
    r = client.get("/api/listing", params={"url": HOUSE_URL})
    assert r.status_code == 404 and r.json()["error"] == "not_found"


async def _page(page):
    return page


def test_listing_lalafo_without_address(make_client):
    client, _ = make_client({LALAFO_URL: lalafo_page(lat=42.8746212, lng=74.5697617)})
    r = client.get("/api/listing", params={"url": LALAFO_URL})
    assert r.status_code == 422 and r.json()["error"] == "no_coordinates"


@pytest.mark.parametrize("params", [{}, {"url": "https://example.com/details/a-1"}, {"text": "без ссылки"}])
def test_listing_needs_supported_link(make_client, params):
    client, fetcher = make_client({})
    r = client.get("/api/listing", params=params)
    assert r.status_code == 400 and r.json()["error"] == "unsupported_link"
    assert fetcher.calls == []


def test_listing_is_cached_but_failures_are_not(make_client):
    client, fetcher = make_client({HOUSE_URL: FetchError("timeout")})
    assert client.get("/api/listing", params={"url": HOUSE_URL}).status_code == 502
    fetcher.pages[HOUSE_URL] = house_page()
    for _ in range(3):
        assert client.get("/api/listing", params={"url": "https://m.house.kg/details/1629026afdba7fe86e26-56591156"}).status_code == 200
    assert fetcher.calls == [HOUSE_URL, HOUSE_URL]


def test_listing_rate_limit_per_ip(make_client):
    client, _ = make_client({})
    codes = [client.get("/api/listing", params={"url": f"https://www.house.kg/details/a-{i}"}).status_code for i in range(25)]
    assert codes[:20] == [404] * 20 and codes[20] == 429


# --- загрузка страниц: только house.kg и lalafo.kg ---------------------------------

def run(coro):
    return asyncio.run(coro)


def test_fetcher_follows_redirects_only_inside_allowed_sites():
    def handler(request):
        if request.url.host == "m.house.kg":
            return httpx.Response(301, headers={"Location": "https://www.house.kg/details/a-1"})
        if request.url.host == "www.house.kg":
            return httpx.Response(200, text="ok")
        return httpx.Response(500)
    page = run(HttpFetcher(transport=httpx.MockTransport(handler)).fetch("https://m.house.kg/details/a-1"))
    assert page.status == 200 and page.html == "ok" and page.url == "https://www.house.kg/details/a-1"


@pytest.mark.parametrize("location", ["http://169.254.169.254/latest/meta-data", "https://evil.com/", "file:///etc/passwd"])
def test_fetcher_refuses_redirect_outside(location):
    transport = httpx.MockTransport(lambda request: httpx.Response(302, headers={"Location": location}))
    with pytest.raises(FetchError):
        run(HttpFetcher(transport=transport).fetch("https://www.house.kg/details/a-1"))


def test_fetcher_limits_redirects_and_size():
    loop = httpx.MockTransport(lambda request: httpx.Response(302, headers={"Location": "https://www.house.kg/details/a-1"}))
    with pytest.raises(FetchError):
        run(HttpFetcher(transport=loop).fetch("https://www.house.kg/details/a-1"))
    huge = httpx.MockTransport(lambda request: httpx.Response(200, content=b"x" * (6 * 1024 * 1024)))
    with pytest.raises(FetchError):
        run(HttpFetcher(transport=huge).fetch("https://www.house.kg/details/a-1"))


def test_fetcher_network_error_is_fetch_error():
    def boom(request):
        raise httpx.ConnectTimeout("timeout")
    with pytest.raises(FetchError):
        run(HttpFetcher(transport=httpx.MockTransport(boom)).fetch("https://www.house.kg/details/a-1"))


# --- «Поделиться» в манифесте приложения ------------------------------------------

def test_app_manifest_has_share_target(make_client):
    client, _ = make_client({})
    share = client.get("/app/manifest.webmanifest").json()["share_target"]
    assert share["action"] == "/app/" and share["method"] == "GET"
    assert share["params"] == {"title": "title", "text": "text", "url": "url"}
