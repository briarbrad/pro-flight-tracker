"""ATC flow-brief adapters — interpret SWIM TFMS-flow / TBFM / TFDM.

The raw SWIM scripts return Envelope C (`results[]`). The iOS client should
not have to re-derive "what does this mean for THIS flight." This module
turns those lists into:

  - advisories[]   {title, text, severity, airport, source, effective_*}
  - metering       {applicable, items[] with fix/eta/status}
  - surface        {applicable, airport, queue_wait_min, ...}
  - effects[]      same {cause, effect, severity, source} shape as /api/brief

Quiet feeds (empty capture, undeployed TFDM, non-US TBFM) are empty
structures, never errors. The Flask handler is responsible for bounded
timeouts; this module only interprets whatever came back.
"""

from __future__ import annotations

# Mirrors scripts/swim_consumer.py TFDM_AIRPORTS (Aug 2026). JFK/LGA are
# intentionally absent — empty TFDM at those airports is expected.
TFDM_AIRPORTS = frozenset({
    "KMIA", "KLAX", "KCLT", "KSFO", "KDCA", "KIAD", "KSAT", "KEWR",
    "KSEA", "KLAS", "KTEB", "KSAN", "KIAH", "KPHX", "KFLL", "KOAK",
    "KRDU", "KHOU", "KIND", "KSJC", "KCLE", "KHPN", "KAUS", "KRSW",
    "KDAY", "KCMH", "KMDW", "KGEG",
})

_ACTION_MARKERS = (
    "GROUND STOP", "GROUNDSTOP", "GROUND STOPPAGE",
)
_WATCH_MARKERS = (
    "GDP", "GROUND DELAY", "MILES IN TRAIL", "MILES-IN-TRAIL",
    " MIT ", "MIT/", "HOLDING", "GROUND DELAY PROGRAM",
)


def is_tfdm_airport(icao: str | None) -> bool:
    return bool(icao) and icao.upper() in TFDM_AIRPORTS


def is_us_nas(icao: str | None) -> bool:
    """TFMS/TBFM are CONUS NAS products (K-prefix)."""
    return bool(icao) and icao.upper().startswith("K") and len(icao) == 4


def _norm(code: str | None) -> str:
    return (code or "").upper().strip()


def _airport3(icao: str | None) -> str:
    c = _norm(icao)
    if len(c) == 4 and c.startswith("K"):
        return c[1:]
    return c


def _results(payload) -> list:
    """Pull `results[]` from a SWIM envelope, treating errors as empty."""
    if not isinstance(payload, dict):
        return []
    raw = payload.get("results")
    return [r for r in raw if isinstance(r, dict)] if isinstance(raw, list) else []


def _blob(*parts) -> str:
    return " ".join(str(p or "") for p in parts).upper()


def _mentions_airport(text: str, icao: str | None) -> bool:
    if not icao:
        return False
    t = (text or "").upper()
    code = _norm(icao)
    three = _airport3(icao)
    return bool(code and code in t) or bool(three and len(three) == 3 and three in t)


def _advisory_airport(record: dict, origin: str | None, dest: str | None) -> str:
    """Best-effort airport this advisory is about."""
    candidates = [
        record.get("origin"),
        record.get("arr_airport"),
        record.get("dep_airport"),
        record.get("element"),
        record.get("control_element"),
        record.get("aerodrome"),
    ]
    text = _blob(record.get("title"), record.get("text"), *candidates)
    for apt in (dest, origin):
        if apt and _mentions_airport(text, apt):
            return _norm(apt)
    for c in candidates:
        c = _norm(c)
        if len(c) in (3, 4) and c.isalnum():
            return c if len(c) == 4 else ("K" + c if c.isalpha() else c)
    return ""


def _severity_from_text(title: str, text: str, msg_type: str = "") -> str:
    blob = _blob(title, text, msg_type)
    # Pad so short tokens like "MIT" / "GS" can match at edges.
    padded = f" {blob} "
    if any(m in blob for m in _ACTION_MARKERS) or " GS " in padded:
        return "ACTION"
    if any(m in padded or m in blob for m in _WATCH_MARKERS):
        return "WATCH"
    return "INFO"


