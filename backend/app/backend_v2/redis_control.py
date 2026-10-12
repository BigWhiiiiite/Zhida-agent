"""Injected Redis cache/control clients; no personal truth lives in these keys."""
from __future__ import annotations

import hashlib
import json
import math
import random
import threading
import time
import uuid
from dataclasses import dataclass
from typing import Any, Callable, Optional


TOKEN_BUCKET_LUA = """
local stamp=redis.call('TIME'); local now=stamp[1]*1000+math.floor(stamp[2]/1000)
local capacity=tonumber(ARGV[1]); local rate=tonumber(ARGV[2]); local cost=tonumber(ARGV[3])
local old=redis.call('HMGET',KEYS[1],'tokens','updated')
local tokens=tonumber(old[1]) or capacity; local updated=tonumber(old[2]) or now
tokens=math.min(capacity,tokens+math.max(0,now-updated)*rate/1000)
local allowed=0; local retry=0
if tokens>=cost then tokens=tokens-cost; allowed=1 else retry=math.ceil((cost-tokens)*1000/rate) end
redis.call('HSET',KEYS[1],'tokens',tokens,'updated',now)
redis.call('PEXPIRE',KEYS[1],math.ceil(2*capacity*1000/rate))
return {allowed,math.floor(tokens),retry}
"""

RELEASE_LOCK_LUA = """
if redis.call('GET',KEYS[1])==ARGV[1] then return redis.call('DEL',KEYS[1]) end
return 0
"""

RENEW_LOCK_LUA = """
if redis.call('GET',KEYS[1])==ARGV[1] then return redis.call('PEXPIRE',KEYS[1],ARGV[2]) end
return 0
"""

# A sliding 60s provider window (not a separate per-worker quota). Token usage is
# reserved conservatively; result settlement may refund an overestimate. Expired
# in-flight requests are deliberately NOT deleted: timeout is not cancellation.
RESERVE_PROVIDER_LUA = """
local stamp=redis.call('TIME'); local now=stamp[1]*1000+math.floor(stamp[2]/1000)
local rpm=tonumber(ARGV[1]); local tpm=tonumber(ARGV[2]); local slots=tonumber(ARGV[3])
local estimate=tonumber(ARGV[4]); local timeout=tonumber(ARGV[5]); local call=ARGV[6]
if redis.call('HEXISTS',KEYS[3],call)==1 then return {0,'already_reserved',0} end
local expired=redis.call('ZRANGEBYSCORE',KEYS[1],'-inf',now-60000)
for _,id in ipairs(expired) do redis.call('HDEL',KEYS[2],id) end
redis.call('ZREMRANGEBYSCORE',KEYS[1],'-inf',now-60000)
if redis.call('ZCOUNT',KEYS[4],'-inf',now)>0 then return {0,'unknown_inflight',1000} end
if redis.call('ZCARD',KEYS[4])>=slots then return {0,'concurrency',1000} end
if redis.call('ZCARD',KEYS[1])>=rpm then return {0,'rpm',1000} end
local used=0; for _,value in ipairs(redis.call('HVALS',KEYS[2])) do used=used+tonumber(value) end
if used+estimate>tpm then return {0,'tpm',1000} end
redis.call('ZADD',KEYS[1],now,call); redis.call('HSET',KEYS[2],call,estimate)
redis.call('HSET',KEYS[3],call,'reserved'); redis.call('ZADD',KEYS[4],now+timeout,call)
return {1,'accepted',0}
"""

SETTLE_PROVIDER_LUA = """
local call=ARGV[1]; local usage=tonumber(ARGV[2]); local state=ARGV[3]
local old=redis.call('HGET',KEYS[3],call)
if not old or old=='completed' or old=='cancelled_before_send' then return 0 end
if state=='cancelled_before_send' and old~='reserved' then return 0 end
if state=='unknown' then redis.call('HSET',KEYS[3],call,'unknown'); return 1 end
if state=='cancelled_before_send' and old=='reserved' then
 redis.call('ZREM',KEYS[1],call); redis.call('HDEL',KEYS[2],call)
elseif redis.call('ZSCORE',KEYS[1],call) and usage>=0 then redis.call('HSET',KEYS[2],call,usage) end
redis.call('HSET',KEYS[3],call,state); redis.call('ZREM',KEYS[4],call)
return 1
"""


class ControlUnavailable(RuntimeError):
    """New protected work must wait; an outage never grants unlimited permits."""


class CacheBusy(RuntimeError):
    pass


@dataclass(frozen=True)
class RateDecision:
    allowed: bool
    remaining: int
    retry_after_ms: int


@dataclass(frozen=True)
class ProviderLimits:
    requests_per_minute: int
    tokens_per_minute: int
    concurrency: int

    def __post_init__(self):
        if min(self.requests_per_minute, self.tokens_per_minute, self.concurrency) < 1:
            raise ValueError('Provider limits must be positive')


