#!/usr/bin/env python3
"""
aviation_weather.py — Aviation weather data tool for Pro Flight Tracker.

Pulls aviation-specific weather from free government APIs:
  - METAR (current observations)        aviationweather.gov
  - TAF   (terminal area forecasts)     aviationweather.gov
  - SIGMET / Convective SIGMET          aviationweather.gov  (domestic, CONUS)
  - ISIGMET (international SIGMET)      aviationweather.gov  (AK/HI/Pacific + non-US FIRs)
  - PIREP (pilot reports)               aviationweather.gov
  - FAA airport delay status            nasstatus.faa.gov

Stdlib-only Python. All output is JSON to stdout.

Usage:
    python3 aviation_weather.py metar   --icao KJFK KLGA
    python3 aviation_weather.py taf     --icao KJFK
    python3 aviation_weather.py sigmet  [--type convective]
    python3 aviation_weather.py isigmet [--hazard turb|ice]
    python3 aviation_weather.py pirep   --icao KJFK [--distance 200]
    python3 aviation_weather.py faa-status --icao KJFK KLGA
    python3 aviation_weather.py brief   --origin KJFK --dest KLAX
"""

import argparse
import json
import os
import sys
import time
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

AWX_BASE = "https://aviationweather.gov/api/data"
NASSTATUS_URL = "https://nasstatus.faa.gov/api/airport-status-information"
FLY_FAA_URL = "https://fly.faa.gov/flyfaa/api/agg_status"

USER_AGENT = os.environ.get("WEATHER_USER_AGENT", "FlightTracker/1.0")
REQUEST_TIMEOUT = 10  # seconds
MAX_RETRIES = 2
RETRY_BACKOFF = 0.5

# ICAO -> IATA mapping for common US airports (FAA uses IATA)
ICAO_TO_IATA = {
    "KJFK": "JFK", "KLGA": "LGA", "KEWR": "EWR", "KLAX": "LAX",
    "KSFO": "SFO", "KORD": "ORD", "KMDW": "MDW", "KATL": "ATL",
    "KDFW": "DFW", "KDEN": "DEN", "KBOS": "BOS", "KPHL": "PHL",
    "KDCA": "DCA", "KIAD": "IAD", "KBWI": "BWI", "KMIA": "MIA",
    "KFLL": "FLL", "KTPA": "TPA", "KMCO": "MCO", "KIAH": "IAH",
    "KHOU": "HOU", "KAUS": "AUS", "KSAN": "SAN", "KSEA": "SEA",
    "KPDX": "PDX", "KPHX": "PHX", "KLAS": "LAS", "KSLC": "SLC",
    "KMSP": "MSP", "KDTW": "DTW", "KCLT": "CLT", "KRDU": "RDU",
    "KPIT": "PIT", "KCLE": "CLE", "KSTL": "STL", "KMKE": "MKE",
    "KSMF": "SMF", "KSJC": "SJC", "KOAK": "OAK", "KBNA": "BNA",
    "KMEM": "MEM", "KMSY": "MSY", "KSAT": "SAT", "KABQ": "ABQ",
    "KHON": "HNL", "PHNL": "HNL", "PANC": "ANC",
}


# ---------------------------------------------------------------------------
# HTTP helpers
# ---------------------------------------------------------------------------

def _fetch(url: str, accept: str = "application/json") -> bytes:
    """Fetch URL with retries. Returns raw bytes."""
    headers = {"User-Agent": USER_AGENT, "Accept": accept}
    last_err = None
    for attempt in range(MAX_RETRIES):
        try:
            req = Request(url, headers=headers)
            with urlopen(req, timeout=REQUEST_TIMEOUT) as resp:
                return resp.read()
        except (HTTPError, URLError, OSError) as exc:
            last_err = exc
            if attempt < MAX_RETRIES - 1:
                time.sleep(RETRY_BACKOFF * (attempt + 1))
    raise last_err  # type: ignore[misc]


def _fetch_json(url: str) -> object:
    """Fetch URL and decode JSON."""
    raw = _fetch(url, accept="application/json")
    return json.loads(raw)


