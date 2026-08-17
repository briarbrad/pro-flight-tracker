#!/usr/bin/env python3
"""flight_data.py — Pro Flight Tracker data puller.

Pulls flight status, aircraft position, and equipment chain data from:
  - AeroAPI v4 (FlightAware)
  - ADS-B Exchange (RapidAPI)
  - OpenSky Network

Standalone stdlib-only script. Reads API keys from environment variables.

Usage:
  python flight_data.py status --flight DL960 [--date 2026-08-13]
  python flight_data.py track --reg N12345
  python flight_data.py track --flight DL960
  python flight_data.py chain --flight DL960 [--date 2026-08-13]
"""
from __future__ import annotations

import argparse
import base64
import json
import os
import ssl
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone, timedelta

# ---------------------------------------------------------------------------
AEROAPI_BASE = "https://aeroapi.flightaware.com/aeroapi"
ADSB_EXCHANGE_HOST = "adsbexchange-com1.p.rapidapi.com"
ADSB_EXCHANGE_BASE = f"https://{ADSB_EXCHANGE_HOST}"
OPENSKY_BASE = "https://opensky-network.org/api"

REQUEST_TIMEOUT = 10
RETRY_BACKOFF = 1

# Matched by PREFIX FAMILY, not exact type. AeroAPI reports subtypes
# (A333, B763, B77W, B78X) that don't contain their parent string — the old
# exact list classified every real widebody as narrowbody, so e.g. an A330's
# turn was judged against a 90-min standard instead of 150.
TURN_TIME_BENCHMARKS = {
    "regional":   {"min": 45, "standard": 60,
                   "types": ["E17", "E19", "E14", "E75", "CRJ", "ERJ",
                             "DH8", "AT7", "AT4", "SF3"]},
    "widebody":   {"min": 90, "standard": 150,
                   "types": ["A33", "A34", "A35", "A38", "B74", "B76",
                             "B77", "B78", "MD11", "IL9"]},
    "narrowbody": {"min": 60, "standard": 90,
                   "types": ["A19N", "A20N", "A21N", "A31", "A32", "B73",
                             "B38M", "B39M", "B75", "MD8", "MD9", "B72"]},
}

# ---------------------------------------------------------------------------
def now_utc():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

def _http_get(url, headers=None, timeout=REQUEST_TIMEOUT, retries=1):
    hdrs = headers or {}
    ctx = ssl.create_default_context()
    last_err = None
    for attempt in range(1 + retries):
        if attempt > 0:
            time.sleep(RETRY_BACKOFF)
        try:
            req = urllib.request.Request(url, headers=hdrs, method="GET")
            with urllib.request.urlopen(req, timeout=timeout, context=ctx) as resp:
                raw = resp.read().decode("utf-8", errors="replace")
                try:
                    data = json.loads(raw)
                except json.JSONDecodeError:
                    data = {"_raw": raw}
                return resp.getcode(), data, None
        except urllib.error.HTTPError as exc:
            body = ""
            try: body = exc.read().decode("utf-8", errors="replace")[:500]
            except Exception: pass
            last_err = f"HTTP {exc.code}: {exc.reason} — {body}"
        except urllib.error.URLError as exc:
            last_err = f"URLError: {exc.reason}"
        except Exception as exc:
            last_err = f"{type(exc).__name__}: {exc}"
    return None, None, last_err

def _parse_iso(s):
    if not s: return None
    s = s.replace("Z", "+00:00")
    try:
        dt = datetime.fromisoformat(s)
        if dt.tzinfo is None: dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except ValueError: pass
    for fmt in ("%Y-%m-%dT%H:%M:%S%z", "%Y-%m-%dT%H:%M:%S"):
        try:
            dt = datetime.strptime(s, fmt)
            if dt.tzinfo is None: dt = dt.replace(tzinfo=timezone.utc)
            return dt
        except ValueError: continue
    return None

def _classify_aircraft(aircraft_type):
    if not aircraft_type: return "narrowbody"
    at = aircraft_type.upper()
    for cat, info in TURN_TIME_BENCHMARKS.items():
        for t in info["types"]:
            if t in at: return cat
    return "narrowbody"

def _meters_to_feet(m):
    return round(m * 3.28084) if m is not None else None

def _ms_to_knots(ms):
    return round(ms * 1.94384) if ms is not None else None