@dataclass(frozen=True)
class ProviderDecision:
    allowed: bool
    reason: str
    retry_after_ms: int


def private_key(namespace: str, *, user_id: str, **versions: Any) -> str:
    """Caller supplies every authorization/source/version affecting its result."""
    if not user_id or not namespace:
        raise ValueError('Private cache keys require user scope')
    encoded = json.dumps({'user_id': user_id, 'versions': versions}, sort_keys=True, separators=(',', ':'),
                         ensure_ascii=False, allow_nan=False)
    return namespace + ':' + hashlib.sha256(encoded.encode()).hexdigest()


class RedisControl:
    def __init__(self, client, *, prefix: str = 'zhida:v2'):
        self.client, self.prefix = client, prefix

    def _eval(self, script: str, keys: list, args: list):
        try:
            return self.client.eval(script, len(keys), *keys, *args)
        except Exception as exc:
            raise ControlUnavailable('Coordination service unavailable') from exc

    def rate_limit(self, *, user_id: str, route_group: str, capacity: float,
                   refill_per_second: float, cost: float = 1) -> RateDecision:
        if not all(math.isfinite(x) and x > 0 for x in (capacity, refill_per_second, cost)) or cost > capacity:
            raise ValueError('Invalid token bucket configuration')
        key = private_key(self.prefix + ':ctl:rate', user_id=user_id, route=route_group)
        allowed, remaining, retry = self._eval(TOKEN_BUCKET_LUA, [key],
                                              [capacity, refill_per_second, cost])
        return RateDecision(bool(allowed), int(remaining), int(retry))

    def provider_keys(self, account: str) -> list:
        if not account:
            raise ValueError('Provider account scope is required')
        tag = hashlib.sha256(account.encode()).hexdigest()[:24]
        stem = self.prefix + ':ctl:provider:{' + tag + '}'
        return [stem + ':requests', stem + ':usage', stem + ':calls', stem + ':inflight']

    def reserve_provider(self, *, account: str, call_id: str, limits: ProviderLimits,
                         estimated_tokens: int, timeout_ms: int = 180000) -> ProviderDecision:
        if not call_id or not 0 <= estimated_tokens <= limits.tokens_per_minute or not 100 <= timeout_ms <= 3600000:
            raise ValueError('Invalid provider reservation')
        accepted, reason, retry = self._eval(RESERVE_PROVIDER_LUA, self.provider_keys(account),
                                            [limits.requests_per_minute, limits.tokens_per_minute,
                                             limits.concurrency, estimated_tokens, timeout_ms, call_id])
        if isinstance(reason, bytes):
            reason = reason.decode()
        return ProviderDecision(bool(accepted), str(reason), int(retry))

    def settle_provider(self, *, account: str, call_id: str,
                        actual_tokens: Optional[int] = None, state: str = 'completed') -> bool:
        if state not in {'completed', 'unknown', 'cancelled_before_send'}:
            raise ValueError('Invalid settlement state')
        if actual_tokens is not None and actual_tokens < 0:
            raise ValueError('Invalid actual usage')
        return bool(self._eval(SETTLE_PROVIDER_LUA, self.provider_keys(account),
                               [call_id, actual_tokens if actual_tokens is not None else -1, state]))

    def mark_provider_sent(self, *, account: str, call_id: str) -> bool:
        script = """
        if redis.call('HGET',KEYS[1],ARGV[1])=='reserved' then
          redis.call('HSET',KEYS[1],ARGV[1],'sent'); return 1
        end
        return 0
        """
        return bool(self._eval(script, [self.provider_keys(account)[2]], [call_id]))

    def prune_settled_calls(self, *, account: str, call_ids: list) -> int:
        """Only the durable model-call reconciler supplies terminal, old IDs."""
        script = """
        local n=0
        for _,id in ipairs(ARGV) do
          local s=redis.call('HGET',KEYS[3],id)
          if (s=='completed' or s=='cancelled_before_send') and
             not redis.call('ZSCORE',KEYS[1],id) and not redis.call('ZSCORE',KEYS[4],id) then
              n=n+redis.call('HDEL',KEYS[3],id)
          end
        end
        return n
        """
        return int(self._eval(script, self.provider_keys(account), list(call_ids)))

    def acquire_rebuild(self, cache_key: str, *, ttl_ms: int = 30000) -> Optional[str]:
        if not 100 <= ttl_ms <= 300000:
            raise ValueError('Invalid lock TTL')
        token = str(uuid.uuid4())
        key = self._lock_key(cache_key)
        try:
            return token if self.client.set(key, token, nx=True, px=ttl_ms) else None
        except Exception as exc:
            raise ControlUnavailable('Cache coordination unavailable') from exc

    def _lock_key(self, cache_key: str) -> str:
        return self.prefix + ':ctl:rebuild:' + hashlib.sha256(cache_key.encode()).hexdigest()

    def renew_rebuild(self, cache_key: str, token: str, *, ttl_ms: int = 30000) -> bool:
        return bool(self._eval(RENEW_LOCK_LUA, [self._lock_key(cache_key)], [token, ttl_ms]))

    def release_rebuild(self, cache_key: str, token: str) -> bool:
        return bool(self._eval(RELEASE_LOCK_LUA, [self._lock_key(cache_key)], [token]))


