#!/usr/bin/env python3
"""Имитация атак на OTP-сервис (только стандартная библиотека).

  1. Перебор кода для одного номера.
  2. Распределённый перебор по 50 номерам с одного IP.

Обе атаки должны быть остановлены защитой: ни один код не угадан.
Код возврата: 0 — защита устояла, 1 — хотя бы одна атака удалась.

    python attack.py                       # http://localhost:8000, 1000 попыток
    python attack.py --url http://host:8000 --attempts 1000 --pause 0
"""
import argparse
import json
import random
import sys
import time
import urllib.error
import urllib.request
from collections import Counter
from concurrent.futures import ThreadPoolExecutor

MEANING = {
    200: "код угадан (!)",
    401: "неверный код",
    410: "нет действующего кода",
    423: "номер заблокирован",
    429: "сработал лимит",
}


def post(url: str, payload: dict) -> int:
    request = urllib.request.Request(
        url,
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            return response.status
    except urllib.error.HTTPError as error:
        return error.code


def random_msisdn() -> str:
    return "9967" + f"{random.randrange(10**8):08d}"


def report(title: str, statuses: Counter, attempts: int, elapsed: float) -> bool:
    print(f"\n=== {title} ===")
    print(f"попыток: {attempts}, время: {elapsed:.1f} с")
    for status, count in sorted(statuses.items()):
        print(f"  HTTP {status}: {count:5d}  — {MEANING.get(status, 'другое')}")
    compared = statuses[401] + statuses[200]
    print(f"  реально сверено с кодом: {compared} из {attempts}")
    breached = statuses[200] > 0
    print("  ИТОГ: " + ("АТАКА УДАЛАСЬ" if breached else "атака остановлена"))
    return breached


def run(base: str, guesses: list[tuple[str, str]], workers: int) -> tuple[Counter, float]:
    started = time.monotonic()
    with ThreadPoolExecutor(workers) as pool:
        statuses = Counter(
            pool.map(lambda g: post(f"{base}/otp/verify", {"msisdn": g[0], "code": g[1]}), guesses)
        )
    return statuses, time.monotonic() - started


def single_number(base: str, attempts: int, workers: int) -> bool:
    msisdn = random_msisdn()
    status = post(f"{base}/otp/send", {"msisdn": msisdn})
    print(f"\nжертва {msisdn}: запрос кода -> HTTP {status}")
    codes = random.sample(range(10**6), attempts)
    statuses, elapsed = run(base, [(msisdn, f"{c:06d}") for c in codes], workers)
    return report("Атака 1: перебор кода одного номера", statuses, attempts, elapsed)


def distributed(base: str, attempts: int, numbers: int, workers: int) -> bool:
    victims = [random_msisdn() for _ in range(numbers)]
    sent = Counter(post(f"{base}/otp/send", {"msisdn": m}) for m in victims)
    print(f"\n{numbers} номеров: запросы кода -> {dict(sent)}")
    # По одной-две попытки на номер, чтобы не упираться в блокировку номера.
    guesses = [(victims[i % numbers], f"{random.randrange(10**6):06d}") for i in range(attempts)]
    statuses, elapsed = run(base, guesses, workers)
    return report(
        f"Атака 2: распределённый перебор по {numbers} номерам с одного IP",
        statuses, attempts, elapsed,
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--url", default="http://localhost:8000")
    parser.add_argument("--attempts", type=int, default=1000, help="попыток на каждую атаку")
    parser.add_argument("--numbers", type=int, default=50, help="номеров во второй атаке")
    parser.add_argument("--workers", type=int, default=20, help="параллельных потоков")
    parser.add_argument(
        "--pause", type=int, default=61,
        help="пауза между атаками, с (чтобы окно лимита по IP обнулилось "
             "и вторая атака проверяла защиту с чистого листа)",
    )
    args = parser.parse_args()
    base = args.url.rstrip("/")

    try:
        breached = single_number(base, args.attempts, args.workers)
        if args.pause:
            print(f"\nпауза {args.pause} с перед второй атакой…")
            time.sleep(args.pause)
        breached |= distributed(base, args.attempts, args.numbers, args.workers)
    except urllib.error.URLError as error:
        print(f"сервис недоступен по адресу {base}: {error.reason}", file=sys.stderr)
        return 2

    print("\n" + ("ЗАЩИТА ПРОБИТА" if breached else "Обе атаки остановлены защитой."))
    return 1 if breached else 0


if __name__ == "__main__":
    sys.exit(main())
