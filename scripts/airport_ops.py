#!/usr/bin/env python3
"""
airport_ops.py — Enhanced airport operations data for Pro Flight Tracker.

Five orthogonal data sources that fill gaps in the base flight/weather scripts:
  - G-AIRMET:    Gridded turbulence & wind shear forecasts (NWS/AWC)
  - TCF:         TFM Convective Forecast — the product FAA traffic management
                 actually uses to call ground stops/reroutes for thunderstorms
  - Lightning:   Real-time lightning strikes near airports (Blitzortung)
  - RVR:         Runway Visual Range per-runway from FAA sensors
  - ATFM Infer:  Eurocontrol ATFM regulation inference from delay patterns

All output is JSON to stdout. Follows the same patterns as aviation_weather.py
and flight_data.py (argparse CLI, env-var secrets, structured JSON output).

Usage:
    python3 airport_ops.py gairmet [--hazard turb-hi turb-lo llws] [--route KJFK LIRF]
    python3 airport_ops.py tcf [--route KJFK KATL]
    python3 airport_ops.py lightning --icao KJFK [--radius 20] [--duration 30]
    python3 airport_ops.py rvr --airport JFK [--raw-metar "KJFK ... R04L/2000V4000FT ..."]
    python3 airport_ops.py atfm-infer --flight DL182 [--date 2026-08-15]

Dependencies:
    - stdlib (always)
    - websockets (for lightning subcommand; pip install websockets)
    - beautifulsoup4 + lxml (for rvr subcommand; pip install beautifulsoup4 lxml)

Env vars:
    - AEROAPI_KEY  — required for atfm-infer subcommand only
"""

import argparse
import asyncio
import json
import math
import os
import re
import ssl
import sys
import time
from datetime import datetime, timezone, timedelta
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

# Optional deps — graceful degradation
try:
    import websockets
    HAS_WEBSOCKETS = True
except ImportError:
    HAS_WEBSOCKETS = False

try:
    from bs4 import BeautifulSoup
    HAS_BS4 = True
except ImportError:
    HAS_BS4 = False


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

AWC_BASE = "https://aviationweather.gov/api/data"
GAIRMET_URL = f"{AWC_BASE}/gairmet"
BLITZORTUNG_WS = "wss://ws1.blitzortung.org/"
RVR_URL = "https://rvr.data.faa.gov/cgi-bin/rvr-details.pl"
AEROAPI_BASE = "https://aeroapi.flightaware.com/aeroapi"

REQUEST_TIMEOUT = 12
USER_AGENT = os.environ.get("WEATHER_USER_AGENT", "FlightTracker/1.0")
RAMP_CLOSURE_RADIUS_NM = 5   # lightning within 5 NM = ramp hold

# ---------------------------------------------------------------------------
# Airport coordinate table  (lat, lon)
# ---------------------------------------------------------------------------
AIRPORT_COORDS = {
    # --- US Major ---
    "KJFK": (40.6413, -73.7781), "KLGA": (40.7769, -73.8740),
    "KEWR": (40.6895, -74.1745), "KLAX": (33.9425, -118.4081),
    "KSFO": (37.6213, -122.3790), "KORD": (41.9742, -87.9073),
    "KATL": (33.6407, -84.4277), "KDFW": (32.8998, -97.0403),
    "KDEN": (39.8561, -104.6737), "KBOS": (42.3656, -71.0096),
    "KPHL": (39.8744, -75.2424), "KDCA": (38.8512, -77.0402),
    "KIAD": (38.9531, -77.4565), "KBWI": (39.1754, -76.6683),
    "KMIA": (25.7959, -80.2870), "KFLL": (26.0726, -80.1527),
    "KTPA": (27.9756, -82.5332), "KMCO": (28.4312, -81.3081),
    "KIAH": (29.9844, -95.3414), "KHOU": (29.6454, -95.2789),
    "KSEA": (47.4502, -122.3088), "KPDX": (45.5898, -122.5951),
    "KPHX": (33.4373, -112.0078), "KLAS": (36.0840, -115.1537),
    "KSLC": (40.7884, -111.9778), "KMSP": (44.8848, -93.2223),
    "KDTW": (42.2124, -83.3534), "KCLT": (35.2144, -80.9473),
    "KRDU": (35.8776, -78.7875), "KPIT": (40.4915, -80.2329),
    "KCLE": (41.4058, -81.8539), "KSTL": (38.7487, -90.3700),
    "KBNA": (36.1263, -86.6774), "KMSY": (29.9934, -90.2580),
    "KSAN": (32.7338, -117.1933), "KAUS": (30.1975, -97.6664),
    "PHNL": (21.3187, -157.9225), "PANC": (61.1744, -149.9964),
    # --- European Major ---
    "LIRF": (41.8003, 12.2389),  "LIRN": (40.8860, 14.2908),
    "LFPG": (49.0097, 2.5479),  "LFPO": (48.7253, 2.3592),
    "LEMD": (40.4936, -3.5668), "LEBL": (41.2971, 2.0785),
    "EGLL": (51.4700, -0.4543), "EGLC": (51.5053, 0.0553),
    "EGKK": (51.1481, -0.1903), "EHAM": (52.3086, 4.7639),
    "EDDF": (50.0379, 8.5622),  "EDDM": (48.3538, 11.7861),
    "LOWW": (48.1103, 16.5697), "LSZH": (47.4647, 8.5492),
    "EIDW": (53.4213, -6.2701), "ENGM": (60.1976, 11.1004),
    "EKCH": (55.6180, 12.6560), "ESSA": (59.6519, 17.9186),
    "EFHK": (60.3172, 24.9633), "LPPT": (38.7813, -9.1359),
    "LGAV": (37.9364, 23.9445), "LTFM": (41.2753, 28.7519),
    "EPWA": (52.1657, 20.9671), "LKPR": (50.1008, 14.2600),
    # --- Middle East / Asia / Other ---
    "OMDB": (25.2528, 55.3644), "OTHH": (25.2731, 51.6081),
    "OEJN": (21.6796, 39.1565), "LLBG": (32.0114, 34.8867),
    "RJTT": (35.5533, 139.7811), "RJAA": (35.7647, 140.3864),
    "VHHH": (22.3080, 113.9185), "WSSS": (1.3644, 103.9915),
    "YSSY": (-33.9461, 151.1772),
    "CYYZ": (43.6777, -79.6248), "CYUL": (45.4706, -73.7408),
    "MMMX": (19.4363, -99.0721),
}