@dataclass
class _Flight:
    ready: threading.Event
    result: Any = None
    error: Optional[BaseException] = None


class VersionedCache:
    """Cache-aside with one local loader, distributed rebuild lock and bounded fallback.

    Loader must validate ownership first. None means authoritative not-found;
    exceptions and pending-index states are never negative-cache entries.
    """
    def __init__(self, cache_client, control: RedisControl, *, max_fallback: int = 2,
                 max_wait_seconds: float = 0.25, random_value: Callable = random.random):
        if max_fallback < 1 or max_wait_seconds < 0:
            raise ValueError('Invalid fallback bounds')
        self.client, self.control = cache_client, control
        self.max_wait, self.random = max_wait_seconds, random_value
        self._fallback = threading.BoundedSemaphore(max_fallback)
        self._mutex = threading.Lock()
        self._flights = {}

    def _read(self, key):
        try:
            raw = self.client.get(key)
            if raw is None:
                return False, None
            item = json.loads(raw)
            if item.get('format') != 1:
                return False, None
            return True, item['value']
        except Exception:
            return False, None

    def _store(self, key, value, ttl):
        try:
            expiry = max(1, int(ttl * (0.8 + 0.4 * self.random())))
            self.client.set(key, json.dumps({'format': 1, 'value': value}, ensure_ascii=False,
                                            allow_nan=False), ex=expiry)
        except Exception:
            # Caching is optional. A successful authoritative read stays successful.
            pass

    def get_or_load(self, key: str, loader: Callable, *, ttl_seconds: int = 300,
                    negative_ttl: int = 30):
        if min(ttl_seconds, negative_ttl) <= 0:
            raise ValueError('Cache TTL must be positive')
        hit, result = self._read(key)
        if hit:
            return result
        with self._mutex:
            flight = self._flights.get(key)
            leader = flight is None
            if leader:
                flight = _Flight(threading.Event())
                self._flights[key] = flight
        if not leader:
            if not flight.ready.wait(self.max_wait):
                raise CacheBusy('Cache rebuild is in progress')
            if flight.error:
                raise flight.error
            return flight.result
        token, coordinated = None, True
        stop_renewal = threading.Event()
        renewal = None
        try:
            try:
                token = self.control.acquire_rebuild(key)
            except ControlUnavailable:
                coordinated = False
            if coordinated and token is None:
                until = time.monotonic() + self.max_wait
                while time.monotonic() < until:
                    hit, result = self._read(key)
                    if hit:
                        flight.result = result
                        return result
                    time.sleep(min(0.025, max(0, until-time.monotonic())))
                raise CacheBusy('Another process is rebuilding this value')
            hit, result = self._read(key)
            if hit:
                flight.result = result
                return result
            if not self._fallback.acquire(blocking=False):
                raise CacheBusy('Authoritative fallback concurrency is full')
            try:
                if token:
                    def renew_while_loading():
                        # Bound coordination, even if a broken loader never returns.
                        deadline = time.monotonic() + 120
                        while not stop_renewal.wait(10) and time.monotonic() < deadline:
                            try:
                                if not self.control.renew_rebuild(key, token):
                                    return
                            except ControlUnavailable:
                                return
                    renewal = threading.Thread(target=renew_while_loading, daemon=True)
                    renewal.start()
                result = loader()
            finally:
                self._fallback.release()
            # Cache lock is only an optimization. Every key is immutable/versioned;
            # a slow expired leader cannot overwrite a newer version's key.
            if token:
                try:
                    if self.control.renew_rebuild(key, token):
                        self._store(key, result, negative_ttl if result is None else ttl_seconds)
                except ControlUnavailable:
                    pass
            elif not coordinated:
                self._store(key, result, negative_ttl if result is None else ttl_seconds)
            flight.result = result
            return result
        except BaseException as exc:
            flight.error = exc
            raise
        finally:
            stop_renewal.set()
            if renewal:
                renewal.join(timeout=0.1)
            if token:
                try:
                    self.control.release_rebuild(key, token)
                except ControlUnavailable:
                    pass
            with self._mutex:
                self._flights.pop(key, None)
                flight.ready.set()

    def invalidate(self, key: str):
        try:
            self.client.delete(key)
        except Exception:
            pass
