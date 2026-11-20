"""Тесты ТЗ 7: очистка, сетка, анализ и сверка с открытым эталоном."""
import json
import math
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from covermap import analysis, cleaning
from covermap.__main__ import main
from covermap.analysis import Cell, Site
from covermap.cleaning import Measurement, clean
from covermap.geo import Bounds, cell_id, haversine
from covermap.pipeline import build
from covermap.render import render

DATA = Path(__file__).resolve().parent.parent / "data" / "07_coverage"
BOUNDS = Bounds(lat_min=42.84, lat_max=42.876, lon_min=74.56, lon_max=74.609,
                origin_lat=42.84, origin_lon=74.56)
TS = "2026-11-16T13:58:30+06:00"
ZONE = ZoneInfo("Asia/Bishkek")


def write_csv(tmp_path, lines, header="lat,lon,rsrp_dbm,timestamp"):
    path = tmp_path / "measurements.csv"
    path.write_text("\n".join([header, *lines]) + "\n", encoding="utf-8")
    return path


def measurement(lat, lon, rsrp, hour=12):
    return Measurement(lat, lon, rsrp, datetime(2026, 11, 16, hour, 0, tzinfo=ZONE))


# --- геометрия ---------------------------------------------------------------

def test_origin_is_cell_zero():
    assert BOUNDS.cell_of(42.84, 74.56) == (0, 0)
    assert cell_id((6, 13)) == "X006_Y013"


def test_cell_size_is_200_m():
    lat, lon = BOUNDS.to_latlon(199.9, 199.9)
    assert BOUNDS.cell_of(lat, lon) == (0, 0)
    lat, lon = BOUNDS.to_latlon(200.1, 400.1)
    assert BOUNDS.cell_of(lat, lon) == (1, 2)


def test_cell_center_roundtrip():
    lat, lon = BOUNDS.cell_center((6, 13))
    assert BOUNDS.cell_of(lat, lon) == (6, 13)
    x, y = BOUNDS.to_xy(lat, lon)
    assert (x, y) == pytest.approx((1300, 2700))


def test_haversine_known_distance():
    assert haversine(0, 0, 0, 1) == pytest.approx(111_194.93, abs=0.01)
    assert haversine(42.85, 74.57, 42.85, 74.57) == 0


# --- очистка ----------------------------------------------------------------

@pytest.mark.parametrize("line, reason", [
    ("", cleaning.EMPTY),
    ("   ", cleaning.EMPTY),
    (",,,", cleaning.EMPTY),
    ("42.85,74.57,-90", cleaning.MALFORMED),
    (f"42.85,74.57,,{TS}", cleaning.MALFORMED),
    (f"abc,74.57,-90,{TS}", cleaning.NOT_NUMBER),
    (f"42.85,74.57,nan,{TS}", cleaning.NOT_NUMBER),
    (f"42.85,74.57,-inf,{TS}", cleaning.NOT_NUMBER),
    (f"42.85,74.57,-9_0,{TS}", cleaning.NOT_NUMBER),
    ("42.85,74.57,-90,вчера", cleaning.BAD_TIME),
    ("42.85,74.57,-90,2026-13-40T25:00:00", cleaning.BAD_TIME),
    ("42.85,74.57,-90,2026-11-16", cleaning.BAD_TIME),
    (f"42.85,74.57,-140.1,{TS}", cleaning.RSRP_RANGE),
    (f"42.85,74.57,-43.9,{TS}", cleaning.RSRP_RANGE),
    (f"42.8399,74.57,-90,{TS}", cleaning.OUT_OF_BOUNDS),
    (f"42.85,74.61,-90,{TS}", cleaning.OUT_OF_BOUNDS),
])
def test_bad_row_is_dropped_with_reason(tmp_path, line, reason):
    result = clean(write_csv(tmp_path, [line]), BOUNDS)
    assert result.measurements == []
    assert dict(result.dropped) == {reason: 1}
    assert result.dropped_rows == 1