ICAO_TO_FAA = {
    "KJFK": "JFK", "KLGA": "LGA", "KEWR": "EWR", "KLAX": "LAX",
    "KSFO": "SFO", "KORD": "ORD", "KATL": "ATL", "KDFW": "DFW",
    "KDEN": "DEN", "KBOS": "BOS", "KPHL": "PHL", "KDCA": "DCA",
    "KIAD": "IAD", "KBWI": "BWI", "KMIA": "MIA", "KFLL": "FLL",
    "KTPA": "TPA", "KMCO": "MCO", "KIAH": "IAH", "KHOU": "HOU",
    "KSEA": "SEA", "KPDX": "PDX", "KPHX": "PHX", "KLAS": "LAS",
    "KSLC": "SLC", "KMSP": "MSP", "KDTW": "DTW", "KCLT": "CLT",
    "KSAN": "SAN", "KAUS": "AUS", "KBNA": "BNA", "KMSY": "MSY",
"KRDU": "RDU", "KPIT": "PIT", "KCLE": "CLE", "KSTL": "STL",
}

# Eurocontrol ATFM — ICAO two-letter prefixes in ECAC airspace
EUROCONTROL_PREFIXES = frozenset({
    "BI",  # Iceland
    "EB", "ED", "EE", "EF", "EG", "EH", "EI", "EK", "EL",
    "EN", "EP", "ES", "ET", "EV", "EY",
    "LA", "LB", "LC", "LD", "LE", "LF", "LG", "LH", "LI",
    "LJ", "LK", "LL", "LM", "LN", "LO", "LP", "LQ", "LR",
    "LS", "LT", "LU", "LW", "LX", "LY", "LZ",
    "GC", "GE",  # Canary Islands, Ceuta
    "UD", "UG", "UK",  # Caucasus/Ukraine (partial ECAC)
})

CARRIER_SHORT_TO_ICAO = {
    "DL": "DAL", "AA": "AAL", "UA": "UAL", "WN": "SWA",
    "B6": "JBU", "AS": "ASA", "NK": "NKS", "F9": "FFT",
    "HA": "HAL", "SY": "SCX", "G4": "AAY",
}


# =========================================================================
#  Utility helpers
# =========================================================================

def now_utc():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def haversine_nm(lat1, lon1, lat2, lon2):
    """Great-circle distance in nautical miles."""
    R = 3440.065
    lat1, lon1, lat2, lon2 = map(math.radians, [lat1, lon1, lat2, lon2])
    dlat = lat2 - lat1
    dlon = lon2 - lon1
    a = (math.sin(dlat / 2) ** 2
         + math.cos(lat1) * math.cos(lat2) * math.sin(dlon / 2) ** 2)
    return 2 * R * math.asin(math.sqrt(a))


def _http_get(url, headers=None, timeout=REQUEST_TIMEOUT, retries=1):
    hdrs = {"User-Agent": USER_AGENT}
    if headers:
        hdrs.update(headers)
    req = Request(url, headers=hdrs)
    ctx = ssl.create_default_context()
    last_err = None
    for attempt in range(1 + retries):
        try:
            with urlopen(req, timeout=timeout, context=ctx) as resp:
                return resp.read().decode("utf-8")
        except (HTTPError, URLError, TimeoutError) as e:
            last_err = e
            if attempt < retries:
                time.sleep(0.5 * (attempt + 1))
    raise last_err


