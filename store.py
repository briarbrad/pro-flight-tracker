#!/usr/bin/env python3
"""
Tracking store for Pro Flight Tracker.

Holds the set of flights being watched by the background tracker.

Two backends:
  - Postgres, when DATABASE_URL is set (Railway sets this automatically once
    you add a Postgres service to the project). State survives redeploys and
    is shared across gunicorn workers and replicas.
  - In-memory, otherwise. Fine for local development; state is per-process and
    dies with the process.

Also provides single-leader election, so exactly one gunicorn worker runs the
background tracker. Without this, `--workers 2` means two tracker threads
polling the same flights and billing AeroAPI twice for identical data.
"""

import fcntl
import os
import threading
from datetime import datetime, timezone, timedelta

DATABASE_URL = os.environ.get("DATABASE_URL", "").strip()

# Postgres advisory lock key for tracker leadership. Arbitrary but must be
# stable across workers.
_LEADER_LOCK_KEY = 8117320
_LEADER_LOCK_FILE = "/tmp/pft-tracker.lock"

# How long a tracked flight lives if nothing else removes it. Covers a flight
# scheduled late in the day plus overnight delay, without polling forever.
DEFAULT_TTL_HOURS = 36

_psycopg = None
_ConnectionPool = None
if DATABASE_URL:
    try:
        import psycopg as _psycopg  # noqa: F401
    except ImportError:
        _psycopg = None
    try:
        from psycopg_pool import ConnectionPool as _ConnectionPool  # noqa: F401
    except ImportError:
        _ConnectionPool = None  # falls back to one connection per call below

_pool = None
_pool_lock = threading.Lock()


def using_postgres() -> bool:
    return bool(DATABASE_URL and _psycopg is not None)


def _get_pool():
    """Lazily create the shared connection pool.

    Every store function used to open (and TCP/TLS-handshake, then close) a
    brand new Postgres connection on every single call — including from the
    tracker loop's tight per-flight iteration and every request handler.
    Under any real concurrency that's the classic way to exhaust Postgres's
    own max_connections long before the app's own load limit. A pool keeps a
    small set of warm connections and hands them out/back, so a normal
    request pays a Python-level checkout instead of a fresh network
    round-trip and auth handshake.
    """
    global _pool
    if _pool is not None or not using_postgres() or _ConnectionPool is None:
        return _pool
    with _pool_lock:
        if _pool is None:
            _pool = _ConnectionPool(
                DATABASE_URL,
                min_size=1,
                max_size=int(os.environ.get("DB_POOL_MAX_SIZE", "10")),
                timeout=10,       # seconds to wait for a free connection
                max_idle=300,     # recycle idle connections after 5 min
                open=True,
            )
    return _pool


def _connect(connect_timeout: int = None):
    """Return something usable as `with _connect() as conn:`.

    Prefers a pooled connection; falls back to a direct one-off connect if
    psycopg_pool isn't installed or the pool hasn't come up yet, so this is
    a strict improvement with no new hard dependency at import time.
    """
    pool = _get_pool()
    if pool is not None:
        return pool.connection()
    kwargs = {"connect_timeout": connect_timeout} if connect_timeout else {}
    return _psycopg.connect(DATABASE_URL, **kwargs)


def backend_name() -> str:
    if using_postgres():
        return "postgres"
    if DATABASE_URL and _psycopg is None:
        return "memory (DATABASE_URL set but psycopg not installed)"
    return "memory"


def health_check() -> dict:
    """Cheap store liveness probe for /health. Never raises.

    Railway's healthcheck only sees /health, and /health never used to touch
    the store — so a dead DATABASE_URL passed the healthcheck while tracking
    silently degraded to in-memory. Surface it here instead.
    """
    info = {"backend": backend_name()}
    if not using_postgres():
        # In-memory (or psycopg missing): nothing to probe, but make the
        # degraded mode visible when DATABASE_URL was set and unusable.
        info["ok"] = not (DATABASE_URL and _psycopg is None)
        return info
    try:
        with _connect(connect_timeout=3) as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT 1")
                cur.fetchone()
        info["ok"] = True
    except Exception as exc:
        info["ok"] = False
        info["error"] = f"{type(exc).__name__}: {exc}"[:200]
    return info


# ---------------------------------------------------------------------------
# In-memory backend
# ---------------------------------------------------------------------------

_mem: dict[str, dict] = {}
_mem_lock = threading.Lock()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(dt) -> str | None:
    if dt is None:
        return None
    if isinstance(dt, str):
        return dt
    return dt.isoformat()


