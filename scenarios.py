#!/usr/bin/env python3
"""Прогон сценариев жюри (формат data/06_otp/scenarios_examples.json).

Каждый сценарий выполняется на свежем экземпляре сервиса внутри процесса:
часы подменены (шаг advance сдвигает их мгновенно), client_ip задаётся как
адрес соединения, capture_delivery читает последнюю строку outbox для номера.

    python scenarios.py                              # открытые примеры
    python scenarios.py path/to/scenarios.json
"""
import argparse
import asyncio
import json
import logging
import sys
import tempfile
from collections import Counter
from datetime import datetime
from pathlib import Path

import httpx

from app.config import Settings
from app.main import create_app

DATA = Path(__file__).resolve().parent / "data" / "06_otp"
DEFAULT_IP = "127.0.0.1"


def last_delivery(outbox: Path, msisdn: str) -> str:
    """Код из последней строки outbox для номера."""
    lines = outbox.read_text(encoding="utf-8").splitlines() if outbox.exists() else []
    for line in reversed(lines):
        record = json.loads(line)
        if record["msisdn"] == msisdn:
            return record["code"]
    raise LookupError(f"в outbox нет доставки для {msisdn}")


def substitute(value, bound: dict):
    """$c1 -> перехваченный код, $wrong:c1 -> заведомо другой шестизначный код."""
    if isinstance(value, dict):
        return {key: substitute(item, bound) for key, item in value.items()}
    if isinstance(value, str) and value.startswith("$wrong:"):
        return f"{(int(bound[value[7:]]) + 1) % 10**6:06d}"
    if isinstance(value, str) and value.startswith("$"):
        return bound[value[1:]]
    return value


class Runner:
    """Один сценарий: свой сервис, свои часы, свой outbox."""

    def __init__(self, settings: Settings, start_ms: int):
        self.settings = settings
        self.app = create_app(settings)
        self.now = start_ms
        self.app.state.clock = lambda: self.now
        self.clients: dict[str, httpx.AsyncClient] = {}
        self.bound: dict[str, str] = {}

    def client(self, ip: str) -> httpx.AsyncClient:
        if ip not in self.clients:
            transport = httpx.ASGITransport(app=self.app, client=(ip, 50000))
            self.clients[ip] = httpx.AsyncClient(transport=transport, base_url="http://otp")
        return self.clients[ip]

    async def request(self, spec: dict) -> int:
        response = await self.client(spec.get("client_ip", DEFAULT_IP)).request(
            spec.get("method", "POST"), spec["path"], json=substitute(spec.get("body"), self.bound)
        )
        return response.status_code

    async def step(self, step: dict) -> str | None:
        """Выполняет шаг; возвращает описание расхождения или None."""
        op = step["op"]
        if op == "request":
            got, want = await self.request(step), step["expected"]["status"]
            if got != want:
                return f"{step['method']} {step['path']}: ожидался {want}, получен {got}"
        elif op == "capture_delivery":
            self.bound[step["bind"]] = last_delivery(self.settings.outbox, step["msisdn"])
        elif op == "advance":
            self.now += round(step["seconds"] * 1000)
        elif op == "parallel":
            statuses = await asyncio.gather(*(self.request(r) for r in step["requests"]))
            got = {str(k): v for k, v in Counter(statuses).items()}
            expected = step["expected"]
            if "count" in expected and len(statuses) != expected["count"]:
                return f"parallel: ожидалось {expected['count']} ответов, получено {len(statuses)}"
            if "status_counts" in expected and got != expected["status_counts"]:
                return f"parallel: ожидалось {expected['status_counts']}, получено {got}"
        else:
            return f"неизвестная операция {op!r}"
        return None

    async def close(self) -> None:
        for client in self.clients.values():
            await client.aclose()
        await self.app.state.store.close()


async def run_scenario(scenario: dict, settings: Settings, start_ms: int) -> list[str]:
    """Возвращает список расхождений (пустой — сценарий пройден)."""
    for key, value in scenario.get("overrides", {}).items():
        if not hasattr(settings, key):
            return [f"неизвестная настройка в overrides: {key}"]
        setattr(settings, key, value)
    runner = Runner(settings, start_ms)
    failures = []
    try:
        for number, step in enumerate(scenario["steps"], 1):
            problem = await runner.step(step)
            if problem:
                failures.append(f"шаг {number}: {problem}")
    finally:
        await runner.close()
    return failures


def start_time(fixture: Path) -> int:
    if fixture.exists():
        raw = json.loads(fixture.read_text(encoding="utf-8")).get("start_time")
        if raw:
            return int(datetime.fromisoformat(raw).timestamp() * 1000)
    return 1_800_000_000_000


def main() -> int:
    parser = argparse.ArgumentParser(description="Прогон сценариев жюри для OTP-сервиса")
    parser.add_argument("file", nargs="?", default=DATA / "scenarios_examples.json", type=Path)
    args = parser.parse_args()
    logging.getLogger("otp.security").setLevel(logging.WARNING)  # не шуметь журналом

    scenarios = json.loads(args.file.read_text(encoding="utf-8"))
    passed = 0
    for scenario in scenarios:
        fixture = args.file.parent / scenario.get("fixture", "test_identities.json")
        with tempfile.TemporaryDirectory() as tmp:
            settings = Settings()
            settings.redis_url = ""
            settings.outbox = Path(tmp) / "sms.jsonl"
            failures = asyncio.run(run_scenario(scenario, settings, start_time(fixture)))
        passed += not failures
        print(f"{'OK  ' if not failures else 'FAIL'} {scenario['id']}  {scenario.get('title', '')}")
        for failure in failures:
            print(f"       {failure}")
    print(f"\nпройдено {passed} из {len(scenarios)}")
    return 0 if passed == len(scenarios) else 1


if __name__ == "__main__":
    sys.exit(main())