def lzw_decompress(compressed):
    """Decompress Blitzortung LZW-encoded WebSocket text frames."""
    if not compressed:
        return compressed
    dict_size = 256
    dictionary = {i: chr(i) for i in range(dict_size)}
    w = chr(ord(compressed[0]))
    result = [w]
    for i in range(1, len(compressed)):
        code = ord(compressed[i])
        if code in dictionary:
            entry = dictionary[code]
        elif code == dict_size:
            entry = w + w[0]
        else:
            entry = chr(code)
        result.append(entry)
        dictionary[dict_size] = w + entry[0]
        dict_size += 1
        w = entry
    return "".join(result)


def _point_near_segment(plat, plon, lat1, lon1, lat2, lon2,
                         threshold_nm=75, steps=20):
    """Check whether point (plat, plon) is within threshold_nm of the
    great-circle segment from (lat1,lon1) to (lat2,lon2), sampled at
    `steps` equally-spaced points."""
    for i in range(steps + 1):
        t = i / steps
        slat = lat1 + t * (lat2 - lat1)
        slon = lon1 + t * (lon2 - lon1)
        if haversine_nm(plat, plon, slat, slon) < threshold_nm:
            return True
    return False


# =========================================================================
#  1.  G-AIRMET  (Graphical Turbulence Guidance — NWS/AWC)
# =========================================================================

def _fetch_gairmet(hazards):
    """Pull G-AIRMET polygons from aviationweather.gov for each hazard."""
    out = {}
    for hz in hazards:
        try:
            raw = _http_get(f"{GAIRMET_URL}?format=json&hazard={hz}")
            out[hz] = json.loads(raw)
        except Exception as e:
            out[hz] = {"error": str(e)}
    return out


def _gairmet_relevant(item, origin, dest):
    """Determine if a single G-AIRMET polygon is relevant to the route."""
    coords = item.get("coords", [])
    if not coords:
        return None
    polygon = []
    for c in coords:
        try:
            polygon.append((float(c["lat"]), float(c["lon"])))
        except (KeyError, ValueError, TypeError):
            continue
    if not polygon:
        return None

    o = AIRPORT_COORDS.get(origin)
    d = AIRPORT_COORDS.get(dest)
    near_origin = near_dest = along_route = False

    if o:
        for plat, plon in polygon:
            if haversine_nm(o[0], o[1], plat, plon) < 100:
                near_origin = True
                break
    if d:
        for plat, plon in polygon:
            if haversine_nm(d[0], d[1], plat, plon) < 100:
                near_dest = True
                break
    if o and d and not (near_origin or near_dest):
        for plat, plon in polygon:
            if _point_near_segment(plat, plon, o[0], o[1], d[0], d[1]):
                along_route = True
                break

    if not (near_origin or near_dest or along_route):
        return None

    base_raw = item.get("base", "SFC")
    top_raw = item.get("top", "")
    try:
        base_ft = int(base_raw) * 100 if base_raw and base_raw != "SFC" else 0
    except ValueError:
        base_ft = base_raw
    try:
        top_ft = int(top_raw) * 100 if top_raw else None
    except ValueError:
        top_ft = top_raw

    return {
        "hazard": item.get("hazard", ""),
        "severity": item.get("severity", ""),
        "base": base_raw,
        "top": top_raw,
        "base_ft": base_ft,
        "top_ft": top_ft,
        "valid_from": item.get("validTime"),
        "expires": datetime.utcfromtimestamp(
            item["expireTime"]).strftime("%Y-%m-%dT%H:%M:%SZ")
        if item.get("expireTime") else None,
        "product": item.get("product", ""),
        "near_origin": near_origin,
        "near_dest": near_dest,
        "along_route": along_route,
    }


