"""Regression tests for store leadership, pool fallback, and SWIM fail-loud.

Run with: pytest tests/test_store_regressions.py -v
"""
import os
import sys
import types

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest  # noqa: E402

import store  # noqa: E402


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------

class _FakeCursor:
    def __init__(self, lock_result=True, fail_on=None):
        self._lock_result = lock_result
        self._fail_on = fail_on or ()
        self.executed = []

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def execute(self, sql, params=None):
        self.executed.append((sql, params))
        for needle in self._fail_on:
            if needle in sql:
                raise RuntimeError("simulated db failure")

    def fetchone(self):
        return [self._lock_result]


class _FakeConn:
    def __init__(self, lock_result=True, fail_on=None):
        self._lock_result = lock_result
        self._fail_on = fail_on or ()
        self.closed = False
        self.cursor_obj = _FakeCursor(lock_result, fail_on)

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def cursor(self):
        return self.cursor_obj

    def close(self):
        self.closed = True

    def commit(self):
        pass


class _FakePsycopg:
    """Stands in for the psycopg module; records connect kwargs."""

    def __init__(self, lock_result=True, fail_on=None, connect_exc=None):
        self._lock_result = lock_result
        self._fail_on = fail_on or ()
        self._connect_exc = connect_exc
        self.connect_kwargs = []
        self.conns = []

    def connect(self, dsn, **kwargs):
        self.connect_kwargs.append(kwargs)
        if self._connect_exc:
            raise self._connect_exc
        conn = _FakeConn(self._lock_result, self._fail_on)
        self.conns.append(conn)
        return conn


@pytest.fixture
def pg(monkeypatch):
    """Make store believe Postgres is configured, with a fake driver."""
    fake = _FakePsycopg()
    monkeypatch.setattr(store, "DATABASE_URL", "postgresql://fake/db")
    monkeypatch.setattr(store, "_psycopg", fake)
    monkeypatch.setattr(store, "_get_pool", lambda: None)
    monkeypatch.setattr(store, "_leader_conn", None)
    monkeypatch.setattr(store, "_leader_fh", None)
    return fake


# ---------------------------------------------------------------------------
# Leadership: heartbeat / re-election
# ---------------------------------------------------------------------------

def test_acquire_leadership_wins_lock(pg):
    assert store.acquire_leadership() is True
    assert store._leader_conn is not None
    # The winner holds the session open to keep the lock.
    assert store._leader_conn.closed is False


def test_acquire_leadership_loses_lock_no_split_brain(pg):
    pg._lock_result = False
    assert store.acquire_leadership() is False
    # Loser must close its connection and must NOT hold any claim.
    assert store._leader_conn is None
    assert all(c.closed for c in pg.conns)


def test_acquire_leadership_db_error_is_not_leadership(pg):
    pg._connect_exc = RuntimeError("connection refused")
    # A Postgres ERROR is "not leader this round" — never permission to
    # fall back to the per-container file lock (that would double-bill).
    assert store.acquire_leadership() is False
    assert store._leader_conn is None
    assert store._leader_fh is None


def test_validate_leadership_heartbeat_ok(pg):
    store._leader_conn = _FakeConn(lock_result=True)
    assert store.validate_leadership() is True
    # Claim retained.
    assert store._leader_conn is not None


def test_validate_leadership_dead_session_releases_claim(pg):
    """A silently dropped lock must release the claim so another worker can
    take over — never keep polling as a phantom leader."""
    store._leader_conn = _FakeConn(
        lock_result=True, fail_on=("pg_try_advisory_lock",))
    assert store.validate_leadership() is False
    assert store._leader_conn is None


def test_validate_leadership_without_claim_is_false(pg):
    store._leader_conn = None
    assert store.validate_leadership() is False


# ---------------------------------------------------------------------------
# Postgres timeouts and pool fallback
# ---------------------------------------------------------------------------

def test_connect_passes_connect_timeout(pg):
    with store._connect(connect_timeout=5) as conn:
        pass
    assert pg.connect_kwargs and pg.connect_kwargs[0].get("connect_timeout") == 5


