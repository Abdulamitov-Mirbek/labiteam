"""Качество связи в точке: то, что плагин показывает в объявлении о квартире."""
from .analysis import Cell
from .geo import haversine
from .pipeline import Report

ESTIMATE_RADIUS_M = 300.0  # в клетке нет данных — берём соседнюю не дальше 300 м
QUALITY = {"green": "хорошая", "yellow": "средняя", "red": "слабая"}


class SignalIndex:
    def __init__(self, report: Report):
        self.bounds = report.bounds
        self.cells = {c.cell: c for c in report.cells}
        self.day = {c.cell: c for c in report.day_cells}
        self.night = {c.cell: c for c in report.night_cells}
        self.centers = {c.cell: self.bounds.cell_center(c.cell) for c in report.cells}
        self.red = [c.cell for c in report.cells if c.color == "red"]

    def _distance(self, lat: float, lon: float, cell: tuple[int, int]) -> float:
        c_lat, c_lon = self.centers[cell]
        return haversine(lat, lon, c_lat, c_lon, self.bounds.radius)

    def _nearest_cell(self, lat: float, lon: float) -> Cell | None:
        nearest = min(self.cells, key=lambda cell: self._distance(lat, lon, cell), default=None)
        if nearest is not None and self._distance(lat, lon, nearest) <= ESTIMATE_RADIUS_M:
            return self.cells[nearest]
        return None

    def _nearest_weak(self, lat: float, lon: float, own: Cell | None) -> int | None:
        if own is not None and own.color == "red":
            return 0
        distances = [self._distance(lat, lon, cell) for cell in self.red]
        return round(min(distances)) if distances else None

    def lookup(self, lat: float, lon: float) -> dict | None:
        """Сводка по точке; None — точка вне района замеров."""
        if not self.bounds.contains(lat, lon):
            return None
        cell = self.cells.get(self.bounds.cell_of(lat, lon))
        estimated = cell is None
        if cell is None:
            cell = self._nearest_cell(lat, lon)
        nearest_weak = self._nearest_weak(lat, lon, cell)
        if cell is None:
            return {"status": "no_data", "nearest_weak_m": nearest_weak}
        day, night = self.day.get(cell.cell), self.night.get(cell.cell)
        return {
            "status": "ok",
            "cell_id": cell.id,
            "color": cell.color,
            "quality": QUALITY[cell.color],
            "median": round(cell.median, 2),
            "samples": cell.samples,
            "estimated": estimated,
            "day_median": round(day.median, 2) if day else None,
            "night_median": round(night.median, 2) if night else None,
            "nearest_weak_m": nearest_weak,
        }
