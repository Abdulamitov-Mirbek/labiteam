"""Тесты OTP-сервиса по ТЗ и уточнениям жюри (CLARIFICATIONS, раздел 6).

По умолчанию тесты идут на хранилище в памяти. Чтобы прогнать их ещё и на
Redis, задайте TEST_REDIS_URL — ВНИМАНИЕ: эта база очищается (FLUSHDB).
"""
import asyncio
import ipaddress
import json
import logging
import os
import re
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.main import create_app
from scenarios import DATA, last_delivery, run_scenario, start_time

REDIS_URL = os.environ.get("TEST_REDIS_URL", "")
BACKENDS = ["memory"] + (["redis"] if REDIS_URL else [])
NUM = "996770730000"
OTHER = "996770730001"
PROXY = "10.0.0.1"  # адрес «тестового стенда», которому доверен X-Forwarded-For
SCENARIOS = json.loads((DATA / "scenarios_examples.json").read_text(encoding="utf-8"))


def make_settings(backend: str, outbox: Path) -> Settings:
    settings = Settings()
    settings.redis_url = REDIS_URL if backend == "redis" else ""
    settings.admin_token = ""
    settings.outbox = outbox
    settings.trusted_proxies = ()
    if backend == "redis":
        import redis

        redis.from_url(REDIS_URL).flushdb()
    return settings


class Env:
    def __init__(self, client: TestClient, app, security_lines: list[str]):
        self.client, self.app, self.security = client, app, security_lines
        self.settings = app.state.settings
        self.now = 1_800_000_000_000
        app.state.clock = lambda: self.now

    def advance(self, sec: float):
        self.now += round(sec * 1000)

    def send(self, msisdn=NUM):
        return self.client.post("/otp/send", json={"msisdn": msisdn})

    def verify(self, code, msisdn=NUM, ip="192.0.2.10"):
        return self.client.post(
            "/otp/verify", json={"msisdn": msisdn, "code": code}, headers={"X-Forwarded-For": ip}
        )

    def code(self, msisdn=NUM) -> str:
        return last_delivery(self.settings.outbox, msisdn)

    def wrong(self, msisdn=NUM) -> str:
        return f"{(int(self.code(msisdn)) + 1) % 10**6:06d}"

    def fail(self, times: int, msisdn=NUM, ip="192.0.2.10") -> list[int]:
        wrong = self.wrong(msisdn)
        return [self.verify(wrong, msisdn, ip).status_code for _ in range(times)]


@pytest.fixture
def security_lines():
    lines: list[str] = []

    class Capture(logging.Handler):
        def emit(self, record):
            lines.append(record.getMessage())

    handler = Capture()
    logging.getLogger("otp.security").addHandler(handler)
    yield lines
    logging.getLogger("otp.security").removeHandler(handler)


@pytest.fixture(params=BACKENDS)
def env(request, tmp_path, security_lines):
    settings = make_settings(request.param, tmp_path / "outbox" / "sms.jsonl")
    settings.trusted_proxies = (ipaddress.ip_network(PROXY),)
    app = create_app(settings)
    with TestClient(app, client=(PROXY, 50000)) as client:
        yield Env(client, app, security_lines)


# --- открытые сценарии жюри ------------------------------------------------

@pytest.mark.parametrize("backend", BACKENDS)
@pytest.mark.parametrize("scenario", SCENARIOS, ids=[s["id"] for s in SCENARIOS])
def test_jury_scenario(scenario, backend, tmp_path):
    settings = make_settings(backend, tmp_path / "sms.jsonl")
    start = start_time(DATA / scenario["fixture"])
    assert asyncio.run(run_scenario(scenario, settings, start)) == []


# --- выдача и доставка ----------------------------------------------------

def test_send_and_verify_success(env):
    r = env.send()
    assert r.status_code == 200
    assert env.verify(env.code()).status_code == 200


