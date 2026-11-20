"""Хранилища состояния OTP.

Оба хранилища реализуют одну и ту же логику и выполняют каждую операцию
атомарно:
  * RedisStore  — Lua-скрипты (Redis исполняет скрипт целиком, без чередования);
  * MemoryStore — синхронный код без await внутри одного event loop.

Время (`now`, миллисекунды) передаётся снаружи: логические проверки сроков
идут по нему, а TTL ключей Redis служат уборкой мусора.

Результат каждой операции — пара (статус, число):

send()         ("ok", задержка_до_следующей_мс) | ("blocked", ждать_мс)
               | ("limited", ждать_мс)
verify()       ("ok", 0) | ("ip_limited", ждать_мс) | ("blocked", ждать_мс)
               | ("missing", 0)  — кода нет или он уже использован
               | ("expired", 0)  — код истёк неиспользованным
               | ("invalid", осталось_попыток)
               | ("invalid_blocked", срок_блокировки_мс) — пятая ошибка
count_verify() ("ok", 0) | ("ip_limited", ждать_мс) — учёт запроса с ошибкой
               формата в лимите по IP
"""
import hmac
import json
import uuid
from collections import deque

from .config import Settings


class StoreUnavailable(Exception):
    """Хранилище не отвечает — запрос нельзя безопасно обработать."""


# Лимит проверок с одного IP: скользящее окно (now - window, now].
# KEYS[1] — множество запросов IP; ARGV: now, окно, лимит, уникальная метка.
IP_LIMIT_LUA = """
local now = tonumber(ARGV[1])
local ip_window = tonumber(ARGV[2])
redis.call('ZREMRANGEBYSCORE', KEYS[1], '-inf', now - ip_window)
if redis.call('ZCARD', KEYS[1]) >= tonumber(ARGV[3]) then
  local oldest = redis.call('ZRANGE', KEYS[1], 0, 0, 'WITHSCORES')
  return {'ip_limited', tonumber(oldest[2]) + ip_window - now}
end
redis.call('ZADD', KEYS[1], now, ARGV[4])
redis.call('PEXPIRE', KEYS[1], ip_window)
"""

COUNT_VERIFY_LUA = IP_LIMIT_LUA + "return {'ok', 0}"

# KEYS: выдачи номера, запись кода, блокировка номера.
SEND_LUA = """
local now = tonumber(ARGV[1])
local window = tonumber(ARGV[2])
local delays = {}
for d in string.gmatch(ARGV[7], '%d+') do delays[#delays + 1] = tonumber(d) end

local till = tonumber(redis.call('GET', KEYS[3]) or 0)
if till > now then return {'blocked', till - now} end

redis.call('ZREMRANGEBYSCORE', KEYS[1], '-inf', now - window)
local n = redis.call('ZCARD', KEYS[1])
if n >= tonumber(ARGV[3]) then
  local oldest = redis.call('ZRANGE', KEYS[1], 0, 0, 'WITHSCORES')
  return {'limited', tonumber(oldest[2]) + window - now}
end
if n > 0 then
  local last = redis.call('ZRANGE', KEYS[1], -1, -1, 'WITHSCORES')
  local wait = tonumber(last[2]) + delays[math.min(n, #delays)] - now
  if wait > 0 then return {'limited', wait} end
end
redis.call('ZADD', KEYS[1], now, ARGV[6])
redis.call('PEXPIRE', KEYS[1], window)
-- SET перезаписывает запись: предыдущий код аннулируется.
redis.call('SET', KEYS[2], string.format('%d:%s', now + tonumber(ARGV[4]), ARGV[5]),
           'PX', tonumber(ARGV[8]))
return {'ok', delays[math.min(n + 1, #delays)]}
"""

