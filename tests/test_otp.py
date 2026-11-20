"""Сценарии проверки OTP-сервиса.

По умолчанию тесты идут на хранилище в памяти. Чтобы прогнать их ещё и на
Redis, задайте TEST_REDIS_URL — ВНИМАНИЕ: эта база очищается (FLUSHDB).
"""
import asyncio
import json
import logging
import os
import re

import httpx
import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.main import create_app

REDIS_URL = os.environ.get("TEST_REDIS_URL", "")
BACKENDS = ["memory"] + (["redis"] if REDIS_URL else [])
NUM = "996700111222"


class Capture(logging.Handler):
    def __init__(self):
        super().__init__()
        self.lines: list[str] = []

    def emit(self, record):
        self.lines.append(record.getMessage())


def make_settings(backend: str) -> Settings:
    settings = Settings()
    settings.redis_url = REDIS_URL if backend == "redis" else ""
    settings.admin_token = ""
    if backend == "redis":
        import redis

        redis.from_url(REDIS_URL).flushdb()
    return settings


class Env:
    def __init__(self, client, app, sms, security):
        self.client, self.app, self.sms, self.security = client, app, sms, security
        self.now = 1_800_000_000_000
        app.state.clock = lambda: self.now

    def advance(self, sec: float):
        self.now += int(sec * 1000)

    def send(self, msisdn=NUM):
        return self.client.post("/otp/send", json={"msisdn": msisdn})

    def verify(self, code, msisdn=NUM):
        return self.client.post("/otp/verify", json={"msisdn": msisdn, "code": code})

    def code(self, msisdn=NUM) -> str:
        for line in reversed(self.sms.lines):
            m = re.search(rf"на {msisdn}: ваш код (\d{{6}})", line)
            if m:
                return m.group(1)
        raise AssertionError("код не был отправлен")

    def wrong(self, msisdn=NUM) -> str:
        return f"{(int(self.code(msisdn)) + 1) % 10**6:06d}"


@pytest.fixture
def capture():
    sms, security = Capture(), Capture()
    logging.getLogger("otp.sms").addHandler(sms)
    logging.getLogger("otp.security").addHandler(security)
    yield sms, security
    logging.getLogger("otp.sms").removeHandler(sms)
    logging.getLogger("otp.security").removeHandler(security)


@pytest.fixture(params=BACKENDS)
def env(request, capture):
    app = create_app(make_settings(request.param))
    with TestClient(app) as client:
        yield Env(client, app, *capture)


# --- выдача и успешная проверка -------------------------------------------

def test_send_and_verify_success(env):
    r = env.send()
    assert r.status_code == 200
    assert re.fullmatch(r"\d{6}", env.code())
    assert env.verify(env.code()).status_code == 200


def test_code_is_not_in_send_response(env):
    r = env.send()
    assert env.code() not in r.text


def test_msisdn_format_is_normalized(env):
    env.send("+996 700 111-222")
    assert env.verify(env.code(), NUM).status_code == 200


def test_invalid_msisdn_rejected(env):
    assert env.send("abc").status_code == 422
    assert env.client.post("/otp/send", json={}).status_code == 422


def test_code_for_other_number_does_not_work(env):
    env.send("996700000001")
    env.send("996700000002")
    assert env.verify(env.code("996700000001"), "996700000002").status_code == 401


# --- срок жизни и одноразовость -------------------------------------------

def test_code_valid_just_before_expiry(env):
    env.send()
    env.advance(119)
    assert env.verify(env.code()).status_code == 200


def test_code_expires_after_120_seconds(env):
    env.send()
    env.advance(120)
    assert env.verify(env.code()).status_code == 410


def test_code_is_single_use(env):
    env.send()
    assert env.verify(env.code()).status_code == 200
    assert env.verify(env.code()).status_code == 410


def test_verify_without_send(env):
    assert env.verify("123456").status_code == 410


