"""API и панель оператора «Tolkun».

GET  /                     панель оператора (карта, «Что если», игра, жалобы)
GET  /api/signal?lat=&lon= качество связи в точке — для Chrome-плагина
GET  /api/listing?url=     связь в доме из объявления house.kg или lalafo.kg — «Поделиться» на телефоне
GET  /api/cells            сетка 200×200 м с медианами, цветами и углами квадратов
GET  /api/listings         квартиры, проверенные через «Поделиться» или плагин, — для карты в приложении
GET  /api/result           result.json
POST /api/complaints       жалоба «Здесь нет связи» {lat, lon, source, site}
POST /api/reports          отметка жителя «как ловит связь» {lat, lon, level, network, source, site}
GET  /api/reports          отметки с id больше after и число отклонённых лимитом
GET  /api/crowd?lat=&lon=  оценка жителей в радиусе 300 м — там, где нет замеров оператора
POST /api/measurements     замер силы связи жителем {lat, lon, accuracy_m, rsrp_dbm, bars, network}
GET  /api/measurements     замеры силы связи с id больше after — вкладка «Замеры» у оператора
GET  /api/complaints       жалобы с id больше after и число отклонённых лимитом

Настройки (переменные окружения):
  COVERMAP_DATA     каталог с данными (по умолчанию data/07_coverage)
  DGIS_KEY          ключ API 2ГИС для подложки панели
  COMPLAINTS_FILE   файл жалоб (по умолчанию var/complaints.jsonl)
  ALLOWED_ORIGINS   источники для CORS через запятую (по умолчанию house.kg и lalafo.kg)
"""
import json
import math
import os
import threading
import time
from statistics import median
from collections import OrderedDict, deque
from datetime import datetime, timezone
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse

from covermap.pipeline import Report, build
from covermap.render import render
from covermap.geo import haversine
from covermap.signal import SignalIndex

from .listing import (
    BISHKEK, FetchError, HttpFetcher, RateLimit, TtlCache, extract_coords, extract_title, find_listing_url, site_of,
)

ROOT = Path(__file__).resolve().parent.parent
WEBAPP = ROOT / "webapp"
FONTS_CSS = ROOT / "covermap" / "vendor" / "fonts.css"
# Файлы приложения, которые отдаются как есть: имя → тип содержимого.
VENDOR = ROOT / "covermap" / "vendor"
APP_FILES = {
    "manifest.webmanifest": (WEBAPP / "manifest.webmanifest", "application/manifest+json"),
    "sw.js": (WEBAPP / "sw.js", "text/javascript"),
    "icon.svg": (WEBAPP / "icon.svg", "image/svg+xml"),
    "icon-192.png": (WEBAPP / "icon-192.png", "image/png"),
    "icon-512.png": (WEBAPP / "icon-512.png", "image/png"),
    # Leaflet для карты в приложении — тот же, что встроен в панель.
    "leaflet.js": (VENDOR / "leaflet.js", "text/javascript"),
    "leaflet.css": (VENDOR / "leaflet.css", "text/css"),
}
LISTINGS_KEEP = 500
CROWD_RADIUS_M = 300  # отметки жителей в этом радиусе — «оценка жителей рядом»
# Замер силы связи жителем: дБм с экрана телефона (та же шкала, что RSRP в замерах
# оператора) и/или число палочек. Браузер сам силу сигнала не отдаёт — её вводит житель.
RSRP_RANGE = (-140.0, -44.0)
MEASUREMENTS_PER_MINUTE = 20  # с одного IP: приложение шлёт автозамер при каждом новом месте
# Автозамер в приложении — то, что браузер отдаёт сам: задержка, оценка сети и скорости.
CONNECTIONS = {"cellular", "wifi", "ethernet", "other", "unknown"}
EFFECTIVE = {"slow-2g", "2g", "3g", "4g"}
MAX_BARS = 4
MAX_ACCURACY_M = 5000
DEFAULT_ORIGINS = "https://house.kg,https://www.house.kg,https://lalafo.kg,https://www.lalafo.kg"
SOURCES = {"plugin", "panel", "app"}
SITES = {"house.kg", "lalafo.kg"}
MAX_BODY_BYTES = 1024
COMPLAINTS_PER_MINUTE = 5      # с одного IP
COMPLAINTS_KEEP = 5000         # сколько последних жалоб держать в памяти
# Отметка жителя «как ловит связь в этом доме»: уровень → цвет на карте.
LEVELS = {"good": "green", "ok": "yellow", "bad": "red", "none": "red"}
NETWORKS = {"2G", "3G", "4G", "5G"}
LISTINGS_PER_MINUTE = 20       # проверок объявлений с одного IP

