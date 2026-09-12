"""Rate limiting: shared Postgres counter when available, honest scope."""
import os
import sys
from contextlib import contextmanager
from datetime import timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import app as app_module  # noqa: E402
import store as store_module  # noqa: E402


def _reset_mem():
    with app_module._rate_lock:
        app_module._rate_buckets.clear()


def test_mem_bucket_allows_limit_then_blocks():
    _reset_mem()
    key = "test-client-1"
    for _ in range(app_module.RATE_LIMIT_PER_MIN):
        assert app_module._rate_limited_mem(key) == 0
    assert app_module._rate_limited_mem(key) > 0


def test_mem_buckets_are_per_key():
    _reset_mem()
    for _ in range(app_module.RATE_LIMIT_PER_MIN):
        app_module._rate_limited_mem("client-a")
    assert app_module._rate_limited_mem("client-b") == 0


def test_scope_is_per_worker_without_postgres(monkeypatch):
    monkeypatch.setattr(app_module.store, "using_postgres", lambda: False)
    _reset_mem()
    assert app_module._rate_limit_scope() == "per-worker"
    assert app_module._rate_limited("some-key") == 0


class _FakeCursor:
    """Emulates the rate_limit_check upsert semantics in Python."""

    def __init__(self, buckets):
        self.buckets = buckets
        self._row = None

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def execute(self, sql, params=()):
        if "INSERT INTO rate_limit_counters" in sql:
            key, now, cutoff = params[0], params[1], params[2]
            window_start, count = self.buckets.get(key, (None, 0))
            if window_start is None or window_start < cutoff:
                window_start, count = now, 1
            else:
                count += 1
            self.buckets[key] = (window_start, count)
            self._row = (count, window_start)
        # CREATE TABLE / DELETE prune: no-ops for the fake.

    def fetchone(self):
        return self._row


class _FakeConn:
    def __init__(self, buckets):
        self.buckets = buckets

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def cursor(self):
        return _FakeCursor(self.buckets)

    def commit(self):
        pass


def _patch_connect(monkeypatch, buckets):
    @contextmanager
    def fake_connect(connect_timeout=None):
        assert connect_timeout == 5
        yield _FakeConn(buckets)
    monkeypatch.setattr(store_module, "_connect", fake_connect)


def test_shared_counter_allows_limit_then_blocks(monkeypatch):
    buckets = {}
    _patch_connect(monkeypatch, buckets)
    key = "shared-client"
    for _ in range(60):
        assert store_module.rate_limit_check(key, 60) == 0
    wait = store_module.rate_limit_check(key, 60)
    assert wait > 0


def test_shared_counter_window_resets(monkeypatch):
    buckets = {}
    _patch_connect(monkeypatch, buckets)
    key = "window-client"
    old = store_module._now() - timedelta(seconds=120)
    buckets[key] = (old, 60)  # stale window, already at limit
    assert store_module.rate_limit_check(key, 60) == 0
    assert buckets[key][1] == 1  # count restarted


def test_shared_counter_failure_falls_back_to_mem(monkeypatch):
    def boom(connect_timeout=None):
        raise OSError("db down")
    monkeypatch.setattr(store_module, "_connect", boom)
    monkeypatch.setattr(app_module.store, "using_postgres", lambda: True)
    _reset_mem()
    # Must not raise; degrades to per-process and says so.
    assert app_module._rate_limited("fallback-client") == 0
    assert app_module._rate_limit_scope() == "per-worker"