def _safe_float(v):
    if v is None: return None
    try: return float(v)
    except (ValueError, TypeError): return None

def _safe_int(v):
    if v is None: return None
    try: return int(float(v))
    except (ValueError, TypeError): return None

# ---------------------------------------------------------------------------
# API Keys
# ---------------------------------------------------------------------------
def _get_aeroapi_key():
    return os.environ.get("AEROAPI_KEY", "").strip() or None

def _get_adsb_key():
    return os.environ.get("ADSB_EXCHANGE_KEY", "").strip() or None

def _get_opensky_creds():
    raw = os.environ.get("OPENSKY_API_KEY", "").strip()
    if not raw or ":" not in raw: return None, None
    parts = raw.split(":", 1)
    return parts[0], parts[1]

# ---------------------------------------------------------------------------
# AeroAPI v4
# ---------------------------------------------------------------------------
def _q(value) -> str:
    """URL-quote a value being interpolated into an upstream URL.

    Idents come from request.args and ride into f-string URLs; without
    quoting, a crafted value like "DL244/route?x=" is path injection — it
    aims our AeroAPI key at an arbitrary API path on our dime. safe="" also
    escapes "/" itself.
    """
    return urllib.parse.quote(str(value or ""), safe="")


def aeroapi_headers():
    key = _get_aeroapi_key()
    if not key: return None
    return {"x-apikey": key, "Accept": "application/json"}

def _local_date_of(iso_ts: str, tz_name: str) -> str | None:
    """Calendar date of an ISO timestamp in the given IANA timezone.

    Falls back to the timestamp's own (UTC) date when the zone is missing or
    unknown, and None when the timestamp itself won't parse.
    """
    if not iso_ts:
        return None
    try:
        dt = datetime.fromisoformat(iso_ts.replace("Z", "+00:00"))
    except ValueError:
        return None
    if tz_name:
        try:
            from zoneinfo import ZoneInfo
            dt = dt.astimezone(ZoneInfo(tz_name))
        except Exception:
            pass
    return dt.strftime("%Y-%m-%d")


def _prefetched_flights(flight_ident):
    """Reuse a flight status the caller already paid for.

    /api/check runs `status` and then `chain` for the same flight, and both
    used to hit /flights/{ident} — one wasted AeroAPI query per check. The
    caller can now pass its Phase 1 result through PFT_PREFETCHED_STATUS
    (JSON, same shape cmd_status returns) and we skip the duplicate call.

    Returns None when there's nothing usable, so callers fall through to the
    live API.
    """
    raw = os.environ.get("PFT_PREFETCHED_STATUS", "")
    if not raw:
        return None
    try:
        payload = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return None
    if not isinstance(payload, dict):
        return None
    # Only reuse it for the flight it actually describes.
    if (payload.get("flight") or "").upper() != (flight_ident or "").upper():
        return None
    flights = (payload.get("data") or {}).get("flights")
    if not flights or not isinstance(flights, list):
        return None
    return flights


def aeroapi_flight_status(flight_ident, date=None):
    prefetched = _prefetched_flights(flight_ident)
    if prefetched is not None:
        return prefetched, None

    hdrs = aeroapi_headers()
    if not hdrs: return None, "AEROAPI_KEY not set"
    url = f"{AEROAPI_BASE}/flights/{_q(flight_ident)}"
    _, data, err = _http_get(url, headers=hdrs)
    if err: return None, f"AeroAPI flights: {err}"
    if not data or "flights" not in data:
        return None, f"AeroAPI: unexpected response"
    flights = data.get("flights", [])
    if not flights: return None, "AeroAPI: no flights found"
    if date:
        target = date if isinstance(date, str) else date.strftime("%Y-%m-%d")
        # Match on the ORIGIN-LOCAL calendar date, not a substring of the UTC
        # timestamp. A JFK 11 PM EDT departure has scheduled_out on the next
        # UTC day, so UTC matching resolved evening flights to the wrong leg
        # (or 404'd the brief). AeroAPI supplies the origin timezone.
        def _dep_local_date(f):
            ts = (f.get("scheduled_out") or f.get("estimated_out")
                  or f.get("scheduled_off") or "")
            tz = ((f.get("origin") or {}).get("timezone")) or ""
            return _local_date_of(ts, tz)
        local_matches = [f for f in flights if _dep_local_date(f) == target]
        if local_matches:
            flights = local_matches
        else:
            # Fallback: the old UTC-substring behavior, so a caller that was
            # (correctly or not) passing UTC dates still finds its leg.
            filtered = [f for f in flights
                        if target in (f.get("scheduled_out") or f.get("estimated_out") or "")]
            if filtered: flights = filtered
    return [_parse_aeroapi_flight(f) for f in flights], None