# Content-Security-Policy. script-src с 'unsafe-inline' — приложение собрано из инлайновых
# скриптов; cdn.jsdelivr.net нужен только Swagger (/api/docs). Карта: тайлы (img https:),
# шрифты Google, пинг до speed.cloudflare.com.
CSP = (
    "default-src 'self'; base-uri 'self'; object-src 'none'; frame-ancestors 'none'; "
    "form-action 'self'; img-src 'self' data: https:; font-src 'self' data: https://fonts.gstatic.com; "
    "style-src 'self' 'unsafe-inline' https://fonts.googleapis.com https://cdn.jsdelivr.net; "
    "script-src 'self' 'unsafe-inline' https://cdn.jsdelivr.net; "
    "connect-src 'self' https://speed.cloudflare.com"
)
SECURITY_HEADERS = {
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "strict-origin-when-cross-origin",
    "Strict-Transport-Security": "max-age=31536000; includeSubDomains",
    "Content-Security-Policy": CSP,
    "Permissions-Policy": "geolocation=(self), camera=(), microphone=(), payment=(), usb=()",
    "Cross-Origin-Opener-Policy": "same-origin",
}


def error(status: int, code: str, detail: str, **headers) -> JSONResponse:
    return JSONResponse({"error": code, "detail": detail}, status_code=status, headers=headers or None)


def in_city(lat: float, lon: float) -> bool:
    """Точка в Бишкеке. Жалобы и отметки жителей принимаются по всему городу,
    а не только в районе замеров: вне района это единственные данные о связи."""
    lat_min, lat_max, lon_min, lon_max = BISHKEK
    return lat_min <= lat <= lat_max and lon_min <= lon <= lon_max


def parse_coordinate(raw) -> float | None:
    """Конечное число из строки или числа JSON; булевы значения не принимаются."""
    if isinstance(raw, bool) or raw is None:
        return None
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return None
    return value if math.isfinite(value) else None


class Complaints:
    """Жалобы жителей: в памяти и в файле JSON Lines. IP не сохраняется."""

    def __init__(self, path: Path, per_minute: int = COMPLAINTS_PER_MINUTE):
        self.path = Path(path)
        self.per_minute = per_minute
        self.items: deque[dict] = deque(maxlen=COMPLAINTS_KEEP)
        self.hits: dict[str, deque[float]] = {}
        self.lock = threading.Lock()
        self.rejected = 0  # отклонено лимитом с момента запуска
        self.clock = time.monotonic
        self._load()

    def _load(self) -> None:
        if not self.path.exists():
            return
        for line in self.path.read_text(encoding="utf-8").splitlines():
            try:
                record = json.loads(line)
            except ValueError:
                continue
            if isinstance(record, dict) and isinstance(record.get("id"), int):
                self.items.append(record)

    @property
    def last_id(self) -> int:
        return self.items[-1]["id"] if self.items else 0

    def allow(self, ip: str) -> int:
        """0 — можно; иначе сколько секунд ждать."""
        now = self.clock()
        hits = self.hits.setdefault(ip, deque())
        while hits and hits[0] <= now - 60:
            hits.popleft()
        if len(hits) >= self.per_minute:
            self.rejected += 1
            return max(1, math.ceil(hits[0] + 60 - now))
        hits.append(now)
        return 0

    def add(self, lat: float, lon: float, cell_id: str | None, source: str, site: str | None, **extra) -> dict:
        with self.lock:
            record = {
                "id": self.last_id + 1,
                "ts": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                "lat": round(lat, 6),
                "lon": round(lon, 6),
                "cell_id": cell_id,
                "source": source,
                "site": site,
                **extra,
            }
            self.items.append(record)
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with self.path.open("a", encoding="utf-8") as file:
                file.write(json.dumps(record, ensure_ascii=False) + "\n")
        return record

    def after(self, last_id: int) -> list[dict]:
        return [item for item in self.items if item["id"] > last_id]