def test_new_send_invalidates_previous_code(env):
    env.send()
    old = env.code()
    env.advance(30)
    assert env.send().status_code == 200
    new = env.code()
    if old != new:
        assert env.verify(old).status_code == 401
    assert env.verify(new).status_code == 200


# --- лимиты отправки ---------------------------------------------------------

def test_progressive_resend_delay(env):
    assert env.send().status_code == 200
    r = env.send()
    assert r.status_code == 429 and r.headers["retry-after"] == "30"
    env.advance(29)
    assert env.send().status_code == 429
    env.advance(1)
    assert env.send().status_code == 200  # вторая выдача, дальше ждать 60 с
    env.advance(59)
    assert env.send().status_code == 429
    env.advance(1)
    assert env.send().status_code == 200  # третья выдача


def test_max_three_sends_per_ten_minutes(env):
    assert env.send().status_code == 200
    env.advance(30)
    assert env.send().status_code == 200
    env.advance(60)
    assert env.send().status_code == 200
    env.advance(120)
    assert env.send().status_code == 429  # задержка прошла, но лимит 3 за 10 минут
    env.advance(389)  # 599 с от первой выдачи
    assert env.send().status_code == 429
    env.advance(1)
    assert env.send().status_code == 200


def test_rejected_send_keeps_current_code(env):
    env.send()
    code = env.code()
    assert env.send().status_code == 429
    assert env.verify(code).status_code == 200


def test_send_limits_are_per_number(env):
    assert env.send("996700000001").status_code == 200
    assert env.send("996700000002").status_code == 200


# --- блокировка номера ------------------------------------------------------

def test_block_after_five_wrong_attempts(env):
    env.send()
    code, wrong = env.code(), env.wrong()
    for _ in range(5):
        assert env.verify(wrong).status_code == 401
    r = env.verify(code)
    assert r.status_code == 423  # даже верный код не принимается


def test_block_lasts_fifteen_minutes(env):
    env.send()
    wrong = env.wrong()
    for _ in range(5):
        env.verify(wrong)
    env.advance(899)
    assert env.send().status_code == 200
    assert env.verify(env.code()).status_code == 423
    env.advance(1)
    assert env.verify(env.code()).status_code == 200


def test_four_wrong_then_correct_succeeds_and_resets_counter(env):
    env.send()
    wrong = env.wrong()
    for _ in range(4):
        assert env.verify(wrong).status_code == 401
    assert env.verify(env.code()).status_code == 200
    env.advance(30)
    env.send()
    wrong = env.wrong()
    for _ in range(4):
        assert env.verify(wrong).status_code == 401
    assert env.verify(env.code()).status_code == 200


def test_new_send_does_not_reset_failure_counter(env):
    env.send()
    wrong = env.wrong()
    for _ in range(3):
        assert env.verify(wrong).status_code == 401
    env.advance(30)
    env.send()
    wrong = env.wrong()
    for _ in range(2):
        assert env.verify(wrong).status_code == 401
    assert env.verify(env.code()).status_code == 423


def test_malformed_code_counts_as_wrong(env):
    env.send()
    for bad in ["", "abc", "12345", "1234567", 123, "x" * 5000]:
        r = env.verify(bad)
        assert r.status_code in (401, 423)
    assert env.verify(env.code()).status_code == 423


# --- лимит по IP -------------------------------------------------------------

def test_ip_limit_twenty_verifies_per_minute(env):
    for i in range(20):
        assert env.verify("000000", f"9967001{i:05d}").status_code == 410
    assert env.verify("000000", "996700199999").status_code == 429
    env.advance(60)
    assert env.verify("000000", "996700199999").status_code == 410


def test_ip_limit_protects_correct_code_too(env):
    env.send()
    for i in range(20):
        env.verify("000000", f"9967001{i:05d}")
    assert env.verify(env.code()).status_code == 429
    env.advance(60)
    assert env.verify(env.code()).status_code == 200