def _fetch_xml(url: str) -> ET.Element:
    """Fetch URL and parse XML."""
    raw = _fetch(url, accept="application/xml, text/xml, */*")
    return ET.fromstring(raw)


def _ts_to_iso(ts) -> str | None:
    """Convert Unix timestamp (int or float) to ISO UTC string."""
    if ts is None:
        return None
    try:
        return datetime.fromtimestamp(int(ts), tz=timezone.utc).isoformat()
    except (ValueError, TypeError, OSError):
        return str(ts)


def _safe(val, default=None):
    """Return val if not None/empty, else default."""
    if val is None or val == "":
        return default
    return val


# ---------------------------------------------------------------------------
# METAR
# ---------------------------------------------------------------------------

def fetch_metar(icao_list: list[str]) -> dict:
    """Fetch and parse METAR observations for one or more ICAO stations."""
    ids = ",".join(icao_list)
    url = f"{AWX_BASE}/metar?ids={ids}&format=json"
    errors = []
    results = {}

    try:
        data = _fetch_json(url)
    except Exception as exc:
        return {"data": {}, "errors": [f"METAR fetch failed: {exc}"]}

    if not isinstance(data, list):
        return {"data": {}, "errors": ["METAR: unexpected response format"]}

    for ob in data:
        icao = ob.get("icaoId", "UNKN")
        clouds = []
        for c in ob.get("clouds", []) or []:
            clouds.append({
                "coverage": _safe(c.get("cover")),
                "base_agl_ft": _safe(c.get("base")),
                "type": _safe(c.get("type")),
            })

        # Determine ceiling (lowest BKN or OVC layer)
        ceiling = None
        for c in clouds:
            cov = (c.get("coverage") or "").upper()
            if cov in ("BKN", "OVC", "VV"):
                base = c.get("base_agl_ft")
                if base is not None and (ceiling is None or base < ceiling):
                    ceiling = base

        # Visibility — can be numeric or string like "10+"
        vis_raw = ob.get("visib")
        if vis_raw is not None:
            vis_str = str(vis_raw)
        else:
            vis_str = None

        # Weather phenomena from raw METAR
        wx_string = _safe(ob.get("wxString"))

        results[icao] = {
            "raw": _safe(ob.get("rawOb")),
            "observation_time": _safe(ob.get("reportTime")),
            "obs_epoch": _safe(ob.get("obsTime")),
            "metar_type": _safe(ob.get("metarType")),
            "wind": {
                "direction_deg": _safe(ob.get("wdir")),
                "speed_kts": _safe(ob.get("wspd")),
                "gust_kts": _safe(ob.get("wgst")),
            },
            "visibility_sm": vis_str,
            "ceiling_ft": ceiling,
            "clouds": clouds,
            "temperature_c": _safe(ob.get("temp")),
            "dewpoint_c": _safe(ob.get("dewp")),
            "altimeter_mb": _safe(ob.get("altim")),
            "slp_mb": _safe(ob.get("slp")),
            "flight_category": _safe(ob.get("fltCat")),
            "weather_phenomena": wx_string,
            "station": {
                "name": _safe(ob.get("name")),
                "lat": _safe(ob.get("lat")),
                "lon": _safe(ob.get("lon")),
                "elevation_m": _safe(ob.get("elev")),
            },
        }

    # Note any requested stations missing from response
    for icao in icao_list:
        if icao not in results:
            errors.append(f"No METAR returned for {icao}")

    return {"data": results, "errors": errors}


# ---------------------------------------------------------------------------
# TAF
# ---------------------------------------------------------------------------