def _parse_aeroapi_flight(f):
    origin = f.get("origin", {}) or {}
    dest = f.get("destination", {}) or {}
    return {
        "fa_flight_id": f.get("fa_flight_id"),
        "ident": f.get("ident"),
        "operator": f.get("operator"),
        "flight_number": f.get("flight_number"),
        "registration": f.get("registration"),
        "aircraft_type": f.get("aircraft_type"),
        "status": f.get("status"),
        "origin_icao": origin.get("code"), "origin_iata": origin.get("code_iata"),
        "origin_name": origin.get("name"), "origin_city": origin.get("city"),
        # IANA zone (e.g. "America/New_York") — AeroAPI supplies this on the
        # airport object. Used to render times in airport-local time instead
        # of Zulu. May be absent; callers fall back to a lookup table.
        "origin_timezone": origin.get("timezone"),
        "dest_icao": dest.get("code"), "dest_iata": dest.get("code_iata"),
        "dest_name": dest.get("name"), "dest_city": dest.get("city"),
        "dest_timezone": dest.get("timezone"),
        "scheduled_out": f.get("scheduled_out"), "estimated_out": f.get("estimated_out"),
        "actual_out": f.get("actual_out"),
        "scheduled_off": f.get("scheduled_off"), "estimated_off": f.get("estimated_off"),
        "actual_off": f.get("actual_off"),
        "scheduled_on": f.get("scheduled_on"), "estimated_on": f.get("estimated_on"),
        "actual_on": f.get("actual_on"),
        "scheduled_in": f.get("scheduled_in"), "estimated_in": f.get("estimated_in"),
        "actual_in": f.get("actual_in"),
        "gate_origin": f.get("gate_origin"), "gate_destination": f.get("gate_destination"),
        "terminal_origin": f.get("terminal_origin"), "terminal_destination": f.get("terminal_destination"),
        "inbound_fa_flight_id": f.get("inbound_fa_flight_id"),
        "route_distance": f.get("route_distance"), "filed_ete": f.get("filed_ete"),
        "progress_percent": f.get("progress_percent"),
        "blocked": f.get("blocked"), "diverted": f.get("diverted"),
        "cancelled": f.get("cancelled"),
    }

def aeroapi_flight_route(fa_flight_id):
    hdrs = aeroapi_headers()
    if not hdrs: return None, "AEROAPI_KEY not set"
    _, data, err = _http_get(f"{AEROAPI_BASE}/flights/{_q(fa_flight_id)}/route", headers=hdrs)
    if err: return None, f"AeroAPI route: {err}"
    return data, None

def aeroapi_flight_position(fa_flight_id):
    hdrs = aeroapi_headers()
    if not hdrs: return None, "AEROAPI_KEY not set"
    _, data, err = _http_get(f"{AEROAPI_BASE}/flights/{_q(fa_flight_id)}/position", headers=hdrs)
    if err: return None, f"AeroAPI position: {err}"
    if not data: return None, "AeroAPI position: empty response"
    pos = data.get("last_position") or data
    return {
        "latitude": pos.get("latitude"), "longitude": pos.get("longitude"),
        "altitude_ft": pos.get("altitude") if pos.get("altitude_type") == "feet"
                       else _meters_to_feet(pos.get("altitude")),
        "groundspeed_kts": pos.get("groundspeed"), "heading": pos.get("heading"),
        "on_ground": pos.get("altitude", 0) == 0 if pos.get("altitude") is not None else None,
        "timestamp": pos.get("timestamp"), "source": "aeroapi",
    }, None

def aeroapi_position_by_ident(flight_ident):
    flights, err = aeroapi_flight_status(flight_ident)
    if err or not flights: return None, err or "No flights found"
    active = None
    for f in flights:
        st = (f.get("status") or "").lower()
        if "en route" in st or "airborne" in st or st == "active":
            active = f; break
    if not active: active = flights[0]
    fa_id = active.get("fa_flight_id")
    if not fa_id: return None, "No fa_flight_id"
    pos, pos_err = aeroapi_flight_position(fa_id)
    if pos:
        pos["registration"] = active.get("registration")
        pos["fa_flight_id"] = fa_id
    return pos, pos_err

