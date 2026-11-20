"""Объявление по ссылке: координаты дома и качество связи в нём.

Нужно для «Поделиться» → Сигнал.kg на телефоне: в приложениях house.kg и lalafo
плагин не работает, поэтому страницу объявления загружает сервер. Он ходит
только на house.kg и lalafo.kg (в том числе по редиректам), с таймаутом и
ограничением размера, и разбирает страницу так же, как плагин (extension/extract.js).
"""
import html as html_lib
import json
import re
import time
from collections import OrderedDict, deque
from dataclasses import dataclass
from urllib.parse import urljoin, urlsplit

import httpx

# Хост → (сайт, канонический хост). m.* и без www — те же сайты.
HOSTS = {
    "house.kg": ("house.kg", "www.house.kg"),
    "www.house.kg": ("house.kg", "www.house.kg"),
    "m.house.kg": ("house.kg", "www.house.kg"),
    "lalafo.kg": ("lalafo.kg", "lalafo.kg"),
    "www.lalafo.kg": ("lalafo.kg", "lalafo.kg"),
    "m.lalafo.kg": ("lalafo.kg", "lalafo.kg"),
}
LISTING_PATH = {
    "house.kg": re.compile(r"^/details/[\w-]+/?$"),
    "lalafo.kg": re.compile(r"^/[\w/-]*-id-\d+/?$"),
}
URL_IN_TEXT = re.compile(r"https?://[^\s<>\"'«»]+", re.I)

# Широкая рамка вокруг Бишкека, как в плагине: всё за ней — мусор или другой город.
BISHKEK = (42.6, 43.1, 74.3, 74.9)
# «Город по умолчанию»: lalafo ставит эту точку, когда адрес не указан (~10 % объявлений).
PLACEHOLDERS = [(42.8746212, 74.5697617)]

TIMEOUT_S = 8.0
MAX_BYTES = 5 * 1024 * 1024
MAX_REDIRECTS = 3
USER_AGENT = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/131.0 Safari/537.36")


class FetchError(Exception):
    """Сайт объявления недоступен или ответил не так, как ожидалось."""


@dataclass
class Page:
    status: int
    url: str
    html: str


def canonical_url(raw: str) -> str | None:
    """https://<основной хост><путь> для поддерживаемых сайтов; иначе None."""
    try:
        parts = urlsplit(raw.strip())
        port = parts.port
    except ValueError:
        return None
    host = (parts.hostname or "").lower()
    if parts.scheme not in ("http", "https") or host not in HOSTS or parts.username or parts.password:
        return None
    if port not in (None, 80, 443):
        return None
    _, main_host = HOSTS[host]
    return f"https://{main_host}{parts.path or '/'}"


def site_of(url: str) -> str | None:
    host = (urlsplit(url).hostname or "").lower()
    return HOSTS[host][0] if host in HOSTS else None


def find_listing_url(text: str | None) -> str | None:
    """Первая ссылка на объявление house.kg или lalafo.kg в тексте «Поделиться»."""
    for match in URL_IN_TEXT.finditer(text or ""):
        url = canonical_url(match.group(0).rstrip(".,;:!?)]}"))
        if url and LISTING_PATH[site_of(url)].match(urlsplit(url).path):
            return url.rstrip("/") if site_of(url) == "lalafo.kg" else url
    return None


def _point(lat, lon) -> tuple[float, float] | None:
    try:
        lat, lon = float(lat), float(lon)
    except (TypeError, ValueError):
        return None
    lat_min, lat_max, lon_min, lon_max = BISHKEK
    if not (lat_min <= lat <= lat_max and lon_min <= lon <= lon_max):
        return None
    if any(abs(lat - a) < 1e-6 and abs(lon - b) < 1e-6 for a, b in PLACEHOLDERS):
        return None
    return lat, lon


def _scripts(page: str, pattern: str) -> list[str]:
    return re.findall(pattern, page, re.I | re.S)


def _find(value, test):
    """Обход JSON без рекурсии: глубина вложенности на чужих страницах не гарантирована."""
    stack, steps = [value], 0
    while stack and steps < 200_000:
        steps += 1
        node = stack.pop()
        if isinstance(node, dict):
            hit = test(node)
            if hit:
                return hit
            stack.extend(node.values())
        elif isinstance(node, list):
            stack.extend(node)
    return None