def _parse(dt) -> datetime | None:
    if dt is None:
        return None
    if isinstance(dt, datetime):
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    try:
        parsed = datetime.fromisoformat(dt)
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
    except (TypeError, ValueError):
        return None


def _row_to_record(row) -> dict:
    return {
        "track_id": row[0],
        "flight": row[1],
        "date": row[2],
        "push_token": row[3],
        "interval_minutes": row[4],
        "last_check": _iso(row[5]),
        "last_risk": row[6],
        "created_at": _iso(row[7]),
        "expires_at": _iso(row[8]),
    }


SCHEMA = """
CREATE TABLE IF NOT EXISTS tracked_flights (
    track_id         TEXT PRIMARY KEY,
    flight           TEXT NOT NULL,
    flight_date      TEXT NOT NULL,
    push_token       TEXT,
    interval_minutes INTEGER NOT NULL DEFAULT 15,
    last_check       TIMESTAMPTZ,
    last_risk        TEXT,
    created_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
    expires_at       TIMESTAMPTZ
);
"""

EDCT_SCHEMA = """
CREATE TABLE IF NOT EXISTS edct_cache (
    flight       TEXT NOT NULL,
    flight_date  TEXT NOT NULL,
    payload      TEXT NOT NULL,
    updated_at   TIMESTAMPTZ NOT NULL,
    PRIMARY KEY (flight, flight_date)
);
"""

# Turn-time/equipment-chain findings (see cache_turn_analysis below) reuse
# the exact same shape as EDCT caching, so the schema is identical bar the
# table name.
TURN_ANALYSIS_SCHEMA = """
CREATE TABLE IF NOT EXISTS turn_analysis_cache (
    flight       TEXT NOT NULL,
    flight_date  TEXT NOT NULL,
    payload      TEXT NOT NULL,
    updated_at   TIMESTAMPTZ NOT NULL,
    PRIMARY KEY (flight, flight_date)
);
"""

SWIM_SCHEMA = """
CREATE TABLE IF NOT EXISTS swim_events (
    id           BIGSERIAL PRIMARY KEY,
    feed         TEXT NOT NULL,
    flight       TEXT,
    airport      TEXT,
    payload      TEXT NOT NULL,
    source_ts    TEXT,
    received_at  TIMESTAMPTZ NOT NULL
);
CREATE INDEX IF NOT EXISTS swim_events_feed_time
    ON swim_events (feed, received_at DESC);
CREATE TABLE IF NOT EXISTS swim_daemon_status (
    queue_name      TEXT PRIMARY KEY,
    last_alive_at   TIMESTAMPTZ,
    last_message_at TIMESTAMPTZ,
    messages_total  BIGINT NOT NULL DEFAULT 0,
    restarts        INTEGER NOT NULL DEFAULT 0,
    note            TEXT
);
"""

SNAPSHOT_SCHEMA = """
CREATE TABLE IF NOT EXISTS flight_snapshots (
    id            BIGSERIAL PRIMARY KEY,
    track_id      TEXT NOT NULL,
    checked_at    TIMESTAMPTZ NOT NULL,
    predicted_out TEXT,
    predicted_in  TEXT,
    delta_minutes DOUBLE PRECISION,
    risk          TEXT
);
CREATE INDEX IF NOT EXISTS flight_snapshots_track_time
    ON flight_snapshots (track_id, checked_at DESC);
"""

_COLS = ("track_id, flight, flight_date, push_token, interval_minutes, "
         "last_check, last_risk, created_at, expires_at")


def init() -> None:
    """Create the table if needed. Safe to call repeatedly.

    app.py only calls this from the leader (see acquire_leadership()), which
    is the real fix for the race below. This except clause is a second,
    independent safety net in case init() is ever called from somewhere that
    doesn't go through leader election.

    CREATE TABLE IF NOT EXISTS is not actually atomic across two concurrent
    sessions the first time a table is created: both can see "doesn't exist"
    and race to create it, and the loser gets a UniqueViolation on Postgres's
    own internal pg_type bookkeeping (error mentions
    pg_type_typname_nsp_index) rather than a clean "already exists". That
    failure means the table now exists — precisely what IF NOT EXISTS was
    supposed to guarantee — so it's swallowed as a success rather than
    propagated as a startup failure.
    """
    if not using_postgres():
        return
    try:
        with _connect() as conn:
            with conn.cursor() as cur:
                cur.execute(SCHEMA)
                cur.execute(EDCT_SCHEMA)
                cur.execute(SWIM_SCHEMA)
                cur.execute(SNAPSHOT_SCHEMA)
            conn.commit()
    except Exception as exc:
        if "pg_type_typname_nsp_index" in str(exc):
            return  # benign: a concurrent session just finished creating it
        raise


