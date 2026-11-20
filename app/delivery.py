"""Имитация SMS-шлюза: каждая выдача — одна строка JSON в файле outbox.

Это единственное место, где код существует в открытом виде. Сервис файл
только дополняет и никогда не читает.
"""
import json
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path

BISHKEK = timezone(timedelta(hours=6))


class DeliveryError(Exception):
    """Код не удалось передать в шлюз."""


class Outbox:
    def __init__(self, path: Path):
        self.path = Path(path)
        self._lock = threading.Lock()

    def deliver(self, msisdn: str, code: str, now_ms: int) -> None:
        ts = datetime.fromtimestamp(now_ms / 1000, BISHKEK).isoformat(timespec="seconds")
        line = json.dumps({"ts": ts, "msisdn": msisdn, "code": code})
        try:
            with self._lock:
                self.path.parent.mkdir(parents=True, exist_ok=True)
                with self.path.open("a", encoding="utf-8") as file:
                    file.write(line + "\n")
        except OSError as error:
            raise DeliveryError(str(error)) from error