def fetch_taf(icao_list: list[str]) -> dict:
    """Fetch and parse TAF forecasts for one or more ICAO stations."""
    ids= ",".join(icao_list)
    url = f"{AWX_BASE}/taf?ids={ids}&format=json"
    errors = []
    coverage_gaps = []
    results = {}

    try:
        data = _fetch_json(url)
    except Exception as exc:
        return {"data": {}, "errors": [f"TAF fetch failed: {exc}"],
                "coverage_gaps": []}

    if not isinstance(data, list):
        return {"data": {}, "errors": ["TAF: unexpected response format"],
                "coverage_gaps": []}

    for taf in data:
        icao = taf.get("icaoId", "UNKN")

        valid_from = _safe(taf.get("validTimeFrom"))
        valid_to = _safe(taf.get("validTimeTo"))

        # Parse forecast periods
        periods = []
        for fcst in taf.get("fcsts", []) or []:
            clouds = []
            for c in fcst.get("clouds", []) or []:
                clouds.append({
                    "coverage": _safe(c.get("cover")),
                    "base_agl_ft": _safe(c.get("base")),
                    "type": _safe(c.get("type")),
                })

            # Ceiling from this period
            ceiling = None
            for c in clouds:
                cov = (c.get("coverage") or "").upper()
                if cov in ("BKN", "OVC", "VV"):
                    base = c.get("base_agl_ft")
                    if base is not None and (ceiling is None or base < ceiling):
                        ceiling = base

            vis_raw = fcst.get("visib")

            periods.append({
                "time_from": _ts_to_iso(fcst.get("timeFrom")),
                "time_to": _ts_to_iso(fcst.get("timeTo")),
                "change_indicator": _safe(fcst.get("fcstChange")),
                "probability": _safe(fcst.get("probability")),
                "time_becoming": _ts_to_iso(fcst.get("timeBec")),
                "wind": {
                    "direction_deg": _safe(fcst.get("wdir")),
                    "speed_kts": _safe(fcst.get("wspd")),
                    "gust_kts": _safe(fcst.get("wgst")),
                },
                "visibility_sm": str(vis_raw) if vis_raw is not None else None,
                "ceiling_ft": ceiling,
                "clouds": clouds,
                "weather": _safe(fcst.get("wxString")),
                "vertical_vis_ft": _safe(fcst.get("vertVis")),
                "wind_shear": {
                    "height_ft": _safe(fcst.get("wshearHgt")),
                    "direction_deg": _safe(fcst.get("wshearDir")),
                    "speed_kts": _safe(fcst.get("wshearSpd")),
                } if any(fcst.get(k) for k in ("wshearHgt", "wshearDir", "wshearSpd")) else None,
            })

        # Check TAF coverage — standard TAF covers 24-30h
        if valid_to:
            valid_to_dt = datetime.fromtimestamp(int(valid_to), tz=timezone.utc)
            hours_coverage = (valid_to_dt - datetime.now(timezone.utc)).total_seconds() / 3600
            if hours_coverage < 6:
                coverage_gaps.append(
                    f"TAF for {icao} expires in {hours_coverage:.0f}h — "
                    f"limited forward coverage"
                )

        results[icao] = {
            "raw": _safe(taf.get("rawTAF")),
            "issue_time": _safe(taf.get("issueTime")),
            "bulletin_time": _safe(taf.get("bulletinTime")),
            "valid_from": _ts_to_iso(valid_from),
            "valid_to": _ts_to_iso(valid_to),
            "forecast_periods": periods,
            "remarks": _safe(taf.get("remarks")),
        }

    for icao in icao_list:
        if icao not in results:
            errors.append(f"No TAF returned for {icao}")
            coverage_gaps.append(
                f"No TAF available for {icao} — forecast gap"
            )

    return {"data": results, "errors": errors, "coverage_gaps": coverage_gaps}


# ---------------------------------------------------------------------------
# SIGMET / Convective SIGMET
# ---------------------------------------------------------------------------