def cmd_gairmet(args):
    hazards = args.hazard or ["turb-hi", "turb-lo", "llws"]
    data = _fetch_gairmet(hazards)

    result = {
        "source": "AWC G-AIRMET",
        "timestamp": now_utc(),
        "hazards_queried": hazards,
    }

    if args.route and len(args.route) == 2:
        origin, dest = [x.upper() for x in args.route]
        relevant = []
        for hz, items in data.items():
            if not isinstance(items, list):
                continue
            for item in items:
                hit = _gairmet_relevant(item, origin, dest)
                if hit:
                    relevant.append(hit)

        result["route"] = f"{origin}-{dest}"
        result["relevant_count"] = len(relevant)
        result["relevant"] = relevant

        sevs = [r["severity"] for r in relevant]
        if any(s in ("SEV", "EXTM") for s in sevs):
            result["risk_level"] = "HIGH"
            result["risk_emoji"] = "🔴"
        elif any(s == "MOD" for s in sevs):
            result["risk_level"] = "MODERATE"
            result["risk_emoji"] = "🟡"
        elif relevant:
            result["risk_level"] = "LOW"
            result["risk_emoji"] = "🟢"
        else:
            result["risk_level"] = "NONE"
            result["risk_emoji"] = "🟢"
            result["summary"] = "No G-AIRMET advisories along route"
    else:
        # Dump all active G-AIRMETs
        all_items = []
        for hz, items in data.items():
            if isinstance(items, list):
                for item in items:
                    all_items.append({
                        "hazard": item.get("hazard"),
                        "severity": item.get("severity"),
                        "base": item.get("base", "SFC"),
                        "top": item.get("top", ""),
                        "valid": item.get("validTime"),
"product": item.get("product"),
                    })
        result["total_active"] = len(all_items)
        result["items"] = all_items

    return result


# =========================================================================
#  2.  TCF  (TFM Convective Forecast — the product FAA traffic management
#      uses to decide ground stops/reroutes for thunderstorms, 2-6h out)
# =========================================================================

def _fetch_tcf():
    """Pull the TCF convective-coverage polygons as a GeoJSON FeatureCollection."""
    try:
        raw = _http_get(f"{AWC_BASE}/tcf?format=geojson")
        return json.loads(raw)
    except Exception as e:
        return {"error": str(e)}


def _polygon_from_geojson(coordinates):
    """Flatten a GeoJSON Polygon's outer ring — [[lon,lat], ...] — into
    (lat, lon) tuples so it can reuse the same haversine/segment helpers as
    G-AIRMET."""
    if not coordinates:
        return []
    ring = coordinates[0] if coordinates and isinstance(coordinates[0], list) \
        and coordinates[0] and isinstance(coordinates[0][0], (list, tuple)) \
        else coordinates
    pts = []
    for pair in ring:
        try:
            lon, lat = pair[0], pair[1]
            pts.append((float(lat), float(lon)))
        except (IndexError, TypeError, ValueError):
            continue
    return pts


def _tcf_relevant(feature, origin, dest):
    """Determine if a single TCF polygon is relevant to the route."""
    geom = feature.get("geometry") or {}
    if geom.get("type") != "Polygon":
        return None
    polygon = _polygon_from_geojson(geom.get("coordinates"))
    if not polygon:
        return None

    o = AIRPORT_COORDS.get(origin)
    d = AIRPORT_COORDS.get(dest)
    near_origin = near_dest = along_route = False

    if o:
        for plat, plon in polygon:
            if haversine_nm(o[0], o[1], plat, plon) < 100:
                near_origin = True
                break
    if d:
        for plat, plon in polygon:
            if haversine_nm(d[0], d[1], plat, plon) < 100:
                near_dest = True
                break
    if o and d and not (near_origin or near_dest):
        for plat, plon in polygon:
            if _point_near_segment(plat, plon, o[0], o[1], d[0], d[1]):
                along_route = True
                break

    if not (near_origin or near_dest or along_route):
        return None

    props = feature.get("properties") or {}
    return {
        "valid_time": props.get("validTime"),
        "issue_time": props.get("issueTime"),
        "coverage": props.get("coverage"),
        "confidence": props.get("confidence"),
        "tops_hundreds_ft": props.get("tops"),
        "near_origin": near_origin,
        "near_dest": near_dest,
        "along_route": along_route,
    }


def cmd_tcf(args):
    fc = _fetch_tcf()

    result = {
        "source": "AWC TFM Convective Forecast (TCF)",
        "timestamp": now_utc(),
    }

    if isinstance(fc, dict) and fc.get("error"):
        result["error"] = fc["error"]
        return result

    features = fc.get("features", []) if isinstance(fc, dict) else []
    result["issue_time"] = fc.get("issueTime") if isinstance(fc, dict) else None

    if args.route and len(args.route) == 2:
        origin, dest = [x.upper() for x in args.route]
        relevant = []
        for feat in features:
            hit = _tcf_relevant(feat, origin, dest)
            if hit:
                relevant.append(hit)

        result["route"] = f"{origin}-{dest}"
        result["relevant_count"] = len(relevant)
        result["relevant"] = relevant

        coverages = [r["coverage"] for r in relevant]
        if "medium" in coverages:
            result["risk_level"] = "MODERATE"
            result["risk_emoji"] = "🟡"
        elif relevant:
            result["risk_level"] = "LOW"
            result["risk_emoji"] = "🟢"
        else:
            result["risk_level"] = "NONE"
            result["risk_emoji"] = "🟢"
            result["summary"] = "No convective forecast areas along route"
    else:
        # Dump all active TCF polygons nationwide
        result["total_active"] = len(features)
        result["items"] = [
            {
                "coverage": (f.get("properties") or {}).get("coverage"),
                "confidence": (f.get("properties") or {}).get("confidence"),
                "tops_hundreds_ft": (f.get("properties") or {}).get("tops"),
                "valid_time": (f.get("properties") or {}).get("validTime"),
            }
            for f in features
        ]

    return result