def _json(text: str):
    try:
        return json.loads(text)
    except ValueError:
        return None


def extract_coords(page: str, url: str) -> tuple[float, float] | None:
    # house.kg: JSON-LD с GeoCoordinates {latitude, longitude}.
    for text in _scripts(page, r"<script[^>]*type=[\"']application/ld\+json[\"'][^>]*>(.*?)</script>"):
        hit = _find(_json(text), lambda n: _point(n.get("latitude"), n.get("longitude"))
                    if n.get("@type") == "GeoCoordinates" or "latitude" in n else None)
        if hit:
            return hit
    # lalafo.kg: __NEXT_DATA__, объект с тем же id, что в адресе (…-id-48196321).
    ad = re.search(r"-id-(\d+)", url or "")
    if ad:
        ad_id = int(ad.group(1))
        for text in _scripts(page, r"<script[^>]*id=[\"']__NEXT_DATA__[\"'][^>]*>(.*?)</script>"):
            hit = _find(_json(text), lambda n: _point(n.get("lat"), n.get("lng", n.get("lon")))
                        if n.get("id") == ad_id and "lat" in n else None)
            if hit:
                return hit
    return None


def extract_title(page: str) -> str | None:
    match = re.search(r"<meta[^>]+property=[\"']og:title[\"'][^>]+content=[\"']([^\"']*)", page, re.I)
    if not match:
        match = re.search(r"<title[^>]*>(.*?)</title>", page, re.I | re.S)
    title = " ".join(html_lib.unescape(match.group(1)).split()) if match else ""
    return title[:200] or None


class HttpFetcher:
    """Загрузка страницы объявления: только разрешённые хосты, таймаут, лимит размера."""

    def __init__(self, transport: httpx.AsyncBaseTransport | None = None):
        self.transport = transport

    async def fetch(self, url: str) -> Page:
        headers = {"User-Agent": USER_AGENT, "Accept": "text/html", "Accept-Language": "ru"}
        try:
            async with httpx.AsyncClient(transport=self.transport, timeout=TIMEOUT_S,
                                         follow_redirects=False, headers=headers) as client:
                for _ in range(MAX_REDIRECTS + 1):
                    async with client.stream("GET", url) as response:
                        if response.is_redirect:
                            target = canonical_url(urljoin(url, response.headers.get("location", "")))
                            if not target:
                                raise FetchError("редирект на посторонний сайт")
                            url = target
                            continue
                        body = bytearray()
                        async for chunk in response.aiter_bytes():
                            body += chunk
                            if len(body) > MAX_BYTES:
                                raise FetchError("страница слишком большая")
                        return Page(response.status_code, url, body.decode(response.encoding or "utf-8", "replace"))
                raise FetchError("слишком много редиректов")
        except httpx.HTTPError as exc:
            raise FetchError(f"сайт недоступен: {type(exc).__name__}") from None


class RateLimit:
    """Не больше limit запросов в минуту с одного IP."""

    def __init__(self, limit: int, clock=time.monotonic):
        self.limit, self.clock = limit, clock
        self.hits: dict[str, deque[float]] = {}

    def wait(self, ip: str) -> int:
        now = self.clock()
        hits = self.hits.setdefault(ip, deque())
        while hits and hits[0] <= now - 60:
            hits.popleft()
        if len(hits) >= self.limit:
            return max(1, int(hits[0] + 60 - now) + 1)
        hits.append(now)
        return 0


class TtlCache:
    def __init__(self, ttl_s: float = 600, size: int = 500, clock=time.monotonic):
        self.ttl, self.size, self.clock = ttl_s, size, clock
        self.items: OrderedDict[str, tuple[float, object]] = OrderedDict()

    def get(self, key: str):
        item = self.items.get(key)
        if item is None or item[0] < self.clock():
            self.items.pop(key, None)
            return None
        self.items.move_to_end(key)
        return item[1]

    def put(self, key: str, value) -> None:
        self.items[key] = (self.clock() + self.ttl, value)
        self.items.move_to_end(key)
        while len(self.items) > self.size:
            self.items.popitem(last=False)