def fetch_sigmet(sigmet_type: str = "sigmet") -> dict:
    """Fetch active SIGMETs or Convective SIGMETs.

    sigmet_type: 'sigmet', 'conv' (convective), or 'all'
    """
    # Map user-friendly names to API parameter
    type_map = {
        "sigmet": "sigmet",
        "convective": "conv",
        "conv": "conv",
        "all": "sigmet",  # will fetch both
    }
    api_type = type_map.get(sigmet_type.lower(), "sigmet")

    urls = []
    if sigmet_type.lower() == "all":
        urls.append(("sigmet", f"{AWX_BASE}/airsigmet?format=json&type=sigmet"))
        urls.append(("convective", f"{AWX_BASE}/airsigmet?format=json&type=conv"))
    else:
        label = "convective" if api_type == "conv" else "sigmet"
        urls.append((label, f"{AWX_BASE}/airsigmet?format=json&type={api_type}"))

    errors = []
    all_sigmets = []

    for label, url in urls:
        try:
            data = _fetch_json(url)
        except Exception as exc:
            errors.append(f"SIGMET ({label}) fetch failed: {exc}")
            continue

        if not isinstance(data, list):
            errors.append(f"SIGMET ({label}): unexpected response format")
            continue

        for sig in data:
            # Parse coordinate polygon
            coords = []
            for c in sig.get("coords", []) or []:
                coords.append({
                    "lat": c.get("lat"),
                    "lon": c.get("lon"),
                })

            all_sigmets.append({
                "id": _safe(sig.get("seriesId")),
                "source": _safe(sig.get("icaoId")),
                "type": _safe(sig.get("airSigmetType")),
                "hazard": _safe(sig.get("hazard")),
                "severity": _safe(sig.get("severity")),
                "valid_from": _ts_to_iso(sig.get("validTimeFrom")),
                "valid_to": _ts_to_iso(sig.get("validTimeTo")),
                "expires_in_minutes": _expiry_minutes(sig.get("validTimeTo")),
                "altitude_hi_ft": _safe(sig.get("altitudeHi1")),
                "altitude_hi2_ft": _safe(sig.get("altitudeHi2")),
                "altitude_low_ft": _safe(sig.get("altitudeLow1")),
                "altitude_low2_ft": _safe(sig.get("altitudeLow2")),
                "movement": {
                    "direction_deg": _safe(sig.get("movementDir")),
                    "speed_kts": _safe(sig.get("movementSpd")),
                } if sig.get("movementDir") is not None else None,
                "area_coords": coords if coords else None,
                "raw": _safe(sig.get("rawAirSigmet")),
                "alpha_char": _safe(sig.get("alphaChar")),
            })

    return {
        "data": all_sigmets,
        "count": len(all_sigmets),
        "errors": errors,
    }


def _expiry_minutes(valid_to_ts) -> int | None:
    """Minutes until expiration. Negative = already expired."""
    if valid_to_ts is None:
        return None
    try:
        exp = datetime.fromtimestamp(int(valid_to_ts), tz=timezone.utc)
        delta = (exp - datetime.now(timezone.utc)).total_seconds() / 60
        return round(delta)
    except (ValueError, TypeError, OSError):
        return None


# ---------------------------------------------------------------------------
# International SIGMET
# ---------------------------------------------------------------------------
#
# /airsigmet only covers the contiguous US. Alaska, Hawaii/Pacific, and every
# non-US FIR are blind spots for that endpoint — this fills them from
# /isigmet, which has no domestic/international split of its own and simply
# returns whatever is globally active.

def fetch_isigmet(hazard: str = None) -> dict:
    """Fetch active international SIGMETs (ISIGMETs).

    hazard: optional filter, 'turb' or 'ice'. None fetches all hazards.
    """
    url = f"{AWX_BASE}/isigmet?format=json"
    if hazard:
        url += f"&hazard={hazard}"

    errors = []
    all_isigmets = []

    try:
        data = _fetch_json(url)
    except Exception as exc:
        return {"data": [], "count": 0,
                "errors": [f"ISIGMET fetch failed: {exc}"]}

    if not isinstance(data, list):
        return {"data": [], "count": 0,
                "errors": ["ISIGMET: unexpected response format"]}

    for sig in data:
        coords = []
        for c in sig.get("coords", []) or []:
            coords.append({
                "lat": c.get("lat"),
                "lon": c.get("lon"),
            })

        all_isigmets.append({
            "id": _safe(sig.get("seriesId")),
            "issuing_office": _safe(sig.get("icaoId")),
            "fir_id": _safe(sig.get("firId")),
            "fir_name": _safe(sig.get("firName")),
            "hazard": _safe(sig.get("hazard")),
            "qualifier": _safe(sig.get("qualifier")),
            "valid_from": _ts_to_iso(sig.get("validTimeFrom")),
            "valid_to": _ts_to_iso(sig.get("validTimeTo")),
            "expires_in_minutes": _expiry_minutes(sig.get("validTimeTo")),
            "base_ft": _safe(sig.get("base")),
            "top_ft": _safe(sig.get("top")),
            "geometry_type": _safe(sig.get("geom")),
            "movement": {
                "direction": _safe(sig.get("dir")),
                "speed_kts": _safe(sig.get("spd")),
            } if sig.get("dir") is not None or sig.get("spd") is not None else None,
            "change": _safe(sig.get("chng")),
            "area_coords": coords if coords else None,
            "raw": _safe(sig.get("rawSigmet")),
        })

    return {
        "data": all_isigmets,
        "count": len(all_isigmets),
        "errors": errors,
    }


