"""Геометрия задания: локальная сетка 200×200 м и расстояние по гаверсинусу.

Сетка строится в локальной равнопромежуточной модели (это не Web Mercator):
    x = R * cos(lat0) * rad(lon − lon0),   y = R * rad(lat − lat0)
    клетка = (floor(x / size), floor(y / size)),   cell_id = X{ix:03d}_Y{iy:03d}
"""
import json
import math
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Bounds:
    lat_min: float
    lat_max: float
    lon_min: float
    lon_max: float
    origin_lat: float
    origin_lon: float
    cell_size: float = 200.0
    radius: float = 6_371_000.0
    timezone: str = "Asia/Bishkek"

    @classmethod
    def load(cls, path: Path) -> "Bounds":
        raw = json.loads(Path(path).read_text(encoding="utf-8"))
        return cls(
            lat_min=raw["lat_min"],
            lat_max=raw["lat_max"],
            lon_min=raw["lon_min"],
            lon_max=raw["lon_max"],
            origin_lat=raw.get("grid_origin_lat", raw["lat_min"]),
            origin_lon=raw.get("grid_origin_lon", raw["lon_min"]),
            cell_size=raw.get("cell_size_m", 200.0),
            radius=raw.get("earth_radius_m", 6_371_000.0),
            timezone=raw.get("timezone", "Asia/Bishkek"),
        )

    def contains(self, lat: float, lon: float) -> bool:
        """Границы района включены."""
        return self.lat_min <= lat <= self.lat_max and self.lon_min <= lon <= self.lon_max

    def to_xy(self, lat: float, lon: float) -> tuple[float, float]:
        x = self.radius * math.cos(math.radians(self.origin_lat)) * math.radians(lon - self.origin_lon)
        y = self.radius * math.radians(lat - self.origin_lat)
        return x, y

    def to_latlon(self, x: float, y: float) -> tuple[float, float]:
        lat = self.origin_lat + math.degrees(y / self.radius)
        lon = self.origin_lon + math.degrees(
            x / (self.radius * math.cos(math.radians(self.origin_lat)))
        )
        return lat, lon

    def cell_of(self, lat: float, lon: float) -> tuple[int, int]:
        x, y = self.to_xy(lat, lon)
        return math.floor(x / self.cell_size), math.floor(y / self.cell_size)

    def cell_center(self, cell: tuple[int, int]) -> tuple[float, float]:
        ix, iy = cell
        return self.to_latlon((ix + 0.5) * self.cell_size, (iy + 0.5) * self.cell_size)

    def cell_corners(self, cell: tuple[int, int]) -> list[tuple[float, float]]:
        """Углы клетки (lat, lon) по часовой стрелке — для отрисовки на карте."""
        ix, iy = cell
        size = self.cell_size
        return [
            self.to_latlon(ix * size, iy * size),
            self.to_latlon(ix * size, (iy + 1) * size),
            self.to_latlon((ix + 1) * size, (iy + 1) * size),
            self.to_latlon((ix + 1) * size, iy * size),
        ]


def cell_id(cell: tuple[int, int]) -> str:
    return f"X{cell[0]:03d}_Y{cell[1]:03d}"


def haversine(lat1: float, lon1: float, lat2: float, lon2: float, radius: float = 6_371_000.0) -> float:
    """Расстояние между точками по дуге большого круга, метры."""
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    dphi = phi2 - phi1
    dlam = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(dlam / 2) ** 2
    return 2 * radius * math.asin(min(1.0, math.sqrt(a)))