def test_forwarded_header_cannot_bypass_ip_limit(env):
    for i in range(20):
        env.verify("000000", f"9967001{i:05d}")
    r = env.client.post(
        "/otp/verify",
        json={"msisdn": NUM, "code": "000000"},
        headers={"X-Forwarded-For": "1.2.3.4", "X-Real-IP": "1.2.3.4"},
    )
    assert r.status_code == 429


# --- параллельные запросы ---------------------------------------------------

@pytest.mark.parametrize("backend", BACKENDS)
def test_parallel_verify_exactly_one_success(backend, capture):
    sms, _ = capture

    async def scenario():
        settings = make_settings(backend)
        settings.ip_limit = 1000
        app = create_app(settings)
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://t") as client:
            await client.post("/otp/send", json={"msisdn": NUM})
            code = re.search(r"ваш код (\d{6})", sms.lines[-1]).group(1)
            results = await asyncio.gather(
                *[client.post("/otp/verify", json={"msisdn": NUM, "code": code}) for _ in range(50)]
            )
        await app.state.store.close()
        return [r.status_code for r in results]

    codes = asyncio.run(scenario())
    assert codes.count(200) == 1
    assert set(codes) == {200, 410}


@pytest.mark.parametrize("backend", BACKENDS)
def test_parallel_wrong_guesses_do_not_exceed_five(backend, capture):
    async def scenario():
        settings = make_settings(backend)
        settings.ip_limit = 1000
        app = create_app(settings)
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://t") as client:
            await client.post("/otp/send", json={"msisdn": NUM})
            results = await asyncio.gather(
                *[client.post("/otp/verify", json={"msisdn": NUM, "code": "bad"}) for _ in range(50)]
            )
        await app.state.store.close()
        return [r.status_code for r in results]

    codes = asyncio.run(scenario())
    assert codes.count(401) == 5
    assert codes.count(423) == 45


# --- хранение и журнал ------------------------------------------------------

def test_only_hmac_hash_is_stored(env):
    env.send()
    code = env.code()
    store = env.app.state.store
    if store.name == "memory":
        dump = repr(store.codes)
    else:
        import redis

        r = redis.from_url(REDIS_URL, decode_responses=True)
        dump = " ".join(str(r.get(k)) for k in r.keys("otp:code:*"))
    assert re.search(r"[0-9a-f]{64}", dump)
    assert code not in re.sub(r"[0-9a-f]{64}|\d{10,}", "", dump)


def test_security_log_has_events_but_no_codes(env):
    env.send()
    code, wrong = env.code(), env.wrong()
    for _ in range(5):
        env.verify(wrong)
    env.verify(code)
    env.send()

    events = env.client.get("/admin/events").json()["events"]
    kinds = {e["event"] for e in events}
    assert {"otp_issued", "verify_failed", "msisdn_blocked",
            "verify_while_blocked", "send_rate_limited"} <= kinds

    journal = "\n".join(env.security.lines) + json.dumps(events)
    assert len(env.security.lines) == len(events)
    journal_without_meta = re.sub(r'"(ts|msisdn|ip)": "[^"]*"', "", journal)
    assert code not in journal_without_meta
    assert wrong not in journal_without_meta


def test_dashboard_and_admin_token(capture):
    settings = make_settings("memory")
    settings.admin_token = "s3cret"
    with TestClient(create_app(settings)) as client:
        assert client.get("/dashboard").status_code == 403
        assert client.get("/admin/events").status_code == 403
        assert client.get("/dashboard?token=s3cret").status_code == 200
        assert client.get("/admin/events", headers={"X-Admin-Token": "s3cret"}).status_code == 200


def test_storage_failure_returns_503(capture):
    settings = make_settings("memory")
    settings.redis_url = "redis://127.0.0.1:1/0"  # заведомо недоступный адрес
    with TestClient(create_app(settings)) as client:
        assert client.post("/otp/send", json={"msisdn": NUM}).status_code == 503
        r = client.post("/otp/verify", json={"msisdn": NUM, "code": "123456"})
        assert r.status_code == 503 and r.json() == {"error": "storage_unavailable"}