# ---------------------------------------------------------------------------
# PIREPs
# ---------------------------------------------------------------------------

def fetch_pirep(icao_list: list[str], distance_nm: int = 200) -> dict:
    """Fetch Pilot Reports near airports."""
    errors = []
    all_pireps = []
    seen_raw = set()  # deduplicate across stations

    for icao in icao_list:
        url = f"{AWX_BASE}/pirep?format=json&id={icao}&distance={distance_nm}"
        try:
            data = _fetch_json(url)
        except Exception as exc:
            errors.append(f"PIREP fetch failed for {icao}: {exc}")
            continue

        if not isinstance(data, list):
            errors.append(f"PIREP for {icao}: unexpected response format")
            continue

        for p in data:
            raw = p.get("rawOb", "")
            if raw in seen_raw:
                continue
            seen_raw.add(raw)

            # Build turbulence entries
            turbulence = []
            for i in (1, 2):
                intensity = _safe(p.get(f"tbInt{i}"))
                if intensity:
                    turbulence.append({
                        "intensity": intensity,
                        "type": _safe(p.get(f"tbType{i}")),
                        "frequency": _safe(p.get(f"tbFreq{i}")),
                        "base_fl": _safe(p.get(f"tbBas{i}")),
                        "top_fl": _safe(p.get(f"tbTop{i}")),
                    })

            # Build icing entries
            icing = []
            for i in (1, 2):
                intensity = _safe(p.get(f"icgInt{i}"))
                if intensity:
                    icing.append({
                        "intensity": intensity,
                        "type": _safe(p.get(f"icgType{i}")),
                        "base_fl": _safe(p.get(f"icgBas{i}")),
                        "top_fl": _safe(p.get(f"icgTop{i}")),
                    })

            report_type = _safe(p.get("pirepType"), "PIREP")
            # UUA = urgent, UA/PIREP = routine
            urgency = "URGENT" if report_type == "UUA" else "ROUTINE"

            all_pireps.append({
                "report_type": report_type,
                "urgency": urgency,
                "observation_time": _ts_to_iso(p.get("obsTime")),
                "receipt_time": _safe(p.get("receiptTime")),
                "location": {
                    "icao": _safe(p.get("icaoId")),
                    "lat": _safe(p.get("lat")),
                    "lon": _safe(p.get("lon")),
                },
                "flight_level": _safe(p.get("fltLvl")),
                "flight_level_type": _safe(p.get("fltLvlType")),
                "aircraft_type": _safe(p.get("acType")),
                "turbulence": turbulence if turbulence else None,
                "icing": icing if icing else None,
                "sky_conditions": _safe(p.get("clouds")),
                "visibility": _safe(p.get("visib")),
                "weather": _safe(p.get("wxString")),
                "temperature_c": _safe(p.get("temp")),
                "wind": {
                    "direction_deg": _safe(p.get("wdir")),
                    "speed_kts": _safe(p.get("wspd")),
                } if p.get("wdir") is not None else None,
                "vertical_gust": _safe(p.get("vertGust")),
                "raw": raw,
            })

    return {
        "data": all_pireps,
        "count": len(all_pireps),
        "query": {"stations": icao_list, "distance_nm": distance_nm},
        "errors": errors,
    }


# ---------------------------------------------------------------------------
# FAA Airport Status
# ---------------------------------------------------------------------------