class Listings:
    """Квартиры, которые жители проверили через «Поделиться» или плагин.

    Только публичные данные объявления: ссылка, сайт, заголовок, точка и связь в
    квадрате. Повторная проверка той же ссылки обновляет запись, а не дублирует её.
    """

    def __init__(self, path: Path):
        self.path = Path(path)
        self.items: "OrderedDict[str, dict]" = OrderedDict()
        self.lock = threading.Lock()
        if self.path.exists():
            try:
                for item in json.loads(self.path.read_text(encoding="utf-8")):
                    if isinstance(item, dict) and isinstance(item.get("url"), str):
                        self.items[item["url"]] = item
            except (ValueError, TypeError):
                self.items.clear()

    def remember(self, found: dict) -> None:
        """Запоминает проверенную квартиру. Вне района замеров — тоже, с пометкой
        «вне зоны»: оператор видит, где жители ищут жильё, а данных о связи нет."""
        signal = found.get("signal") or {}
        inside = bool(found.get("in_area")) and signal.get("status") == "ok"
        record = {
            "url": found["url"], "site": found.get("site"), "title": (found.get("title") or "")[:200],
            "lat": round(found["lat"], 6), "lon": round(found["lon"], 6), "in_area": inside,
            "cell_id": signal.get("cell_id") if inside else None,
            "color": signal.get("color") if inside else None,
            "quality": signal.get("quality") if inside else "вне зоны",
            "median": signal.get("median") if inside else None,
            "checked": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        }
        with self.lock:
            self.items.pop(record["url"], None)
            self.items[record["url"]] = record
            while len(self.items) > LISTINGS_KEEP:
                self.items.popitem(last=False)
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text(json.dumps(list(self.items.values()), ensure_ascii=False), encoding="utf-8")
            tmp.replace(self.path)

    def all(self) -> list[dict]:
        return list(reversed(self.items.values()))


class Measurements(Complaints):
    """Замеры силы связи жителями: точка, дБм и/или палочки, сеть."""


def auto_color(ping_ms: float, effective: str | None) -> str:
    """Цвет автозамера: оценка сети браузером и задержка. 4G и пинг до 150 мс — хорошо,
    2G или пинг больше 400 мс — плохо, остальное — средне."""
    if effective in ("slow-2g", "2g") or ping_ms > 400:
        return "red"
    if effective == "3g" or ping_ms > 150:
        return "yellow"
    return "green"


def signal_color(rsrp: float | None, bars: int | None) -> str:
    """Цвет замера по тем же порогам, что сетка: ≥ −90 зелёный, < −105 красный.
    Без дБм — по палочкам: 3–4 зелёный, 2 жёлтый, 0–1 красный."""
    if rsrp is not None:
        return "green" if rsrp >= -90 else "red" if rsrp < -105 else "yellow"
    return "green" if bars >= 3 else "yellow" if bars == 2 else "red"


class Reports(Complaints):
    """Отметки жителей «как ловит связь»: то же хранилище и тот же лимит, свой файл."""


def _number_in(raw, low: float, high: float) -> float | None:
    value = parse_coordinate(raw)
    return value if value is not None and low <= value <= high else None


def parse_manual(fields: dict) -> dict | None:
    """Ручной замер: дБм с экрана телефона и/или палочки."""
    raw_rsrp, bars, network = fields.get("rsrp_dbm"), fields.get("bars"), fields.get("network")
    rsrp = None if raw_rsrp is None else _number_in(raw_rsrp, *RSRP_RANGE)
    if raw_rsrp is not None and rsrp is None:
        return None
    if bars is not None and (not isinstance(bars, int) or isinstance(bars, bool) or not 0 <= bars <= MAX_BARS):
        return None
    if (rsrp is None and bars is None) or (network is not None and network not in NETWORKS):
        return None
    return {"rsrp_dbm": None if rsrp is None else round(rsrp, 1), "bars": bars, "network": network,
            "color": signal_color(rsrp, bars)}