# =========================================================================
#  3.  Lightning  (Blitzortung real-time network)
# =========================================================================

async def _collect_strikes(lat, lon, radius_nm, duration_sec):
    """Connect to Blitzortung WS, collect strikes near (lat,lon)."""
    strikes = []
    ramp = []
    t0 = time.time()
    try:
        async with websockets.connect(
            BLITZORTUNG_WS, close_timeout=5, open_timeout=10,
        ) as ws:
            await ws.send('{"a": 111}')
            while time.time() - t0 < duration_sec:
                try:
                    raw = await asyncio.wait_for(ws.recv(), timeout=2.0)
                    decoded = lzw_decompress(raw)
                    data = json.loads(decoded)
                    slat = data.get("lat")
                    slon = data.get("lon")
                    if slat is None or slon is None:
                        continue
                    dist = haversine_nm(lat, lon, slat, slon)
                    if dist <= radius_nm:
                        s = {
                            "lat": round(slat, 4),
                            "lon": round(slon, 4),
                            "distance_nm": round(dist, 1),
                            "time_ns": data.get("time"),
                            "polarity": data.get("pol"),
                            "stations": data.get("mcg"),
                        }
                        strikes.append(s)
                        if dist <= RAMP_CLOSURE_RADIUS_NM:
                            ramp.append(s)
                except asyncio.TimeoutError:
                    continue
                except (json.JSONDecodeError, ValueError):
                    continue
    except Exception as e:
        return {"error": str(e), "strikes": strikes, "ramp_alerts": ramp}
    return {"strikes": strikes, "ramp_alerts": ramp}


def cmd_lightning(args):
    if not HAS_WEBSOCKETS:
        return {"error": "websockets not installed (pip install websockets)",
                "source": "Blitzortung"}

    icao = args.icao.upper()
    coords = AIRPORT_COORDS.get(icao)
    if not coords:
        return {"error": f"No coordinates for {icao}. Add to AIRPORT_COORDS."}

    lat, lon = coords
    radius = args.radius
    duration = args.duration

    res = asyncio.run(_collect_strikes(lat, lon, radius, duration))
    strikes = res.get("strikes", [])
    ramp = res.get("ramp_alerts", [])

    ramp_risk = "HIGH" if ramp else "NONE"
    if len(strikes) > 20:
        activity = "HIGH"
    elif len(strikes) > 5:
        activity = "MODERATE"
    elif strikes:
        activity = "LOW"
    else:
        activity = "NONE"

    out = {
        "source": "Blitzortung",
        "timestamp": now_utc(),
        "airport": icao,
        "airport_coords": {"lat": lat, "lon": lon},
        "search_radius_nm": radius,
        "collection_duration_sec": duration,
        "total_strikes": len(strikes),
        "strikes_within_5nm": len(ramp),
        "ramp_closure_risk": ramp_risk,
        "activity_level": activity,
        "risk_emoji": ("🔴" if ramp_risk == "HIGH"
                       else "🟡" if activity in ("MODERATE", "HIGH")
                       else "🟢"),
        "note": ("Lightning within 5 NM triggers ramp closure / ground stop. "
                 "All-clear requires 15+ min with no strikes within 5 NM."),
    }
    if res.get("error"):
        out["connection_error"] = res["error"]
    if strikes:
        out["nearest_strike_nm"] = round(min(s["distance_nm"] for s in strikes), 1)
        out["farthest_strike_nm"] = round(max(s["distance_nm"] for s in strikes), 1)
    out["strikes"] = strikes[:50]  # cap output size

    return out


# =========================================================================
#  4.  RVR  (FAA Runway Visual Range)
# =========================================================================

def _parse_rvr_html(html):
    """Parse the FAA RVR HTML table into structured per-runway data.

    Columns in the table:
      RWY  — runway designator (04L, 22R, …)
      TD   — touchdown zone RVR (feet)
      MP   — midpoint RVR
      RO   — rollout RVR
      E    — equipment status flag
      C    — calibration flag
    Values: '>6000' (good), numeric feet, 'FFF' (fault), blank (no sensor).
    """
    if HAS_BS4:
        return _parse_rvr_bs4(html)
    return _parse_rvr_regex(html)


def _rvr_value(text):
    """Convert a single RVR cell to a structured value."""
    t = text.strip()
    if not t:
        return {"raw": "", "status": "no_sensor"}
    if t == "FFF":
        return {"raw": "FFF", "status": "fault"}
    m = re.match(r"([<>]?)(\d+)", t)
    if m:
        prefix = m.group(1)
        val = int(m.group(2))
        return {
            "raw": t,
            "feet": val,
            "modifier": "above" if prefix == ">" else ("below" if prefix == "<" else "exact"),
            "status": "ok",
        }
    return {"raw": t, "status": "unknown"}