# ---------------------------------------------------------------------------
# ADS-B Exchange (RapidAPI)
# ---------------------------------------------------------------------------
def adsb_exchange_headers():
    key = _get_adsb_key()
    if not key: return None
    return {"x-rapidapi-key": key, "x-rapidapi-host": ADSB_EXCHANGE_HOST, "Accept": "application/json"}

def adsb_by_registration(reg):
    hdrs = adsb_exchange_headers()
    if not hdrs: return None, "ADSB_EXCHANGE_KEY not set"
    reg_clean = reg.strip().upper().replace("-", "")
    _, data, err = _http_get(f"{ADSB_EXCHANGE_BASE}/v2/registration/{_q(reg_clean)}/", headers=hdrs)
    if err: return None, f"ADS-B Exchange: {err}"
    ac_list = data.get("ac") if data else None
    if not ac_list: return None, "ADS-B Exchange: no aircraft data"
    return _parse_adsb_aircraft(ac_list[0]), None

def adsb_by_callsign(callsign):
    hdrs = adsb_exchange_headers()
    if not hdrs: return None, "ADSB_EXCHANGE_KEY not set"
    _, data, err = _http_get(f"{ADSB_EXCHANGE_BASE}/v2/callsign/{_q(callsign.strip().upper())}/", headers=hdrs)
    if err: return None, f"ADS-B Exchange callsign: {err}"
    ac_list = data.get("ac") if data else None
    if not ac_list: return None, "ADS-B Exchange: no aircraft data for callsign"
    return _parse_adsb_aircraft(ac_list[0]), None

def _parse_adsb_aircraft(ac):
    alt = ac.get("alt_baro")
    if isinstance(alt, str) and alt.lower() == "ground": alt = 0
    return {
        "latitude": _safe_float(ac.get("lat")),
        "longitude": _safe_float(ac.get("lon")),
        "altitude_ft": _safe_int(alt),
        "groundspeed_kts": _safe_float(ac.get("gs")),
        "heading": _safe_float(ac.get("track")),
        "vertical_rate": _safe_float(ac.get("baro_rate")),
        "on_ground": bool(ac.get("gnd") or (isinstance(alt, (int, float)) and alt == 0)),
        "registration": ac.get("r") or ac.get("reg"),
        "aircraft_type": ac.get("t") or ac.get("desc"),
        "callsign": (ac.get("flight") or "").strip(),
        "icao24": ac.get("hex"), "squawk": ac.get("squawk"),
        "source": "adsb_exchange",
    }

# ---------------------------------------------------------------------------
# OpenSky Network
# ---------------------------------------------------------------------------
def opensky_headers():
    user, pw = _get_opensky_creds()
    if user and pw:
        cred = base64.b64encode(f"{user}:{pw}".encode()).decode()
        return {"Authorization": f"Basic {cred}", "Accept": "application/json"}
    return {"Accept": "application/json"}

def opensky_by_icao24(icao24):
    _, data, err = _http_get(f"{OPENSKY_BASE}/states/all?icao24={_q(icao24.strip().lower())}",
                             headers=opensky_headers())
    if err: return None, f"OpenSky: {err}"
    states = data.get("states") if data else None
    if not states: return None, "OpenSky: no state vectors"
    return _parse_opensky_state(states[0]), None

def opensky_by_callsign(callsign):
    _, data, err = _http_get(f"{OPENSKY_BASE}/states/all?callsign={_q(callsign.strip().upper())}",
                             headers=opensky_headers())
    if err: return None, f"OpenSky callsign: {err}"
    states = data.get("states") if data else None
    if not states: return None, "OpenSky: no state vectors for callsign"
    return _parse_opensky_state(states[0]), None

def _parse_opensky_state(sv):
    return {
        "latitude": sv[6], "longitude": sv[5],
        "altitude_ft": _meters_to_feet(sv[7]) if sv[7] is not None else _meters_to_feet(sv[13]),
        "groundspeed_kts": _ms_to_knots(sv[9]),
        "heading": sv[10],
        "vertical_rate": _meters_to_feet(sv[11]) if sv[11] is not None else None,
        "on_ground": sv[8], "icao24": sv[0],
        "callsign": (sv[1] or "").strip(), "squawk": sv[14],
        "source": "opensky",
    }

