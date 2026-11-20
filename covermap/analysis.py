"""Анализ покрытия: медианы по клеткам, слабые зоны, выбор площадок."""
import csv
from dataclasses import dataclass
from itertools import combinations
from pathlib import Path
from statistics import median

from .cleaning import Measurement, parse_number
from .geo import Bounds, cell_id, haversine

MIN_SAMPLES = 5          # клетки с меньшим числом замеров не учитываются
GREEN_FROM = -90.0       # зелёный: медиана >= −90
RED_BELOW = -105.0       # красный (слабая зона): медиана < −105
COVER_RADIUS_M = 1000.0  # радиус покрытия новой БС
MIN_TOWER_DISTANCE_M = 500.0
NIGHT_HOURS = (22, 6)    # ночь: [22:00, 06:00) местного времени


@dataclass(frozen=True)
class Site:
    id: str
    lat: float
    lon: float


@dataclass(frozen=True)
class Cell:
    cell: tuple[int, int]
    median: float
    samples: int

    @property
    def id(self) -> str:
        return cell_id(self.cell)

    @property
    def color(self) -> str:
        if self.median >= GREEN_FROM:
            return "green"
        return "red" if self.median < RED_BELOW else "yellow"


@dataclass(frozen=True)
class Selection:
    """Результат выбора площадок: отсортированные ID и число покрытых клеток."""
    sites: tuple[str, ...]
    covered: frozenset


def load_sites(path: Path) -> list[Site]:
    """Читает towers.csv / candidates.csv (id, lat, lon); битые строки — ошибка."""
    sites = []
    with Path(path).open(encoding="utf-8-sig", newline="") as file:
        for number, row in enumerate(csv.DictReader(file), 2):
            row = {(key or "").strip().lower(): (value or "") for key, value in row.items()}
            lat, lon = parse_number(row.get("lat", "")), parse_number(row.get("lon", ""))
            site_id = row.get("id", "").strip()
            if not site_id or lat is None or lon is None:
                raise ValueError(f"{path}, строка {number}: нужны id, lat, lon")
            sites.append(Site(site_id, lat, lon))
    return sites


def aggregate(measurements: list[Measurement], bounds: Bounds) -> tuple[list[Cell], int]:
    """Медиана RSRP по клеткам. Возвращает (учтённые клетки, число пропущенных
    из-за нехватки замеров)."""
    by_cell: dict[tuple[int, int], list[float]] = {}
    for m in measurements:
        by_cell.setdefault(bounds.cell_of(m.lat, m.lon), []).append(m.rsrp)
    cells = [
        Cell(cell, round(median(values), 6), len(values))
        for cell, values in by_cell.items()
        if len(values) >= MIN_SAMPLES
    ]
    cells.sort(key=lambda c: c.cell)
    return cells, len(by_cell) - len(cells)


def worst_cells(cells: list[Cell], count: int = 3) -> list[Cell]:
    """Худшие клетки: по медиане, при равенстве — по cell_id."""
    return sorted(cells, key=lambda c: (c.median, c.id))[:count]


def allowed_candidates(candidates: list[Site], towers: list[Site], bounds: Bounds) -> list[Site]:
    """Площадки, удалённые от КАЖДОЙ существующей БС не меньше чем на 500 м."""
    return [
        c for c in candidates
        if all(
            haversine(c.lat, c.lon, t.lat, t.lon, bounds.radius) >= MIN_TOWER_DISTANCE_M
            for t in towers
        )
    ]


def coverage(candidates: list[Site], red: list[Cell], bounds: Bounds) -> dict[str, frozenset]:
    """Для каждой площадки — множество красных клеток с центром не дальше 1 км."""
    centers = {c.id: bounds.cell_center(c.cell) for c in red}
    return {
        site.id: frozenset(
            cid for cid, (lat, lon) in centers.items()
            if haversine(site.lat, site.lon, lat, lon, bounds.radius) <= COVER_RADIUS_M
        )
        for site in candidates
    }


def best_pair(cover: dict[str, frozenset], size: int = 2) -> Selection:
    """Полный перебор: набор площадок с наибольшим объединением покрытия.
    При равенстве — лексикографически наименьший отсортированный набор ID."""
    ids = sorted(cover)
    if len(ids) <= size:
        return Selection(tuple(ids), frozenset().union(*cover.values()) if ids else frozenset())
    best = None
    for combo in combinations(ids, size):  # combinations идут в лексикографическом порядке
        union = frozenset().union(*(cover[i] for i in combo))
        if best is None or len(union) > len(best.covered):
            best = Selection(combo, union)
    return best


def greedy_pair(cover: dict[str, frozenset], size: int = 2) -> Selection:
    """Жадный выбор: каждый раз берётся площадка, добавляющая больше всего
    ещё не покрытых клеток (при равенстве — с меньшим ID)."""
    chosen: list[str] = []
    covered: frozenset = frozenset()
    for _ in range(min(size, len(cover))):
        pick = max(
            (i for i in sorted(cover) if i not in chosen),
            key=lambda i: len(cover[i] - covered),
        )
        chosen.append(pick)
        covered |= cover[pick]
    return Selection(tuple(sorted(chosen)), covered)


def is_night(m: Measurement) -> bool:
    start, end = NIGHT_HOURS
    return m.time.hour >= start or m.time.hour < end


def split_day_night(measurements: list[Measurement]) -> tuple[list[Measurement], list[Measurement]]:
    day = [m for m in measurements if not is_night(m)]
    night = [m for m in measurements if is_night(m)]
    return day, night
