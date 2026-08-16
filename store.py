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
if DATABASE_URL:
    try:
        import psycopg as _psycopg  # noqa: F401
    except ImportError:
        _psycopg = None


def using_postgres() -> bool:
    return bool(DATABASE_URL and _psycopg is not None)


def backend_name() -> str:
    if using_postgres():
        return "postgres"
    if DATABASE_URL and _psycopg is None:
        return "memory (DATABASE_URL set but psycopg not installed)"
    return "memory"


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

_COLS = ("track_id, flight, flight_date, push_token, interval_minutes, "
         "last_check, last_risk, created_at, expires_at")


def init() -> None:
    """Create the table if needed. Safe to call repeatedly."""
    if not using_postgres():
        return
    with _psycopg.connect(DATABASE_URL) as conn:
        with conn.cursor() as cur:
            cur.execute(SCHEMA)
        conn.commit()


# ---------------------------------------------------------------------------
# CRUD
# ---------------------------------------------------------------------------

def add(track_id: str, flight: str, date: str, push_token: str,
        interval_minutes: int, ttl_hours: int = DEFAULT_TTL_HOURS) -> dict:
    """Insert or update a tracked flight. Returns the stored record."""
    now = _now()
    expires_at = now + timedelta(hours=ttl_hours)

    if using_postgres():
        with _psycopg.connect(DATABASE_URL) as conn:
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
        with _psycopg.connect(DATABASE_URL) as conn:
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
        with _psycopg.connect(DATABASE_URL) as conn:
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
        with _psycopg.connect(DATABASE_URL) as conn:
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


def mark_checked(track_id: str, when: datetime, risk: str) -> None:
    """Record that a check just ran."""
    if using_postgres():
        with _psycopg.connect(DATABASE_URL) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "UPDATE tracked_flights SET last_check = %s, last_risk = %s "
                    "WHERE track_id = %s",
                    (when, risk, track_id),
                )
            conn.commit()
        return

    with _mem_lock:
        if track_id in _mem:
            _mem[track_id]["last_check"] = _iso(when)
            _mem[track_id]["last_risk"] = risk


def purge_expired(now: datetime = None) -> int:
    """Drop flights past their TTL. Returns how many were removed."""
    now = now or _now()

    if using_postgres():
        with _psycopg.connect(DATABASE_URL) as conn:
            with conn.cursor() as cur:
                cur.execute("DELETE FROM tracked_flights "
                            "WHERE expires_at IS NOT NULL AND expires_at <= %s",
                            (now,))
                deleted = cur.rowcount
            conn.commit()
        return deleted

    with _mem_lock:
        stale = [tid for tid, r in _mem.items()
                 if (_parse(r.get("expires_at")) or now + timedelta(days=1)) <= now]
        for tid in stale:
            del _mem[tid]
        return len(stale)


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
            conn = _psycopg.connect(DATABASE_URL, autocommit=True)
            with conn.cursor() as cur:
                cur.execute("SELECT pg_try_advisory_lock(%s)", (_LEADER_LOCK_KEY,))
                got = cur.fetchone()[0]
            if got:
                _leader_conn = conn  # keep the session alive to hold the lock
                return True
            conn.close()
            return False
        except Exception:
            # Fall through to the file lock rather than leaving the tracker dead.
            pass

    try:
        fh = open(_LEADER_LOCK_FILE, "w")
        fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        _leader_fh = fh  # keep the fd open to hold the lock
        return True
    except (OSError, BlockingIOError):
        return False
