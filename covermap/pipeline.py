"""Сборка результата: от файлов с данными до result.json, отчёта и карты."""
from dataclasses import dataclass
from pathlib import Path
from statistics import median

from . import analysis
from .cleaning import CleanResult, clean
from .geo import Bounds


@dataclass
class Report:
    bounds: Bounds
    cleaned: CleanResult
    cells: list[analysis.Cell]
    cells_skipped: int
    worst: list[analysis.Cell]
    towers: list[analysis.Site]
    candidates: list[analysis.Site]
    allowed: list[analysis.Site]
    cover: dict[str, frozenset]
    brute: analysis.Selection
    greedy: analysis.Selection
    day_cells: list[analysis.Cell]
    night_cells: list[analysis.Cell]
    day_count: int
    night_count: int

    @property
    def red(self) -> list[analysis.Cell]:
        return [c for c in self.cells if c.color == "red"]

    def result(self) -> dict:
        """Содержимое result.json — ровно поля из ТЗ."""
        return {
            "worst_cells": [{"cell_id": c.id, "rsrp_median": c.median} for c in self.worst],
            "best_candidates": list(self.brute.sites),
            "dropped_rows": self.cleaned.dropped_rows,
            "covered_weak_cells": len(self.brute.covered),
        }

    def summary(self) -> dict:
        """Расширенная сводка: причины отбрасывания, сравнение алгоритмов, день/ночь."""
        colors = {name: sum(c.color == name for c in self.cells) for name in ("green", "yellow", "red")}
        allowed = len(self.allowed)
        return {
            "total_rows": self.cleaned.total_rows,
            "clean_rows": len(self.cleaned.measurements),
            "dropped_rows": self.cleaned.dropped_rows,
            "dropped": dict(self.cleaned.dropped),
            "cells_with_data": len(self.cells) + self.cells_skipped,
            "cells_skipped": self.cells_skipped,
            "min_samples": analysis.MIN_SAMPLES,
            "colors": colors,
            "allowed_candidates": allowed,
            "covered_weak_cells": len(self.brute.covered),
            "brute": {
                "sites": list(self.brute.sites),
                "covered": len(self.brute.covered),
                "pairs_checked": allowed * (allowed - 1) // 2,
            },
            "greedy": {"sites": list(self.greedy.sites), "covered": len(self.greedy.covered)},
            "night_hours": list(analysis.NIGHT_HOURS),
            "periods": {
                "day": _period(self.day_cells, self.day_count),
                "night": _period(self.night_cells, self.night_count),
            },
        }


def _period(cells: list[analysis.Cell], measurements: int) -> dict:
    return {
        "measurements": measurements,
        "cells": len(cells),
        "red": sum(c.color == "red" for c in cells),
        "median": round(median(c.median for c in cells), 2) if cells else None,
    }


def build(measurements: Path, towers: Path, candidates: Path, bounds: Path) -> Report:
    area = Bounds.load(bounds)
    cleaned = clean(measurements, area)
    cells, skipped = analysis.aggregate(cleaned.measurements, area)
    tower_sites = analysis.load_sites(towers)
    candidate_sites = analysis.load_sites(candidates)
    allowed = analysis.allowed_candidates(candidate_sites, tower_sites, area)
    red = [c for c in cells if c.color == "red"]
    cover = analysis.coverage(allowed, red, area)
    day, night = analysis.split_day_night(cleaned.measurements)
    return Report(
        bounds=area,
        cleaned=cleaned,
        cells=cells,
        cells_skipped=skipped,
        worst=analysis.worst_cells(cells),
        towers=tower_sites,
        candidates=candidate_sites,
        allowed=allowed,
        cover=cover,
        brute=analysis.best_pair(cover),
        greedy=analysis.greedy_pair(cover),
        day_cells=analysis.aggregate(day, area)[0],
        night_cells=analysis.aggregate(night, area)[0],
        day_count=len(day),
        night_count=len(night),
    )
