"""Чтение и очистка замеров.

Порядок проверок строки (первая сработавшая причина идёт в статистику):
  1. структура — пустая строка, не хватает обязательных полей;
  2. числа — lat, lon, rsrp_dbm должны быть конечными числами;
  3. время — timestamp в ISO 8601;
  4. RSRP в диапазоне −140…−44 дБм (границы включены);
  5. координаты внутри района (границы включены);
  6. дубль — совпадение всех четырёх нормализованных полей с более ранней строкой
     (числа сравниваются как числа: «-98.2» и «-98.20» — одно и то же).
"""
import csv
import math
import re
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone, tzinfo
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .geo import Bounds

REQUIRED = ("lat", "lon", "rsrp_dbm", "timestamp")
RSRP_MIN, RSRP_MAX = -140.0, -44.0
MAX_FILE_BYTES = 50 * 1024 * 1024  # защита от случайно поданного огромного файла
NUMBER_RE = re.compile(r"[+-]?(\d+\.?\d*|\.\d+)([eE][+-]?\d+)?")

# Причины отбрасывания — ключи статистики.
EMPTY, MALFORMED, NOT_NUMBER, BAD_TIME, RSRP_RANGE, OUT_OF_BOUNDS, DUPLICATE = (
    "empty_row", "malformed_row", "not_a_number", "bad_timestamp",
    "rsrp_out_of_range", "out_of_bounds", "duplicate",
)


@dataclass(frozen=True)
class Measurement:
    lat: float
    lon: float
    rsrp: float
    time: datetime  # с часовым поясом района


@dataclass
class CleanResult:
    measurements: list[Measurement]
    total_rows: int
    dropped: Counter = field(default_factory=Counter)

    @property
    def dropped_rows(self) -> int:
        return sum(self.dropped.values())


def local_zone(name: str) -> tzinfo:
    """Часовой пояс района. Если в системе нет базы поясов (Windows без пакета
    tzdata), для Бишкека берётся постоянное смещение UTC+6."""
    try:
        return ZoneInfo(name)
    except ZoneInfoNotFoundError:
        if name == "Asia/Bishkek":
            return timezone(timedelta(hours=6), name)
        raise ValueError(f"неизвестный часовой пояс {name!r}; установите пакет tzdata") from None


def parse_number(text: str) -> float | None:
    """Конечное десятичное число или None."""
    text = text.strip()
    if not NUMBER_RE.fullmatch(text):
        return None
    value = float(text)
    return value if math.isfinite(value) else None


def parse_time(text: str, zone: tzinfo) -> datetime | None:
    """Момент времени из ISO 8601; время без смещения считается местным."""
    text = text.strip()
    if "T" not in text.upper() and " " not in text:
        return None  # только дата или мусор — времени замера нет
    try:
        moment = datetime.fromisoformat(text)
    except ValueError:
        return None
    return moment.replace(tzinfo=zone) if moment.tzinfo is None else moment.astimezone(zone)


def read_rows(path: Path) -> tuple[list[str], list[list[str]]]:
    """Заголовок и строки данных; пустые физические строки сохраняются как []."""
    size = Path(path).stat().st_size
    if size > MAX_FILE_BYTES:
        raise ValueError(f"{path}: файл {size // 2**20} МБ больше лимита {MAX_FILE_BYTES // 2**20} МБ")
    lines = Path(path).read_text(encoding="utf-8-sig").splitlines()
    if not lines:
        raise ValueError(f"{path}: файл пуст, нет строки заголовка")
    header = [name.strip().lower() for name in next(csv.reader([lines[0]]))]
    missing = [name for name in REQUIRED if name not in header]
    if missing:
        raise ValueError(f"{path}: в заголовке нет полей {', '.join(missing)}")
    rows = [next(csv.reader([line]), []) if line.strip() else [] for line in lines[1:]]
    return header, rows


def clean(path: Path, bounds: Bounds) -> CleanResult:
    header, rows = read_rows(path)
    index = {name: header.index(name) for name in REQUIRED}
    zone = local_zone(bounds.timezone)
    result = CleanResult(measurements=[], total_rows=len(rows))
    seen: set[tuple] = set()

    for row in rows:
        reason, measurement = _check(row, index, bounds, zone)
        if reason is None:
            key = (measurement.lat, measurement.lon, measurement.rsrp, measurement.time)
            if key in seen:
                reason = DUPLICATE
            else:
                seen.add(key)
                result.measurements.append(measurement)
        if reason is not None:
            result.dropped[reason] += 1
    return result


def _check(row: list[str], index: dict, bounds: Bounds, zone: tzinfo):
    """Возвращает (причина, None) для плохой строки или (None, Measurement)."""
    if not any(cell.strip() for cell in row):
        return EMPTY, None
    if len(row) <= max(index.values()):
        return MALFORMED, None
    raw = {name: row[position] for name, position in index.items()}
    if not all(value.strip() for value in raw.values()):
        return MALFORMED, None

    lat, lon, rsrp = (parse_number(raw[name]) for name in ("lat", "lon", "rsrp_dbm"))
    if lat is None or lon is None or rsrp is None:
        return NOT_NUMBER, None
    moment = parse_time(raw["timestamp"], zone)
    if moment is None:
        return BAD_TIME, None
    if not RSRP_MIN <= rsrp <= RSRP_MAX:
        return RSRP_RANGE, None
    if not bounds.contains(lat, lon):
        return OUT_OF_BOUNDS, None
    return None, Measurement(lat, lon, rsrp, moment)