def _parse_rvr_bs4(html):
    soup = BeautifulSoup(html, "html.parser")
    table = soup.find("table", attrs={"border": True})
    if not table:
        return []
    rows = table.find_all("tr")
    runways = []
    for row in rows[1:]:  # skip header row
        cells = row.find_all(["th", "td"])
        if len(cells) < 4:
            continue
        texts = [c.get_text(strip=True) for c in cells]
        rwy = texts[0]
        td = _rvr_value(texts[1]) if len(texts) > 1 else _rvr_value("")
        mp = _rvr_value(texts[2]) if len(texts) > 2 else _rvr_value("")
        ro = _rvr_value(texts[3]) if len(texts) > 3 else _rvr_value("")
        runways.append({
            "runway": rwy,
            "touchdown": td,
            "midpoint": mp,
            "rollout": ro,
        })
    return runways


def _parse_rvr_regex(html):
    """Fallback regex parser when BS4 is unavailable."""
    runways = []
    pattern = re.compile(
        r"<tr>\s*<th>(\w+)</th>\s*"
        r"<td[^>]*>(.*?)</td>\s*"
        r"<td[^>]*>(.*?)</td>\s*"
        r"<td[^>]*>(.*?)</td>",
        re.IGNORECASE | re.DOTALL,
    )
    for m in pattern.finditer(html):
        rwy = m.group(1).strip()
        runways.append({
            "runway": rwy,
            "touchdown": _rvr_value(m.group(2)),
            "midpoint": _rvr_value(m.group(3)),
            "rollout": _rvr_value(m.group(4)),
        })
    return runways


def _parse_metar_rvr(metar_text):
    """Extract RVR groups from a METAR string.

    Examples:  R04L/2000V4000FT   R22R/P6000FT   R28L/M0600FT
    """
    out = []
    for m in re.finditer(
        r"R(\d{2}[LCR]?)/([MP]?)(\d{4})(?:V([MP]?)(\d{4}))?FT",
        metar_text,
    ):
        entry = {
            "runway": m.group(1),
            "rvr_ft": int(m.group(3)),
            "modifier": {"M": "below", "P": "above"}.get(m.group(2), "exact"),
        }
        if m.group(5):
            entry["rvr_variable_ft"] = int(m.group(5))
            entry["variable_modifier"] = (
                {"M": "below", "P": "above"}.get(m.group(4), "exact")
            )
            entry["rvr_range"] = f"{entry['rvr_ft']}-{entry['rvr_variable_ft']} ft"
        out.append(entry)
    return out


def _rvr_risk(runways):
    """Assess overallRVR visibility risk from parsed runway data."""
    min_val = None
    for rwy in runways:
        for zone in ("touchdown", "midpoint", "rollout"):
            v = rwy.get(zone, {})
            if v.get("status") == "ok" and "feet" in v:
                ft = v["feet"]
                # '>6000' means at least 6000 — use 6000 as floor
                if min_val is None or ft < min_val:
                    min_val = ft

    if min_val is None:
        return "NOT_APPLICABLE", "🟢", "No numeric RVR data — likely VFR"
    if min_val < 600:
        return ("CRITICAL", "🔴",
                f"RVR {min_val} ft — below CAT I minimums (1800 ft). "
                "Only CAT II/III equipped aircraft can land.")
    if min_val < 1800:
        return ("HIGH", "🔴",
                f"RVR {min_val} ft — below standard CAT I minimums. "
                "Reduced arrival rate likely.")
    if min_val < 4000:
        return ("MODERATE", "🟡",
                f"RVR {min_val} ft — IFR but above CAT I minimums.")
    return "LOW", "🟢", f"RVR ≥{min_val} ft — good visibility"


def cmd_rvr(args):
    # Normalise to FAA 3-letter code
    apt = args.airport.upper()
    if apt in ICAO_TO_FAA:
        apt = ICAO_TO_FAA[apt]
    elif len(apt) == 4 and apt.startswith("K"):
        apt = apt[1:]

    result = {"source": "FAA RVR", "timestamp": now_utc(), "airport": apt}

    try:
        html = _http_get(f"{RVR_URL}?content=table&airport={apt}", timeout=15)
        runways = _parse_rvr_html(html)
        result["available"] = True
        result["runways"] = runways

        # Extract observation time from page
        ts_m = re.search(r"(\d{2}:\d{2}:\d{2})z\s*</th>", html)
        if ts_m:
            result["observation_time_z"] = ts_m.group(1) + "Z"
    except Exception as e:
        result["available"] = False
        result["error"] = str(e)
        runways = []

    # Also parse METAR RVR if given
    if args.raw_metar:
        metar_rvr = _parse_metar_rvr(args.raw_metar)
        if metar_rvr:
            result["metar_rvr"] = metar_rvr

    risk, emoji, note = _rvr_risk(runways)
    result["visibility_risk"] = risk
    result["risk_emoji"] = emoji
    result["note"] = note

    return result