def parse_modem(fields: dict) -> dict | None:
    """Замер из APK: уровень сигнала, который приложение само прочитало с модема телефона."""
    rsrp = _number_in(fields.get("rsrp_dbm"), *RSRP_RANGE)
    network = fields.get("network")
    if rsrp is None or (network is not None and network not in NETWORKS):
        return None
    return {"rsrp_dbm": round(rsrp, 1), "bars": None, "network": network, "color": signal_color(rsrp, None)}


def parse_auto(fields: dict) -> dict | None:
    """Автозамер: задержка до сервера замера и то, что браузер сообщает о сети."""
    ping = _number_in(fields.get("ping_ms"), 0, 20000)
    effective, connection = fields.get("effective"), fields.get("connection") or "unknown"
    rtt = None if fields.get("rtt_ms") is None else _number_in(fields.get("rtt_ms"), 0, 20000)
    downlink = None if fields.get("downlink_mbps") is None else _number_in(fields.get("downlink_mbps"), 0, 10000)
    if (ping is None or connection not in CONNECTIONS or (effective is not None and effective not in EFFECTIVE)
            or (fields.get("rtt_ms") is not None and rtt is None)
            or (fields.get("downlink_mbps") is not None and downlink is None)):
        return None
    return {"ping_ms": round(ping), "rtt_ms": None if rtt is None else round(rtt),
            "downlink_mbps": None if downlink is None else round(downlink, 2),
            "effective": effective, "connection": connection, "color": auto_color(ping, effective)}


