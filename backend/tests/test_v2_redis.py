"""Pure cache boundary tests; Lua behavior is covered by opt-in real Redis tests."""
import threading
from concurrent.futures import ThreadPoolExecutor

import pytest

from app.backend_v2.redis_control import (CacheBusy, ControlUnavailable, ProviderLimits,
                                         RedisControl, VersionedCache, private_key)


class MemoryCache:
    def __init__(self):
        self.values, self.expiries = {}, {}

    def get(self, key):
        return self.values.get(key)

    def set(self, key, value, **kwargs):
        self.values[key], self.expiries[key] = value, kwargs.get('ex')
        return True

    def delete(self, key):
        self.values.pop(key, None)


class LocalLocks:
    def __init__(self):
        self.tokens = {}

    def acquire_rebuild(self, key):
        if key in self.tokens:
            return None
        self.tokens[key] = 'owner'
        return 'owner'

    def renew_rebuild(self, key, token):
        return self.tokens.get(key) == token

    def release_rebuild(self, key, token):
        if self.tokens.get(key) == token:
            del self.tokens[key]
            return True
        return False


class UnavailableControl(LocalLocks):
    def acquire_rebuild(self, key):
        raise ControlUnavailable('not connected')


def test_private_keys_isolate_user_source_revision_and_query_without_plaintext():
    key = private_key('test', user_id='alice', revision=1, query='private question')
    assert 'private question' not in key and 'alice' not in key
    assert key != private_key('test', user_id='bob', revision=1, query='private question')
    assert key != private_key('test', user_id='alice', revision=2, query='private question')
    with pytest.raises(ValueError):
        private_key('test', user_id='', revision=1)


def test_cache_reuses_result_and_versions_do_not_reuse_old_value():
    client = MemoryCache()
    cache = VersionedCache(client, LocalLocks(), random_value=lambda: 0.5)
    assert cache.get_or_load('user:rev1', lambda: {'value': 'old'}) == {'value': 'old'}
    assert cache.get_or_load('user:rev1', lambda: pytest.fail('unexpected reload')) == {'value': 'old'}
    assert cache.get_or_load('user:rev2', lambda: {'value': 'new'}) == {'value': 'new'}
    assert client.expiries['user:rev1'] == 300


def test_negative_cache_only_caches_authoritative_none_not_exceptions():
    client = MemoryCache()
    cache = VersionedCache(client, LocalLocks(), random_value=lambda: 0.5)
    assert cache.get_or_load('missing', lambda: None) is None
    assert client.expiries['missing'] == 30
    assert cache.get_or_load('missing', lambda: pytest.fail('negative cache missed')) is None

    def failure():
        raise RuntimeError('index not ready')
    with pytest.raises(RuntimeError):
        cache.get_or_load('pending', failure)
    assert 'pending' not in client.values
    assert cache.get_or_load('pending', lambda: 'ready') == 'ready'


def test_singleflight_calls_loader_once_for_concurrent_requests():
    cache = VersionedCache(MemoryCache(), LocalLocks(), max_wait_seconds=2)
    started, release = threading.Event(), threading.Event()
    calls = []

    def load():
        calls.append(1)
        started.set()
        assert release.wait(2)
        return 'one'

    with ThreadPoolExecutor(max_workers=4) as pool:
        first = pool.submit(cache.get_or_load, 'same', load)
        assert started.wait(1)
        rest = [pool.submit(cache.get_or_load, 'same', load) for _ in range(3)]
        release.set()
        assert [future.result() for future in [first, *rest]] == ['one'] * 4
    assert calls == [1]


def test_control_outage_has_bounded_database_fallback():
    cache = VersionedCache(MemoryCache(), UnavailableControl(), max_fallback=1)
    started, release = threading.Event(), threading.Event()

    def slow():
        started.set()
        assert release.wait(2)
        return 'done'

    with ThreadPoolExecutor(max_workers=1) as pool:
        first = pool.submit(cache.get_or_load, 'first', slow)
        assert started.wait(1)
        with pytest.raises(CacheBusy):
            cache.get_or_load('second', lambda: pytest.fail('unbounded fallback'))
        release.set()
        assert first.result() == 'done'


def test_another_process_rebuild_does_not_fan_out_to_database():
    locks = LocalLocks()
    locks.acquire_rebuild('key')
    cache = VersionedCache(MemoryCache(), locks, max_wait_seconds=0)
    with pytest.raises(CacheBusy):
        cache.get_or_load('key', lambda: pytest.fail('other owner still holds lock'))


def test_rate_control_failure_fails_closed_and_validates_limits():
    class Broken:
        def eval(self, *args):
            raise OSError('disconnected')
    control = RedisControl(Broken())
    with pytest.raises(ControlUnavailable):
        control.rate_limit(user_id='one', route_group='write', capacity=2, refill_per_second=1)
    with pytest.raises(ValueError):
        ProviderLimits(0, 100, 1)
    with pytest.raises(ValueError):
        control.rate_limit(user_id='one', route_group='write', capacity=1, refill_per_second=float('nan'))
    keys = control.provider_keys('secret-account-name')
    assert len({key.split('{')[1].split('}')[0] for key in keys}) == 1
    assert all('secret-account-name' not in key for key in keys)


def test_rebuild_owner_is_not_part_of_lock_key():
    class FakeClient:
        def __init__(self):
            self.keys = []
        def set(self, key, value, **kwargs):
            self.keys.append((key, value))
            return len(self.keys) == 1
    client = FakeClient()
    control = RedisControl(client)
    assert control.acquire_rebuild('same')
    assert control.acquire_rebuild('same') is None
    assert client.keys[0][0] == client.keys[1][0]
    assert client.keys[0][1] != client.keys[1][1]
