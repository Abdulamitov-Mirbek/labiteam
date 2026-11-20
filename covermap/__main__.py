"""Запуск: python -m covermap [--data КАТАЛОГ] [--out КАТАЛОГ]

На выходе: result.json (ответ для жюри), report.json (расширенная сводка)
и map.html (карта покрытия).
"""
import argparse
import json
import os
import sys
from pathlib import Path

from .pipeline import build
from .render import render

DEFAULT_DATA = Path(__file__).resolve().parent.parent / "data" / "07_coverage"


def parse_args(argv=None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="covermap", description="Карта покрытия и выбор площадок для новых БС")
    parser.add_argument("--data", type=Path, default=DEFAULT_DATA,
                        help="каталог с measurements.csv, towers.csv, candidates.csv, bounds.json")
    parser.add_argument("--measurements", type=Path, help="путь к measurements.csv (вместо файла из --data)")
    parser.add_argument("--towers", type=Path, help="путь к towers.csv")
    parser.add_argument("--candidates", type=Path, help="путь к candidates.csv")
    parser.add_argument("--bounds", type=Path, help="путь к bounds.json")
    parser.add_argument("--out", type=Path, default=Path("out"), help="каталог для результатов (по умолчанию out/)")
    parser.add_argument("--dgis-key", default=os.environ.get("DGIS_KEY", ""),
                        help="ключ API 2ГИС для подложки карты (или переменная окружения DGIS_KEY)")
    return parser.parse_args(argv)


def main(argv=None) -> int:
    args = parse_args(argv)
    paths = {
        name: getattr(args, name) or args.data / filename
        for name, filename in [
            ("measurements", "measurements.csv"), ("towers", "towers.csv"),
            ("candidates", "candidates.csv"), ("bounds", "bounds.json"),
        ]
    }
    try:
        report = build(**paths)
    except FileNotFoundError as error:
        print(f"Ошибка: файл не найден — {error.filename}", file=sys.stderr)
        return 2
    except (ValueError, KeyError) as error:
        print(f"Ошибка во входных данных: {error}", file=sys.stderr)
        return 2

    args.out.mkdir(parents=True, exist_ok=True)
    result = report.result()
    (args.out / "result.json").write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    (args.out / "report.json").write_text(
        json.dumps(report.summary(), ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    (args.out / "map.html").write_text(render(report, args.dgis_key), encoding="utf-8")

    summary = report.summary()
    print(f"Строк: {summary['total_rows']}, отброшено: {summary['dropped_rows']}, осталось: {summary['clean_rows']}")
    for reason, count in sorted(summary["dropped"].items(), key=lambda item: -item[1]):
        print(f"  {reason}: {count}")
    print(f"Клеток с замерами: {summary['cells_with_data']}, не учтено (< {summary['min_samples']} замеров): "
          f"{summary['cells_skipped']}")
    print("Цвета: зелёных {green}, жёлтых {yellow}, красных {red}".format(**summary["colors"]))
    print("Худшие квадраты: " + ", ".join(f"{c['cell_id']} ({c['rsrp_median']})" for c in result["worst_cells"]))
    print(f"Лучшая пара площадок: {' + '.join(result['best_candidates'])} — "
          f"покрывает {result['covered_weak_cells']} красных квадратов")
    print(f"Жадный алгоритм: {' + '.join(summary['greedy']['sites'])} — {summary['greedy']['covered']}")
    print(f"Результаты: {args.out / 'result.json'}, {args.out / 'report.json'}, {args.out / 'map.html'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