# =========================================================================
#  5.  ATFM Inference  (Eurocontrol heuristic from AeroAPI data)
# =========================================================================

def _is_eurocontrol(icao):
    return len(icao) >= 2 and icao[:2].upper() in EUROCONTROL_PREFIXES


def _to_icao_carrier(flight):
    """Convert 2-letter IATA carrier code to 3-letter ICAO if needed."""
    for short, full in CARRIER_SHORT_TO_ICAO.items():
        if flight.startswith(short) and not flight.startswith(full):
            return full + flight[len(short):]
    return flight


def _infer_atfm(flt):
    """Score likelihood of Eurocontrol ATFM/CTOT regulation on a flight."""
    dest_icao = flt.get("destination", {}).get("code_icao", "")
    if not _is_eurocontrol(dest_icao):
        return {
            "applicable": False,
            "reason": f"Destination {dest_icao} not in Eurocontrol airspace",
        }

    indicators = []
    confidence = 0

    # ── Departure delay signature ──
    sched_out = flt.get("scheduled_out")
    est_out = flt.get("estimated_out")
    delay_min = None
    if sched_out and est_out:
        try:
            s = datetime.fromisoformat(sched_out.replace("Z", "+00:00"))
            e = datetime.fromisoformat(est_out.replace("Z", "+00:00"))
            delay_min = (e - s).total_seconds() / 60
        except (ValueError, TypeError):
            pass

    if delay_min is not None and 15 <= delay_min <= 120:
        indicators.append({
            "type": "departure_delay_in_ctot_range",
            "detail": (f"{int(delay_min)}-min gap between scheduled and "
                       "estimated departure (CTOT range 15-120 min)"),
            "weight": 30,
        })
        confidence += 30

        # CTOT slots are assigned in 5-min windows
        if delay_min % 5 < 2:
            indicators.append({
                "type": "ctot_slot_alignment",
                "detail": "Delay aligns to 5-minute CTOT slot increment",
                "weight": 15,
            })
            confidence += 15

    # ── Origin weather clear → delay not local ──
    # (caller can enrich flt dict with origin_flt_cat from METAR)
    origin_cat = flt.get("origin_flt_cat", "")
    if origin_cat == "VFR":
        indicators.append({
            "type": "clear_origin_weather",
            "detail": "Origin is VFR — delay unlikely to be local weather",
            "weight": 20,
        })
        confidence += 20

    # ── European peak traffic windows ──
    if est_out:
        try:
            e = datetime.fromisoformat(est_out.replace("Z", "+00:00"))
            # Arrival UTC ≈ departure + block time.  Estimate from scheduled.
            sched_in = flt.get("scheduled_in")
            if sched_in:
                arr = datetime.fromisoformat(sched_in.replace("Z", "+00:00"))
                arr_h = arr.hour
            else:
                arr_h = (e.hour + 8) % 24  # rough transatlantic guess
            # European morning/evening banks
            if 5 <= arr_h <= 10 or 14 <= arr_h <= 20:
                indicators.append({
                    "type": "european_peak_traffic_window",
                    "detail": (f"Arrival ~{arr_h:02d}Z falls in European "
                               "peak traffic window"),
                    "weight": 10,
                })
                confidence += 10
        except (ValueError, TypeError):
            pass

    # ── Flight status contains ATFM-like delay code ──
    status = flt.get("status", "")
    if status and "delay" in status.lower():
        indicators.append({
            "type": "status_delay_flag",
            "detail": f"AeroAPI status = '{status}'",
            "weight": 10,
        })
        confidence += 10

    # ── Verdict ──
    confidence = min(confidence, 95)
    if confidence >= 50:
        verdict, emoji = "PROBABLE", "🟡"
    elif confidence >= 25:
        verdict, emoji = "POSSIBLE", "🟡"
    elif indicators:
        verdict, emoji = "UNLIKELY", "🟢"
    else:
        verdict, emoji = "NO_INDICATION", "🟢"

    return {
        "applicable": True,
        "destination": dest_icao,
        "in_eurocontrol": True,
        "verdict": verdict,
        "confidence_pct": confidence,
        "risk_emoji": emoji,
        "indicators": indicators,
        "delay_min": int(delay_min) if delay_min and delay_min > 0 else 0,
        "note": (
            "Eurocontrol ATFM assigns CTOT (Calculated Take-Off Time) slots "
            "to manage European airspace capacity. Direct CTOT data requires "
            "institutional B2B access. This assessment infers ATFM regulation "
            "from departure delay patterns, origin weather, and traffic windows."
        ),
    }


