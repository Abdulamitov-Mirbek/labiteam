"""OTP-сервис: выдача и проверка одноразовых кодов с защитой от перебора."""
import hashlib
import hmac
import ipaddress
import json
import logging
import math
import re
import secrets
import sys
import time
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse

from .config import Settings
from .delivery import DeliveryError, Outbox
from .store import StoreUnavailable, make_store

MSISDN_RE = re.compile(r"996\d{9}")
CODE_RE = re.compile(r"\d{6}")
DASHBOARD_HTML = (Path(__file__).parent / "dashboard.html").read_text(encoding="utf-8")


def _logger(name: str) -> logging.Logger:
    log = logging.getLogger(name)
    if not log.handlers:
        handler = logging.StreamHandler(sys.stdout)
        handler.setFormatter(logging.Formatter("%(message)s"))
        log.addHandler(handler)
        log.setLevel(logging.INFO)
        log.propagate = False
    return log


# Журнал событий безопасности: кодов в нём нет никогда.
security_log = _logger("otp.security")
error_log = _logger("otp.error")


def seconds(ms: int) -> int:
    return max(1, math.ceil(ms / 1000))


def error(status: int, code: str, **extra) -> JSONResponse:
    headers = {"Retry-After": str(extra["retry_after"])} if "retry_after" in extra else None
    return JSONResponse({"error": code, **extra}, status_code=status, headers=headers)


async def read_fields(request: Request) -> dict:
    """Тело запроса как словарь; при некорректном JSON — пустой словарь."""
    try:
        body = await request.json()
    except (ValueError, UnicodeDecodeError):
        return {}
    return body if isinstance(body, dict) else {}


def valid(pattern: re.Pattern, value) -> bool:
    return isinstance(value, str) and pattern.fullmatch(value) is not None