def _callsign_match(record_id: str | None, flight: str | None) -> bool:
    if not flight or not record_id:
        return False
    a = record_id.upper().replace(" ", "")
    b = flight.upper().replace(" ", "")
    return b in a or a in b


def interpret_tfms_flow(payload, origin: str | None = None,
                        dest: str | None = None,
                        flight: str | None = None) -> dict:
    """Turn tfms-flow `results[]` into advisories + effects."""
    advisories = []
    effects = []
    seen = set()

    for rec in _results(payload):
        rtype = rec.get("type") or rec.get("msg_type") or ""
        title = rec.get("title") or ""
        text = rec.get("text") or ""
        airport = _advisory_airport(rec, origin, dest)
        source = "tfms-flow"

        if rtype in ("tfms_advisory", "GADV") or rec.get("msg_type") == "GADV":
            title = title or rec.get("advisory_number") or "TFMS advisory"
            text = text or ""
            sev = _severity_from_text(title, text, "GADV")
            key = ("gadv", rec.get("advisory_number"), title, rec.get("effective_start"))
            if key in seen:
                continue
            seen.add(key)
            adv = {
                "title": title.strip() or "TFMS advisory",
                "text": (text or "").strip(),
                "severity": sev,
                "airport": airport,
                "source": source,
                "effective_start": rec.get("effective_start") or None,
                "effective_end": rec.get("effective_end") or None,
                "kind": "advisory",
            }
            advisories.append(adv)
            effects.append(_advisory_effect(adv, origin, dest))

        elif rtype in ("tfms_restriction", "RSTR") or rec.get("msg_type") == "RSTR":
            element = rec.get("element") or rec.get("control_element") or ""
            mit = rec.get("mit_value")
            title = title or (
                f"Restriction {element}".strip() if element else "TFMS restriction")
            bits = []
            if rec.get("element_type"):
                bits.append(str(rec["element_type"]))
            if mit:
                bits.append(f"MIT {mit}")
            if rec.get("avg_delay_minutes"):
                bits.append(f"avg delay {rec['avg_delay_minutes']} min")
            text = text or (", ".join(bits) if bits else "Flow restriction")
            sev = _severity_from_text(title, text, "RSTR")
            if mit and sev == "INFO":
                sev = "WATCH"
            key = ("rstr", element, mit, rec.get("start_time"), rec.get("source_timestamp"))
            if key in seen:
                continue
            seen.add(key)
            adv = {
                "title": title.strip(),
                "text": text.strip(),
                "severity": sev,
                "airport": airport,
                "source": source,
                "effective_start": rec.get("start_time") or rec.get("effective_start") or None,
                "effective_end": rec.get("end_time") or rec.get("effective_end") or None,
                "kind": "restriction",
            }
            advisories.append(adv)
            effects.append(_advisory_effect(adv, origin, dest))

        elif rtype in ("tfms_tmi_flight", "TMI_FLIGHT_LIST") or rec.get("msg_type") == "TMI_FLIGHT_LIST":
            if flight and not _callsign_match(rec.get("flight_id"), flight):
                # Keep airport-level programs; only drop other flights' TMI rows.
                continue
            fcas = rec.get("fca_ids") or []
            title = title or (
                f"TMI / FCA assignment for {rec.get('flight_id') or flight or 'flight'}")
            text = text or (
                f"FCA {', '.join(fcas)}" if fcas else
                f"Status {rec.get('status') or 'assigned'}")
            sev = "WATCH"
            key = ("tmi", rec.get("flight_id"), tuple(fcas), rec.get("source_timestamp"))
            if key in seen:
                continue
            seen.add(key)
            adv = {
                "title": title.strip(),
                "text": text.strip(),
                "severity": sev,
                "airport": airport or _norm(rec.get("arr_airport") or rec.get("dep_airport")),
                "source": source,
                "effective_start": rec.get("entry_time") or None,
                "effective_end": rec.get("exit_time") or None,
                "kind": "tmi",
            }
            advisories.append(adv)
            effects.append(_advisory_effect(adv, origin, dest))

    return {
        "advisories": advisories,
        "effects": [e for e in effects if e],
        "count": len(advisories),
    }