# ---------------------------------------------------------------------------
# CRUD
# ---------------------------------------------------------------------------

def add(track_id: str, flight: str, date: str, push_token: str,
        interval_minutes: int, ttl_hours: int = DEFAULT_TTL_HOURS) -> dict:
    """Insert or update a tracked flight. Returns the stored record."""
    now = _now()
    expires_at = now + timedelta(hours=ttl_hours)

    if using_postgres():
        with _connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO tracked_flights
                        (track_id, flight, flight_date, push_token,
                         interval_minutes, created_at, expires_at)
                    VALUES (%s, %s, %s, %s, %s, %s, %s)
                    ON CONFLICT (track_id) DO UPDATE SET
                        push_token = EXCLUDED.push_token,
                        interval_minutes = EXCLUDED.interval_minutes,
                        expires_at = EXCLUDED.expires_at
                    RETURNING """ + _COLS,
                    (track_id, flight, date, push_token, interval_minutes,
                     now, expires_at),
                )
                row = cur.fetchone()
            conn.commit()
        return _row_to_record(row)

    record = {
        "track_id": track_id,
        "flight": flight,
        "date": date,
        "push_token": push_token,
        "interval_minutes": interval_minutes,
        "last_check": None,
        "last_risk": None,
        "created_at": _iso(now),
        "expires_at": _iso(expires_at),
    }
    with _mem_lock:
        existing = _mem.get(track_id)
        if existing:
            record["last_check"] = existing.get("last_check")
            record["last_risk"] = existing.get("last_risk")
            record["created_at"] = existing.get("created_at")
        _mem[track_id] = record
    return dict(record)


def remove(track_id: str) -> bool:
    """Stop tracking. Returns True if something was removed."""
    if using_postgres():
        with _connect() as conn:
            with conn.cursor() as cur:
                cur.execute("DELETE FROM tracked_flights WHERE track_id = %s",
                            (track_id,))
                deleted = cur.rowcount
            conn.commit()
        return deleted > 0

    with _mem_lock:
        return _mem.pop(track_id, None) is not None


def list_all() -> list[dict]:
    """Every tracked flight, newest first."""
    if using_postgres():
        with _connect() as conn:
            with conn.cursor() as cur:
                cur.execute(f"SELECT {_COLS} FROM tracked_flights "
                            "ORDER BY created_at DESC")
                rows = cur.fetchall()
        return [_row_to_record(r) for r in rows]

    with _mem_lock:
        return sorted(
            (dict(r) for r in _mem.values()),
            key=lambda r: r.get("created_at") or "",
            reverse=True,
        )


def due_for_check(now: datetime = None) -> list[dict]:
    """Flights whose interval has elapsed (never-checked ones always qualify)."""
    now = now or _now()

    if using_postgres():
        with _connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    f"""
                    SELECT {_COLS} FROM tracked_flights
                    WHERE (expires_at IS NULL OR expires_at > %s)
                      AND (last_check IS NULL
                           OR last_check <= %s - (interval_minutes
                                                  * INTERVAL '1 minute'))
                    """,
                    (now, now),
                )
                rows = cur.fetchall()
        return [_row_to_record(r) for r in rows]

    due = []
    with _mem_lock:
        for record in _mem.values():
            expires = _parse(record.get("expires_at"))
            if expires and expires <= now:
                continue
            last = _parse(record.get("last_check"))
            if last is None:
                due.append(dict(record))
                continue
            interval = record.get("interval_minutes") or 15
            if (now - last) >= timedelta(minutes=interval):
                due.append(dict(record))
    return due


def mark_checked(track_id: str, when: datetime, risk: str,
                  interval_minutes: int = None) -> None:
    """Record that a check just ran.

    interval_minutes, when given, replaces the flight's stored check
    interval so the *next* due_for_check() cadence reflects how urgent the
    flight actually is right now (e.g. tighten near boarding/taxi, loosen
    while it's still hours from departure) instead of staying pinned to
    whatever interval the client happened to request at track-creation time.
    """
    if using_postgres():
        with _connect() as conn:
            with conn.cursor() as cur:
                if interval_minutes is not None:
                    cur.execute(
                        "UPDATE tracked_flights SET last_check = %s, "
                        "last_risk = %s, interval_minutes = %s "
                        "WHERE track_id = %s",
                        (when, risk, interval_minutes, track_id),
                    )
                else:
                    cur.execute(
                        "UPDATE tracked_flights SET last_check = %s, "
                        "last_risk = %s WHERE track_id = %s",
                        (when, risk, track_id),
                    )
            conn.commit()
        return

    with _mem_lock:
        if track_id in _mem:
            _mem[track_id]["last_check"] = _iso(when)
            _mem[track_id]["last_risk"] = risk
            if interval_minutes is not None:
                _mem[track_id]["interval_minutes"] = interval_minutes


def purge_expired(now: datetime = None) -> int:
    """Drop flights past their TTL. Returns how many were removed.

    Also prunes delay-trend snapshots past the same tracking window, so the
    history table can never outgrow the set of flights anyone is tracking.
    """
    now = now or _now()
    snapshot_cutoff = now - timedelta(hours=SNAPSHOT_RETENTION_HOURS)

    if using_postgres():
        with _connect() as conn:
            with conn.cursor() as cur:
                cur.execute("DELETE FROM tracked_flights "
                            "WHERE expires_at IS NOT NULL AND expires_at <= %s",
                            (now,))
                deleted = cur.rowcount
                try:
                    cur.execute("DELETE FROM flight_snapshots "
                                "WHERE checked_at <= %s", (snapshot_cutoff,))
                except Exception:
                    pass  # table may not exist yet on a fresh database
            conn.commit()
        return deleted

    with _mem_lock:
        stale = [tid for tid, r in _mem.items()
                 if (_parse(r.get("expires_at")) or now + timedelta(days=1)) <= now]
        for tid in stale:
            del _mem[tid]
        for tid in list(_snap_mem):
            rows = [r for r in _snap_mem[tid]
                    if (_parse(r.get("checked_at")) or now) > snapshot_cutoff]
            if rows:
                _snap_mem[tid] = rows
            else:
                del _snap_mem[tid]
        return len(stale)


# ---------------------------------------------------------------------------
# Delay-trend snapshots
#
# One row per scheduled tracker check: the predicted out/in times and the
# delay-vs-schedule delta at that moment. tracked_flights only ever kept the
# LAST risk tier, so "is this delay growing, shrinking, or holding steady"
# was silently discarded on every poll. This table makes that trend durable.
# Written on the tracker's existing cadence — recording it costs zero extra
# AeroAPI queries. Pruned to the flight's own tracking window (TTL), so it
# is a rolling history, not an archive.
# ---------------------------------------------------------------------------

SNAPSHOT_RETENTION_HOURS = DEFAULT_TTL_HOURS

_snap_mem: dict[str, list] = {}


def record_snapshot(track_id: str, checked_at: datetime,
                    predicted_out: str = None, predicted_in: str = None,
                    delta_minutes: float = None, risk: str = None) -> None:
    """Append one check's outcome to the flight's delay history.

    Best-effort: a failure here must never break the tracker loop. Prunes
    this track's rows past the retention window on every write, so growth
    is bounded even if purge_expired never runs.
    """
    if not track_id:
        return
    checked_at = checked_at or _now()
    cutoff = checked_at - timedelta(hours=SNAPSHOT_RETENTION_HOURS)
    if using_postgres():
        try:
            with _connect(connect_timeout=5) as conn:
                with conn.cursor() as cur:
                    cur.execute(SNAPSHOT_SCHEMA)  # lazy-create: non-leader workers
                    cur.execute(
                        "INSERT INTO flight_snapshots "
                        "(track_id, checked_at, predicted_out, predicted_in, "
                        " delta_minutes, risk) VALUES (%s, %s, %s, %s, %s, %s)",
                        (track_id, checked_at, predicted_out, predicted_in,
                         delta_minutes, risk))
                    cur.execute(
                        "DELETE FROM flight_snapshots "
                        "WHERE track_id = %s AND checked_at < %s",
                        (track_id, cutoff))
                conn.commit()
            return
        except Exception:
            pass  # fall through to memory so at least this worker remembers
    with _mem_lock:
        rows = _snap_mem.setdefault(track_id, [])
        rows.append({
            "track_id": track_id,
            "checked_at": _iso(checked_at),
            "predicted_out": predicted_out,
            "predicted_in": predicted_in,
            "delta_minutes": delta_minutes,
            "risk": risk,
        })
        _snap_mem[track_id] = [
            r for r in rows
            if (_parse(r.get("checked_at")) or checked_at) >= cutoff
        ][-500:]


def recent_snapshots(track_id: str, limit: int = 12) -> list[dict]:
    """The last `limit` snapshots for a track, oldest first.

    Oldest-first so callers (and the client) read the series left-to-right
    as it happened: +2 → +4 → +6. Returns [] on any failure — trend data
    is an enhancement, never worth failing a response over.
    """
    if not track_id:
        return []
    if using_postgres():
        try:
            with _connect(connect_timeout=5) as conn:
                with conn.cursor() as cur:
                    cur.execute(
                        "SELECT track_id, checked_at, predicted_out, "
                        "predicted_in, delta_minutes, risk "
                        "FROM flight_snapshots WHERE track_id = %s "
                        "ORDER BY checked_at DESC LIMIT %s",
                        (track_id, limit))
                    rows = cur.fetchall()
            return [{
                "track_id": r[0],
                "checked_at": _iso(r[1]),
                "predicted_out": r[2],
                "predicted_in": r[3],
                "delta_minutes": r[4],
                "risk": r[5],
            } for r in reversed(rows)]
        except Exception:
            return []
    with _mem_lock:
        return [dict(r) for r in _snap_mem.get(track_id, [])[-limit:]]


# ---------------------------------------------------------------------------
# EDCT cache
#
# EDCTs (FAA-assigned wheels-up slots) are discovered by the expensive
# /api/brief SWIM lookup. The cheap /api/flight/live endpoint can't afford
# that lookup on every refresh, but a slot assigned 20 minutes ago is still
# the controlling fact — so the brief caches what it finds here and /live
# re-attaches it with ?edct=cached. Entries go stale fast (traffic management
# revises slots), hence the short TTL.
# ---------------------------------------------------------------------------

EDCT_TTL_MINUTES = 45

_edct_mem: dict[tuple, dict] = {}


def _edct_key(flight: str, date: str) -> tuple:
    return ((flight or "").upper(), date or "")


def cache_edct(flight: str, date: str, edct: dict) -> None:
    """Persist an EDCT found by the brief. Empty/None edct is ignored.

    Best-effort: a failure here must never break the brief response.
    """
    if not edct or not edct.get("edct"):
        return
    now = _now()
    if using_postgres():
        try:
            import json as _json
            with _connect(connect_timeout=5) as conn:
                with conn.cursor() as cur:
                    cur.execute(EDCT_SCHEMA)  # lazy-create: non-leader workers
                    cur.execute(
                        "INSERT INTO edct_cache (flight, flight_date, payload, updated_at) "
                        "VALUES (%s, %s, %s, %s) "
                        "ON CONFLICT (flight, flight_date) DO UPDATE "
                        "SET payload = EXCLUDED.payload, updated_at = EXCLUDED.updated_at",
                        (_edct_key(flight, date)[0], date, _json.dumps(edct), now))
                conn.commit()
            return
        except Exception:
            pass  # fall through to memory so at least this worker remembers
    with _mem_lock:
        _edct_mem[_edct_key(flight, date)] = {"payload": dict(edct),
                                              "updated_at": now}


def get_cached_edct(flight: str, date: str) -> dict | None:
    """Return {"payload": {...}, "cached_at": iso} if a fresh entry exists.

    Entries older than EDCT_TTL_MINUTES are treated as absent — a revised or
    expired slot is worse than no slot, because predict_times would present
    it as the authoritative wheels-up time.
    """
    cutoff = _now() - timedelta(minutes=EDCT_TTL_MINUTES)
    if using_postgres():
        try:
            import json as _json
            with _connect(connect_timeout=5) as conn:
                with conn.cursor() as cur:
                    cur.execute(
                        "SELECT payload, updated_at FROM edct_cache "
                        "WHERE flight = %s AND flight_date = %s",
                        _edct_key(flight, date))
                    row = cur.fetchone()
                    if not row:
                        # Any-date fallback: EDCTs are same-day facts and the
                        # SWIM daemon keys them by the slot's UTC date, which
                        # can differ from the client's origin-local date near
                        # midnight. A FRESH entry for this flight under any
                        # date is almost certainly today's leg.
                        cur.execute(
                            "SELECT payload, updated_at FROM edct_cache "
                            "WHERE flight = %s ORDER BY updated_at DESC LIMIT 1",
                            (_edct_key(flight, date)[0],))
                        row = cur.fetchone()
            if not row:
                return None
            payload, updated_at = row
            updated_at = _parse(updated_at)
            if not updated_at or updated_at < cutoff:
                return None
            return {"payload": _json.loads(payload), "cached_at": _iso(updated_at)}
        except Exception:
            return None
    with _mem_lock:
        entry = _edct_mem.get(_edct_key(flight, date))
        if not entry:
            fkey = _edct_key(flight, date)[0]
            candidates = [(k, e) for k, e in _edct_mem.items() if k[0] == fkey]
            if candidates:
                entry = max(candidates, key=lambda kv: _iso(kv[1]["updated_at"]))[1]
    if not entry:
        return None
    if _parse(entry["updated_at"]) < cutoff:
        return None
    return {"payload": dict(entry["payload"]), "cached_at": _iso(entry["updated_at"])}


# ---------------------------------------------------------------------------
# Turn-time / equipment-chain caching
#
# The equipment_chain lookup (inbound aircraft + turn-time math) is the
# single most predictive signal the app computes, but it costs 2 AeroAPI
# queries and only ever ran from /api/brief on a manual tap. That meant:
# the cheap /api/flight/live tile could show a live delay while its own
# "no single cause was identified" note was true only because /live never
# looked. Same fix shape as EDCT caching above: the brief caches what it
# found here, keyed on whether it was actually a binding constraint, and
# /live (and the tracker's background poll) re-attach it cheaply instead
# of staying blind to a finding that already exists.
# ---------------------------------------------------------------------------

TURN_ANALYSIS_TTL_MINUTES = 30

_turn_mem: dict[tuple, dict] = {}


def _turn_key(flight: str, date: str) -> tuple:
    return ((flight or "").upper(), date or "")


def cache_turn_analysis(flight: str, date: str, turn_analysis: dict) -> None:
    """Persist a turn-time finding found by the brief. Ignored if empty.

    Best-effort: a failure here must never break the brief response.
    """
    if not turn_analysis or turn_analysis.get("turn_time_available_min") is None:
        return
    now = _now()
    if using_postgres():
        try:
            import json as _json
            with _connect(connect_timeout=5) as conn:
                with conn.cursor() as cur:
                    cur.execute(TURN_ANALYSIS_SCHEMA)  # lazy-create
                    cur.execute(
                        "INSERT INTO turn_analysis_cache "
                        "(flight, flight_date, payload, updated_at) "
                        "VALUES (%s, %s, %s, %s) "
                        "ON CONFLICT (flight, flight_date) DO UPDATE "
                        "SET payload = EXCLUDED.payload, updated_at = EXCLUDED.updated_at",
                        (_turn_key(flight, date)[0], date,
                         _json.dumps(turn_analysis), now))
                conn.commit()
            return
        except Exception:
            pass  # fall through to memory so at least this worker remembers
    with _mem_lock:
        _turn_mem[_turn_key(flight, date)] = {"payload": dict(turn_analysis),
                                              "updated_at": now}


def get_cached_turn_analysis(flight: str, date: str) -> dict | None:
    """Return {"payload": {...}, "cached_at": iso} if a fresh entry exists.

    Entries older than TURN_ANALYSIS_TTL_MINUTES are treated as absent — the
    inbound aircraft's own ETA moves, so a stale turn-time verdict is worse
    than none. The TTL is shorter than EDCT's because turn time changes
    continuously as the inbound flight progresses, where an EDCT is a fixed
    slot until FAA traffic management revises it.
    """
    cutoff = _now() - timedelta(minutes=TURN_ANALYSIS_TTL_MINUTES)
    if using_postgres():
        try:
            import json as _json
            with _connect(connect_timeout=5) as conn:
                with conn.cursor() as cur:
                    cur.execute(
                        "SELECT payload, updated_at FROM turn_analysis_cache "
                        "WHERE flight = %s AND flight_date = %s",
                        _turn_key(flight, date))
                    row = cur.fetchone()
                    if not row:
                        cur.execute(
                            "SELECT payload, updated_at FROM turn_analysis_cache "
                            "WHERE flight = %s ORDER BY updated_at DESC LIMIT 1",
                            (_turn_key(flight, date)[0],))
                        row = cur.fetchone()
            if not row:
                return None
            payload, updated_at = row
            updated_at = _parse(updated_at)
            if not updated_at or updated_at < cutoff:
                return None
            return {"payload": _json.loads(payload), "cached_at": _iso(updated_at)}
        except Exception:
            return None
    with _mem_lock:
        entry = _turn_mem.get(_turn_key(flight, date))
        if not entry:
            fkey = _turn_key(flight, date)[0]
            candidates = [(k, e) for k, e in _turn_mem.items() if k[0] == fkey]
            if candidates:
                entry = max(candidates, key=lambda kv: _iso(kv[1]["updated_at"]))[1]
    if not entry:
        return None
    if _parse(entry["updated_at"]) < cutoff:
        return None
    return {"payload": dict(entry["payload"]), "cached_at": _iso(entry["updated_at"])}


# ---------------------------------------------------------------------------
# SWIM daemon storage
#
# The long-running SWIM consumer (swim_daemon.py, leader process only)
# parses messages as they arrive and writes them here; request paths read
# recent events instead of spawning a JVM. Rows are pruned aggressively —
# this is a rolling window, not an archive.
# ---------------------------------------------------------------------------

SWIM_EVENT_RETENTION_MINUTES = 60
SWIM_DAEMON_STALE_SECONDS = 90   # heartbeat older than this = daemon not serving

_swim_mem: dict[str, list] = {}          # feed -> [{payload, flight, airport, received_at}]
_swim_status_mem: dict[str, dict] = {}


def swim_record_events(feed: str, records: list) -> None:
    """Append parsed SWIM records for a feed. Best-effort, never raises."""
    if not records:
        return
    now = _now()
    rows = []
    import json as _json
    for r in records:
        if not isinstance(r, dict):
            continue
        flight = (r.get("flight_id") or r.get("callsign") or "") or None
        airport = (r.get("airport") or r.get("apt") or "") or None
        src_ts = (r.get("source_timestamp") or r.get("timestamp") or "") or None
        rows.append((feed, flight, airport, _json.dumps(r, default=str),
                     src_ts, now))
    if not rows:
        return
    if using_postgres():
        try:
            with _connect(connect_timeout=5) as conn:
                with conn.cursor() as cur:
                    cur.executemany(
                        "INSERT INTO swim_events "
                        "(feed, flight, airport, payload, source_ts, received_at) "
                        "VALUES (%s, %s, %s, %s, %s, %s)", rows)
                    cur.execute(
                        "DELETE FROM swim_events WHERE received_at < %s",
                        (now - timedelta(minutes=SWIM_EVENT_RETENTION_MINUTES),))
                conn.commit()
            return
        except Exception:
            pass
    with _mem_lock:
        bucket = _swim_mem.setdefault(feed, [])
        for feed_, flight, airport, payload, src_ts, ts in rows:
            bucket.append({"payload": payload, "flight": flight,
                           "airport": airport, "received_at": ts})
        cutoff = now - timedelta(minutes=SWIM_EVENT_RETENTION_MINUTES)
        _swim_mem[feed] = [e for e in bucket if e["received_at"] >= cutoff][-2000:]


def swim_recent_events(feed: str, window_seconds: int = 900,
                       airport: str = None, flight: str = None,
                       keyword: str = None, limit: int = 50) -> list:
    """Recent parsed records for a feed, newest first, filtered.

    Filters mirror the subprocess parsers' semantics loosely: airport
    matches with and without the K prefix, flight matches space-stripped
    uppercase, keyword is a case-insensitive substring — all against the
    stored JSON payload, so fields the per-feed parsers set (flight_id,
    airport, raw text) are all searchable.
    """
    import json as _json
    cutoff = _now() - timedelta(seconds=window_seconds)
    raw = []
    if using_postgres():
        try:
            with _connect(connect_timeout=5) as conn:
                with conn.cursor() as cur:
                    cur.execute(
                        "SELECT payload FROM swim_events "
                        "WHERE feed = %s AND received_at >= %s "
                        "ORDER BY received_at DESC LIMIT 500",
                        (feed, cutoff))
                    raw = [r[0] for r in cur.fetchall()]
        except Exception:
            raw = []
    else:
        with _mem_lock:
            raw = [e["payload"] for e in reversed(_swim_mem.get(feed, []))
                   if e["received_at"] >= cutoff]

    def _match(payload: str) -> bool:
        up = payload.upper()
        if airport:
            a = airport.upper()
            forms = {a}
            if len(a) == 4 and a.startswith("K"):
                forms.add(a[1:])
            elif len(a) == 3:
                forms.add("K" + a)
            if not any(f in up for f in forms):
                return False
        if flight:
            if flight.upper().replace(" ", "") not in up.replace(" ", ""):
                return False
        if keyword:
            if keyword.upper() not in up:
                return False
        return True

    out = []
    for payload in raw:
        if not _match(payload):
            continue
        try:
            out.append(_json.loads(payload))
        except (ValueError, TypeError):
            continue
        if len(out) >= limit:
            break
    return out


def swim_daemon_heartbeat(queue_name: str, messages_delta: int = 0,
                          got_message: bool = False, restarted: bool = False,
                          note: str = None) -> None:
    """Daemon liveness ping. Best-effort, never raises."""
    now = _now()
    if using_postgres():
        try:
            with _connect(connect_timeout=5) as conn:
                with conn.cursor() as cur:
                    cur.execute(
                        "INSERT INTO swim_daemon_status "
                        "(queue_name, last_alive_at, last_message_at, "
                        " messages_total, restarts, note) "
                        "VALUES (%s, %s, %s, %s, %s, %s) "
                        "ON CONFLICT (queue_name) DO UPDATE SET "
                        "last_alive_at = EXCLUDED.last_alive_at, "
                        "last_message_at = CASE WHEN %s THEN EXCLUDED.last_alive_at "
                        "                 ELSE swim_daemon_status.last_message_at END, "
                        "messages_total = swim_daemon_status.messages_total + %s, "
                        "restarts = swim_daemon_status.restarts + %s, "
                        "note = COALESCE(%s, swim_daemon_status.note)",
                        (queue_name, now, now if got_message else None,
                         messages_delta, 1 if restarted else 0, note,
                         got_message, messages_delta, 1 if restarted else 0,
                         note))
                conn.commit()
            return
        except Exception:
            pass
    with _mem_lock:
        s = _swim_status_mem.setdefault(queue_name, {
            "messages_total": 0, "restarts": 0, "last_message_at": None,
            "note": None})
        s["last_alive_at"] = now
        if got_message:
            s["last_message_at"] = now
        s["messages_total"] += messages_delta
        if restarted:
            s["restarts"] += 1
        if note:
            s["note"] = note


def swim_daemon_health(queue_name: str) -> dict:
    """{"alive": bool, ...} — alive means a recent heartbeat exists."""
    row = None
    if using_postgres():
        try:
            with _connect(connect_timeout=5) as conn:
                with conn.cursor() as cur:
                    cur.execute(
                        "SELECT last_alive_at, last_message_at, messages_total, "
                        "restarts, note FROM swim_daemon_status "
                        "WHERE queue_name = %s", (queue_name,))
                    row = cur.fetchone()
        except Exception:
            row = None
        if row:
            last_alive, last_msg, total, restarts, note = row
        else:
            return {"alive": False}
    else:
        with _mem_lock:
            s = _swim_status_mem.get(queue_name)
        if not s:
            return {"alive": False}
        last_alive, last_msg = s.get("last_alive_at"), s.get("last_message_at")
        total, restarts, note = s["messages_total"], s["restarts"], s.get("note")

    last_alive = _parse(last_alive)
    alive = bool(last_alive and
                 (_now() - last_alive).total_seconds() < SWIM_DAEMON_STALE_SECONDS)
    return {"alive": alive, "last_alive_at": _iso(last_alive),
            "last_message_at": _iso(_parse(last_msg)) if last_msg else None,
            "messages_total": total, "restarts": restarts, "note": note}


# ---------------------------------------------------------------------------
# Leader election
#
# Exactly one process should run the background tracker. With Postgres we use
# a session-scoped advisory lock, which works across replicas. Without it we
# fall back to an flock on a file in /tmp, which works across gunicorn workers
# inside one container.
# ---------------------------------------------------------------------------

_leader_conn = None   # held open for process lifetime — releasing drops the lock
_leader_fh = None


def acquire_leadership() -> bool:
    """Try to become the tracker leader. Non-blocking; returns success."""
    global _leader_conn, _leader_fh

    if using_postgres():
        try:
            conn = _psycopg.connect(DATABASE_URL, autocommit=True,
                                    connect_timeout=5)
            with conn.cursor() as cur:
                cur.execute("SELECT pg_try_advisory_lock(%s)", (_LEADER_LOCK_KEY,))
                got = cur.fetchone()[0]
            if got:
                _leader_conn = conn  # keep the session alive to hold the lock
                return True
            conn.close()
            return False
        except Exception as exc:
            # Treat a Postgres ERROR as "not leader this round" — NOT as
            # permission to fall back to the file lock. The flock below is
            # per-container: during a transient DB blip at deploy time, two
            # replicas would each win their own local flock and both run the
            # tracker, double-billing AeroAPI — the exact split-brain leader
            # election exists to prevent. _watch_for_leadership() retries
            # every 15s, so leadership is claimed as soon as Postgres is back.
            import sys as _sys
            print(f"[STORE] Leadership check failed, will retry: "
                  f"{type(exc).__name__}: {exc}", file=_sys.stderr)
            return False

    # File lock: reached only when there is NO Postgres configured at all
    # (local dev / single container). Cross-worker within one container,
    # not cross-replica.
    try:
        fh = open(_LEADER_LOCK_FILE, "w")
        fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        _leader_fh = fh  # keep the fd open to hold the lock
        return True
    except (OSError, BlockingIOError):
        return False
