"""Настройки сервиса. Значения по умолчанию соответствуют ТЗ и уточнениям жюри."""
import ipaddress
import os
import secrets
from dataclasses import dataclass, field
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent


def _int(name: str, default: int) -> int:
    return int(os.environ.get(name, default))


def _secret() -> bytes:
    value = os.environ.get("OTP_SECRET", "")
    # Без заданного секрета ключ живёт только в памяти процесса: после
    # перезапуска активные коды (жизнь 120 с) просто перестают подходить.
    return value.encode() if value else secrets.token_bytes(32)


def _networks(raw: str) -> tuple:
    return tuple(ipaddress.ip_network(x.strip(), strict=False) for x in raw.split(",") if x.strip())


@dataclass
class Settings:
    secret: bytes = field(default_factory=_secret)
    redis_url: str = field(default_factory=lambda: os.environ.get("REDIS_URL", ""))
    admin_token: str = field(default_factory=lambda: os.environ.get("ADMIN_TOKEN", ""))

    # Файл «SMS-шлюза»: сюда дописывается каждая выдача, сервис его не читает.
    outbox: Path = field(
        default_factory=lambda: Path(
            os.environ.get("OTP_OUTBOX") or REPO_ROOT / "outbox" / "sms.jsonl"
        )
    )
    # Адреса/подсети, от которых принимается X-Forwarded-For (тестовый стенд,
    # обратный прокси). По умолчанию пусто: IP берётся только из соединения.
    trusted_proxies: tuple = field(
        default_factory=lambda: _networks(os.environ.get("OTP_TRUSTED_PROXIES", ""))
    )

    code_ttl: int = field(default_factory=lambda: _int("OTP_CODE_TTL", 120))
    # Сколько хранить запись об истёкшем коде, чтобы отвечать 410, а не 401.
    record_retention: int = field(default_factory=lambda: _int("OTP_RECORD_RETENTION", 86400))
    # Задержка перед повторной отправкой после 1-й, 2-й, 3-й выдачи.
    send_delays: tuple[int, ...] = field(
        default_factory=lambda: tuple(
            int(x) for x in os.environ.get("OTP_SEND_DELAYS", "30,60,120").split(",")
        )
    )
    send_limit: int = field(default_factory=lambda: _int("OTP_SEND_LIMIT", 3))
    send_window: int = field(default_factory=lambda: _int("OTP_SEND_WINDOW", 600))

    max_failures: int = field(default_factory=lambda: _int("OTP_MAX_FAILURES", 5))
    block_ttl: int = field(default_factory=lambda: _int("OTP_BLOCK_TTL", 900))

    ip_limit: int = field(default_factory=lambda: _int("OTP_IP_LIMIT", 20))
    ip_window: int = field(default_factory=lambda: _int("OTP_IP_WINDOW", 60))

    events_keep: int = field(default_factory=lambda: _int("OTP_EVENTS_KEEP", 1000))