def create_app(
    data_dir: Path | None = None,
    dgis_key: str | None = None,
    complaints_file: Path | None = None,
    reports_file: Path | None = None,
    listings_file: Path | None = None,
    measurements_file: Path | None = None,
    origins: list[str] | None = None,
    listing_fetcher=None,
) -> FastAPI:
    data_dir = Path(data_dir or os.environ.get("COVERMAP_DATA") or ROOT / "data" / "07_coverage")
    dgis_key = dgis_key if dgis_key is not None else os.environ.get("DGIS_KEY", "")
    complaints_file = Path(
        complaints_file or os.environ.get("COMPLAINTS_FILE") or ROOT / "var" / "complaints.jsonl"
    )
    reports_file = Path(reports_file or os.environ.get("REPORTS_FILE") or complaints_file.with_name("reports.jsonl"))
    listings_file = Path(listings_file or os.environ.get("LISTINGS_FILE") or complaints_file.with_name("listings.json"))
    measurements_file = Path(measurements_file or os.environ.get("MEASUREMENTS_FILE") or complaints_file.with_name("measurements.jsonl"))
    if origins is None:
        origins = [o.strip() for o in os.environ.get("ALLOWED_ORIGINS", DEFAULT_ORIGINS).split(",") if o.strip()]

    report: Report = build(
        data_dir / "measurements.csv", data_dir / "towers.csv",
        data_dir / "candidates.csv", data_dir / "bounds.json",
    )
    index = SignalIndex(report)
    panel_html = render(report, dgis_key, live=True)
    cells = [
        {
            "cell_id": c.id, "median": round(c.median, 2), "color": c.color, "samples": c.samples,
            "center": [round(x, 6) for x in report.bounds.cell_center(c.cell)],
            "corners": [[round(a, 6), round(b, 6)] for a, b in report.bounds.cell_corners(c.cell)],
        }
        for c in report.cells
    ]

    app = FastAPI(title="Tolkun", docs_url="/api/docs", redoc_url=None)
    app.state.report = report
    app.state.complaints = Complaints(complaints_file)
    app.state.reports = Reports(reports_file)
    app.state.listings = Listings(listings_file)
    app.state.measurements = Measurements(measurements_file, MEASUREMENTS_PER_MINUTE)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=origins,
        allow_methods=["GET", "POST"],
        allow_headers=["Content-Type"],
    )

    @app.middleware("http")
    async def security_headers(request: Request, call_next):
        response = await call_next(request)
        for name, value in SECURITY_HEADERS.items():
            response.headers.setdefault(name, value)
        return response

    def client_ip(request: Request) -> str:
        # Только адрес соединения: X-Forwarded-For не учитывается.
        return request.client.host if request.client else "unknown"

    @app.get("/", response_class=HTMLResponse)
    async def panel():
        return panel_html

    app_html = (WEBAPP / "app.html").read_text(encoding="utf-8").replace(
        "/*FONTS_CSS*/", FONTS_CSS.read_text(encoding="utf-8"), 1)

    @app.get("/app", include_in_schema=False)
    async def app_redirect():
        return RedirectResponse("/app/")

    @app.get("/app/", response_class=HTMLResponse, include_in_schema=False)
    async def mobile_app():
        return app_html

    @app.get("/app/{name}", include_in_schema=False)
    async def app_file(name: str):
        if name not in APP_FILES:
            return error(404, "not_found", "нет такого файла")
        path, media_type = APP_FILES[name]
        headers = {"Cache-Control": "no-cache"} if name == "sw.js" else None
        return FileResponse(path, media_type=media_type, headers=headers)

    @app.get("/tolkun.apk", include_in_schema=False)
    async def android_apk():
        # Нативное Android-приложение: тот же интерфейс + реальный замер dBm модема.
        apk = WEBAPP / "tolkun.apk"
        if not apk.exists():
            return error(404, "not_found", "APK ещё не собран")
        return FileResponse(apk, media_type="application/vnd.android.package-archive", filename="tolkun.apk")

    @app.get("/manifest.webmanifest", include_in_schema=False)
    async def panel_manifest():
        return FileResponse(WEBAPP / "panel.webmanifest", media_type="application/manifest+json")

    @app.get("/sw.js", include_in_schema=False)
    async def panel_worker():
        return FileResponse(WEBAPP / "panel-sw.js", media_type="text/javascript", headers={"Cache-Control": "no-cache"})

    @app.get("/api/signal")
    async def signal(lat: str = "", lon: str = ""):
        lat_value, lon_value = parse_coordinate(lat), parse_coordinate(lon)
        if lat_value is None or lon_value is None:
            return error(400, "bad_request", "lat и lon должны быть числами")
        result = index.lookup(lat_value, lon_value)
        if result is None:
            return error(422, "out_of_area", "точка вне района замеров")
        return result

    fetcher = listing_fetcher or HttpFetcher()
    listing_cache = TtlCache(ttl_s=600)
    listing_limit = RateLimit(LISTINGS_PER_MINUTE)

    @app.get("/api/listing")
    async def listing(request: Request, url: str = "", text: str = "", title: str = ""):
        """Связь в доме из объявления house.kg или lalafo.kg: для «Поделиться» на телефоне.

        Ссылку можно передать в url или внутри text — приложения кладут её по-разному.
        """
        link = find_listing_url(url) or find_listing_url(text) or find_listing_url(title)
        if not link:
            return error(400, "unsupported_link", "нужна ссылка на объявление house.kg или lalafo.kg")
        wait = listing_limit.wait(client_ip(request))
        if wait:
            return error(429, "rate_limited", "слишком много проверок, попробуйте позже", **{"Retry-After": str(wait)})
        cached = listing_cache.get(link)
        if cached is None:
            try:
                page = await fetcher.fetch(link)
            except FetchError:
                # Не кешируем: сайт может ожить через минуту.
                return error(502, "site_unavailable", "сайт объявления не ответил, попробуйте ещё раз")
            # house.kg на снятое объявление отвечает редиректом на главную — это тоже «нет объявления».
            if page.status in (404, 410) or not find_listing_url(page.url):
                cached = (404, "not_found", "объявление не найдено или снято")
            elif page.status != 200:
                return error(502, "site_unavailable", "сайт объявления не ответил, попробуйте ещё раз")
            elif (coords := extract_coords(page.html, page.url)) is None:
                cached = (422, "no_coordinates", "в объявлении не указан точный адрес дома")
            else:
                lat, lon = coords
                cached = {
                    "url": link, "site": site_of(link), "title": extract_title(page.html),
                    "lat": lat, "lon": lon, "in_area": report.bounds.contains(lat, lon),
                    "signal": index.lookup(lat, lon),
                }
            listing_cache.put(link, cached)
        if isinstance(cached, tuple):
            return error(*cached)
        app.state.listings.remember(cached)
        return cached

    @app.get("/api/listings")
    async def listings():
        """Проверенные квартиры для карты в приложении — новые первыми."""
        return {"listings": app.state.listings.all()}

    @app.get("/api/cells")
    async def all_cells():
        return {"cells": cells}

    @app.get("/api/result")
    async def result():
        return report.result()

    async def read_object(request: Request) -> dict | JSONResponse:
        """Тело запроса — объект JSON не больше MAX_BODY_BYTES; иначе готовый ответ с ошибкой."""
        body = await request.body()
        if len(body) > MAX_BODY_BYTES:
            return error(413, "too_large", f"тело запроса больше {MAX_BODY_BYTES} байт")
        try:
            fields = json.loads(body)
        except (ValueError, UnicodeDecodeError):
            return error(400, "bad_request", "ожидается JSON")
        if not isinstance(fields, dict):
            return error(400, "bad_request", "ожидается объект JSON")
        return fields

    @app.post("/api/complaints", status_code=201)
    async def add_complaint(request: Request):
        fields = await read_object(request)
        if isinstance(fields, JSONResponse):
            return fields
        lat, lon = parse_coordinate(fields.get("lat")), parse_coordinate(fields.get("lon"))
        source = fields.get("source", "plugin")
        site = fields.get("site")
        if lat is None or lon is None or source not in SOURCES or (site is not None and site not in SITES):
            return error(400, "bad_request", "нужны числа lat, lon, source: plugin или panel, site: house.kg, lalafo.kg или null")
        if not in_city(lat, lon):
            return error(422, "out_of_city", "точка вне Бишкека")
        wait = app.state.complaints.allow(client_ip(request))
        if wait:
            return error(429, "rate_limited", "слишком много жалоб, попробуйте позже",
                         **{"Retry-After": str(wait)})
        found = index.lookup(lat, lon)
        cell_id = found.get("cell_id") if found else None
        return app.state.complaints.add(lat, lon, cell_id, source, site)

    @app.get("/api/complaints")
    async def complaints(after: int = 0):
        return {"complaints": app.state.complaints.after(after), "rejected": app.state.complaints.rejected}

    @app.post("/api/reports", status_code=201)
    async def add_report(request: Request):
        """Отметка жителя: как на самом деле ловит связь в этом доме."""
        fields = await read_object(request)
        if isinstance(fields, JSONResponse):
            return fields
        lat, lon = parse_coordinate(fields.get("lat")), parse_coordinate(fields.get("lon"))
        level, network = fields.get("level"), fields.get("network")
        source, site = fields.get("source", "plugin"), fields.get("site")
        if (lat is None or lon is None or level not in LEVELS or source not in SOURCES
                or (network is not None and network not in NETWORKS)
                or (site is not None and site not in SITES)):
            return error(400, "bad_request",
                         "нужны числа lat, lon; level: good, ok, bad или none; network: 2G–5G или null")
        if not in_city(lat, lon):
            return error(422, "out_of_city", "точка вне Бишкека")
        wait = app.state.reports.allow(client_ip(request))
        if wait:
            return error(429, "rate_limited", "слишком много отметок, попробуйте позже",
                         **{"Retry-After": str(wait)})
        found = index.lookup(lat, lon)
        cell_id = found.get("cell_id") if found else None
        return app.state.reports.add(lat, lon, cell_id, source, site, level=level, network=network)

    @app.post("/api/measurements", status_code=201)
    async def add_measurement(request: Request):
        """Сила связи, которую житель увидел на своём телефоне, с точкой GPS.

        Нужно хотя бы одно: rsrp_dbm (−140…−44) или bars (0–4). Видно оператору
        во вкладке «Замеры» и на карте; в районе замеров — с медианой квадрата для сравнения.
        """
        fields = await read_object(request)
        if isinstance(fields, JSONResponse):
            return fields
        lat, lon = parse_coordinate(fields.get("lat")), parse_coordinate(fields.get("lon"))
        accuracy = parse_coordinate(fields.get("accuracy_m"))
        if lat is None or lon is None or accuracy is None or not 0 <= accuracy <= MAX_ACCURACY_M:
            return error(400, "bad_request", "нужны lat, lon и accuracy_m от 0 до 5000")
        method = fields.get("method", "manual")
        if method == "auto":
            values = parse_auto(fields)
        elif method == "modem":
            values = parse_modem(fields)
        elif method == "manual":
            values = parse_manual(fields)
        else:
            values = None
        if values is None:
            return error(400, "bad_request",
                         "manual: rsrp_dbm от −140 до −44 или bars 0–4; modem: rsrp_dbm; auto: ping_ms и данные сети")
        if not in_city(lat, lon):
            return error(422, "out_of_city", "точка вне Бишкека")
        wait = app.state.measurements.allow(client_ip(request))
        if wait:
            return error(429, "rate_limited", "слишком много замеров, попробуйте позже", **{"Retry-After": str(wait)})
        found = index.lookup(lat, lon)
        cell_id = found.get("cell_id") if found else None
        return app.state.measurements.add(lat, lon, cell_id, "app", None, accuracy_m=round(accuracy), method=method, **values)

    @app.get("/api/measurements")
    async def measurements(after: int = 0):
        return {"measurements": app.state.measurements.after(after), "rejected": app.state.measurements.rejected}

    @app.get("/api/crowd")
    async def crowd(lat: str = "", lon: str = ""):
        """Оценка жителей рядом с точкой: отметки «как ловит связь» и жалобы в радиусе 300 м.

        Нужна там, где нет замеров оператора: плагин и приложение показывают её вместо медианы.
        """
        lat_value, lon_value = parse_coordinate(lat), parse_coordinate(lon)
        if lat_value is None or lon_value is None:
            return error(400, "bad_request", "lat и lon должны быть числами")
        if not in_city(lat_value, lon_value):
            return error(422, "out_of_city", "точка вне Бишкека")
        near = lambda item: haversine(lat_value, lon_value, item["lat"], item["lon"]) <= CROWD_RADIUS_M
        levels = {key: 0 for key in LEVELS}
        for item in app.state.reports.items:
            if item.get("level") in levels and near(item):
                levels[item["level"]] += 1
        total = sum(levels.values())
        # Итог — самая частая оценка; при равенстве берём худшую, чтобы не приукрашивать.
        order = ["none", "bad", "ok", "good"]
        verdict = max(order, key=lambda key: (levels[key], -order.index(key))) if total else None
        nearby = [item for item in app.state.measurements.items if near(item)]
        dbm = sorted(item["rsrp_dbm"] for item in nearby if item.get("rsrp_dbm") is not None)
        return {
            "radius_m": CROWD_RADIUS_M,
            "measurements": len(nearby),
            "median_rsrp_dbm": round(median(dbm), 1) if dbm else None,
            # Итог замеров жителей с учётом тех, где указаны только палочки: самый частый
            # цвет, при равенстве — худший.
            "measurements_color": max(("red", "yellow", "green"),
                                      key=lambda c: (sum(1 for item in nearby if item.get("color") == c),
                                                     -("red", "yellow", "green").index(c))) if nearby else None,
            "reports": total,
            "levels": levels,
            "verdict": verdict,
            "color": LEVELS[verdict] if verdict else None,
            "complaints": sum(1 for item in app.state.complaints.items if near(item)),
            "in_area": report.bounds.contains(lat_value, lon_value),
        }

    @app.get("/api/reports")
    async def reports(after: int = 0):
        return {"reports": app.state.reports.after(after), "rejected": app.state.reports.rejected}

    @app.get("/health")
    async def health():
        return {"status": "ok"}

    return app
