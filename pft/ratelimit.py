"""Per-minute rate cap: shared Postgres counter when available."""

import os
import threading
import time

from flask import request

import store

from pft.logging import log

RATE_LIMIT_PER_MIN = max(1, int(os.environ.get("RATE_LIMIT_PER_MIN", "60") or 60))


_rate_lock = threading.Lock()


_rate_buckets: dict[str, tuple[float, int]] = {}  # key -> (window_start, count)


def _client_key() -> str:
    """Rate-limit key: the bearer token if presented, else the client IP."""
    auth = request.headers.get("Authorization", "")
    if auth.startswith("Bearer ") and len(auth) > 7:
        return "tok:" + auth[7:15]  # prefix is plenty for bucketing
    fwd = request.headers.get("X-Forwarded-For", "")
    if fwd:
        return "ip:" + fwd.split(",")[0].strip()
    return "ip:" + (request.remote_addr or "unknown")


def _rate_limited_mem(key: str) -> int:
    """Per-process bucket. Used when Postgres is unavailable — the 429 body
    then says scope=per-worker instead of pretending the limit is shared."""
    now = time.monotonic()
    with _rate_lock:
        start, count = _rate_buckets.get(key, (now, 0))
        if now - start >= 60.0:
            start, count = now, 0
        count += 1
        _rate_buckets[key] = (start, count)
        # Opportunistic prune so the dict can't grow unbounded under an
        # address-rotating scanner.
        if len(_rate_buckets) > 1000:
            cutoff = now - 120.0
            for k in [k for k, (s, _) in _rate_buckets.items() if s < cutoff]:
                del _rate_buckets[k]
    if count > RATE_LIMIT_PER_MIN:
        return max(1, int(60.0 - (now - start)) + 1)
    return 0


_rate_shared_ok = True  # False while the shared counter is erroring


def _rate_limited(key: str) -> int:
    """Count a request against key's window. Returns seconds to wait, 0 = ok.

    Postgres-backed (shared across workers/replicas) when DATABASE_URL is
    set; per-process otherwise. A shared-counter failure degrades to
    per-process loudly rather than 500ing the request.
    """
    global _rate_shared_ok
    if store.using_postgres():
        try:
            retry = store.rate_limit_check(key, RATE_LIMIT_PER_MIN)
            _rate_shared_ok = True
            return retry
        except Exception as exc:
            _rate_shared_ok = False
            log(f"[RATE] Shared counter failed "
                f"({type(exc).__name__}: {exc}); per-process fallback")
    return _rate_limited_mem(key)


def _rate_limit_scope() -> str:
    """'shared' when the limit is actually enforced cluster-wide."""
    if store.using_postgres() and _rate_shared_ok:
        return "shared"
    return "per-worker"