def cmd_atfm_infer(args):
    api_key = os.environ.get("AEROAPI_KEY")
    if not api_key:
        return {"error": "AEROAPI_KEY not set", "source": "ATFM Inference"}

    flight = _to_icao_carrier(args.flight.upper())
    qs = ""
    if args.date:
        qs = f"?start={args.date}T00:00:00Z&end={args.date}T23:59:59Z"

    try:
        raw = _http_get(
            f"{AEROAPI_BASE}/flights/{flight}{qs}",
            headers={"x-apikey": api_key},
        )
        data = json.loads(raw)
    except Exception as e:
        return {"error": f"AeroAPI: {e}", "source": "ATFM Inference",
                "flight": flight}

    flights = data.get("flights", [])
    if not flights:
        return {"error": f"No flights for {flight}",
                "source": "ATFM Inference"}

    flt = flights[0]
    atfm = _infer_atfm(flt)

    out = {
        "source": "ATFM Inference (Eurocontrol heuristic)",
        "timestamp": now_utc(),
        "flight": flight,
        "route": (f"{flt.get('origin', {}).get('code', '')} → "
                  f"{flt.get('destination', {}).get('code', '')}"),
        "scheduled_departure": flt.get("scheduled_out"),
        "estimated_departure": flt.get("estimated_out"),
        "scheduled_arrival": flt.get("scheduled_in"),
        "aircraft_type": flt.get("aircraft_type"),
    }
    out.update(atfm)

    return out


# =========================================================================
#  CLI
# =========================================================================

def build_parser():
    ap = argparse.ArgumentParser(
        description="Enhanced airport operations data for Pro Flight Tracker",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Subcommands:
  gairmet      G-AIRMET turbulence / wind-shear forecasts (NWS/AWC)
  tcf          TFM Convective Forecast — thunderstorm coverage driving
               FAA ground stops/reroutes (NWS/AWC)
  lightning    Real-time lightning near an airport (Blitzortung)
  rvr          Runway Visual Range per-runway (FAA)
  atfm-infer   Eurocontrol ATFM regulation inference from delay patterns
""",
    )
    sub = ap.add_subparsers(dest="cmd")

    # -- gairmet --
    ga = sub.add_parser("gairmet", help="G-AIRMET turbulence forecasts")
    ga.add_argument(
        "--hazard", nargs="+",
        choices=["turb-hi", "turb-lo", "llws", "ifr",
                 "mt-obsc", "sfc-wind", "ice"],
        help="Hazard types (default: turb-hi turb-lo llws)",
    )
    ga.add_argument(
        "--route", nargs=2, metavar=("ORIGIN", "DEST"),
        help="Filter to route between two ICAO airports",
    )

    # -- tcf --
    tc = sub.add_parser("tcf", help="TFM Convective Forecast (2-6h "
                                    "thunderstorm coverage/confidence)")
    tc.add_argument(
        "--route", nargs=2, metavar=("ORIGIN", "DEST"),
        help="Filter to route between two ICAO airports",
    )

    # -- lightning --
    lt = sub.add_parser("lightning", help="Real-time lightning near airport")
    lt.add_argument("--icao", required=True, help="Airport ICAO code")
    lt.add_argument("--radius", type=int, default=20,
                    help="Search radius NM (default 20)")
    lt.add_argument("--duration", type=int, default=30,
                    help="Collection window seconds (default 30)")

    # -- rvr --
    rv = sub.add_parser("rvr", help="Runway Visual Range (FAA)")
    rv.add_argument("--airport", required=True,
                    help="Airport code (ICAO or IATA)")
    rv.add_argument("--raw-metar",
                    help="Optional METAR string to parse for RVR groups")

    # -- atfm-infer --
    at = sub.add_parser("atfm-infer",
                        help="Eurocontrol ATFM regulation inference")
    at.add_argument("--flight", required=True, help="Flight (e.g. DL182)")
    at.add_argument("--date", help="YYYY-MM-DD")

    return ap


def dispatch(args) -> dict:
    """Run a parsed command in-process and return its payload.

    The cmd_* functions used to print JSON themselves; they now return
    dicts so app.py can import this script as a module instead of
    spawning it. main() still prints for the CLI.
    """
    return {"gairmet": cmd_gairmet,
            "tcf": cmd_tcf,
            "lightning": cmd_lightning,
            "rvr": cmd_rvr,
            "atfm-infer": cmd_atfm_infer}[args.cmd](args)


def main():
    ap = build_parser()
    args = ap.parse_args()
    if not args.cmd:
        ap.print_help()
        sys.exit(1)

    result = dispatch(args)
    json.dump(result, sys.stdout, indent=2)
    print()


if __name__ == "__main__":
    main()