# ---------------------------------------------------------------------------
# Subcommands
# ---------------------------------------------------------------------------
def cmd_status(args):
    errors = []
    flights, err = aeroapi_flight_status(args.flight, date=args.date)
    if err: errors.append({"source": "aeroapi", "error": err})
    # Route is a SECOND paid AeroAPI query and nothing downstream uses it —
    # not analysis.py, not the iOS client. It used to be bought
    # unconditionally, silently doubling the cost of every status poll,
    # tracker cycle, and brief. Now opt-in via --with-route; the "route" key
    # stays in the payload (null) so existing decoders are unaffected.
    route_data = None
    if flights and getattr(args, "with_route", False):
        fa_id = flights[0].get("fa_flight_id")
        if fa_id:
            route_data, route_err = aeroapi_flight_route(fa_id)
            if route_err: errors.append({"source": "aeroapi_route", "error": route_err})
    return {"pull_time": now_utc(), "source": "aeroapi", "command": "status",
            "flight": args.flight, "date": args.date,
            "data": {"flights": flights or [], "route": route_data}, "errors": errors}

def cmd_track(args):
    errors, position = [], None
    reg, flight = args.reg, args.flight
    with ThreadPoolExecutor(max_workers=3) as pool:
        futures = {}
        if reg: futures["adsb_reg"] = pool.submit(adsb_by_registration, reg)
        if flight:
            futures["adsb_callsign"] = pool.submit(adsb_by_callsign, flight)
            futures["opensky_callsign"] = pool.submit(opensky_by_callsign, flight)
        results_map = {}
        for key, fut in futures.items():
            try: results_map[key] = fut.result(timeout=15)
            except Exception as exc: results_map[key] = (None, f"{key}: {exc}")
    for key in ["adsb_reg", "adsb_callsign", "opensky_callsign"]:
        if key in results_map:
            pos, err = results_map[key]
            if pos and pos.get("latitude") is not None:
                position = pos; break
            if err: errors.append({"source": key, "error": err})
    if not position and flight:
        pos, err = aeroapi_position_by_ident(flight)
        if pos and pos.get("latitude") is not None: position = pos
        elif err: errors.append({"source": "aeroapi_position", "error": err})
    return {"pull_time": now_utc(), "source": position.get("source") if position else None,
            "command": "track", "query": {"registration": reg, "flight": flight},
            "data": position, "errors": errors}

def cmd_chain(args):
    errors = []
    flights, err = aeroapi_flight_status(args.flight, date=args.date)
    if err: errors.append({"source": "aeroapi_status", "error": err})
    if not flights:
        return {"pull_time": now_utc(), "source": "aeroapi", "command": "chain",
                "flight": args.flight, "data": None,
                "errors": errors or [{"source": "aeroapi", "error": "No flight data"}]}
    primary = flights[0]
    tail = primary.get("registration")
    aircraft_type = primary.get("aircraft_type")
    inbound_id = primary.get("inbound_fa_flight_id")
    inbound_data, position_data = None, None
    with ThreadPoolExecutor(max_workers=3) as pool:
        futures = {}
        if inbound_id:
            futures["inbound"] = pool.submit(_get_inbound_status, inbound_id)
        if tail:
            futures["position_reg"] = pool.submit(adsb_by_registration, tail)
        elif args.flight:
            futures["position_cs"] = pool.submit(adsb_by_callsign, args.flight)
        for key, fut in futures.items():
            try:
                data, err = fut.result(timeout=15)
                if key == "inbound":
                    if err: errors.append({"source": "aeroapi_inbound", "error": err})
                    inbound_data = data
                elif key.startswith("position"):
                    if err: errors.append({"source": key, "error": err})
                    if data and data.get("latitude") is not None: position_data = data
            except Exception as exc:
                errors.append({"source": key, "error": str(exc)})
    if not position_data and inbound_id:
        pos, err = aeroapi_flight_position(inbound_id)
        if pos and pos.get("latitude") is not None: position_data = pos
        elif err: errors.append({"source": "aeroapi_position_fallback", "error": err})
    turn_analysis = _analyze_turn_time(primary, inbound_data, aircraft_type)
    return {"pull_time": now_utc(), "source": "aeroapi+adsb", "command": "chain",
            "flight": args.flight, "date": args.date,
            "data": {"outbound_flight": primary, "inbound_flight": inbound_data,
                     "aircraft_position": position_data, "tail_number": tail,
                     "aircraft_type": aircraft_type,
                     "aircraft_category": _classify_aircraft(aircraft_type),
                     "turn_analysis": turn_analysis}, "errors": errors}

