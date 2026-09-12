"""Answer SWIM requests from the daemon's stored events."""

from datetime import datetime, timezone

import store

from pft.script_modules import swim_consumer
from pft.logging import log

_SWIM_FEED_TO_QUEUE = swim_consumer.FEED_QUEUE_MAP  # single source of truth

# How far back a daemon-served window reaches. Generous compared with the
# 8-18s subprocess capture windows — the daemon has been listening the
# whole time, which is the point.
_SWIM_SERVE_WINDOW_SECONDS = 900


def _swim_argv_to_query(args: list) -> dict:
    """["tfms-flight","--flight","DAL5187","--duration","8"] -> fields."""
    q = {"feed": str(args[0]) if args else "", "airport": None,
         "flight": None, "keyword": None, "limit": 50}
    i = 1
    while i < len(args):
        a, val = str(args[i]), (str(args[i + 1]) if i + 1 < len(args) else None)
        if a in ("--airport", "-a"):
            q["airport"] = val; i += 2
        elif a in ("--flight", "-f"):
            q["flight"] = val; i += 2
        elif a in ("--keyword", "-k"):
            q["keyword"] = val; i += 2
        elif a in ("--limit", "-n") and val and val.isdigit():
            q["limit"] = int(val); i += 2
        else:
            i += 2 if val and not val.startswith("-") else 1
    return q


class _SwimStoreUnavailable(Exception):
    """The SWIM event store couldn't be read. Fail loud — never spawn a
    competing per-request JVM consumer (it would steal the live daemon's
    JMS messages) and never report the outage as an empty feed."""


def _swim_daemon_serve(args: list):
    """Answer a SWIM request from daemon-collected events, or None.

    Raises _SwimStoreUnavailable when the store itself can't be read.
    """
    q = _swim_argv_to_query(args)
    queue_name = _SWIM_FEED_TO_QUEUE.get(q["feed"])
    if not queue_name:
        return None
    try:
        health = store.swim_daemon_health(queue_name)
    except Exception as exc:
        # The heartbeat read failed but the daemon may be alive — a
        # per-request consumer would split the JMS stream, so fail loud
        # instead of silently spawning one.
        raise _SwimStoreUnavailable(
            f"SWIM event store unreadable for queue {queue_name} "
            f"({type(exc).__name__}); retry in 30s")
    if health.get("db_error"):
        # The daemon may be alive — only the heartbeat read failed.
        # Falling through to a per-request consumer here would split the
        # JMS stream, so fail loud instead.
        raise _SwimStoreUnavailable(
            f"SWIM event store unreadable for queue {queue_name}; "
            f"retry in 30s")
    if not health.get("alive"):
        return None

    try:
        results = store.swim_recent_events(
            q["feed"], window_seconds=_SWIM_SERVE_WINDOW_SECONDS,
            airport=q["airport"], flight=q["flight"], keyword=q["keyword"],
            limit=q["limit"])
    except Exception as exc:
        # Same hazard as the db_error case above: the daemon may be alive
        # and only the event read failed. Never start a competing consumer.
        raise _SwimStoreUnavailable(
            f"SWIM event read failed for queue {queue_name} "
            f"({type(exc).__name__}: {exc}); retry in 30s")
    return {
        "feed": q["feed"],
        "query": {"airport": q["airport"], "flight": q["flight"],
                  "duration_seconds": None},
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "fetched_at": datetime.now(timezone.utc).isoformat(),
        "served_from": "daemon",
        "window_seconds": _SWIM_SERVE_WINDOW_SECONDS,
        "daemon_last_message_at": health.get("last_message_at"),
        "total_raw_messages": health.get("messages_total"),
        "filtered_results": len(results),
        "results": results,
    }
