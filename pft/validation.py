"""Query-parameter sanitizing and airport-code helpers."""

from datetime import datetime

# Quote characters that commonly ride along from a mis-quoted curl/shell call,
# including the smart quotes that appear when a command is pasted from chat.
_STRAY = '\'"`‘’“” \t\r\n'


def clean_param(value: str, maxlen: int = 32) -> str:
    """Strip stray quotes/whitespace. Returns '' for anything unusable."""
    if not value:
        return ""
    v = value.strip(_STRAY)[:maxlen]
    # A value starting with '-' would be read as a flag by argparse.
    return "" if v.startswith("-") else v


def clean_ident(value: str, maxlen: int = 16) -> str:
    """Clean an airport/flight/keyword identifier: alphanumerics only."""
    v = clean_param(value, maxlen).upper()
    return v if v.replace("-", "").replace("_", "").isalnum() else ""


def clean_date(value: str) -> str:
    """Validate a YYYY-MM-DD date string. Returns '' for anything else.

    Dates ride into subprocess argv and into AeroAPI result filtering, so
    anything that isn't a real calendar date is dropped rather than passed
    through.
    """
    v = clean_param(value or "", 10)
    if not v:
        return ""
    try:
        datetime.strptime(v, "%Y-%m-%d")
        return v
    except ValueError:
        return ""


# Non-CONUS US airports where ICAO isn't simply "K" + FAA code.
_FAA_TO_ICAO = {
    # Alaska
    "ANC": "PANC", "FAI": "PAFA", "JNU": "PAJN", "KTN": "PAKT", "BET": "PABE",
    "OTZ": "PAOT", "OME": "PAOM", "SIT": "PASI", "ADQ": "PADQ", "BRW": "PABR",
    # Hawaii
    "HNL": "PHNL", "OGG": "PHOG", "KOA": "PHKO", "LIH": "PHLI", "ITO": "PHTO",
    # Territories
    "GUM": "PGUM", "SPN": "PGSN", "SJU": "TJSJ", "STT": "TIST", "STX": "TISX",
    "PPG": "NSTU",
}


_ICAO_TO_FAA = {v: k for k, v in _FAA_TO_ICAO.items()}


# Major non-US IATA → ICAO. A client sending dest=LHR (or the mistaken
# dest=KLHR) must not become "KLHR", which is not a real airport.
_IATA_TO_ICAO_INTL = {
    "LHR": "EGLL", "LGW": "EGKK", "LCY": "EGLC", "STN": "EGSS", "MAN": "EGCC",
    "CDG": "LFPG", "ORY": "LFPO", "FCO": "LIRF", "MXP": "LIMC", "NAP": "LIRN",
    "AMS": "EHAM", "FRA": "EDDF", "MUC": "EDDM", "MAD": "LEMD", "BCN": "LEBL",
    "DUB": "EIDW", "ZRH": "LSZH", "VIE": "LOWW", "CPH": "EKCH", "ARN": "ESSA",
    "HEL": "EFHK", "LIS": "LPPT", "ATH": "LGAV", "IST": "LTFM", "WAW": "EPWA",
    "PRG": "LKPR", "DXB": "OMDB", "DOH": "OTHH", "HND": "RJTT", "NRT": "RJAA",
    "HKG": "VHHH", "SIN": "WSSS", "SYD": "YSSY", "YYZ": "CYYZ", "YUL": "CYUL",
    "MEX": "MMMX", "CTA": "LICC", "BGY": "LIME", "BRU": "EBBR", "OSL": "ENGM",
}


def _is_conus(icao: str) -> bool:
    """True for contiguous-US ICAO codes (the domestic /airsigmet coverage
    area). Alaska/Hawaii/territories are 'P'/'T'/'N'-prefixed, same as every
    non-US airport — all of them are blind spots for the domestic SIGMET feed
    and need the international one instead."""
    return bool(icao) and icao.startswith("K")