def fetch_faa_status(icao_list: list[str]) -> dict:
    """Fetch FAA delay/status information for airports.

    Primary source: nasstatus.faa.gov XML API (returns ALL active programs).
    Fallback: fly.faa.gov per-airport API.
    """
    errors = []
    results = {}

    # Build the set of identifiers we care about (both ICAO and IATA)
    target_icao = set(icao_list)
    target_iata = set()
    for icao in icao_list:
        iata = ICAO_TO_IATA.get(icao)
        if iata:
            target_iata.add(iata)
        else:
            # For unknown airports, try stripping leading K
            if icao.startswith("K") and len(icao) == 4:
                target_iata.add(icao[1:])

    # Initialize results for all requested airports
    for icao in icao_list:
        results[icao] = {
            "ground_delay_programs": [],
            "ground_stops": [],
            "arrival_departure_delays": [],
            "closures": [],
            "status": "NO_ACTIVE_DELAYS",
            "source": None,
        }

    # Try primary NASSTATUS API
    try:
        root = _fetch_xml(NASSTATUS_URL)
        _parse_nasstatus_xml(root, results, target_icao, target_iata, icao_list)
    except Exception as exc:
        errors.append(f"NASSTATUS primary API failed: {exc}")

        # Fallback: try fly.faa.gov for each airport
        for icao in icao_list:
            iata = ICAO_TO_IATA.get(icao)
            if not iata:
                if icao.startswith("K") and len(icao) == 4:
                    iata = icao[1:]
                else:
                    continue
            try:
                _fetch_flyfaa(iata, icao, results)
            except Exception as exc2:
                errors.append(f"fly.faa.gov fallback failed for {iata}: {exc2}")

    # Set overall status
    for icao in icao_list:
        r = results[icao]
        if r["closures"]:
            r["status"] = "CLOSED"
        elif r["ground_stops"]:
            r["status"] = "GROUND_STOP"
        elif r["ground_delay_programs"]:
            r["status"] = "GDP_ACTIVE"
        elif r["arrival_departure_delays"]:
            r["status"] = "DELAYS"
        else:
            r["status"] = "NO_ACTIVE_DELAYS"

    return {"data": results, "errors": errors}


def _match_airport(arpt_code: str, target_icao: set, target_iata: set,
                   icao_list: list[str]) -> str | None:
    """Match an airport code from FAA data to one of our requested ICAO codes."""
    arpt = arpt_code.strip().upper()

    # Direct ICAO match
    if arpt in target_icao:
        return arpt

    # IATA match -> find corresponding ICAO
    if arpt in target_iata:
        for icao in icao_list:
            iata = ICAO_TO_IATA.get(icao)
            if iata == arpt:
                return icao
            if icao.startswith("K") and icao[1:] == arpt:
                return icao

    return None