def _get_inbound_status(fa_flight_id):
    hdrs = aeroapi_headers()
    if not hdrs: return None, "AEROAPI_KEY not set"
    _, data, err = _http_get(f"{AEROAPI_BASE}/flights/{_q(fa_flight_id)}", headers=hdrs)
    if err: return None, f"AeroAPI inbound: {err}"
    flights = data.get("flights", []) if data else []
    if not flights:
        if data and data.get("fa_flight_id"): return _parse_aeroapi_flight(data), None
        return None, "No inbound flight data"
    return _parse_aeroapi_flight(flights[0]), None

def _analyze_turn_time(outbound, inbound, aircraft_type):
    if not inbound:
        return {"turn_time_available_min": None, "sufficient": None,
                "note": "No inbound flight data — cannot calculate turn time"}
    sched_out = _parse_iso(outbound.get("scheduled_out"))
    if not sched_out:
        return {"turn_time_available_min": None, "note": "No scheduled departure time"}
    inbound_eta_str = (inbound.get("estimated_in") or inbound.get("estimated_on")
                       or inbound.get("scheduled_in"))
    inbound_eta = _parse_iso(inbound_eta_str)
    if not inbound_eta:
        return {"turn_time_available_min": None, "note": "No ETA for inbound"}
    turn_minutes = round((sched_out - inbound_eta).total_seconds() / 60.0, 1)
    category = _classify_aircraft(aircraft_type)
    bench = TURN_TIME_BENCHMARKS.get(category, TURN_TIME_BENCHMARKS["narrowbody"])
    req_min, req_std = bench["min"], bench["standard"]
    if turn_minutes < 0:
        sufficient, note = False, f"Inbound arrives AFTER outbound departs by {abs(turn_minutes):.0f}min — delay certain"
    elif turn_minutes < req_min:
        sufficient, note = False, f"Turn {turn_minutes:.0f}min below minimum {req_min}min for {category} — very tight"
    elif turn_minutes < req_std:
        sufficient, note = True, f"Turn {turn_minutes:.0f}min below standard {req_std}min for {category} — tight but possible"
    else:
        sufficient, note = True, f"Turn {turn_minutes:.0f}min adequate for {category} (std: {req_std}min)"
    return {"turn_time_available_min": turn_minutes,
            "turn_time_required_min_minimum": req_min,
            "turn_time_required_min_standard": req_std,
            "aircraft_category": category, "sufficient": sufficient,
            "inbound_eta": inbound_eta_str,
            "outbound_scheduled_departure": outbound.get("scheduled_out"),
            "inbound_flight_id": inbound.get("fa_flight_id"),
            "inbound_ident": inbound.get("ident"), "note": note}

# ---------------------------------------------------------------------------
def main():
    parser = argparse.ArgumentParser(prog="flight_data",
        description="Pro Flight Tracker — flight status, position, equipment chain")
    sub = parser.add_subparsers(dest="command", required=True)
    p = sub.add_parser("status")
    p.add_argument("--flight", required=True); p.add_argument("--date", default=None)
    p.add_argument("--with-route", action="store_true",
                   help="Also fetch the filed route (costs one extra AeroAPI query)")
    p = sub.add_parser("track")
    p.add_argument("--reg", default=None); p.add_argument("--flight", default=None)
    p = sub.add_parser("chain")
    p.add_argument("--flight", required=True); p.add_argument("--date", default=None)
    args = parser.parse_args()
    if args.command == "track" and not args.reg and not args.flight:
        parser.error("track requires --reg or --flight")
    result = {"status": cmd_status, "track": cmd_track, "chain": cmd_chain}[args.command](args)
    json.dump(result, sys.stdout, indent=2, default=str)
    sys.stdout.write("\n")

if __name__ == "__main__":
    main()
