"""API и панель оператора «Сигнал.kg».

GET  /                     панель оператора (карта, «Что если», игра, жалобы)
GET  /api/signal?lat=&lon= качество связи в точке — для Chrome-плагина
GET  /api/listing?url=     связь в доме из объявления house.kg или lalafo.kg — «Поделиться» на телефоне
GET  /api/cells            сетка 200×200 м с медианами и цветами
GET  /api/result           result.json
POST /api/complaints       жалоба «Здесь нет связи» {lat, lon, source, site}
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
from collections import deque
from datetime import datetime, timezone
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse

from covermap.pipeline import Report, build
from covermap.render import render
from covermap.signal import SignalIndex

from .listing import (
    FetchError, HttpFetcher, RateLimit, TtlCache, extract_coords, extract_title, find_listing_url, site_of,
)

ROOT = Path(__file__).resolve().parent.parent
WEBAPP = ROOT / "webapp"
FONTS_CSS = ROOT / "covermap" / "vendor" / "fonts.css"
# Файлы приложения, которые отдаются как есть: имя → тип содержимого.
APP_FILES = {
    "manifest.webmanifest": "application/manifest+json",
    "sw.js": "text/javascript",
    "icon.svg": "image/svg+xml",
    "icon-192.png": "image/png",
    "icon-512.png": "image/png",
}
DEFAULT_ORIGINS = "https://house.kg,https://www.house.kg,https://lalafo.kg,https://www.lalafo.kg"
SOURCES = {"plugin", "panel", "app"}
SITES = {"house.kg", "lalafo.kg"}
MAX_BODY_BYTES = 1024
COMPLAINTS_PER_MINUTE = 5      # с одного IP
COMPLAINTS_KEEP = 5000         # сколько последних жалоб держать в памяти
LISTINGS_PER_MINUTE = 20       # проверок объявлений с одного IP


def error(status: int, code: str, detail: str, **headers) -> JSONResponse:
    return JSONResponse({"error": code, "detail": detail}, status_code=status, headers=headers or None)


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

    def __init__(self, path: Path):
        self.path = Path(path)
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
        if len(hits) >= COMPLAINTS_PER_MINUTE:
            self.rejected += 1
            return max(1, math.ceil(hits[0] + 60 - now))
        hits.append(now)
        return 0

    def add(self, lat: float, lon: float, cell_id: str | None, source: str, site: str | None) -> dict:
        with self.lock:
            record = {
                "id": self.last_id + 1,
                "ts": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                "lat": round(lat, 6),
                "lon": round(lon, 6),
                "cell_id": cell_id,
                "source": source,
                "site": site,
            }
            self.items.append(record)
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with self.path.open("a", encoding="utf-8") as file:
                file.write(json.dumps(record, ensure_ascii=False) + "\n")
        return record

    def after(self, last_id: int) -> list[dict]:
        return [item for item in self.items if item["id"] > last_id]


def create_app(
    data_dir: Path | None = None,
    dgis_key: str | None = None,
    complaints_file: Path | None = None,
    origins: list[str] | None = None,
    listing_fetcher=None,
) -> FastAPI:
    data_dir = Path(data_dir or os.environ.get("COVERMAP_DATA") or ROOT / "data" / "07_coverage")
    dgis_key = dgis_key if dgis_key is not None else os.environ.get("DGIS_KEY", "")
    complaints_file = Path(
        complaints_file or os.environ.get("COMPLAINTS_FILE") or ROOT / "var" / "complaints.jsonl"
    )
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
        }
        for c in report.cells
    ]

    app = FastAPI(title="Сигнал.kg", docs_url="/api/docs", redoc_url=None)
    app.state.report = report
    app.state.complaints = Complaints(complaints_file)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=origins,
        allow_methods=["GET", "POST"],
        allow_headers=["Content-Type"],
    )

    @app.middleware("http")
    async def security_headers(request: Request, call_next):
        response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Referrer-Policy"] = "no-referrer-when-downgrade"
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
        headers = {"Cache-Control": "no-cache"} if name == "sw.js" else None
        return FileResponse(WEBAPP / name, media_type=APP_FILES[name], headers=headers)

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
        return cached

    @app.get("/api/cells")
    async def all_cells():
        return {"cells": cells}

    @app.get("/api/result")
    async def result():
        return report.result()

    @app.post("/api/complaints", status_code=201)
    async def add_complaint(request: Request):
        body = await request.body()
        if len(body) > MAX_BODY_BYTES:
            return error(413, "too_large", f"тело запроса больше {MAX_BODY_BYTES} байт")
        try:
            fields = json.loads(body)
        except (ValueError, UnicodeDecodeError):
            return error(400, "bad_request", "ожидается JSON")
        if not isinstance(fields, dict):
            return error(400, "bad_request", "ожидается объект JSON")
        lat, lon = parse_coordinate(fields.get("lat")), parse_coordinate(fields.get("lon"))
        source = fields.get("source", "plugin")
        site = fields.get("site")
        if lat is None or lon is None or source not in SOURCES or (site is not None and site not in SITES):
            return error(400, "bad_request", "нужны числа lat, lon, source: plugin или panel, site: house.kg, lalafo.kg или null")
        if not report.bounds.contains(lat, lon):
            return error(422, "out_of_area", "точка вне района замеров")
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

    @app.get("/health")
    async def health():
        return {"status": "ok"}

    return app