def create_app(settings: Settings | None = None, store=None) -> FastAPI:
    settings = settings or Settings()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        yield
        await app.state.store.close()

    app = FastAPI(title="OTP-сервис", lifespan=lifespan)
    app.state.settings = settings
    app.state.store = store or make_store(settings)
    app.state.outbox = Outbox(settings.outbox)
    # Часы и генератор кода подменяются в тестах.
    app.state.clock = lambda: int(time.time() * 1000)
    app.state.generate_code = lambda: f"{secrets.randbelow(10**6):06d}"

    @app.exception_handler(StoreUnavailable)
    async def store_unavailable(request: Request, exc: StoreUnavailable):
        # Закрытый отказ: без хранилища лимиты не проверить, поэтому коды
        # не выдаются и не принимаются.
        error_log.error("хранилище недоступно: %s", exc)
        return error(503, "storage_unavailable", retry_after=5)

    def code_hash(msisdn: str, code: str) -> str:
        # Хеш привязан к номеру: код одного номера не подходит к другому.
        return hmac.new(settings.secret, f"{msisdn}:{code}".encode(), hashlib.sha256).hexdigest()

    async def audit(event: str, msisdn: str | None, ip: str, **details) -> None:
        record = {
            "ts": datetime.now(timezone.utc).isoformat(timespec="milliseconds"),
            "event": event,
            "msisdn": msisdn,
            "ip": ip,
            **details,
        }
        security_log.info(json.dumps(record, ensure_ascii=False))
        await app.state.store.add_event(record)

    def is_trusted(address: str) -> bool:
        try:
            ip = ipaddress.ip_address(address)
        except ValueError:
            return False
        return any(ip in network for network in settings.trusted_proxies)

    def client_ip(request: Request) -> str:
        """IP клиента — адрес соединения. X-Forwarded-For учитывается только
        когда соединение пришло от доверенного адреса (OTP_TRUSTED_PROXIES)."""
        peer = request.client.host if request.client else "unknown"
        if not is_trusted(peer):
            return peer
        hops = [h.strip() for h in request.headers.get("x-forwarded-for", "").split(",")]
        for hop in reversed(hops):
            if hop and not is_trusted(hop):
                try:
                    return str(ipaddress.ip_address(hop))
                except ValueError:
                    break
        return peer

    @app.post("/otp/send")
    async def send(request: Request):
        ip = client_ip(request)
        msisdn = (await read_fields(request)).get("msisdn")
        if not valid(MSISDN_RE, msisdn):
            return error(400, "bad_request", detail="msisdn: строка из 12 цифр, начинается с 996")

        now = app.state.clock()
        code = app.state.generate_code()
        status, value = await app.state.store.send(msisdn, code_hash(msisdn, code), now)
        if status == "blocked":
            await audit("send_while_blocked", msisdn, ip, retry_after=seconds(value))
            return error(423, "blocked", retry_after=seconds(value))
        if status == "limited":
            await audit("send_rate_limited", msisdn, ip, retry_after=seconds(value))
            return error(429, "rate_limited", retry_after=seconds(value))
        try:
            app.state.outbox.deliver(msisdn, code, now)
        except DeliveryError as exc:
            error_log.error("код не доставлен: %s", exc)
            await audit("delivery_failed", msisdn, ip)
            return error(503, "delivery_failed", retry_after=5)
        await audit("otp_issued", msisdn, ip)
        return {"status": "sent", "ttl": settings.code_ttl, "retry_after": seconds(value)}

    @app.post("/otp/verify")
    async def verify(request: Request):
        ip = client_ip(request)
        fields = await read_fields(request)
        msisdn, code = fields.get("msisdn"), fields.get("code")
        now = app.state.clock()
        store = app.state.store

        if not (valid(MSISDN_RE, msisdn) and valid(CODE_RE, code)):
            # Ошибка формата не трогает счётчик ошибок номера, но запрос
            # всё равно учитывается в лимите по IP.
            msisdn = msisdn if valid(MSISDN_RE, msisdn) else None
            status, value = await store.count_verify(ip, now)
            if status == "ip_limited":
                await audit("ip_rate_limited", msisdn, ip, retry_after=seconds(value))
                return error(429, "ip_rate_limited", retry_after=seconds(value))
            await audit("verify_bad_format", msisdn, ip)
            return error(400, "bad_request", detail="нужны msisdn (12 цифр с 996) и code (6 цифр)")

        status, value = await store.verify(msisdn, ip, code_hash(msisdn, code), now)
        if status == "ok":
            await audit("verify_success", msisdn, ip)
            return {"status": "verified"}
        if status == "ip_limited":
            await audit("ip_rate_limited", msisdn, ip, retry_after=seconds(value))
            return error(429, "ip_rate_limited", retry_after=seconds(value))
        if status == "blocked":
            await audit("verify_while_blocked", msisdn, ip, retry_after=seconds(value))
            return error(423, "blocked", retry_after=seconds(value))
        if status == "missing":
            await audit("verify_no_active_code", msisdn, ip)
            return error(401, "invalid_code")
        if status == "expired":
            await audit("verify_expired", msisdn, ip)
            return error(410, "code_expired")
        if status == "invalid_blocked":
            # Пятая ошибка: номер блокируется, и этот же ответ уже 423.
            await audit("verify_failed", msisdn, ip, attempts_left=0)
            await audit("msisdn_blocked", msisdn, ip, block_seconds=seconds(value))
            return error(423, "blocked", retry_after=seconds(value))
        await audit("verify_failed", msisdn, ip, attempts_left=value)
        return error(401, "invalid_code", attempts_left=value)

    def require_admin(request: Request) -> None:
        if not settings.admin_token:
            return
        given = request.headers.get("x-admin-token") or request.query_params.get("token") or ""
        if not hmac.compare_digest(given.encode(), settings.admin_token.encode()):
            raise HTTPException(403, "нужен токен администратора")

    @app.get("/admin/events")
    async def events(request: Request, limit: int = 200):
        require_admin(request)
        limit = max(1, min(limit, settings.events_keep))
        return {"store": app.state.store.name, "events": await app.state.store.events(limit)}

    @app.get("/dashboard", response_class=HTMLResponse)
    async def dashboard(request: Request):
        require_admin(request)
        return DASHBOARD_HTML

    @app.get("/health")
    async def health():
        return {"status": "ok", "store": app.state.store.name}

    return app


app = create_app()
