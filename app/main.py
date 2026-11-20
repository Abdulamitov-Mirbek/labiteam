"""OTP-сервис: выдача и проверка одноразовых кодов с защитой от перебора."""
import hashlib
import hmac
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
from pydantic import BaseModel

from .config import Settings
from .store import StoreUnavailable, make_store

MSISDN_RE = re.compile(r"\d{7,15}")
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


# Два раздельных канала: «SMS-шлюз» (единственное место, где виден код)
# и журнал событий безопасности (кодов в нём нет никогда).
sms_log = _logger("otp.sms")
security_log = _logger("otp.security")
error_log = _logger("otp.error")


class SendBody(BaseModel):
    msisdn: str | int


class VerifyBody(BaseModel):
    msisdn: str | int
    code: str | int


def normalize_msisdn(raw: str | int) -> str:
    digits = re.sub(r"[\s\-()]", "", str(raw)).removeprefix("+")
    if not MSISDN_RE.fullmatch(digits):
        raise HTTPException(422, "msisdn: ожидается 7–15 цифр")
    return digits


def seconds(ms: int) -> int:
    return max(1, math.ceil(ms / 1000))


def create_app(settings: Settings | None = None, store=None) -> FastAPI:
    settings = settings or Settings()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        yield
        await app.state.store.close()

    app = FastAPI(title="OTP-сервис", lifespan=lifespan)
    app.state.settings = settings
    app.state.store = store or make_store(settings)
    app.state.clock = lambda: int(time.time() * 1000)

    @app.exception_handler(StoreUnavailable)
    async def store_unavailable(request: Request, exc: StoreUnavailable):
        # Закрытый отказ: без хранилища лимиты не проверить, поэтому коды
        # не выдаются и не принимаются.
        error_log.error("хранилище недоступно: %s", exc)
        return JSONResponse(
            {"error": "storage_unavailable"}, status_code=503, headers={"Retry-After": "5"}
        )

    def code_hash(msisdn: str, code: str) -> str:
        # Хеш привязан к номеру: код одного номера не подходит к другому.
        return hmac.new(settings.secret, f"{msisdn}:{code}".encode(), hashlib.sha256).hexdigest()

    async def audit(event: str, msisdn: str, ip: str, **details) -> None:
        record = {
            "ts": datetime.now(timezone.utc).isoformat(timespec="milliseconds"),
            "event": event,
            "msisdn": msisdn,
            "ip": ip,
            **details,
        }
        security_log.info(json.dumps(record, ensure_ascii=False))
        await app.state.store.add_event(record)

    def limited(error: str, wait_ms: int, status: int = 429) -> JSONResponse:
        wait = seconds(wait_ms)
        return JSONResponse(
            {"error": error, "retry_after": wait},
            status_code=status,
            headers={"Retry-After": str(wait)},
        )

    def client_ip(request: Request) -> str:
        # Только адрес TCP-соединения; заголовки вида X-Forwarded-For игнорируются.
        return request.client.host if request.client else "unknown"

    @app.post("/otp/send")
    async def send(body: SendBody, request: Request):
        msisdn = normalize_msisdn(body.msisdn)
        ip = client_ip(request)
        code = f"{secrets.randbelow(10**6):06d}"
        status, value = await app.state.store.send(
            msisdn, code_hash(msisdn, code), app.state.clock()
        )
        if status == "limited":
            await audit("send_rate_limited", msisdn, ip, retry_after=seconds(value))
            return limited("rate_limited", value)
        sms_log.info(f"[SMS] на {msisdn}: ваш код {code} (действует {settings.code_ttl} с)")
        await audit("otp_issued", msisdn, ip)
        return {"status": "sent", "ttl": settings.code_ttl, "retry_after": seconds(value)}

    @app.post("/otp/verify")
    async def verify(body: VerifyBody, request: Request):
        msisdn = normalize_msisdn(body.msisdn)
        ip = client_ip(request)
        code = str(body.code)[:64]
        status, value = await app.state.store.verify(
            msisdn, ip, code_hash(msisdn, code), app.state.clock()
        )
        if status == "ok":
            await audit("verify_success", msisdn, ip)
            return {"status": "verified"}
        if status == "ip_limited":
            await audit("ip_rate_limited", msisdn, ip, retry_after=seconds(value))
            return limited("ip_rate_limited", value)
        if status == "blocked":
            await audit("verify_while_blocked", msisdn, ip, retry_after=seconds(value))
            return limited("blocked", value, status=423)
        if status == "expired":
            await audit("verify_no_active_code", msisdn, ip)
            return JSONResponse({"error": "code_expired"}, status_code=410)
        if status == "invalid_blocked":
            await audit("verify_failed", msisdn, ip, attempts_left=0)
            await audit("msisdn_blocked", msisdn, ip, block_seconds=seconds(value))
            return JSONResponse({"error": "invalid_code", "attempts_left": 0}, status_code=401)
        await audit("verify_failed", msisdn, ip, attempts_left=value)
        return JSONResponse({"error": "invalid_code", "attempts_left": value}, status_code=401)

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