def _advisory_effect(adv: dict, origin: str | None, dest: str | None) -> dict:
    airport = adv.get("airport") or ""
    at_origin = airport and origin and _norm(airport) == _norm(origin)
    at_dest = airport and dest and _norm(airport) == _norm(dest)
    where = ("at origin" if at_origin else
             "at destination" if at_dest else
             (f"at {airport}" if airport else "in the NAS"))
    sev = adv.get("severity") or "INFO"
    kind = adv.get("kind")
    title = adv.get("title") or "TFMS flow item"

    if kind == "restriction":
        effect = (
            f"Miles-in-trail / restriction {where} reduces throughput. "
            "Expect extra spacing on departure or arrival, not necessarily "
            "a cancelled flight."
        )
    elif sev == "ACTION" and at_dest:
        effect = (
            "A ground stop or equivalent at the destination holds departures "
            "bound there on the ground until the program lifts."
        )
    elif sev == "ACTION" and at_origin:
        effect = (
            "A ground stop at the origin primarily holds inbound arrivals. "
            "This departure sees congestion and late inbound equipment, "
            "not a direct hold of its own."
        )
    elif "GDP" in _blob(title) or "GROUND DELAY" in _blob(title):
        effect = (
            "A ground delay program meters arrivals into the named airport. "
            "A flight arriving there may receive an EDCT; a flight departing "
            "it sees only indirect congestion."
        )
    elif kind == "tmi":
        effect = (
            "This flight (or its city pair) is on a traffic-management "
            "initiative / FCA list. Expect a controlled time if one is "
            "assigned."
        )
    else:
        effect = f"Traffic-management advisory {where}."

    return {
        "cause": title,
        "effect": effect,
        "severity": sev,
        "source": "tfms-flow",
    }


def interpret_tbfm(payload, flight: str | None = None,
                   dest: str | None = None) -> dict:
    """Turn TBFM metering messages into a client-ready `metering` block."""
    items = []
    dest_n = _norm(dest)
    for rec in _results(payload):
        if dest_n:
            rec_dest = _norm(rec.get("dest_airport"))
            if rec_dest and rec_dest != dest_n and rec_dest != _airport3(dest):
                # Parser already filtered; keep a loose match on 3-letter.
                if rec_dest not in (dest_n, _airport3(dest)):
                    pass
        eta = rec.get("eta") if isinstance(rec.get("eta"), dict) else {}
        info = rec.get("flight_info") if isinstance(rec.get("flight_info"), dict) else {}
        fix = (info.get("mfx") or info.get("meterFix") or info.get("fix")
               or eta.get("fix") or eta.get("mfx") or rec.get("dest_airport") or "")
        eta_time = (eta.get("cta") or eta.get("sta") or eta.get("eta")
                    or eta.get("meterFixTime") or eta.get("tma") or eta.get("time"))
        status = rec.get("msg_type") or eta.get("status") or info.get("status") or ""
        items.append({
            "flight_id": rec.get("flight_id") or "",
            "fix": fix or None,
            "eta": eta_time or None,
            "status": status or None,
            "dest_airport": rec.get("dest_airport") or dest_n or None,
            "dep_airport": rec.get("dep_airport") or None,
            "this_flight": _callsign_match(rec.get("flight_id"), flight),
        })

    applicable = is_us_nas(dest) if dest else bool(items)
    note = None
    if dest and not is_us_nas(dest):
        applicable = False
        note = (f"{dest} is outside the US NAS — TBFM arrival metering "
                "is not published for this destination.")
    elif applicable and not items:
        note = ("No TBFM metering messages matched this capture window"
                + (f" for {flight}" if flight else "")
                + ". Empty is normal when the flight is not yet in the "
                  "arrival stream.")

    return {
        "applicable": applicable,
        "airport": dest_n or None,
        "items": items[:40],
        "count": len(items),
        "note": note,
    }