def to_icao(code: str) -> str:
    """Best-effort 4-letter ICAO. Passes through anything already 4 chars."""
    c = clean_ident(code)
    if not c:
        return ""
    if len(c) == 4:
        # dest=KLHR is a common client mistake (K + IATA). Map it.
        if c.startswith("K") and c[1:] in _IATA_TO_ICAO_INTL:
            return _IATA_TO_ICAO_INTL[c[1:]]
        return c
    if len(c) == 3:
        return (_FAA_TO_ICAO.get(c)
                or _IATA_TO_ICAO_INTL.get(c)
                or ("K" + c))
    return c


def to_faa(code: str) -> str:
    """Best-effort 3-letter FAA code (what the RVR feed expects)."""
    c = clean_ident(code)
    if not c:
        return ""
    if len(c) == 3:
        return c
    if c in _ICAO_TO_FAA:
        return _ICAO_TO_FAA[c]
    if len(c) == 4 and c.startswith("K"):
        return c[1:]
    return c


def airport_list(raw: str):
    """Split a comma/space separated airport list into ICAO codes.

    Returns (icao_codes, {icao: as_the_caller_wrote_it}).
    """
    mapping = {}
    codes = []
    for token in (raw or "").replace(",", " ").split():
        original = clean_ident(token)
        if not original:
            continue
        icao = to_icao(original)
        if icao and icao not in mapping:
            mapping[icao] = original
            codes.append(icao)
    return codes, mapping


def rekey_airports(payload, mapping: dict):
    """Rename `data` keys from ICAO back to what the caller requested."""
    if not isinstance(payload, dict) or not isinstance(payload.get("data"), dict):
        return payload
    renamed = {}
    for key, value in payload["data"].items():
        renamed[mapping.get(key, key)] = value
    payload["data"] = renamed
    # Keep the resolution visible so a client can tell what was actually queried.
    payload["resolved"] = {orig: icao for icao, orig in mapping.items()
                           if orig != icao}
    return payload


def clean_int(value: str, default: int, lo: int, hi: int) -> str:
    """Coerce an int into [lo, hi], falling back to default on junk."""
    try:
        n = int(clean_param(value, 8))
    except (TypeError, ValueError):
        n = default
    return str(max(lo, min(hi, n)))


def clean_duration(value: str, default: int, lo: int = 1, hi: int = 30) -> str:
    """Coerce a duration to a sane int, falling back to the endpoint default."""
    return clean_int(value, default, lo, hi)


def _extract_flights(flight_status: dict) -> list:
    """Pull the flight list out of a flight_data.py `status` payload.

    The real shape nests it two deep: {"command":"status","data":{"flights":[...]}}.
    The bare "flights" fallback is for a payload that's already been unwrapped.
    """
    if not isinstance(flight_status, dict):
        return []
    inner = flight_status.get("data")
    if isinstance(inner, dict) and isinstance(inner.get("flights"), list):
        return inner["flights"]
    if isinstance(flight_status.get("flights"), list):
        return flight_status["flights"]
    return []


_AIRLINE_MAP = {
    "DL": "DAL", "AA": "AAL", "UA": "UAL", "WN": "SWA", "B6": "JBU",
    "AS": "ASA", "NK": "NKS", "F9": "FFT", "HA": "HAL", "SY": "SCX",
    "G4": "AAY", "BA": "BAW", "AF": "AFR", "LH": "DLH", "KL": "KLM",
    "AZ": "ITY", "IB": "IBE", "EI": "EIN", "AY": "FIN", "SK": "SAS",
    "TP": "TAP", "TK": "THY", "EK": "UAE", "QR": "QTR", "CX": "CPA",
    "SQ": "SIA", "NH": "ANA", "JL": "JAL", "QF": "QFA", "AC": "ACA",
    "AM": "AMX", "VS": "VIR", "LX": "SWR", "OS": "AUA", "SN": "BEL",
    "AT": "RAM",
}


def _iata_to_icao_airline(iata: str) -> str:
    return _AIRLINE_MAP.get(iata.upper(), iata.upper())


def _icao_to_faa(icao: str) -> str:
    """Convert ICAO code to FAA/IATA (strip K prefix for US airports)."""
    if icao and len(icao) == 4 and icao.startswith("K"):
        return icao[1:]
    return icao