# KEYS: запросы IP, блокировка номера, запись кода, счётчик ошибок.
VERIFY_LUA = IP_LIMIT_LUA + """
local max_failures = tonumber(ARGV[6])
local block_ttl = tonumber(ARGV[7])

local till = tonumber(redis.call('GET', KEYS[2]) or 0)
if till > now then return {'blocked', till - now} end

local rec = redis.call('GET', KEYS[3])
if not rec then return {'missing', 0} end
local sep = string.find(rec, ':', 1, true)
if tonumber(string.sub(rec, 1, sep - 1)) <= now then return {'expired', 0} end

-- Сравнение хешей за постоянное время: проходим все байты без раннего выхода.
local stored = string.sub(rec, sep + 1)
local cand = ARGV[5]
local diff = bit.bxor(#stored, #cand)
for i = 1, #stored do
  diff = bit.bor(diff, bit.bxor(string.byte(stored, i), string.byte(cand, i) or 0))
end
if diff == 0 then
  -- Код одноразовый: удаляется в том же атомарном шаге, что и проверка,
  -- поэтому из параллельных запросов успешен ровно один.
  redis.call('DEL', KEYS[3], KEYS[4])
  return {'ok', 0}
end

local n = redis.call('INCR', KEYS[4])
redis.call('PEXPIRE', KEYS[4], block_ttl)
if n >= max_failures then
  redis.call('SET', KEYS[2], string.format('%d', now + block_ttl), 'PX', block_ttl)
  redis.call('DEL', KEYS[4])
  return {'invalid_blocked', block_ttl}
end
return {'invalid', max_failures - n}
"""


class RedisStore:
    name = "redis"

    def __init__(self, settings: Settings):
        import redis.asyncio as redis

        self.s = settings
        self.r = redis.from_url(settings.redis_url, decode_responses=True)
        self._send = self.r.register_script(SEND_LUA)
        self._verify = self.r.register_script(VERIFY_LUA)
        self._count_verify = self.r.register_script(COUNT_VERIFY_LUA)

    async def _call(self, operation, *args, **kwargs):
        from redis.exceptions import RedisError

        try:
            return await operation(*args, **kwargs)
        except (RedisError, OSError) as error:
            raise StoreUnavailable(str(error)) from error

    def _ip_args(self, now: int) -> list:
        return [now, self.s.ip_window * 1000, self.s.ip_limit, f"{now}-{uuid.uuid4().hex}"]

    async def send(self, msisdn: str, code_hash: str, now: int):
        s = self.s
        status, value = await self._call(
            self._send,
            keys=[f"otp:sends:{msisdn}", f"otp:code:{msisdn}", f"otp:block:{msisdn}"],
            args=[
                now, s.send_window * 1000, s.send_limit, s.code_ttl * 1000,
                code_hash, f"{now}-{uuid.uuid4().hex}",
                ",".join(str(d * 1000) for d in s.send_delays),
                (s.code_ttl + s.record_retention) * 1000,
            ],
        )
        return status, int(value)

    async def verify(self, msisdn: str, ip: str, code_hash: str, now: int):
        s = self.s
        status, value = await self._call(
            self._verify,
            keys=[
                f"otp:ip:{ip}", f"otp:block:{msisdn}",
                f"otp:code:{msisdn}", f"otp:fails:{msisdn}",
            ],
            args=[*self._ip_args(now), code_hash, s.max_failures, s.block_ttl * 1000],
        )
        return status, int(value)

    async def count_verify(self, ip: str, now: int):
        status, value = await self._call(
            self._count_verify, keys=[f"otp:ip:{ip}"], args=self._ip_args(now)
        )
        return status, int(value)

    async def add_event(self, event: dict) -> None:
        pipe = self.r.pipeline()
        pipe.lpush("otp:events", json.dumps(event, ensure_ascii=False))
        pipe.ltrim("otp:events", 0, self.s.events_keep - 1)
        await self._call(pipe.execute)

    async def events(self, limit: int) -> list[dict]:
        rows = await self._call(self.r.lrange, "otp:events", 0, limit - 1)
        return [json.loads(row) for row in rows]

    async def close(self) -> None:
        await self.r.aclose()