def interpret_tfdm(payload, airport: str | None = None,
                   flight: str | None = None) -> dict:
    """Turn TFDM surface messages into a single `surface` block.

    TFDM is not deployed everywhere. Callers should skip the JVM when
    `is_tfdm_airport` is false; this interpreter still returns a structured
    empty success if they pass a quiet/undeployed payload.
    """
    apt = _norm(airport)
    deployed = is_tfdm_airport(apt) if apt else False
    results = _results(payload)

    chosen = _pick_tfdm_record(results, flight)
    if chosen:
        queue = chosen.get("queue_wait_minutes")
        taxi = chosen.get("taxi_out_minutes")
        return {
            "applicable": True,
            "airport": apt or _norm(chosen.get("aerodrome") or chosen.get("dep_airport")),
            "queue_wait_min": queue,
            "estimated_taxi_out_min": taxi,
            "earliest_wheels_up": (chosen.get("runway_departure_earliest")
                                   or chosen.get("runway_departure_estimated")),
            "state": chosen.get("flight_state") or None,
            "flight_id": chosen.get("flight_id") or None,
            "runway": chosen.get("runway_assigned") or chosen.get("runway_predicted"),
            "note": None,
            "matched_this_flight": _callsign_match(chosen.get("flight_id"), flight),
        }

    if apt and not deployed:
        return {
            "applicable": False,
            "airport": apt,
            "queue_wait_min": None,
            "estimated_taxi_out_min": None,
            "earliest_wheels_up": None,
            "state": None,
            "note": (f"{apt} is not in the TFDM deployment set "
                     "(JFK/LGA are not live; KEWR is the NY-area airport). "
                     "Empty is success."),
        }

    return {
        "applicable": bool(deployed),
        "airport": apt or None,
        "queue_wait_min": None,
        "estimated_taxi_out_min": None,
        "earliest_wheels_up": None,
        "state": None,
        "note": ("TFDM capture was quiet — no surface-management messages "
                 "in the window. Empty is success; the feed is a firehose "
                 "and a short listen often misses a given flight."),
    }


def _pick_tfdm_record(results: list, flight: str | None):
    if not results:
        return None
    if flight:
        matches = [r for r in results
                   if _callsign_match(r.get("flight_id"), flight)]
        if matches:
            return matches[-1]
    scored = [r for r in results
              if r.get("queue_wait_minutes") is not None
              or r.get("taxi_out_minutes") is not None
              or r.get("runway_departure_earliest")
              or r.get("runway_departure_estimated")]
    return (scored or results)[-1]


def tfdm_effects(surface: dict) -> list:
    if not isinstance(surface, dict) or not surface.get("applicable"):
        return []
    queue = surface.get("queue_wait_min")
    taxi = surface.get("estimated_taxi_out_min")
    effects = []
    apt = surface.get("airport") or "origin"
    if isinstance(queue, (int, float)) and queue >= 15:
        effects.append({
            "cause": f"TFDM departure queue at {apt} is {int(queue)} min",
            "effect": "Surface congestion is already in the wheels-up "
                      "estimate. Expect a longer taxi even if the gate "
                      "time looks on schedule.",
            "severity": "WATCH" if queue >= 20 else "INFO",
            "source": "tfdm",
        })
    elif isinstance(taxi, (int, float)) and taxi >= 40:
        effects.append({
            "cause": f"TFDM estimated taxi-out at {apt} is {int(taxi)} min",
            "effect": "Tower surface management is forecasting a long "
                      "taxi. The airline gate time may not include all "
                      "of this.",
            "severity": "WATCH",
            "source": "tfdm",
        })
    ew = surface.get("earliest_wheels_up")
    if ew:
        effects.append({
            "cause": f"TFDM earliest wheels-up {ew}",
            "effect": "That is the surface-management floor — the flight "
                      "is not expected off the runway before this time.",
            "severity": "INFO",
            "source": "tfdm",
        })
    return effects


