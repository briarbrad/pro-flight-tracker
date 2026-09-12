"""Bounded TTL cache for script results."""

import copy
import threading
import time
from datetime import datetime, timezone

from flask import has_request_context, request

import analysis

from pft.validation import _extract_flights

_TTL_BY_CALL = {
    ("aviation_weather.py", "metar"): 300,
    ("aviation_weather.py", "taf"): 1800,
    ("aviation_weather.py", "sigmet"): 1800,
    ("aviation_weather.py", "isigmet"): 1800,
    ("aviation_weather.py", "pirep"): 600,
    ("aviation_weather.py", "faa-status"): 300,
    ("aviation_weather.py", "brief"): 300,
    ("aviation_weather.py", "open-meteo"): 1800,
    ("airport_ops.py", "gairmet"): 1800,
    ("airport_ops.py", "tcf"): 1800,
    ("airport_ops.py", "lightning"): 60,
    ("airport_ops.py", "rvr"): 120,
    ("airport_ops.py", "atfm-infer"): 600,
    ("flight_data.py", "chain"): 600,
    ("flight_data.py", "track"): 30,
    ("swim_consumer.py", "tfms-flow"): 300,
    ("swim_consumer.py", "tfms-flight"): 240,
    ("swim_consumer.py", "tbfm"): 300,
    ("swim_consumer.py", "itws"): 300,
    ("swim_consumer.py", "notams"): 900,
    ("swim_consumer.py", "stdds"): 180,
    ("swim_consumer.py", "sfdps"): 300,
    ("swim_consumer.py", "tfdm"): 240,
}


_CACHE_MAX_ENTRIES = 500


_STALE_MAX_SECONDS = 6 * 3600   # never serve anything older than this


class _TTLCache:
    """Bounded TTL cache: one dict+lock+eviction implementation.

    Shared by the script-result cache and the narrative/chat cache, which
    previously each hand-rolled their own lock/dict/eviction. Internally
    key -> [value, stored_at, ttl]; on overflow, expired entries go first,
    then oldest.
    """

    def __init__(self, max_entries: int):
        self._max = max_entries
        self._lock = threading.Lock()
        self._data = {}

    def put(self, key, value, ttl: int) -> None:
        if ttl <= 0:
            return
        with self._lock:
            self._data[key] = [value, time.monotonic(), ttl]
            self._evict_locked()

    def get(self, key):
        """Value regardless of expiry (caller decides fresh vs stale)."""
        with self._lock:
            entry = self._data.get(key)
            return entry[0] if entry else None

    def get_fresh(self, key):
        """Value only when unexpired; expired entries are dropped."""
        with self._lock:
            entry = self._data.get(key)
            if not entry:
                return None
            value, stored_at, ttl = entry
            if time.monotonic() - stored_at >= ttl:
                del self._data[key]
                return None
            return value

    def get_with_age(self, key):
        """(value, age_seconds, ttl_seconds) regardless of expiry."""
        with self._lock:
            entry = self._data.get(key)
            if not entry:
                return None
            value, stored_at, ttl = entry
            return value, time.monotonic() - stored_at, ttl

    def clear(self) -> None:
        with self._lock:
            self._data.clear()

    def delete(self, key) -> None:
        with self._lock:
            self._data.pop(key, None)

    def __len__(self) -> int:
        with self._lock:
            return len(self._data)

    def _evict_locked(self) -> None:
        if len(self._data) <= self._max:
            return
        now = time.monotonic()
        expired = [k for k, (_, stored_at, ttl) in self._data.items()
                   if now - stored_at >= ttl]
        for k in expired:
            del self._data[k]
        while len(self._data) > self._max:
            oldest = min(self._data, key=lambda k: self._data[k][1])
            del self._data[oldest]


_script_cache = _TTLCache(max_entries=_CACHE_MAX_ENTRIES)


def _cache_key(script: str, args: list) -> tuple:
    parts = [str(a) for a in args]
    if not parts:
        return (script,)
    head, rest = parts[0], parts[1:]
    # Normalize: --flag value pairs sort canonically, so reordered
    # equivalent flags don't create duplicate cache entries. Positional
    # args stay order-sensitive.
    flags: list[tuple[str, str]] = []
    positionals: list[str] = []
    i = 0
    while i < len(rest):
        tok = rest[i]
        if (tok.startswith("--") and i + 1 < len(rest)
                and not rest[i + 1].startswith("--")):
            flags.append((tok, rest[i + 1]))
            i += 2
        else:
            positionals.append(tok)
            i += 1
    flags.sort()
    return (script, head, tuple(positionals), tuple(flags))


def _ttl_for(script: str, args: list, data: dict) -> int:
    sub = str(args[0]) if args else ""
    if script == "flight_data.py" and sub == "status":
        # Phase-derived: an airborne flight's status holds for its refresh
        # interval; a finished flight never changes again.
        try:
            flights = _extract_flights(data)
            if flights:
                now = datetime.now(timezone.utc)
                phase = analysis.compute_phase(flights[0], now)
                horizon = analysis.compute_horizon(flights[0], now, phase)
                interval = analysis.refresh_interval(phase, horizon)
                if interval is None:
                    return 3600  # terminal — nothing further will change
                return max(60, min(int(interval), 900))
        except Exception:
            pass
        return 120
    return _TTL_BY_CALL.get((script, sub), 0)


def _cache_get(key: tuple):
    got = _script_cache.get_with_age(key)
    if not got:
        return None
    value, age, ttl = got
    # No deepcopy on the way out: _cache_put takes the copy on the way in,
    # and nothing downstream mutates a returned entry in place (_annotated
    # copies before stamping; handlers only read). Callers must not mutate
    # the returned dict.
    return {"data": value["data"], "status": value["status"],
            "stored_at": time.monotonic() - age, "ttl": ttl}


def _cache_put(key: tuple, data: dict, status: int, ttl: int) -> None:
    if ttl <= 0 or status != 200 or not isinstance(data, dict):
        return
    _script_cache.put(key, {"data": copy.deepcopy(data), "status": status},
                      ttl)


def _annotated(entry: dict, stale: bool = False, reason: str = None) -> tuple[dict, int]:
    # Shallow-copy before stamping: entry["data"] may be the cache's own
    # object (no deepcopy on get), and the "cache" note must not leak into
    # the stored copy. One level is enough — only a top-level key is added.
    data = dict(entry["data"])
    note = {"hit": True, "age_seconds": int(time.monotonic() - entry["stored_at"])}
    if stale:
        note["stale"] = True
        note["reason"] = reason or "served stale"
    data["cache"] = note
    return data, entry["status"]


def _nocache_requested() -> bool:
    # Valueless ?nocache counts: presence is the flag, "=1" is optional.
    return has_request_context() and "nocache" in request.args