def _parse_nasstatus_xml(root: ET.Element, results: dict,
                         target_icao: set, target_iata: set,
                         icao_list: list[str]) -> None:
    """Parse the NASSTATUS XML response into results dict.

    Real XML structure (verified Aug 2026):
        AIRPORT_STATUS_INFORMATION
          Delay_type
            Name = "Ground Delay Programs"
            Ground_Delay_List
              Ground_Delay
                ARPT / Reason / Avg / Max
          Delay_type
            Name = "General Arrival/Departure Delay Info"
            Arrival_Departure_Delay_List
              Delay
                ARPT / Reason
                Arrival_Departure
                  Min / Max / Trend
          Delay_type
            Name = "Ground Stops"
            Ground_Stop_List
              Ground_Stop
                ARPT / Reason / End / EndTime
          Delay_type
            Name = "Airport Closures"
            Airport_Closure_List
              Airport
                ARPT / Reason / Start / Reopen
    """
    for dtype in root.findall("Delay_type"):
        name = _xt(dtype, "Name") or ""
        name_lower = name.lower()

        # --- Ground Delay Programs ---
        if "ground delay" in name_lower:
            for gdl in dtype.iter("Ground_Delay_List"):
                for gd in gdl.findall("Ground_Delay"):
                    arpt = _xt(gd, "ARPT")
                    if not arpt:
                        continue
                    icao = _match_airport(arpt, target_icao, target_iata,
                                          icao_list)
                    if not icao:
                        continue
                    results[icao]["ground_delay_programs"].append({
                        "airport": arpt,
                        "reason": _xt(gd, "Reason"),
                        "average_delay": _xt(gd, "Avg"),
                        "max_delay": _xt(gd, "Max"),
                    })
                    results[icao]["source"] = "nasstatus"

        # --- General Arrival / Departure Delays ---
        elif "arrival" in name_lower or "departure" in name_lower:
            for adl in dtype.iter("Arrival_Departure_Delay_List"):
                for delay in adl.findall("Delay"):
                    arpt = _xt(delay, "ARPT")
                    if not arpt:
                        continue
                    icao = _match_airport(arpt, target_icao, target_iata,
                                          icao_list)
                    if not icao:
                        continue

                    ad_elem = delay.find("Arrival_Departure")
                    if ad_elem is not None:
                        delay_info = {
                            "airport": arpt,
                            "reason": _xt(delay, "Reason"),
                            "min_delay": _xt(ad_elem, "Min"),
                            "max_delay": _xt(ad_elem, "Max"),
                            "trend": _xt(ad_elem, "Trend"),
                        }
                    else:
                        delay_info = {
                            "airport": arpt,
                            "reason": _xt(delay, "Reason"),
                        }
                    results[icao]["arrival_departure_delays"].append(
                        delay_info)
                    results[icao]["source"] = "nasstatus"

        # --- Ground Stops ---
        elif "ground stop" in name_lower:
            for gsl in dtype.iter("Ground_Stop_List"):
                for gs in gsl.findall("Ground_Stop"):
                    arpt = _xt(gs, "ARPT")
                    if not arpt:
                        continue
                    icao = _match_airport(arpt, target_icao, target_iata,
                                          icao_list)
                    if not icao:
                        continue
                    results[icao]["ground_stops"].append({
                        "airport": arpt,
                        "reason": _xt(gs,"Reason"),
                        "expected_end": _xt(gs, "End") or _xt(gs, "EndTime"),
                    })
                    results[icao]["source"] = "nasstatus"

        # --- Airport Closures ---
        elif "closure" in name_lower:
            for acl in dtype.iter("Airport_Closure_List"):
                for ap in acl.findall("Airport"):
                    arpt = _xt(ap, "ARPT")
                    if not arpt:
                        continue
                    icao = _match_airport(arpt, target_icao, target_iata,
                                          icao_list)
                    if not icao:
                        continue
                    results[icao]["closures"].append({
                        "airport": arpt,
                        "reason": _xt(ap, "Reason"),
                        "start": _xt(ap, "Start"),
                        "reopen": _xt(ap, "Reopen"),
                    })
                    results[icao]["source"] = "nasstatus"


def _xt(elem: ET.Element, child_tag: str) -> str | None:
    """Get text content of an XML child element."""
    child = elem.find(child_tag)
    if child is not None and child.text:
        return child.text.strip()
    return None


def _fetch_flyfaa(iata: str, icao: str, results: dict) -> None:
    """Fallback: fetch from fly.faa.gov per-airport API."""
    url = f"{FLY_FAA_URL}?airport={iata}"
    data = _fetch_json(url)

    if not isinstance(data, dict):
        return

    # This API returns varied structures — handle gracefully
    status = data.get("status", {})
    if isinstance(status, dict):
        reason = status.get("reason")
        delay_type = status.get("type")
        avg_delay = status.get("avgDelay")
        if reason and reason.lower() not in ("", "no known delays"):
            results[icao]["arrival_departure_delays"].append({
                "airport": iata,
                "reason": reason,
                "type": delay_type,
                "average_delay": avg_delay,
            })

    results[icao]["source"] = "fly.faa.gov"


# ---------------------------------------------------------------------------
# Full Briefing (parallel)
# ---------------------------------------------------------------------------