class MemoryStore:
    """Хранилище в памяти процесса (для запуска без Redis, один воркер)."""

    name = "memory"

    def __init__(self, settings: Settings):
        self.s = settings
        self.sends: dict[str, list[int]] = {}
        self.codes: dict[str, tuple[int, str]] = {}  # msisdn -> (истекает, хеш)
        self.fails: dict[str, tuple[int, int]] = {}  # msisdn -> (истекает, счётчик)
        self.blocks: dict[str, int] = {}  # msisdn -> заблокирован до
        self.ips: dict[str, list[int]] = {}
        self.log: deque[dict] = deque(maxlen=settings.events_keep)
        self._ops = 0

    def _sweep(self, now: int) -> None:
        """Раз в 1000 операций выбрасывает устаревшие записи."""
        self._ops += 1
        if self._ops % 1000:
            return
        s = self.s
        retention = s.record_retention * 1000
        self.sends = {k: v for k, v in self.sends.items() if v[-1] > now - s.send_window * 1000}
        self.ips = {k: v for k, v in self.ips.items() if v[-1] > now - s.ip_window * 1000}
        self.codes = {k: v for k, v in self.codes.items() if v[0] + retention > now}
        self.fails = {k: v for k, v in self.fails.items() if v[0] > now}
        self.blocks = {k: v for k, v in self.blocks.items() if v > now}

    def _hit_ip(self, ip: str, now: int) -> int:
        """Учитывает запрос в лимите IP; возвращает 0 или время ожидания, мс."""
        window = self.s.ip_window * 1000
        hits = [t for t in self.ips.get(ip, []) if t > now - window]
        self.ips[ip] = hits
        if len(hits) >= self.s.ip_limit:
            return hits[0] + window - now
        hits.append(now)
        return 0

    async def send(self, msisdn: str, code_hash: str, now: int):
        s = self.s
        self._sweep(now)
        till = self.blocks.get(msisdn, 0)
        if till > now:
            return "blocked", till - now

        window = s.send_window * 1000
        delays = [d * 1000 for d in s.send_delays]
        sent = [t for t in self.sends.get(msisdn, []) if t > now - window]
        n = len(sent)
        if n >= s.send_limit:
            return "limited", sent[0] + window - now
        if n > 0:
            wait = sent[-1] + delays[min(n, len(delays)) - 1] - now
            if wait > 0:
                return "limited", wait
        sent.append(now)
        self.sends[msisdn] = sent
        self.codes[msisdn] = (now + s.code_ttl * 1000, code_hash)
        return "ok", delays[min(n + 1, len(delays)) - 1]

    async def verify(self, msisdn: str, ip: str, code_hash: str, now: int):
        s = self.s
        self._sweep(now)
        wait = self._hit_ip(ip, now)
        if wait:
            return "ip_limited", wait

        till = self.blocks.get(msisdn, 0)
        if till > now:
            return "blocked", till - now

        record = self.codes.get(msisdn)
        if record is None:
            return "missing", 0
        if record[0] <= now:
            return "expired", 0

        if hmac.compare_digest(record[1], code_hash):
            del self.codes[msisdn]
            self.fails.pop(msisdn, None)
            return "ok", 0

        block_ttl = s.block_ttl * 1000
        expires, count = self.fails.get(msisdn, (0, 0))
        count = count + 1 if expires > now else 1
        if count >= s.max_failures:
            self.blocks[msisdn] = now + block_ttl
            self.fails.pop(msisdn, None)
            return "invalid_blocked", block_ttl
        self.fails[msisdn] = (now + block_ttl, count)
        return "invalid", s.max_failures - count

    async def count_verify(self, ip: str, now: int):
        wait = self._hit_ip(ip, now)
        return ("ip_limited", wait) if wait else ("ok", 0)

    async def add_event(self, event: dict) -> None:
        self.log.appendleft(event)

    async def events(self, limit: int) -> list[dict]:
        return list(self.log)[:limit]

    async def close(self) -> None:
        pass


def make_store(settings: Settings):
    return RedisStore(settings) if settings.redis_url else MemoryStore(settings)