def test_delivery_line_format(env):
    env.send()
    lines = env.settings.outbox.read_text().splitlines()
    assert len(lines) == 1
    record = json.loads(lines[0])
    assert set(record) == {"ts", "msisdn", "code"}
    assert record["msisdn"] == NUM
    assert re.fullmatch(r"\d{6}", record["code"])
    assert re.fullmatch(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d\+06:00", record["ts"])


def test_code_is_not_in_send_response(env):
    r = env.send()
    assert env.code() not in r.text


def test_code_with_leading_zeros(env):
    env.app.state.generate_code = lambda: "000000"
    env.send()
    assert env.code() == "000000"
    assert env.verify("000000").status_code == 200


def test_code_for_other_number_does_not_work(env):
    env.send(NUM)
    env.send(OTHER)
    assert env.verify(env.code(NUM), OTHER).status_code == 401


# --- формат запросов -------------------------------------------------------

@pytest.mark.parametrize("msisdn", [
    "abc", "", "99677073000", "9967707300000", "+996770730000", "796770730000",
    996770730000, None, ["996770730000"],
])
def test_send_bad_msisdn_is_400(env, msisdn):
    assert env.send(msisdn).status_code == 400


def test_send_bad_json_is_400(env):
    for content in [b"{not json", b"[]", b"", b'"996770730000"']:
        r = env.client.post("/otp/send", content=content,
                            headers={"Content-Type": "application/json"})
        assert r.status_code == 400


@pytest.mark.parametrize("code", ["", "12345", "1234567", "12345a", " 123456", 123456, None])
def test_verify_bad_code_is_400_and_not_counted_as_failure(env, code):
    env.send()
    for _ in range(6):
        assert env.verify(code).status_code == 400
    assert env.verify(env.code()).status_code == 200


def test_verify_bad_json_is_400(env):
    r = env.client.post("/otp/verify", content=b"{oops",
                        headers={"Content-Type": "application/json"})
    assert r.status_code == 400


# --- срок жизни и одноразовость -------------------------------------------

def test_code_valid_just_before_expiry(env):
    env.send()
    env.advance(119.999)
    assert env.verify(env.code()).status_code == 200


def test_code_expires_at_exactly_120_seconds(env):
    env.send()
    env.advance(120)
    assert env.verify(env.code()).status_code == 410
    assert env.verify(env.wrong()).status_code == 410


def test_expired_code_does_not_count_as_failure(env):
    env.send()
    env.advance(120)
    for _ in range(6):
        assert env.verify(env.wrong()).status_code == 410
    env.send()
    assert env.verify(env.code()).status_code == 200


def test_reuse_is_401(env):
    env.send()
    assert env.verify(env.code()).status_code == 200
    assert env.verify(env.code()).status_code == 401


def test_verify_without_send_is_401_and_not_counted(env):
    for _ in range(6):
        assert env.verify("123456").status_code == 401
    env.send()
    assert env.verify(env.code()).status_code == 200


def test_new_send_invalidates_previous_code(env):
    codes = iter(["111111", "222222"])
    env.app.state.generate_code = lambda: next(codes)
    env.send()
    env.advance(30)
    assert env.send().status_code == 200
    assert env.verify("111111").status_code == 401
    assert env.verify("222222").status_code == 200


# --- лимиты отправки ---------------------------------------------------------

def test_progressive_resend_delay(env):
    assert env.send().status_code == 200
    r = env.send()
    assert r.status_code == 429 and r.headers["retry-after"] == "30"
    env.advance(29.999)
    assert env.send().status_code == 429
    env.advance(0.001)
    assert env.send().status_code == 200  # вторая выдача, дальше ждать 60 с
    env.advance(59.999)
    assert env.send().status_code == 429
    env.advance(0.001)
    assert env.send().status_code == 200  # третья выдача


def test_max_three_sends_per_ten_minutes(env):
    assert env.send().status_code == 200
    env.advance(30)
    assert env.send().status_code == 200
    env.advance(60)
    assert env.send().status_code == 200
    env.advance(120)
    assert env.send().status_code == 429  # задержка прошла, но в окне 3 выдачи
    env.advance(389.999)  # 599.999 с от первой выдачи
    assert env.send().status_code == 429
    env.advance(0.001)  # первая выдача вышла из окна (t-600, t]
    assert env.send().status_code == 200


def test_step_resets_after_window_clears(env):
    env.send()
    env.advance(600)
    assert env.send().status_code == 200  # снова «первая» выдача
    env.advance(30)
    assert env.send().status_code == 200  # достаточно 30 с, не 60


def test_rejected_send_keeps_current_code(env):
    env.send()
    code = env.code()
    assert env.send().status_code == 429
    assert env.verify(code).status_code == 200


def test_send_limits_are_per_number(env):
    assert env.send(NUM).status_code == 200
    assert env.send(OTHER).status_code == 200


# --- блокировка номера ------------------------------------------------------

def test_fifth_wrong_attempt_is_423(env):
    env.send()
    assert env.fail(5) == [401, 401, 401, 401, 423]
    assert env.verify(env.code()).status_code == 423  # верный код тоже не принимается
    assert env.send().status_code == 423


def test_block_lasts_exactly_900_seconds(env):
    env.send()
    env.fail(5)
    env.advance(899.999)
    assert env.send().status_code == 423
    assert env.verify(env.code()).status_code == 423
    env.advance(0.001)
    assert env.send().status_code == 200
    assert env.verify(env.code()).status_code == 200


def test_success_resets_failure_counter(env):
    env.send()
    assert env.fail(4) == [401] * 4
    assert env.verify(env.code()).status_code == 200
    env.advance(30)
    env.send()
    assert env.fail(4) == [401] * 4
    assert env.verify(env.code()).status_code == 200


def test_new_send_does_not_reset_failure_counter(env):
    env.send()
    assert env.fail(3) == [401] * 3
    env.advance(30)
    env.send()
    assert env.fail(2) == [401, 423]


def test_block_is_per_number(env):
    env.send(NUM)
    env.send(OTHER)
    env.fail(5, NUM)
    assert env.verify(env.code(OTHER), OTHER).status_code == 200


# --- лимит по IP -------------------------------------------------------------

def test_ip_limit_twenty_verifies_per_minute(env):
    for _ in range(20):
        assert env.verify("000000").status_code == 401
    assert env.verify("000000").status_code == 429
    env.advance(59.999)
    assert env.verify("000000").status_code == 429
    env.advance(0.001)
    assert env.verify("000000").status_code == 401


def test_ip_limit_is_per_ip(env):
    for _ in range(20):
        env.verify("000000", ip="192.0.2.10")
    assert env.verify("000000", ip="192.0.2.10").status_code == 429
    assert env.verify("000000", ip="198.51.100.20").status_code == 401


def test_ip_limit_is_checked_before_code_and_block(env):
    env.send()
    env.fail(5)
    for _ in range(15):
        assert env.verify(env.code()).status_code == 423
    assert env.verify(env.code()).status_code == 429


def test_bad_format_verify_counts_toward_ip_limit(env):
    for _ in range(20):
        assert env.verify("bad").status_code == 400
    assert env.verify("bad").status_code == 429
    env.send()
    assert env.verify(env.code()).status_code == 429


def test_forwarded_header_ignored_from_untrusted_peer(tmp_path):
    settings = make_settings("memory", tmp_path / "sms.jsonl")
    with TestClient(create_app(settings), client=("203.0.113.5", 50000)) as client:
        for i in range(21):
            r = client.post("/otp/verify", json={"msisdn": NUM, "code": "000000"},
                            headers={"X-Forwarded-For": f"192.0.2.{i}"})
        assert r.status_code == 429


# --- параллельные запросы ---------------------------------------------------

@pytest.mark.parametrize("backend", BACKENDS)
def test_parallel_wrong_guesses_get_only_four_comparisons(backend, tmp_path):
    scenario = {
        "steps": [
            {"op": "request", "method": "POST", "path": "/otp/send",
             "body": {"msisdn": NUM}, "expected": {"status": 200}},
            {"op": "capture_delivery", "msisdn": NUM, "bind": "c1"},
            {"op": "parallel",
             "requests": [{"path": "/otp/verify", "body": {"msisdn": NUM, "code": "$wrong:c1"}}] * 20,
             "expected": {"count": 20, "status_counts": {"401": 4, "423": 16}}},
        ]
    }
    settings = make_settings(backend, tmp_path / "sms.jsonl")
    assert asyncio.run(run_scenario(scenario, settings, 1_800_000_000_000)) == []


# --- хранение, журнал, отказы -------------------------------------------------

def test_only_hmac_hash_is_stored(env):
    env.send()
    code = env.code()
    store = env.app.state.store
    if store.name == "memory":
        dump = repr(store.codes)
    else:
        import redis

        r = redis.from_url(REDIS_URL, decode_responses=True)
        dump = " ".join(f"{k}={r.get(k) if r.type(k) == 'string' else r.zrange(k, 0, -1)}"
                        for k in r.keys("otp:*") if r.type(k) in ("string", "zset"))
    assert re.search(r"[0-9a-f]{64}", dump)
    assert code not in re.sub(r"[0-9a-f]{32,}|\d{10,}", "", dump)


def test_security_log_has_events_but_no_codes(env):
    env.app.state.generate_code = lambda: "483920"
    env.send()
    env.fail(5)
    env.verify("483920")
    env.send()

    events = env.client.get("/admin/events").json()["events"]
    kinds = {e["event"] for e in events}
    assert {"otp_issued", "verify_failed", "msisdn_blocked",
            "verify_while_blocked", "send_while_blocked"} <= kinds
    assert len(env.security) == len(events)

    journal = "\n".join(env.security) + json.dumps(events)
    journal = re.sub(r'"ts": "[^"]*"', "", journal)
    assert "483920" not in journal and "483921" not in journal


def test_dashboard_and_admin_token(tmp_path):
    settings = make_settings("memory", tmp_path / "sms.jsonl")
    settings.admin_token = "s3cret"
    with TestClient(create_app(settings)) as client:
        assert client.get("/dashboard").status_code == 403
        assert client.get("/admin/events").status_code == 403
        assert client.get("/dashboard?token=s3cret").status_code == 200
        assert client.get("/admin/events", headers={"X-Admin-Token": "s3cret"}).status_code == 200


def test_storage_failure_returns_503(tmp_path):
    settings = make_settings("memory", tmp_path / "sms.jsonl")
    settings.redis_url = "redis://127.0.0.1:1/0"  # заведомо недоступный адрес
    with TestClient(create_app(settings)) as client:
        assert client.post("/otp/send", json={"msisdn": NUM}).status_code == 503
        r = client.post("/otp/verify", json={"msisdn": NUM, "code": "123456"})
        assert r.status_code == 503 and r.json()["error"] == "storage_unavailable"


def test_delivery_failure_returns_503(tmp_path):
    blocker = tmp_path / "file"
    blocker.write_text("")
    settings = make_settings("memory", blocker / "sms.jsonl")  # каталог создать нельзя
    with TestClient(create_app(settings)) as client:
        assert client.post("/otp/send", json={"msisdn": NUM}).status_code == 503