def test_pool_unavailable_falls_back_to_one_off_connect(monkeypatch):
    """psycopg_pool missing (or pool never opened) must degrade to a direct
    connect, not crash."""
    monkeypatch.setattr(store, "_ConnectionPool", None)
    monkeypatch.setattr(store, "_pool", None)
    monkeypatch.setattr(store, "_pool_retry_at", 0.0)
    fake = _FakePsycopg()
    monkeypatch.setattr(store, "_psycopg", fake)
    monkeypatch.setattr(store, "DATABASE_URL", "postgresql://fake/db")
    assert store._get_pool() is None
    with store._connect(connect_timeout=5):
        pass
    assert fake.connect_kwargs[0].get("connect_timeout") == 5


# ---------------------------------------------------------------------------
# Postgres write fallbacks stay bounded and loud
# ---------------------------------------------------------------------------

def test_cache_edct_pg_failure_falls_back_to_bounded_memory(pg, monkeypatch, capsys):
    pg._fail_on = ("INSERT INTO edct_cache",)
    edct = {"edct": "2026-09-12T14:00:00Z", "flight": "DAL1"}
    store.cache_edct("DAL1", "2026-09-12", edct)
    # Loud: the failure is surfaced on stderr, not swallowed.
    assert "cache_edct" in capsys.readouterr().err
    # The entry landed in the bounded memory fallback...
    key = store._edct_key("DAL1", "2026-09-12")
    assert store._edct_mem[key]["payload"]["edct"] == edct["edct"]
    # ...where the memory-mode read path finds it.
    monkeypatch.setattr(store, "DATABASE_URL", "")
    got = store.get_cached_edct("DAL1", "2026-09-12")
    assert got is not None and got["payload"]["edct"] == edct["edct"]


def test_mem_fallback_is_bounded():
    bucket = {}
    with store._mem_lock:
        for i in range(store._MEM_FALLBACK_MAX + 50):
            bucket[f"k{i}"] = {"updated_at": f"2026-09-12T00:{i % 60:02d}:00Z"}
            store._evict_mem_overflow(bucket)
    assert len(bucket) <= store._MEM_FALLBACK_MAX


# ---------------------------------------------------------------------------
# SWIM event-store read failures: 503, never a competing consumer
# ---------------------------------------------------------------------------

def _swim_env(monkeypatch):
    monkeypatch.setenv("SWIM_PASSWORD", "x")


def test_swim_event_read_failure_is_503_not_subprocess(monkeypatch):
    from pft import swim_serve
    from pft.runner import _execute
    _swim_env(monkeypatch)
    monkeypatch.setattr(
        store, "swim_daemon_health",
        lambda q: {"alive": True, "db_error": None})
    def boom(*a, **k):
        raise RuntimeError("db gone")
    monkeypatch.setattr(store, "swim_recent_events", boom)
    monkeypatch.setattr(swim_serve, "store", store)
    data, status = _execute("swim_consumer.py", ["tbfm", "--airport", "KJFK"],
                            20)
    assert status == 503
    assert "retry_after_seconds" in data


def test_swim_health_read_failure_is_503(monkeypatch):
    from pft import swim_serve
    _swim_env(monkeypatch)
    def boom(q):
        raise RuntimeError("db gone")
    monkeypatch.setattr(store, "swim_daemon_health", boom)
    monkeypatch.setattr(swim_serve, "store", store)
    with pytest.raises(swim_serve._SwimStoreUnavailable):
        swim_serve._swim_daemon_serve(["tbfm", "--airport", "KJFK"])


def test_swim_dead_daemon_still_falls_back_to_subprocess(monkeypatch):
    """Daemon confirmed dead (not merely unreadable) keeps the old per-request
    consumer path — the outage is the daemon, not the store."""
    from pft import swim_serve
    _swim_env(monkeypatch)
    monkeypatch.setattr(
        store, "swim_daemon_health",
        lambda q: {"alive": False, "db_error": None})
    monkeypatch.setattr(swim_serve, "store", store)
    assert swim_serve._swim_daemon_serve(["tbfm", "--airport", "KJFK"]) is None


def test_swim_unknown_feed_not_daemon_served(monkeypatch):
    from pft import swim_serve
    _swim_env(monkeypatch)
    monkeypatch.setattr(swim_serve, "store", store)
    assert swim_serve._swim_daemon_serve(["nope-feed"]) is None


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