def test_range_and_bounds_are_inclusive(tmp_path):
    lines = [
        f"42.84,74.56,-140,{TS}",
        f"42.876,74.609,-44,{TS}",
    ]
    result = clean(write_csv(tmp_path, lines), BOUNDS)
    assert len(result.measurements) == 2 and result.dropped_rows == 0


def test_duplicates_compare_numbers_as_numbers(tmp_path):
    lines = [
        f"42.85,74.57,-98.2,{TS}",
        f"42.850,74.5700,-98.20,{TS}",   # то же число в другой записи
        f"42.85,74.57,-98.2,{TS}",       # точный повтор
        f"42.85,74.57,-98.3,{TS}",       # другое значение — не дубль
        "42.85,74.57,-98.2,2026-11-16T13:58:31+06:00",  # другое время — не дубль
    ]
    result = clean(write_csv(tmp_path, lines), BOUNDS)
    assert len(result.measurements) == 3
    assert dict(result.dropped) == {cleaning.DUPLICATE: 2}


def test_invalid_row_is_not_counted_as_duplicate(tmp_path):
    lines = [f"42.85,74.57,-150,{TS}"] * 2
    result = clean(write_csv(tmp_path, lines), BOUNDS)
    assert dict(result.dropped) == {cleaning.RSRP_RANGE: 2}


def test_column_order_follows_header(tmp_path):
    path = write_csv(tmp_path, [f"{TS},-90,74.57,42.85"], header="timestamp,rsrp_dbm,lon,lat")
    result = clean(path, BOUNDS)
    assert result.measurements[0].lat == 42.85 and result.measurements[0].rsrp == -90


def test_missing_header_field_is_an_error(tmp_path):
    with pytest.raises(ValueError, match="rsrp_dbm"):
        clean(write_csv(tmp_path, [], header="lat,lon,timestamp"), BOUNDS)


# --- анализ -----------------------------------------------------------------

def test_cells_with_fewer_than_five_samples_are_skipped():
    points = [measurement(42.8405, 74.5605, -100)] * 4 + [measurement(42.8505, 74.5705, -110)] * 5
    cells, skipped = analysis.aggregate(points, BOUNDS)
    assert len(cells) == 1 and skipped == 1
    assert cells[0].samples == 5 and cells[0].median == -110


def test_median_of_even_count():
    values = [-100, -101, -120.5, -121, -130, -90]
    cells, _ = analysis.aggregate([measurement(42.8405, 74.5605, v) for v in values], BOUNDS)
    assert cells[0].median == -110.75


@pytest.mark.parametrize("median, color", [
    (-89.9, "green"), (-90, "green"), (-90.1, "yellow"),
    (-105, "yellow"), (-105.1, "red"), (-130, "red"),
])
def test_color_thresholds(median, color):
    assert Cell((0, 0), median, 5).color == color


def test_worst_cells_sorted_by_median_then_id():
    cells = [Cell((2, 0), -120, 5), Cell((1, 0), -120, 5), Cell((0, 0), -110, 5), Cell((3, 0), -125, 5)]
    assert [c.id for c in analysis.worst_cells(cells)] == ["X003_Y000", "X001_Y000", "X002_Y000"]


def test_candidate_closer_than_500_m_to_any_tower_is_rejected():
    tower = Site("B1", 42.85, 74.57)
    metre = math.degrees(1 / BOUNDS.radius)  # градусов широты в одном метре
    near = Site("C1", 42.85 + 499 * metre, 74.57)
    edge = Site("C2", 42.85 + 500.01 * metre, 74.57)
    far_tower = Site("B2", 42.87, 74.60)
    allowed = analysis.allowed_candidates([near, edge], [far_tower, tower], BOUNDS)
    assert [c.id for c in allowed] == ["C2"]