def fetch_brief(origin: str, dest: str) -> dict:
    """Full aviation weather briefing: METAR + TAF + SIGMET + PIREP + FAA
    for origin and destination airports, all fetched in parallel."""
    errors = []
    coverage_gaps = []
    data = {}

    stations = [origin, dest]

    with ThreadPoolExecutor(max_workers=7) as pool:
        futures = {
            pool.submit(fetch_metar, stations): "metar",
            pool.submit(fetch_taf, stations): "taf",
            pool.submit(fetch_sigmet, "all"): "sigmet",
            pool.submit(fetch_isigmet): "isigmet",
            pool.submit(fetch_pirep, stations, 200): "pirep",
            pool.submit(fetch_faa_status, stations): "faa_status",
        }

        for future in as_completed(futures):
            key = futures[future]
            try:
                result = future.result()
                data[key] = result.get("data", result)
                errors.extend(result.get("errors", []))
                coverage_gaps.extend(result.get("coverage_gaps", []))
            except Exception as exc:
                errors.append(f"{key} failed: {exc}")
                data[key] = None

    return {
        "origin": origin,
        "destination": dest,
        "data": data,
        "errors": errors,
        "coverage_gaps": coverage_gaps,
    }


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="aviation_weather",
        description="Aviation weather data for Pro Flight Tracker",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    # metar
    p_metar = sub.add_parser("metar", help="Fetch METAR observations")
    p_metar.add_argument("--icao", nargs="+", required=True,
                         help="ICAO airport codes (e.g. KJFK KLGA)")

    # taf
    p_taf = sub.add_parser("taf", help="Fetch TAF forecasts")
    p_taf.add_argument("--icao", nargs="+", required=True,
                       help="ICAO airport codes")

    # sigmet
    p_sig = sub.add_parser("sigmet", help="Fetch SIGMETs")
    p_sig.add_argument("--type", default="all",
                       choices=["sigmet", "convective", "conv", "all"],
                       help="SIGMET type (default: all)")

    # isigmet
    p_isig = sub.add_parser("isigmet", help="Fetch international SIGMETs "
                                             "(AK/HI/Pacific + non-US FIRs)")
    p_isig.add_argument("--hazard", default=None, choices=["turb", "ice"],
                        help="Filter by hazard (default: all)")

    # pirep
    p_pirep = sub.add_parser("pirep", help="Fetch PIREPs")
    p_pirep.add_argument("--icao", nargs="+", required=True,
                         help="ICAO airport codes")
    p_pirep.add_argument("--distance", type=int, default=200,
                         help="Search radius in NM (default: 200)")

    # faa-status
    p_faa = sub.add_parser("faa-status", help="Fetch FAA delay status")
    p_faa.add_argument("--icao", nargs="+", required=True,
                       help="ICAO airport codes")

    # brief
    p_brief = sub.add_parser("brief",
                             help="Full aviation weather briefing")
    p_brief.add_argument("--origin", required=True,
                         help="Origin ICAO code")
    p_brief.add_argument("--dest", required=True,
                         help="Destination ICAO code")

    return parser


def dispatch(args) -> dict:
    """Run a parsed command in-process and return the CLI's output dict.

    Same payload as the CLI (pull_time + command + result), minus the
    printing — this is what app.py calls when it imports this script as a
    module instead of spawning it.
    """
    pull_time = datetime.now(timezone.utc).isoformat()

    if args.command == "metar":
        result = fetch_metar(args.icao)
    elif args.command == "taf":
        result = fetch_taf(args.icao)
    elif args.command == "sigmet":
        result = fetch_sigmet(args.type)
    elif args.command == "isigmet":
        result = fetch_isigmet(args.hazard)
    elif args.command == "pirep":
        result = fetch_pirep(args.icao, args.distance)
    elif args.command == "faa-status":
        result = fetch_faa_status(args.icao)
    elif args.command == "brief":
        result = fetch_brief(args.origin, args.dest)
    else:
        return {"error": f"Unknown command: {args.command!r}"}

    output = {
        "pull_time": pull_time,
        "command": args.command,
    }
    output.update(result)
    return output


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()

    output = dispatch(args)
    if output.get("error") and "Unknown command" in str(output.get("error")):
        parser.print_help()
        sys.exit(1)

    json.dump(output, sys.stdout, indent=2, default=str)
    sys.stdout.write("\n")


if __name__ == "__main__":
    main()
