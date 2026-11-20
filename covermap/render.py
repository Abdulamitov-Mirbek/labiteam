"""HTML-карта на Leaflet. Библиотека встроена в файл: карта открывается без
сервера, а без интернета не подгружается только подложка."""
import json
from pathlib import Path

from . import analysis
from .analysis import Cell
from .pipeline import Report
from .signal import SignalIndex

HERE = Path(__file__).parent
# Пример для вкладки «Плагин»: дом из реального объявления (Раззакова / Боконбаева).
PLUGIN_EXAMPLE = (42.8687, 74.604)
WEAK_FROM, WEAK_TO = -90.0, -125.0  # шкала тепловой карты: от «нормально» до «очень слабо»


def _cell(cell: Cell, report: Report) -> dict:
    return {
        "id": cell.id,
        "median": round(cell.median, 2),
        "samples": cell.samples,
        "color": cell.color,
        "corners": [[round(lat, 6), round(lon, 6)] for lat, lon in report.bounds.cell_corners(cell.cell)],
    }


def _heat_points(report: Report) -> list[list[float]]:
    points = []
    for m in report.cleaned.measurements:
        weakness = (WEAK_FROM - m.rsrp) / (WEAK_FROM - WEAK_TO)
        if weakness > 0:
            points.append([round(m.lat, 5), round(m.lon, 5), round(min(weakness, 1.0), 3)])
    return points


def _candidate(site: analysis.Site, report: Report, allowed: set, chosen: set) -> dict:
    reason = None
    if site.id not in allowed:
        nearest = analysis.nearest_site(site, report.towers, report.bounds)
        if nearest:
            reason = {"tower": nearest[0].id, "distance_m": round(nearest[1])}
    return {
        "id": site.id, "lat": site.lat, "lon": site.lon,
        "allowed": site.id in allowed,
        "chosen": site.id in chosen,
        "greedy": site.id in report.greedy.sites,
        "covers": sorted(report.cover.get(site.id, ())),
        "reason": reason,
    }


def payload(report: Report, dgis_key: str = "", live: bool = False) -> dict:
    b = report.bounds
    allowed = {site.id for site in report.allowed}
    chosen = set(report.brute.sites)
    worst = []
    for cell in report.worst:
        lat, lon = b.cell_center(cell.cell)
        worst.append({**_cell(cell, report), "center": [round(lat, 6), round(lon, 6)]})
    return {
        "dgis_key": dgis_key,
        "live": live,
        "bounds": {"lat_min": b.lat_min, "lat_max": b.lat_max, "lon_min": b.lon_min, "lon_max": b.lon_max},
        "summary": report.summary(),
        "grids": {
            "all": [_cell(c, report) for c in report.cells],
            "day": [_cell(c, report) for c in report.day_cells],
            "night": [_cell(c, report) for c in report.night_cells],
        },
        "worst": worst,
        "covered": sorted(report.brute.covered),
        "red": sorted(c.id for c in report.red),
        "towers": [{"id": t.id, "lat": t.lat, "lon": t.lon} for t in report.towers],
        "candidates": [_candidate(c, report, allowed, chosen) for c in report.candidates],
        "heat": _heat_points(report),
        "plugin_example": SignalIndex(report).lookup(*PLUGIN_EXAMPLE),
    }


def render(report: Report, dgis_key: str = "", live: bool = False) -> str:
    """HTML карты. live=True — страница отдаётся сервером и опрашивает жалобы."""
    data = json.dumps(payload(report, dgis_key, live), ensure_ascii=False, separators=(",", ":"))
    parts = {
        "/*LEAFLET_CSS*/": (HERE / "vendor" / "leaflet.css").read_text(encoding="utf-8"),
        "/*FONTS_CSS*/": (HERE / "vendor" / "fonts.css").read_text(encoding="utf-8"),
        "/*LEAFLET_JS*/": (HERE / "vendor" / "leaflet.js").read_text(encoding="utf-8"),
        "/*HEAT_JS*/": (HERE / "vendor" / "leaflet-heat.js").read_text(encoding="utf-8"),
        "__DATA__": data.replace("</", "<\\/"),
    }
    html = (HERE / "template.html").read_text(encoding="utf-8")
    for marker, content in parts.items():
        html = html.replace(marker, content.replace("</script", "<\\/script"), 1)
    return html