def test_coverage_uses_cell_centre_within_1_km():
    red = [Cell((0, 0), -110, 5), Cell((10, 0), -110, 5)]
    lat, lon = BOUNDS.cell_center((0, 0))
    cover = analysis.coverage([Site("C1", lat, lon)], red, BOUNDS)
    assert cover == {"C1": frozenset({"X000_Y000"})}


def test_best_pair_counts_union_without_double_counting():
    cover = {"A": frozenset("12345"), "B": frozenset("12346"), "C": frozenset("789")}
    best = analysis.best_pair(cover)
    assert best.sites == ("A", "C") and len(best.covered) == 8


def test_best_pair_tie_takes_lexicographically_smallest():
    cover = {"C03": frozenset("12"), "C01": frozenset("34"), "C02": frozenset("56")}
    assert analysis.best_pair(cover).sites == ("C01", "C02")


def test_greedy_can_lose_to_brute_force():
    # A — лучшая в одиночку, но лучшая пара — B + C.
    cover = {"A": frozenset("1234567"), "B": frozenset("123789"), "C": frozenset("456abc")}
    assert analysis.greedy_pair(cover).sites[0] == "A"
    assert len(analysis.greedy_pair(cover).covered) < len(analysis.best_pair(cover).covered)
    assert analysis.best_pair(cover).sites == ("B", "C")


def test_fewer_than_two_candidates():
    assert analysis.best_pair({}).sites == ()
    one = analysis.best_pair({"C1": frozenset("12")})
    assert one.sites == ("C1",) and len(one.covered) == 2


def test_day_night_split():
    day, night = analysis.split_day_night(
        [measurement(42.85, 74.57, -90, hour) for hour in (5, 6, 12, 21, 22, 23)]
    )
    assert [m.time.hour for m in day] == [6, 12, 21]
    assert [m.time.hour for m in night] == [5, 22, 23]


# --- открытые данные жюри ----------------------------------------------------

@pytest.fixture(scope="module")
def report():
    return build(DATA / "measurements.csv", DATA / "towers.csv", DATA / "candidates.csv", DATA / "bounds.json")


def test_result_matches_expected(report):
    expected = json.loads((DATA / "expected.json").read_text(encoding="utf-8"))
    assert report.result() == expected


def test_open_data_facts_from_readme(report):
    summary = report.summary()
    assert summary["total_rows"] == 5000 and summary["clean_rows"] == 4800
    assert summary["cells_with_data"] == 398 and summary["cells_skipped"] == 11
    assert summary["brute"]["covered"] >= summary["greedy"]["covered"]


def test_map_is_self_contained(report):
    html = render(report)
    assert "/*LEAFLET_JS*/" not in html and "__DATA__" not in html
    assert "X006_Y013" in html and "L.heatLayer" in html
    assert '<script src=' not in html and '<link' not in html


# --- запуск из командной строки ------------------------------------------------

def test_cli_writes_outputs(tmp_path, capsys):
    assert main(["--out", str(tmp_path)]) == 0
    result = json.loads((tmp_path / "result.json").read_text(encoding="utf-8"))
    assert set(result) == {"worst_cells", "best_candidates", "dropped_rows", "covered_weak_cells"}
    assert (tmp_path / "map.html").stat().st_size > 100_000
    assert (tmp_path / "report.json").exists()
    assert "C09 + C14" in capsys.readouterr().out


def test_cli_missing_file_is_reported(tmp_path, capsys):
    assert main(["--data", str(tmp_path), "--out", str(tmp_path / "out")]) == 2
    assert "файл не найден" in capsys.readouterr().err


def test_cli_bad_header_is_reported(tmp_path, capsys):
    bad = tmp_path / "m.csv"
    bad.write_text("a,b,c\n1,2,3\n", encoding="utf-8")
    assert main(["--measurements", str(bad), "--out", str(tmp_path / "out")]) == 2
    assert "Ошибка во входных данных" in capsys.readouterr().err