def metering_effects(metering: dict, flight: str | None = None) -> list:
    if not isinstance(metering, dict) or not metering.get("applicable"):
        return []
    mine = [i for i in (metering.get("items") or [])
            if i.get("this_flight") or _callsign_match(i.get("flight_id"), flight)]
    if not mine:
        return []
    item = mine[0]
    eta = item.get("eta") or "unspecified"
    fix = item.get("fix") or metering.get("airport") or "meter fix"
    return [{
        "cause": f"TBFM arrival metering for {item.get('flight_id') or flight} "
                 f"at {fix}, ETA {eta}",
        "effect": "The flight is in the destination's time-based flow. "
                  "The metered time is what arrival ATC is building to, "
                  "not the airline's scheduled in-time.",
        "severity": "WATCH",
        "source": "tbfm",
    }]


def merge_tfms_payloads(*payloads) -> dict:
    """Dedup `results[]` from multiple tfms-flow captures."""
    seen = set()
    merged = []
    for payload in payloads:
        for rec in _results(payload):
            key = (
                rec.get("type"), rec.get("msg_type"),
                rec.get("advisory_number"), rec.get("title"),
                rec.get("flight_id"), rec.get("element"),
                rec.get("source_timestamp"), rec.get("effective_start"),
            )
            if key in seen:
                continue
            seen.add(key)
            merged.append(rec)
    return {"results": merged}


def assemble_flow_brief(*, flight: str | None, date: str | None,
                        origin: str | None, dest: str | None,
                        swim: dict, timings: dict,
                        sources_tried: list,
                        sources_quiet: list,
                        aeroapi_queries_used: int = 0) -> dict:
    """Build the client-ready flow-brief envelope from interpreted parts."""
    tfms = interpret_tfms_flow(
        merge_tfms_payloads(swim.get("tfms_flow"),
                            swim.get("tfms_flow_origin"),
                            swim.get("tfms_flow_dest")),
        origin=origin, dest=dest, flight=flight,
    )
    metering = interpret_tbfm(swim.get("tbfm"), flight=flight, dest=dest)

    origin_surface = interpret_tfdm(swim.get("tfdm") or swim.get("tfdm_origin"),
                                    airport=origin, flight=flight)
    dest_surface = None
    if dest and dest != origin and is_tfdm_airport(dest):
        dest_surface = interpret_tfdm(swim.get("tfdm_dest"),
                                      airport=dest, flight=flight)

    # Origin surface is the one the client renders as `surface`; dest is
    # extra when TFDM is actually deployed there.
    surface = origin_surface
    if dest and not is_tfdm_airport(origin or "") and dest_surface:
        surface = dest_surface

    effects = list(tfms.get("effects") or [])
    effects += metering_effects(metering, flight)
    effects += tfdm_effects(origin_surface)
    if dest_surface:
        effects += tfdm_effects(dest_surface)

    _sev = {"ACTION": 0, "WATCH": 1, "INFO": 2}
    effects.sort(key=lambda e: _sev.get(e.get("severity"), 3))
    advisories = tfms.get("advisories") or []
    advisories.sort(key=lambda a: _sev.get(a.get("severity"), 3))

    return {
        "flight": flight or None,
        "date": date or None,
        "origin": origin or None,
        "dest": dest or None,
        "advisories": advisories,
        "metering": metering,
        "surface": surface,
        "surface_dest": dest_surface,
        "effects": effects,
        "sources_tried": sources_tried,
        "sources_quiet": sources_quiet,
        "timings": timings,
        "aeroapi_queries_used": aeroapi_queries_used,
        "note": (
            "SWIM captures are short live listens. Empty arrays mean the "
            "feed was quiet or not deployed — not a server error. TBFM and "
            "TFMS are US NAS products; TFDM is only at equipped airports."
        ),
    }
