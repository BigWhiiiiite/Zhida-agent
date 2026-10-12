"""Opt-in real Redis checks; only randomly prefixed test keys are removed."""
import os
import uuid

import pytest

from app.backend_v2.redis_control import ProviderLimits, RedisControl, VersionedCache


@pytest.fixture
def live_control():
    url = os.getenv('ZHIDA_TEST_REDIS_URL')
    if not url:
        pytest.skip('Set ZHIDA_TEST_REDIS_URL for real Redis integration')
    import redis
    client = redis.Redis.from_url(url, decode_responses=True, socket_timeout=2)
    client.ping()
    prefix = 'zhida:test:' + uuid.uuid4().hex
    control = RedisControl(client, prefix=prefix)
    try:
        yield client, control, prefix
    finally:
        keys = list(client.scan_iter(match=prefix + ':*'))
        if keys:
            client.delete(*keys)
        client.close()


def test_real_redis_bucket_and_foreign_unlock(live_control):
    client, control, prefix = live_control
    first = control.rate_limit(user_id='one', route_group='write', capacity=1, refill_per_second=0.01)
    second = control.rate_limit(user_id='one', route_group='write', capacity=1, refill_per_second=0.01)
    assert first.allowed and not second.allowed and second.retry_after_ms > 0
    other = control.rate_limit(user_id='two', route_group='write', capacity=1, refill_per_second=0.01)
    assert other.allowed
    token = control.acquire_rebuild(prefix + ':cache')
    assert token and not control.release_rebuild(prefix + ':cache', 'foreign')
    assert control.acquire_rebuild(prefix + ':cache') is None
    assert control.release_rebuild(prefix + ':cache', token)


def test_real_redis_provider_reservations_unknowns_and_actual_tokens(live_control):
    client, control, prefix = live_control
    limits = ProviderLimits(5, 1000, 1)
    assert control.reserve_provider(account='test', call_id='a', limits=limits, estimated_tokens=800).allowed
    assert not control.reserve_provider(account='test', call_id='a', limits=limits, estimated_tokens=800).allowed
    assert control.mark_provider_sent(account='test', call_id='a')
    assert not control.settle_provider(account='test', call_id='a', state='cancelled_before_send')
    assert control.settle_provider(account='test', call_id='a', state='unknown')
    assert not control.reserve_provider(account='test', call_id='b', limits=limits, estimated_tokens=100).allowed
    # Even expired inflight requests remain quarantined until an actual outcome is reconciled.
    client.zadd(control.provider_keys('test')[3], {'a': 0})
    result = control.reserve_provider(account='test', call_id='b', limits=limits, estimated_tokens=100)
    assert not result.allowed and result.reason == 'unknown_inflight'
    assert control.settle_provider(account='test', call_id='a', actual_tokens=100)
    assert control.reserve_provider(account='test', call_id='b', limits=limits, estimated_tokens=800).allowed
    assert not control.settle_provider(account='test', call_id='a', actual_tokens=0)


def test_real_redis_negative_cache_and_version_miss(live_control):
    client, control, prefix = live_control
    cache = VersionedCache(client, control, random_value=lambda: 0.5)
    key = prefix + ':cache:rev1'
    assert cache.get_or_load(key, lambda: None) is None
    assert cache.get_or_load(key, lambda: pytest.fail('unexpected origin request')) is None
    assert 0 < client.ttl(key) <= 30
    assert cache.get_or_load(prefix + ':cache:rev2', lambda: {'evidence': 'new'}) == {'evidence': 'new'}
