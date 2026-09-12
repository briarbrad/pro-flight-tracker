"""Background flight tracker: leadership election + polling loop."""

import atexit
import json
import os
import sys
import threading
import time
from datetime import datetime, timezone

import store
import analysis

from pft.runner import (
    CHECK_TIMEOUT, run_script, run_scripts_parallel,
)
from pft.validation import (
    _AIRLINE_MAP, _extract_flights, _iata_to_icao_airline,
    _icao_to_faa, clean_ident, to_faa, to_icao,
)
from pft.logging import log

def send_push_notification(push_token: str, title: str, body: str, data: dict = None):
    """Send a push notification via Expo Push API."""
    import urllib.request
    message = {
        "to": push_token,
        "sound": "default",
        "title": title,
        "body": body,
    }
    if data:
        message["data"] = data

    req_data = json.dumps([message]).encode("utf-8")
    req = urllib.request.Request(
        "https://exp.host/--/api/v2/push/send",
        data=req_data,
        headers={"Content-Type": "application/json"},
        method="POST"
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return json.loads(resp.read().decode())
    except Exception as e:
        print(f"[PUSH ERROR] {e}", file=sys.stderr)
        return None


def _escalate(current: str, candidate: str) -> str:
    """Return whichever risk level is worse."""
    return candidate if _risk_rank(candidate) > _risk_rank(current) else current


def _parse_iso(value: str):
    """Parse an ISO 8601 timestamp, tolerating a trailing Z. None on failure.

    Delegates to analysis.parse_iso: naive timestamps are normalized to
    UTC (these API timestamps are always UTC) so callers can subtract
    results from aware datetimes without 500ing.
    """
    return analysis.parse_iso(value)


def _departure_delay_minutes(flight: dict):
    """Minutes the departure has slipped vs schedule, or None if unknowable.

    AeroAPI status strings are things like "Scheduled" / "En Route" / "Arrived"
    and often never literally say "Delayed", so comparing the estimated and
    scheduled times is the reliable signal.
    """
    scheduled = _parse_iso(flight.get("scheduled_out"))
    if not scheduled:
        return None
    actual = _parse_iso(flight.get("actual_out"))
    estimated = _parse_iso(flight.get("estimated_out"))
    effective = actual or estimated
    if not effective:
        return None
    return (effective - scheduled).total_seconds() / 60.0


def _arrival_delay_minutes(flight: dict):
    """Minutes the gate arrival has slipped vs schedule, or None if unknowable."""
    scheduled = _parse_iso(flight.get("scheduled_in"))
    if not scheduled:
        return None
    actual = _parse_iso(flight.get("actual_in"))
    estimated = _parse_iso(flight.get("estimated_in"))
    effective = actual or estimated
    if not effective:
        return None
    return (effective - scheduled).total_seconds() / 60.0


def _snapshot_fields(flight: dict):
    """(predicted_out, predicted_in, delta_minutes) for a delay-trend snapshot.

    Everything comes from the status payload the tracker already fetched —
    recording a snapshot costs zero additional AeroAPI queries. delta_minutes
    tracks the next gate event that can still move: the departure slip until
    the aircraft is out, then the arrival slip.
    """
    if not isinstance(flight, dict):
        return None, None, None
    predicted_out = (flight.get("actual_out") or flight.get("estimated_out")
                     or flight.get("scheduled_out"))
    predicted_in = (flight.get("actual_in") or flight.get("estimated_in")
                    or flight.get("scheduled_in"))
    if flight.get("actual_out"):
        delta = _arrival_delay_minutes(flight)
        if delta is None:
            delta = _departure_delay_minutes(flight)
    else:
        delta = _departure_delay_minutes(flight)
    return (predicted_out, predicted_in,
            round(delta, 1) if delta is not None else None)


def _record_and_get_delay_trend(flight: str, date: str, primary: dict,
                               risk: str) -> dict | None:
    """Write a snapshot from THIS request's own live data, then read back
    the trend series.

    Previously delay_trend only ever reflected the separate background
    tracker's own polling cadence, completely decoupled from whatever live
    status this same request just fetched for the hero tile. That let the
    trend history show "0 -> 0 -> 0 -> 0, holding steady" on the exact same
    screen as a tile already showing a live +30 min delay, whenever the
    tracker's own next scheduled check hadn't landed yet. Recording a
    snapshot here — from data already in hand, so it costs nothing extra —
    means the trend can never be staler than the screen it sits on.
    """
    try:
        track_id = f"{flight}_{date}"
        snap_out, snap_in, snap_delta = _snapshot_fields(primary)
        store.record_snapshot(track_id, datetime.now(timezone.utc),
                              predicted_out=snap_out, predicted_in=snap_in,
                              delta_minutes=snap_delta, risk=risk)
    except Exception:
        pass  # trend data is an enhancement, never worth failing the request
    return _delay_trend_payload(flight, date)


def _delay_trend_payload(flight: str, date: str) -> dict | None:
    """delay_trend block for the brief/live envelopes, or None.

    Reads the snapshots the background tracker has been writing on its
    normal cadence. None (→ JSON null) when nothing usable exists yet — a
    single check is a data point, not a trend, and the client must show
    nothing rather than a false "holding steady".
    """
    try:
        snaps = store.recent_snapshots(f"{flight}_{date}")
    except Exception:
        return None
    usable = [s for s in snaps if s.get("delta_minutes") is not None]
    if not usable:
        return None
    return {
        "snapshots": [{
            "checked_at": s.get("checked_at"),
            "delta_minutes": s.get("delta_minutes"),
            "risk": s.get("risk"),
        } for s in usable],
        "direction": analysis.classify_delay_trend(usable),
        "checks": len(usable),
    }


def _faa_airport_records(faa_status: dict) -> list:
    """Yield the per-airport records from an aviation_weather.py faa-status payload.

    Real shape: {"command":"faa-status","data":{"KJFK":{...},"KEWR":{...}}}
    — keyed by ICAO. The old code looked for a top-level "programs" list, which
    has never existed, so ground stops and GDPs were silently never detected.
    """
    if not isinstance(faa_status, dict):
        return []
    data = faa_status.get("data")
    if not isinstance(data, dict):
        return []
    return [rec for rec in data.values() if isinstance(rec, dict)]


def extract_risk_level(check_data: dict) -> str:
    """Overall risk level for push notifications: 'LOW', 'MODERATE', or 'HIGH'.

    Deliberately conservative and self-contained — this drives whether the user
    gets woken up, not what the client displays. /api/check still returns raw
    data and does no interpretation.
    """
    data = check_data.get("data", {})
    risk = "LOW"

    # ---- FAA delay programs at the airports involved -------------------
    for record in _faa_airport_records(data.get("faa_status")):
        if record.get("ground_stops"):
            risk = _escalate(risk, "HIGH")
        if record.get("ground_delay_programs"):
            risk = _escalate(risk, "MODERATE")
        if record.get("arrival_departure_delays"):
            risk = _escalate(risk, "MODERATE")
        if record.get("closures"):
            risk = _escalate(risk, "MODERATE")

    # ---- The flight itself ---------------------------------------------
    for flight in _extract_flights(data.get("flight_status")):
        if not isinstance(flight, dict):
            continue

        if flight.get("cancelled") or flight.get("diverted"):
            risk = _escalate(risk, "HIGH")
            continue

        status = (flight.get("status") or "").lower()
        if "cancel" in status or "divert" in status:
            risk = _escalate(risk, "HIGH")
            continue
        if "delay" in status:
            risk = _escalate(risk, "MODERATE")

        delay = _departure_delay_minutes(flight)
        if delay is not None:
            if delay >= 45:
                risk = _escalate(risk, "HIGH")
            elif delay >= 15:
                risk = _escalate(risk, "MODERATE")

    # ---- Equipment/turn-time (structural, not weather) ------------------
    # This is what lets risk escalate — and therefore an alert fire — BEFORE
    # the airline's own estimate has moved at all: a binding turn-time
    # deficit guarantees a slip regardless of what estimated_out currently
    # says. Mirrors the same thresholds assess() uses for the on-demand
    # brief, so the tracker's judgment can't disagree with the brief's.
    turn_analysis = data.get("turn_analysis")
    if isinstance(turn_analysis, dict):
        available = turn_analysis.get("turn_time_available_min")
        minimum = turn_analysis.get("turn_time_required_min_minimum")
        standard = turn_analysis.get("turn_time_required_min_standard")
        if available is not None and minimum is not None:
            if available < minimum:
                risk = _escalate(risk, "HIGH")
            elif standard and available < standard:
                risk = _escalate(risk, "MODERATE")

    # ---- TFMS flow advisories (ground stops / GDP issuances) -----------
    flow = data.get("tfms_flow_gdp")
    if isinstance(flow, dict):
        for result in flow.get("results", []):
            if not isinstance(result, dict):
                continue
            text = f"{result.get('title', '')} {result.get('text', '')}".upper()
            if not text.strip():
                text = str(result).upper()
            # A cancellation advisory is the program ENDING — not a new risk.
            if "CANCEL" in text or "PURGE" in text:
                continue
            if "GROUND STOP" in text or result.get("msg_type") == "RSTR":
                risk = _escalate(risk, "HIGH")
            elif "GDP" in text or "GROUND DELAY" in text:
                risk = _escalate(risk, "MODERATE")

    return risk


def _flight_is_finished(status_data: dict) -> str | None:
    """Return a reason string if the flight is over, else None.

    Once a flight has arrived, been cancelled, or diverted, there is nothing
    left to warn about — continuing to poll just spends AeroAPI credit.
    """
    if not isinstance(status_data, dict):
        return None

    flights = status_data.get("data", {}).get("flights") or status_data.get("flights")
    if not flights:
        return None

    f = flights[0]
    if not isinstance(f, dict):
        return None

    if f.get("cancelled"):
        return "cancelled"
    if f.get("diverted"):
        return "diverted"
    if f.get("actual_in"):
        return "arrived at gate"
    status = (f.get("status") or "").lower()
    if "arrived" in status or "landed" in status:
        return "arrived"
    return None


def background_tracker():
    """Background thread that periodically checks tracked flights.

    Only ever runs in the single process that won leader election — see
    store.acquire_leadership(). Two copies of this loop would double AeroAPI
    spend for identical data.

    Runs a check immediately on entry, then every 60s after. Running
    immediately matters most for the retry path in _watch_for_leadership():
    when a worker finally wins the lock minutes after boot, anything that
    was already due (e.g. last_check was never set) shouldn't have to wait a
    further 60s on top of however long the retry took.
    """
    global TRACKER_IS_LEADER
    first_pass = True
    while True:
        if not first_pass:
            time.sleep(60)  # Check every minute if any flights are due
        first_pass = False

        # Leadership heartbeat: a dead Postgres session drops the advisory
        # lock server-side while this process still believes it is leader.
        # If we lost it, stop this loop and re-enter the election instead
        # of double-polling against whoever won it — that is double AeroAPI
        # spend for identical data.
        try:
            still_leader = store.validate_leadership()
        except Exception as exc:
            log(f"[TRACKER] Leadership validation errored: "
                f"{type(exc).__name__}: {exc}")
            still_leader = False
        if not still_leader:
            log("[TRACKER] Leadership lost; returning to election")
            TRACKER_IS_LEADER = False
            watcher = threading.Thread(target=_watch_for_leadership,
                                       daemon=True)
            watcher.start()
            return

        now = datetime.now(timezone.utc)

        try:
            dropped = store.purge_expired(now)
            if dropped:
                print(f"[TRACKER] Purged {dropped} expired flight(s)", file=sys.stderr)
            flights_to_check = store.due_for_check(now)
        except Exception as exc:
            print(f"[TRACKER] Store error: {type(exc).__name__}: {exc}",
                  file=sys.stderr)
            continue  # next iteration still sleeps first — no tight-looping

        for info in flights_to_check:
            track_id = info.get("track_id", "?")
            try:
                flight = info["flight"]
                date = info["date"]

                # Phase 1 — flight status + flow advisories.
                # flight_data.py status = 1 AeroAPI query (route is opt-in
                # and the tracker never buys it); SWIM is free.
                quick_tasks = [
                    {
                        "key": "flight_status",
                        "script": "flight_data.py",
                        "args": ["status", "--flight", flight, "--date", date],
                        "timeout": 20
                    },
                    {
                        "key": "tfms_flow_gdp",
                        "script": "swim_consumer.py",
                        "args": ["tfms-flow", "--keyword", "GDP", "--duration", "8"],
                        "timeout": 25
                    },
                ]

                results = run_scripts_parallel(quick_tasks, max_workers=2)

                # Horizon gate. Without this the tracker escalates on a delay
                # program that is active RIGHT NOW for a flight leaving
                # tomorrow — the program will have expired long before
                # departure, so the alert is a false alarm. Same reasoning as
                # /api/brief; see Example 1 in analytical-framework.md.
                tracked_flights_found = _extract_flights(
                    results.get("flight_status", {}).get("data"))
                primary = tracked_flights_found[0] if tracked_flights_found else {}
                # Phase-aware, same as /api/brief: a flight holding on a
                # taxiway used to fall into the hours<0 branch and have
                # faa_status gated off, so an active ground stop that was
                # genuinely holding it went unalerted.
                tracked_phase = analysis.compute_phase(primary, now)
                horizon = analysis.compute_horizon(primary, now, tracked_phase)
                plan = analysis.source_plan(horizon["hours_to_next_event"],
                                            tracked_phase)

                # Phase 2 — FAA delay programs, but only when they can still
                # matter at this horizon. Free call, but a misleading one
                # when the flight is a day out.
                airports = []
                for f in tracked_flights_found:
                    for key in ("origin_icao", "dest_icao"):
                        code = f.get(key)
                        if code and code not in airports:
                            airports.append(code)

                if airports and plan["faa_status"]["relevant"]:
                    faa = run_scripts_parallel([{
                        "key": "faa_status",
                        "script": "aviation_weather.py",
                        "args": ["faa-status", "--icao"] + airports,
                        "timeout": 15,
                    }], max_workers=1)
                    results.update(faa)

                # Phase 3 — equipment/turn-time. This is the single most
                # predictive signal the app computes ("the aircraft
                # physically cannot turn around in time"), but it used to
                # only ever run on a manual /api/brief tap — meaning even a
                # binding equipment constraint sitting there for hours could
                # never raise risk or fire an alert on its own. Reuse a
                # fresh cached finding for free first (e.g. from a brief the
                # user already ran); only pay for a fresh lookup (1 AeroAPI
                # inbound query — status is prefetched) when there's no fresh
                # cache AND the horizon says
                # it's still knowable pre-pushback — same window /api/brief
                # itself gates on, so this can't get expensive far out.
                turn_analysis = {}
                if plan["equipment_chain"]["relevant"]:
                    cached_turn = store.get_cached_turn_analysis(flight, date)
                    if cached_turn:
                        turn_analysis = cached_turn["payload"]
                    else:
                        # Reuse Phase 1 status so chain doesn't re-buy
                        # /flights/{ident}. /api/check and /api/brief already
                        # did this; the tracker was the remaining spender.
                        status_payload = results.get("flight_status", {}).get("data")
                        chain_data, chain_code = run_script(
                            "flight_data.py",
                            ["chain", "--flight", flight, "--date", date],
                            timeout=30,
                            env_extras=_status_prefetch_env(status_payload))
                        if chain_code == 200 and isinstance(chain_data, dict):
                            inner = chain_data.get("data") or {}
                            turn_analysis = inner.get("turn_analysis") or {}
                            # Same local-time grounding /api/brief applies —
                            # a cache entry written from here must be just
                            # as citable as one written from a manual brief,
                            # or the narrative guardrail has a hole again.
                            tracker_origin_tz = analysis.resolve_timezone(
                                primary.get("origin_icao"),
                                primary.get("origin_timezone"))
                            if turn_analysis.get("inbound_eta"):
                                turn_analysis["inbound_eta_local"] = (
                                    analysis.to_local(turn_analysis["inbound_eta"],
                                                      tracker_origin_tz)["display"])
                            if turn_analysis.get("outbound_scheduled_departure"):
                                turn_analysis["outbound_scheduled_departure_local"] = (
                                    analysis.to_local(
                                        turn_analysis["outbound_scheduled_departure"],
                                        tracker_origin_tz)["display"])
                            store.cache_turn_analysis(flight, date, turn_analysis)

                # Drop anything the horizon says is not decision-relevant so
                # it cannot drive an alert.
                relevance = {"flight_status": True,
                             "faa_status": plan["faa_status"]["relevant"],
                             "tfms_flow_gdp": plan["tfms_flow"]["relevant"]}
                check_data = {"data": {
                    k: v["data"] for k, v in results.items()
                    if relevance.get(k, True)
                }}
                check_data["data"]["turn_analysis"] = turn_analysis

                new_risk = extract_risk_level(check_data)
                old_risk = info.get("last_risk")

                # Update tracking state. Re-derive the check cadence every
                # pass instead of leaving it pinned to whatever the client
                # requested at track-creation time: a flight still hours
                # from departure doesn't need re-checking every 15 minutes,
                # and one that's just started taxiing needs tighter cadence
                # than any fixed default would give it.
                next_interval = analysis.tracking_interval_minutes(
                    horizon, tracked_phase)
                store.mark_checked(track_id, now, new_risk,
                                    interval_minutes=next_interval)

                # Durable delay history: one snapshot per scheduled check,
                # from data already in hand (no extra AeroAPI spend). This
                # is what /api/brief and /api/flight/live read back as
                # delay_trend — without it, whether a delay is growing or
                # shrinking is discarded on every poll.
                snap_out, snap_in, snap_delta = _snapshot_fields(primary)
                store.record_snapshot(track_id, now,
                                      predicted_out=snap_out,
                                      predicted_in=snap_in,
                                      delta_minutes=snap_delta,
                                      risk=new_risk)

                # Stop tracking once the flight is over.
                finished = _flight_is_finished(
                    results.get("flight_status", {}).get("data")
                )
                if finished:
                    store.remove(track_id)
                    print(f"[TRACKER] {flight} {finished} — untracked",
                          file=sys.stderr)

                # Notify on a risk transition — and also on the very first
                # check if the flight is already at risk. Previously the
                # `old_risk and ...` guard meant a flight that was ALREADY
                # HIGH when you started tracking it never notified at all,
                # because there was no prior value to differ from.
                emoji = {"LOW": "🟢", "MODERATE": "🟡", "HIGH": "🔴"}.get(new_risk, "⚪")
                title = body_text = None

                if old_risk is None:
                    if new_risk != "LOW":
                        title = f"{emoji} {flight} Risk: {new_risk}"
                        body_text = "Already elevated when tracking started. Tap to see details."
                elif new_risk != old_risk:
                    direction = ("↑ Elevated" if _risk_rank(new_risk) > _risk_rank(old_risk)
                                 else "↓ Improved")
                    title = f"{emoji} {flight} Risk: {new_risk}"
                    body_text = f"{direction} from {old_risk}. Tap to see details."

                if title:
                    send_push_notification(
                        info["push_token"],
                        title,
                        body_text,
                        {"flight": flight, "date": date, "risk": new_risk}
                    )

            except Exception as e:
                print(f"[TRACKER ERROR] {track_id}: {e}", file=sys.stderr)


def _risk_rank(level: str) -> int:
    return {"LOW": 0, "MODERATE": 1, "HIGH": 2}.get(level, -1)


# IATA → ICAO airline code mapping (for SWIM callsigns)






TRACKER_IS_LEADER = False  # flipped True by _become_leader(), possibly long
# after start_background_tracker() kicks off the election thread


def _become_leader() -> None:
    """Take over as tracker leader: init schema, start the polling thread."""
    global TRACKER_IS_LEADER

    # Leadership FIRST, schema init SECOND. store.init() runs
    # CREATE TABLE IF NOT EXISTS, which is not actually safe against two
    # sessions doing it at once on a table that's never existed — both see
    # "doesn't exist" and race, and the loser can hit a UniqueViolation on
    # Postgres's internal pg_type bookkeeping. Only ever calling this from
    # the confirmed leader means exactly one process, cluster-wide, ever
    # runs it — the race can't happen at all.
    try:
        store.init()
    except Exception as exc:
        print(f"[TRACKER] Store init failed: {type(exc).__name__}: {exc}",
              file=sys.stderr)

    thread = threading.Thread(target=background_tracker, daemon=True)
    thread.start()
    TRACKER_IS_LEADER = True
    print(f"[TRACKER] Background flight tracker started "
          f"(pid={os.getpid()}, store={store.backend_name()})", file=sys.stderr)

    # The persistent SWIM consumer rides the same leadership: exactly one
    # process cluster-wide holds the JMS connections, so request-path
    # fallbacks never compete with it for queue messages.
    try:
        import swim_daemon
        if swim_daemon.start(airline_map=_AIRLINE_MAP):
            # Otherwise the JVM consumers are orphaned on redeploy/restart.
            atexit.register(swim_daemon.stop)
    except Exception as exc:
        print(f"[TRACKER] SWIM daemon failed to start: "
              f"{type(exc).__name__}: {exc}", file=sys.stderr)


def _watch_for_leadership(poll_seconds: int = 15) -> None:
    """Keep retrying acquire_leadership() until it succeeds.

    Rolling deploys run the old and new deployment side by side for a short
    overlap so traffic never drops. Every worker in the new deployment can
    lose its very first leadership attempt simply because the OLD
    deployment's leader hasn't been killed yet — that's the overlap working
    as intended, not a failure. But a one-shot "try once at boot, give up
    forever" check has no way to notice once the old leader's container is
    stopped and the lock frees up moments later. This loop is what actually
    claims it once that happens.
    """
    while True:
        time.sleep(poll_seconds)
        if store.acquire_leadership():
            print(f"[TRACKER] Acquired leadership on retry, pid={os.getpid()}",
                  file=sys.stderr)
            _become_leader()
            return


def start_background_tracker() -> bool:
    """Try to become tracker leader now; if that fails, keep retrying.

    Runs at IMPORT time, not under `if __name__ == "__main__"`. Gunicorn
    imports this module as `app`, so anything gated on __main__ never executes
    in production — which is why the tracker used to silently never run.

    Leader election keeps that fix from creating a worse problem: with
    --workers 2, every worker would otherwise start its own tracker and bill
    AeroAPI twice for identical data.

    Returns whether THIS call won leadership immediately. A False return does
    not mean this process gave up — see _watch_for_leadership.
    """
    if os.environ.get("DISABLE_TRACKER", "").lower() in ("1", "true", "yes"):
        print("[TRACKER] Disabled via DISABLE_TRACKER", file=sys.stderr)
        return False

    if store.acquire_leadership():
        _become_leader()
        return True

    print(f"[TRACKER] Standby (another worker holds the lease), "
          f"pid={os.getpid()}", file=sys.stderr)
    watcher = threading.Thread(target=_watch_for_leadership, daemon=True)
    watcher.start()
    return False
