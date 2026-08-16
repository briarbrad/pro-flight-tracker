#!/usr/bin/env python3
"""
FAA SWIM Consumer — Python wrapper for the L3Harris JMS jumpstart client.

Connects to FAA SWIM Solace brokers, captures messages for a time window,
parses XML, filters by airport/flight, and returns structured JSON.

Feeds:
  tbfm    — Time-Based Flow Management (arrival metering/sequencing)
  itws    — Integrated Terminal Weather System (wind shear, gust fronts, storm cells)
  notams  — NOTAM distribution via AIM FNS (AIXM 5.1 format)
  sfdps   — Flight Data Publication Service (FIXM positions/routes)
  stdds   — Surface movement events (ASDEX/ASDE-X)

Usage:
  swim_consumer.py tbfm    --airport KJFK [--flight DAL182] [--duration 15]
  swim_consumer.py itws    --airport KJFK [--duration 15]
  swim_consumer.py notams  --airport KJFK [--duration 20]
  swim_consumer.py sfdps   --flight DAL182 [--duration 15]
  swim_consumer.py stdds   --airport KJFK [--duration 10]

Requires:
  - Java 25+ runtime
  - SWIM jumpstart JAR (swim/ directory)
  - SWIM_PASSWORD environment variable
"""

import argparse
import json
import os
import re
import subprocess
import sys
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
SWIM_DIR = SCRIPT_DIR.parent / "swim"
CONFIG_PATH = SWIM_DIR / "config.json"
RUN_SCRIPT = SWIM_DIR / "bin" / "run"

# XML namespace maps for parsing
NS = {
    # TBFM
    'tbfm': 'urn:us:gov:dot:faa:atm:tfm:tbfmmeteringpublication:1.1.0',
    # SFDPS / FIXM
    'nas': 'http://www.faa.aero/nas/3.0',
    'fixm_base': 'http://www.fixm.aero/base/3.0',
    'fixm_flight': 'http://www.fixm.aero/flight/3.0',
    'fixm_found': 'http://www.fixm.aero/foundation/3.0',
    # ITWS
    'itws': '',  # No namespace in ITWS messages
    # STDDS / SMES
    'smes': 'urn:us:gov:dot:faa:atm:terminal:entities:v4-0:smes:surfacemovementevent',
    # NOTAMs / AIXM
    'aixm_msg': 'http://www.aixm.aero/schema/5.1/message',
    'aixm': 'http://www.aixm.aero/schema/5.1',
    'event': 'http://www.aixm.aero/schema/5.1/event',
    'gml': 'http://www.opengis.net/gml/3.2',
    'fnse': 'http://www.aixm.aero/schema/5.1/extensions/FAA/FNSE',
    # TFMS
    'tfms': 'urn:us:gov:dot:faa:atm:tfm:tfmdataservice',
    'tfms_fd': 'urn:us:gov:dot:faa:atm:tfm:flightdata',
    'tfms_fi': 'urn:us:gov:dot:faa:atm:tfm:flowinformation',
    'tfms_nxce': 'urn:us:gov:dot:faa:atm:tfm:tfmdatacoreelements',
    'tfms_nxcm': 'urn:us:gov:dot:faa:atm:tfm:flightdatacommonmessages',
    'tfms_fce': 'urn:us:gov:dot:faa:atm:tfm:ficommondatatypes',
    # TFDM
    'tfdm_nas': 'http://www.faa.aero/nas/4.1',
    'tfdm_fx': 'http://www.fixm.aero/flight/4.0',
}


def load_config():
    """Load SWIM queue configuration."""
    with open(CONFIG_PATH) as f:
        return json.load(f)


# Set by run_consumer() so main() can surface JVM failures instead of
# silently reporting "0 messages".
LAST_STDERR = ''

# JVM/broker failures worth bubbling up to the caller verbatim.
FATAL_STDERR_MARKERS = (
    'UnsupportedClassVersionError',   # JRE older than the JAR (needs Java 25+)
    'Error: Unable to access jarfile',
    'ClassNotFoundException',
    'NoClassDefFoundError',
    'ConfigException',               # a -D property is missing or malformed
    'Failed to create the connection',
    'Failed to start the connection',
    'error 401',                     # bad SWIM credentials
    'Authentication',
)


def run_consumer(feed: str, duration: int, password: str) -> list:
    """
    Run the Java jumpstart consumer for `duration` seconds,
    return list of (headers_dict, xml_string) tuples.

    Uses `timeout` to cleanly kill the Java process, and separates
    stdout (message data) from stderr (JVM logging).

    Args are passed as an argv list rather than a shell string: SWIM
    passwords routinely contain $ ! & ; and quotes, all of which the shell
    would mangle (or worse, execute).
    """
    global LAST_STDERR

    config = load_config()
    feed_cfg = config['queues'][feed]
    broker = config['provider_urls'][feed_cfg['broker']]

    if not os.access(RUN_SCRIPT, os.X_OK):
        try:
            os.chmod(RUN_SCRIPT, 0o755)
        except OSError:
            pass

    cmd = [
        'timeout', str(duration), str(RUN_SCRIPT),
        '-Djava.net.preferIPv4Stack=true',
        f'-DproviderUrl={broker}',
        f'-Dqueue={feed_cfg["queue"]}',
        f'-DconnectionFactory={config["connection_factory"]}',
        f'-Dusername={config["username"]}',
        f'-Dpassword={password}',
        f'-Dvpn={feed_cfg["vpn"]}',
        '-Doutput=com.harris.cinnato.outputs.StdoutOutput',
        '-Dmetrics=false',
        '-Djson=false',
        '-Dheaders=true',
    ]

    proc = subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        cwd=str(SWIM_DIR),
    )

    try:
        stdout, stderr = proc.communicate(timeout=duration + 15)
    except subprocess.TimeoutExpired:
        proc.kill()
        stdout, stderr = proc.communicate()

    # Only parse stdout (clean message output); stderr is JVM logging
    output = stdout.decode('utf-8', errors='replace') if stdout else ''
    LAST_STDERR = stderr.decode('utf-8', errors='replace') if stderr else ''

    return parse_raw_output(output)


def stderr_diagnostic() -> str:
    """Return a JVM error worth reporting, or '' if stderr looks like noise."""
    for line in LAST_STDERR.split('\n'):
        if any(marker in line for marker in FATAL_STDERR_MARKERS):
            return line.strip()
    return ''


def parse_raw_output(output: str) -> list:
    """
    Parse the jumpstart stdout into list of (headers_dict, xml_body) tuples.
    
    Output format (repeating):
      Property name = X; value = Y
      ...
      <?xml ...>  (may be single-line or multi-line XML)
      ...xml body...
      Property name = ...  (next message starts)
    
    State machine with 3 states:
      HEADERS: accumulating Property lines
      XML: accumulating XML body lines (started by <?xml)
    When in XML state and we see a Property line, emit message and switch back.
    """
    messages = []
    current_headers = {}
    xml_lines = []
    in_xml = False
    
    for line in output.split('\n'):
        stripped = line.strip()
        if not stripped:
            continue
        
        # Check for property header line
        prop_match = re.match(r'^Property name = (.+?); value = (.+)$', stripped)
        
        if prop_match:
            if in_xml and xml_lines:
                # We were accumulating XML — emit the message
                xml_body = '\n'.join(xml_lines).strip()
                if xml_body:
                    messages.append((dict(current_headers), xml_body))
                current_headers = {}
                xml_lines = []
                in_xml = False
            # Accumulate this property
            current_headers[prop_match.group(1)] = prop_match.group(2)
            continue
        
        # Check for XML start
        if stripped.startswith('<?xml '):
            if in_xml and xml_lines:
                # Previous XML wasn't terminated by properties — emit it
                xml_body = '\n'.join(xml_lines).strip()
                if xml_body:
                    messages.append((dict(current_headers), xml_body))
                current_headers = {}
                xml_lines = []
            in_xml = True
            xml_lines = [stripped]
            continue
        
        # If we're in XML mode, accumulate
        if in_xml:
            xml_lines.append(stripped)
            continue
        
        # Ignore other lines (JVM logging, etc.)
    
    # Emit any trailing message
    if in_xml and xml_lines:
        xml_body = '\n'.join(xml_lines).strip()
        if xml_body:
            messages.append((dict(current_headers), xml_body))

    return messages


# ============================================================
# TBFM Parser
# ============================================================

def parse_tbfm(messages: list, airport: str = None, flight: str = None) -> list:
    """Parse TBFM metering messages, filter by airport/flight."""
    results = []
    airport_icao = airport.upper() if airport else None
    # Strip 'K' prefix for TBFM matching (uses 3-letter codes)
    airport_3 = airport_icao[1:] if airport_icao and airport_icao.startswith('K') and len(airport_icao) == 4 else airport_icao
    flight_upper = flight.upper().replace(' ', '') if flight else None

    for headers, xml_body in messages:
        # Quick header-level filter
        dest_apt = headers.get('DEST_APT', '')
        if airport_icao and airport_icao not in dest_apt and airport_3 and airport_3 not in dest_apt:
            continue

        try:
            root = ET.fromstring(xml_body)
        except ET.ParseError:
            continue

        # Find all <air> elements
        for tma in root.iter(f'{{{NS["tbfm"]}}}tma'):
            msg_time = tma.get('msgTime', '')
            for air in tma.iter(f'{{{NS["tbfm"]}}}air'):
                aid = air.get('aid', '')
                gufi = air.get('gufi', '')
                apt = air.get('apt', '')
                dap = air.get('dap', '')
                air_type = air.get('airType', '')

                # Flight filter
                if flight_upper and flight_upper not in aid.upper().replace(' ', ''):
                    continue

                record = {
                    'type': 'tbfm_metering',
                    'flight_id': aid,
                    'gufi': gufi,
                    'dest_airport': apt,
                    'dep_airport': dap,
                    'msg_type': air_type,
                    'msg_time': msg_time,
                    'artcc': headers.get('ARTCC', ''),
                    'data_group': headers.get('DATA_GROUP', ''),
                }

                # Parse ETA data
                eta_elem = air.find(f'{{{NS["tbfm"]}}}eta')
                if eta_elem is not None:
                    eta_data = {}
                    for child in eta_elem:
                        tag = child.tag.split('}')[-1]
                        eta_data[tag] = child.text
                    record['eta'] = eta_data

                # Parse flight data
                flt_elem = air.find(f'{{{NS["tbfm"]}}}flt')
                if flt_elem is not None:
                    flt_data = {}
                    for child in flt_elem:
                        tag = child.tag.split('}')[-1]
                        flt_data[tag] = child.text
                    record['flight_info'] = flt_data

                results.append(record)

    return results


# ============================================================
# ITWS Parser
# ============================================================

def parse_itws(messages: list, airport: str = None) -> list:
    """Parse ITWS terminal weather alerts, filter by airport."""
    results = []
    airport_icao = airport.upper() if airport else None
    airport_3 = airport_icao[1:] if airport_icao and airport_icao.startswith('K') and len(airport_icao) == 4 else airport_icao

    for headers, xml_body in messages:
        # Header-level filter
        msg_airport = headers.get('airport', headers.get('ITWSsite', ''))
        if airport_icao:
            if airport_icao not in msg_airport.upper() and (airport_3 and airport_3 not in msg_airport.upper()):
                continue

        try:
            root = ET.fromstring(xml_body)
        except ET.ParseError:
            continue

        record = {
            'type': 'itws_alert',
            'airport': msg_airport,
            'product_id': headers.get('productID', ''),
            'source_type': headers.get('DEX_SOURCE_TYPE', ''),
        }

        # Parse product header
        ph = root.find('.//product_header')
        if ph is not None:
            msg_name_el = ph.find('.//product_msg_name')
            if msg_name_el is not None:
                record['product_name'] = msg_name_el.text

            gen_time = ph.find('product_header_generation_time_seconds')
            if gen_time is not None:
                record['generation_time'] = gen_time.get('gregorian', gen_time.text)

            exp_time = ph.find('product_header_expiration_time_seconds')
            if exp_time is not None:
                record['expiration_time'] = exp_time.get('gregorian', exp_time.text)

        # Parse specific alert types
        # Gust front
        gf = root.find('.//gf_eti')
        if gf is not None:
            gf_near = gf.find('gf_eti_near')
            gf_min = gf.find('gf_eti_minutes')
            gf_hor = gf.find('gf_eti_horizon')
            record['alert_type'] = 'GUST_FRONT'
            record['gust_front'] = {
                'near': gf_near.text if gf_near is not None else None,
                'minutes_to_impact': gf_min.text if gf_min is not None else None,
                'horizon_minutes': gf_hor.text if gf_hor is not None else None,
            }

        # Wind shear
        ws = root.find('.//ws_alert')
        if ws is not None:
            record['alert_type'] = 'WIND_SHEAR'
            ws_data = {}
            for child in ws:
                tag = child.tag
                ws_data[tag] = child.text
            record['wind_shear'] = ws_data

        # Microburst
        mb = root.find('.//mb_alert')
        if mb is not None:
            record['alert_type'] = 'MICROBURST'
            mb_data = {}
            for child in mb:
                tag = child.tag
                mb_data[tag] = child.text
            record['microburst'] = mb_data

        # Storm motion
        sm = root.find('.//storm_motion')
        if sm is not None:
            record['alert_type'] = 'STORM_CELL'
            sm_data = {}
            for child in sm:
                tag = child.tag
                sm_data[tag] = child.text
            record['storm_motion'] = sm_data

        # Lightning
        lt = root.find('.//lightning')
        if lt is not None:
            record.setdefault('alert_type', 'LIGHTNING')
            lt_data = {}
            for child in lt:
                tag = child.tag
                lt_data[tag] = child.text
            record['lightning'] = lt_data

        # Precipitation
        precip = root.find('.//precip')
        if precip is not None:
            record.setdefault('alert_type', 'PRECIPITATION')

        results.append(record)

    return results


# ============================================================
# NOTAM Parser
# ============================================================

def parse_notams(messages: list, airport: str = None) -> list:
    """Parse AIXM NOTAM messages, filter by airport."""
    results = []
    airport_icao = airport.upper() if airport else None
    # Also match 3-letter FAA code
    airport_3 = airport_icao[1:] if airport_icao and airport_icao.startswith('K') and len(airport_icao) == 4 else None

    for headers, xml_body in messages:
        # Header-level filter on ICAOId or LocationDesignator
        header_icao = headers.get('us_gov_dot_faa_aim_fns_nds_ICAOId', '')
        header_loc = headers.get('us_gov_dot_faa_aim_fns_nds_LocationDesignator', '')
        if airport_icao:
            if (airport_icao not in header_icao.upper() and
                (not airport_3 or airport_3 not in header_loc.upper()) and
                airport_icao not in header_loc.upper()):
                continue

        try:
            root = ET.fromstring(xml_body)
        except ET.ParseError:
            continue

        # Find the NOTAM event
        for notam in root.iter(f'{{{NS["event"]}}}NOTAM'):
            record = {
                'type': 'notam',
                'icao': header_icao,
                'location': header_loc,
                'notam_status': headers.get('us_gov_dot_faa_aim_fns_nds_NOTAMStatus', ''),
                'notam_function': headers.get('us_gov_dot_faa_aim_fns_nds_NOTAMFunction', ''),
            }

            # Extract NOTAM fields
            for field in ['number', 'year', 'type', 'issued', 'affectedFIR',
                          'location', 'effectiveStart', 'effectiveEnd',
                          'text', 'schedule']:
                elem = notam.find(f'{{{NS["event"]}}}{field}')
                if elem is not None and elem.text:
                    record[f'notam_{field}'] = elem.text

            # Get the simple text translation
            for trans in notam.iter(f'{{{NS["event"]}}}NOTAMTranslation'):
                ttype = trans.find(f'{{{NS["event"]}}}type')
                if ttype is not None and ttype.text == 'LOCAL_FORMAT':
                    simple = trans.find(f'{{{NS["event"]}}}simpleText')
                    if simple is not None:
                        record['notam_simple_text'] = simple.text

            # Get airport name from extension
            for ext in root.iter(f'{{{NS["fnse"]}}}EventExtension'):
                name_el = ext.find(f'{{{NS["fnse"]}}}airportname')
                if name_el is not None:
                    record['airport_name'] = name_el.text

            results.append(record)

    return results


# ============================================================
# SFDPS Parser
# ============================================================

def _local(tag: str) -> str:
    """Strip namespace URI from an ElementTree tag, returning local name."""
    return tag.split('}')[-1] if '}' in tag else tag


def parse_sfdps(messages: list, flight: str = None, airport: str = None) -> list:
    """Parse FIXM flight data messages, filter by flight/airport.
    
    FIXM messages use complex namespaces (ns2/ns3/ns5) with xsi:type attributes.
    We match by local element names to avoid namespace fragility.
    """
    results = []
    flight_upper = flight.upper().replace(' ', '') if flight else None
    airport_icao = airport.upper() if airport else None

    for headers, xml_body in messages:
        try:
            root = ET.fromstring(xml_body)
        except ET.ParseError:
            continue

        # Find all <flight> elements (any namespace)
        for fl in root.iter():
            if _local(fl.tag) != 'flight':
                continue

            # Extract aircraft ID from flightIdentification element
            acid = ''
            for elem in fl.iter():
                if _local(elem.tag) == 'flightIdentification':
                    acid = elem.get('aircraftIdentification', '')
                    break

            if flight_upper and flight_upper not in acid.upper().replace(' ', ''):
                continue

            record = {
                'type': 'sfdps_position',
                'flight_id': acid,
                'centre': fl.get('centre', ''),
                'source': fl.get('source', ''),
                'timestamp': fl.get('timestamp', ''),
            }

            # Extract arrival/departure points
            for elem in fl.iter():
                lname = _local(elem.tag)
                if lname == 'arrival':
                    record['arrival_point'] = elem.get('arrivalPoint', '')
                elif lname == 'departure':
                    record['departure_point'] = elem.get('departurePoint', '')

            # Airport filter
            if airport_icao:
                arr_pt = record.get('arrival_point', '').upper()
                dep_pt = record.get('departure_point', '').upper()
                if airport_icao not in arr_pt and airport_icao not in dep_pt:
                    continue

            # Extract position data from enRoute/position
            for elem in fl.iter():
                lname = _local(elem.tag)
                # Position element has positionTime attribute
                if lname == 'position' and elem.get('positionTime'):
                    record['position_time'] = elem.get('positionTime', '')
                    # Walk children for altitude, speed, lat/lon
                    for child in elem.iter():
                        cname = _local(child.tag)
                        if cname == 'altitude' and child.text:
                            try:
                                record['altitude_ft'] = float(child.text)
                            except ValueError:
                                pass
                        elif cname == 'surveillance' and child.text:
                            try:
                                record['ground_speed_kts'] = float(child.text)
                            except ValueError:
                                pass
                        elif cname == 'pos' and child.text:
                            coords = child.text.strip().split()
                            if len(coords) >= 2:
                                try:
                                    record['latitude'] = float(coords[0])
                                    record['longitude'] = float(coords[1])
                                except ValueError:
                                    pass
                                break  # First <pos> is actual position
                    break  # Only need first position element

            # Flight status
            for elem in fl.iter():
                if _local(elem.tag) == 'flightStatus':
                    record['flight_status'] = elem.get('fdpsFlightStatus', '')
                    break

            # GUFI
            for elem in fl.iter():
                if _local(elem.tag) == 'gufi' and elem.text:
                    record['gufi'] = elem.text.strip()
                    break

            results.append(record)

    return results


# ============================================================
# STDDS Parser
# ============================================================

# TRACON → primary airport mapping for STDDS TAIS filtering
TRACON_AIRPORTS = {
    'N90': ['JFK', 'LGA', 'EWR', 'TEB', 'HPN'],  # NY TRACON
    'SCT': ['LAX', 'SNA', 'BUR', 'ONT', 'LGB'],   # SoCal TRACON
    'NCT': ['SFO', 'OAK', 'SJC'],                  # NorCal TRACON
    'C90': ['ORD', 'MDW'],                          # Chicago TRACON
    'A80': ['ATL'],                                  # Atlanta TRACON
    'PCT': ['DCA', 'IAD', 'BWI'],                   # Potomac TRACON
    'D10': ['DFW', 'DAL'],                           # Dallas TRACON
    'I90': ['IAH', 'HOU'],                           # Houston TRACON
    'S56': ['SEA'],                                   # Seattle TRACON
    'MIA': ['MIA', 'FLL', 'PBI'],                    # Miami TRACON
    'D01': ['DEN'],                                   # Denver TRACON
    'A90': ['BOS'],                                   # Boston TRACON
    'P50': ['PHX'],                                   # Phoenix TRACON
    'M98': ['MSP'],                                   # Minneapolis TRACON
    'L30': ['LAS'],                                   # Las Vegas TRACON
}


def parse_stdds(messages: list, airport: str = None) -> list:
    """Parse STDDS surface movement events, filter by airport.
    
    STDDS has two message types:
      - SMES (ASDEX): has <airport> element directly
      - TAIS: uses TRACON code (e.g., N90 for JFK/LGA/EWR)
    """
    results = []
    airport_icao = airport.upper() if airport else None
    airport_3 = airport_icao[1:] if airport_icao and airport_icao.startswith('K') and len(airport_icao) == 4 else airport_icao

    # Build set of matching TRACON codes for the requested airport
    matching_tracons = set()
    if airport_3:
        for tracon, airports in TRACON_AIRPORTS.items():
            if airport_3 in airports:
                matching_tracons.add(tracon)

    for headers, xml_body in messages:
        # STDDS uses 3-letter airport codes and TRACON codes
        msg_airport = headers.get('airport', '')
        msg_tracon = headers.get('tracon', headers.get('srcTracon', ''))
        
        if not msg_airport:
            # Try to extract from XML <airport> or <src> element
            apt_match = re.search(r'<airport>(\w+)</airport>', xml_body)
            if apt_match:
                msg_airport = apt_match.group(1)
            elif not msg_tracon:
                src_match = re.search(r'<src>(\w+)</src>', xml_body)
                if src_match:
                    msg_tracon = src_match.group(1)
        
        if airport_icao:
            matched = False
            ma = msg_airport.upper()
            mt = msg_tracon.upper()
            # Direct airport match
            if airport_icao in ma or (airport_3 and airport_3 in ma):
                matched = True
            # TRACON match
            elif mt in matching_tracons:
                matched = True
                msg_airport = msg_airport or f'{mt}(TRACON)'
            if not matched:
                continue

        msg_type = headers.get('msgType', headers.get('mex', ''))

        try:
            root = ET.fromstring(xml_body)
        except ET.ParseError:
            continue

        # Try SMES format (ASDEX surface position reports)
        tracks = []
        for pr in root.iter(f'{{{NS["smes"]}}}positionReport'):
            track = {}
            for child in pr:
                tag = child.tag.split('}')[-1]
                if tag == 'position':
                    for pos_child in child:
                        ptag = pos_child.tag.split('}')[-1]
                        if ptag in ('latitude', 'longitude'):
                            try:
                                track[ptag] = float(pos_child.text)
                            except (ValueError, TypeError):
                                pass
                elif tag in ('time', 'track', 'stid', 'seqNum'):
                    track[tag] = child.text
            if track:
                tracks.append(track)

        if tracks:
            record = {
                'type': 'stdds_surface',
                'airport': msg_airport,
                'msg_type': msg_type,
                'timestamp': headers.get('timestamp', ''),
                'track_count': len(tracks),
                'tracks': tracks[:10],
            }
            results.append(record)
            continue

        # Try TAIS format (Terminal Automation — TRACON track data)
        tais_tracks = []
        for elem in root.iter():
            lname = _local(elem.tag)
            if lname == 'track':
                track = {}
                for child in elem:
                    cname = _local(child.tag)
                    if cname == 'trackNum':
                        track['trackNum'] = child.text
                    elif cname == 'mrtTime':
                        track['time'] = child.text
                    elif cname == 'status':
                        track['status'] = child.text
                    elif cname == 'acAddress':
                        track['icao_hex'] = child.text
                    elif cname == 'acId':
                        track['callsign'] = child.text
                    elif cname == 'lat':
                        try: track['latitude'] = float(child.text)
                        except (ValueError, TypeError): pass
                    elif cname == 'lon':
                        try: track['longitude'] = float(child.text)
                        except (ValueError, TypeError): pass
                    elif cname == 'alt':
                        try: track['altitude'] = float(child.text)
                        except (ValueError, TypeError): pass
                    elif cname == 'groundSpeed':
                        try: track['ground_speed'] = float(child.text)
                        except (ValueError, TypeError): pass
                if track:
                    tais_tracks.append(track)

        if tais_tracks:
            record = {
                'type': 'stdds_tais',
                'airport': msg_airport or msg_tracon,
                'tracon': msg_tracon,
                'msg_type': msg_type,
                'timestamp': headers.get('timestamp', ''),
                'track_count': len(tais_tracks),
                'tracks': tais_tracks[:10],
            }
            results.append(record)

    return results


# ============================================================
# TFMS FlightData Parser
# ============================================================

def _dms_to_decimal(degrees: str, direction: str, minutes: str = '0', seconds: str = '0') -> float:
    """Convert DMS (degrees/minutes/seconds) to decimal degrees."""
    try:
        d = float(degrees)
        m = float(minutes)
        s = float(seconds)
        decimal = d + m / 60.0 + s / 3600.0
        if direction.upper() in ('S', 'W'):
            decimal = -decimal
        return round(decimal, 5)
    except (ValueError, TypeError):
        return 0.0


def _parse_altitude(simple_alt: str) -> int:
    """Parse TFMS simpleAltitude string to feet. '370' -> 37000 ft (FL370). '300C' -> 30000 (climbing)."""
    if not simple_alt:
        return None
    clean = simple_alt.rstrip('CDcda').strip()
    try:
        return int(clean) * 100
    except ValueError:
        return None


def parse_tfms_flight(messages: list, airport: str = None, flight: str = None) -> list:
    """
    Parse TFMS FlightData messages (track positions, departures, route amendments).
    Filter by airport ICAO code or flight callsign.

    Message types in data:
      trackInformation        — position, altitude, speed, ETA, fix times
      departureInformation    — actual departure time, ETD/ETA
      flightPlanAmendmentInformation — route changes, new aircraft type
    """
    results = []
    flight_upper = flight.upper().replace(' ', '') if flight else None
    airport_icao = airport.upper() if airport else None
    airport_3 = (
        airport_icao[1:]
        if airport_icao and airport_icao.startswith('K') and len(airport_icao) == 4
        else airport_icao
    )

    for headers, xml_body in messages:
        if headers.get('TFMDataClass', '') != 'FlightData':
            continue
        try:
            root = ET.fromstring(xml_body)
        except ET.ParseError:
            continue

        for msg in root.iter():
            if _local(msg.tag) != 'fltdMessage':
                continue

            acid = msg.get('acid', '')
            airline = msg.get('airline', '')
            arr_arpt = msg.get('arrArpt', '')
            dep_arpt = msg.get('depArpt', '')
            msg_type = msg.get('msgType', '')
            source_ts = msg.get('sourceTimeStamp', '')
            flight_ref = msg.get('flightRef', '')

            if flight_upper and flight_upper not in acid.upper().replace(' ', ''):
                continue

            if airport_icao:
                matched = any(
                    airport_icao in apt.upper() or (airport_3 and airport_3 in apt.upper())
                    for apt in (arr_arpt, dep_arpt)
                )
                if not matched:
                    continue

            record = {
                'type': 'tfms_flight',
                'flight_id': acid,
                'airline': airline,
                'arr_airport': arr_arpt,
                'dep_airport': dep_arpt,
                'msg_type': msg_type,
                'source_timestamp': source_ts,
                'flight_ref': flight_ref,
            }

            if msg_type == 'trackInformation':
                for elem in msg.iter():
                    lname = _local(elem.tag)
                    if lname == 'speed' and elem.text:
                        try:
                            record['speed_kts'] = int(elem.text)
                        except ValueError:
                            pass
                    elif lname == 'simpleAltitude' and elem.text:
                        alt_ft = _parse_altitude(elem.text)
                        if alt_ft:
                            record['altitude_ft'] = alt_ft
                            record['altitude_raw'] = elem.text  # e.g. '370' or '300C'
                    elif lname == 'latitudeDMS':
                        record['latitude'] = _dms_to_decimal(
                            elem.get('degrees', '0'), elem.get('direction', 'N'),
                            elem.get('minutes', '0'), elem.get('seconds', '0')
                        )
                    elif lname == 'longitudeDMS':
                        record['longitude'] = _dms_to_decimal(
                            elem.get('degrees', '0'), elem.get('direction', 'W'),
                            elem.get('minutes', '0'), elem.get('seconds', '0')
                        )
                    elif lname == 'timeAtPosition' and elem.text:
                        record['position_time'] = elem.text
                    elif lname == 'eta' and (elem.get('etaType') or elem.get('timeValue')):
                        record['eta'] = {
                            'type': elem.get('etaType', ''),
                            'time': elem.get('timeValue', ''),
                        }
                    elif lname == 'routeOfFlight' and elem.text:
                        record['route'] = elem.text
                    elif lname == 'arrivalFixAndTime':
                        record['arrival_fix'] = elem.get('fixName', '')
                        record['arrival_fix_time'] = elem.get('arrTime', '')
                    elif lname == 'departureFixAndTime':
                        record['departure_fix'] = elem.get('fixName', '')
                        record['departure_fix_time'] = elem.get('arrTime', '')
                    elif lname == 'gufi' and elem.text:
                        record.setdefault('gufi', elem.text)
                    elif lname == 'etd' and (elem.get('etdType') or elem.get('timeValue')):
                        record['etd'] = {
                            'type': elem.get('etdType', ''),
                            'time': elem.get('timeValue', ''),
                        }

            elif msg_type == 'departureInformation':
                for elem in msg.iter():
                    lname = _local(elem.tag)
                    if lname == 'timeOfDeparture' and elem.text:
                        record['actual_departure'] = elem.text
                    elif lname == 'etd' and (elem.get('etdType') or elem.get('timeValue')):
                        record['etd'] = {
                            'type': elem.get('etdType', ''),
                            'time': elem.get('timeValue', ''),
                        }
                    elif lname == 'eta' and (elem.get('etaType') or elem.get('timeValue')):
                        record['eta'] = {
                            'type': elem.get('etaType', ''),
                            'time': elem.get('timeValue', ''),
                        }

            elif msg_type == 'flightPlanAmendmentInformation':
                for elem in msg.iter():
                    lname = _local(elem.tag)
                    if lname == 'newFlightAircraftSpecs' and elem.text:
                        record['aircraft_type'] = elem.text
                    elif lname == 'newRouteOfFlight':
                        record['route'] = elem.get('legacyFormat', '')
                    elif lname == 'newCoordinationTime' and elem.text:
                        record['new_coordination_time'] = elem.text
                        record['coordination_type'] = elem.get('type', '')

            results.append(record)

    return results


# ============================================================
# TFMS FlowInformation Parser
# ============================================================

def parse_tfms_flow(messages: list, airport: str = None, keyword: str = None) -> list:
    """
    Parse TFMS FlowInformation messages.

    Message types:
      GADV            — General Advisory from ATCSCC (GDP issuances, cancellations, EDCT text)
      TMI_FLIGHT_LIST — Per-flight TFM Initiative (FCA) assignments
      RSTR            — Restrictions (ground stops, miles-in-trail)

    Filter by:
      --airport  : airport ICAO code — matches in advisory text or flight routing
      --keyword  : freetext keyword in advisory title/body (e.g. GDP, EDCT, HOLD)
    """
    results = []
    airport_icao = airport.upper() if airport else None
    airport_3 = (
        airport_icao[1:]
        if airport_icao and airport_icao.startswith('K') and len(airport_icao) == 4
        else airport_icao
    )
    keyword_upper = keyword.upper() if keyword else None

    for headers, xml_body in messages:
        if headers.get('TFMDataClass', '') != 'FlowInformation':
            continue
        try:
            root = ET.fromstring(xml_body)
        except ET.ParseError:
            continue

        for fi_msg in root.iter():
            if _local(fi_msg.tag) != 'fiMessage':
                continue

            msg_type = fi_msg.get('msgType', '')
            source_facility = fi_msg.get('sourceFacility', '')
            source_ts = fi_msg.get('sourceTimeStamp', '')

            # ── GADV: General Advisory (GDP/GS issuance, cancellation, EDCT text) ──
            if msg_type == 'GADV':
                adv_title = adv_text = adv_number = start_time = end_time = origin = date_sent = ''
                for elem in fi_msg.iter():
                    lname = _local(elem.tag)
                    if lname == 'advisoryTitle' and elem.text:
                        adv_title = elem.text
                    elif lname == 'advisoryText' and elem.text:
                        adv_text = elem.text
                    elif lname == 'advisoryNumber' and elem.text:
                        adv_number = elem.text
                    elif lname == 'startTime' and elem.text:
                        start_time = elem.text
                    elif lname == 'endTime' and elem.text:
                        end_time = elem.text
                    elif lname == 'origin' and elem.text:
                        origin = elem.text
                    elif lname == 'dateSent' and elem.text:
                        date_sent = elem.text

                combined = (adv_title + ' ' + adv_text).upper()
                if airport_icao:
                    if airport_icao not in combined and (not airport_3 or airport_3 not in combined):
                        continue
                if keyword_upper and keyword_upper not in combined:
                    continue

                results.append({
                    'type': 'tfms_advisory',
                    'msg_type': 'GADV',
                    'advisory_number': adv_number,
                    'origin': origin,
                    'date_sent': date_sent,
                    'source_facility': source_facility,
                    'source_timestamp': source_ts,
                    'title': adv_title,
                    'text': adv_text,
                    'effective_start': start_time,
                    'effective_end': end_time,
                })

            # ── TMI_FLIGHT_LIST: Which flights are in which FCA/TMI program ──
            elif msg_type == 'TMI_FLIGHT_LIST':
                for fd in fi_msg.iter():
                    if _local(fd.tag) != 'flightData':
                        continue

                    acid = gufi = dep_apt = arr_apt = flight_ref = status = ''
                    fca_ids = []
                    entry_time = exit_time = ''

                    for elem in fd.iter():
                        lname = _local(elem.tag)
                        if lname == 'aircraftId' and elem.text and not acid:
                            acid = elem.text
                        elif lname == 'gufi' and elem.text and not gufi:
                            gufi = elem.text
                        elif lname == 'departurePoint':
                            for child in elem:
                                if _local(child.tag) == 'airport' and child.text:
                                    dep_apt = child.text
                        elif lname == 'arrivalPoint':
                            for child in elem:
                                if _local(child.tag) == 'airport' and child.text:
                                    arr_apt = child.text
                        elif lname == 'flightReference' and elem.text:
                            flight_ref = elem.text
                        elif lname == 'status' and elem.text:
                            status = elem.text
                        elif lname == 'fcaId' and elem.text:
                            fca_ids.append(elem.text)
                        elif lname == 'entryTm' and elem.text and not entry_time:
                            entry_time = elem.text
                        elif lname == 'exitTm' and elem.text and not exit_time:
                            exit_time = elem.text

                    if not acid:
                        continue

                    if airport_icao:
                        combined = (arr_apt + ' ' + dep_apt).upper()
                        if airport_icao not in combined and (not airport_3 or airport_3 not in combined):
                            continue

                    # Keyword filter: check FCA IDs and flight data
                    if keyword_upper:
                        tmi_str = (' '.join(fca_ids) + ' ' + arr_apt + ' ' + dep_apt + ' ' + acid).upper()
                        if keyword_upper not in tmi_str:
                            continue

                    results.append({
                        'type': 'tfms_tmi_flight',
                        'msg_type': 'TMI_FLIGHT_LIST',
                        'flight_id': acid,
                        'gufi': gufi,
                        'dep_airport': dep_apt,
                        'arr_airport': arr_apt,
                        'flight_ref': flight_ref,
                        'status': status,
                        'fca_ids': fca_ids,
                        'source_timestamp': source_ts,
                        'entry_time': entry_time,
                        'exit_time': exit_time,
                    })

            # ── RSTR: Restriction (ground stop, MIT) ──
            elif msg_type == 'RSTR':
                rstr = {
                    'type': 'tfms_restriction',
                    'msg_type': 'RSTR',
                    'source_facility': source_facility,
                    'source_timestamp': source_ts,
                }
                for elem in fi_msg.iter():
                    lname = _local(elem.tag)
                    if lname == 'element' and elem.text:
                        rstr['element'] = elem.text
                    elif lname == 'elementType' and elem.text:
                        rstr['element_type'] = elem.text
                    elif lname == 'controlElement' and elem.text:
                        rstr['control_element'] = elem.text
                    elif lname == 'startTime' and elem.text:
                        rstr.setdefault('start_time', elem.text)
                    elif lname == 'endTime' and elem.text:
                        rstr.setdefault('end_time', elem.text)
                    elif lname == 'mitValue' and elem.text:
                        rstr['mit_value'] = elem.text
                    elif lname == 'avgDelay' and elem.text:
                        rstr['avg_delay_minutes'] = elem.text

                combined = str(rstr).upper()
                if airport_icao:
                    if airport_icao not in combined and (not airport_3 or airport_3 not in combined):
                        continue
                if keyword_upper and keyword_upper not in combined:
                    continue

                results.append(rstr)

    return results


# ============================================================
# TFDM Parser
# ============================================================

def _parse_duration(iso_duration: str) -> int:
    """Parse ISO 8601 duration string to minutes. PT46M -> 46, PT1H30M -> 90."""
    if not iso_duration:
        return None
    m = re.match(r'PT(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?', iso_duration)
    if not m:
        return None
    hours = int(m.group(1) or 0)
    minutes = int(m.group(2) or 0)
    seconds = int(m.group(3) or 0)
    return hours * 60 + minutes + (1 if seconds >= 30 else 0)


# TFDM deployment status as of Aug 2026 — JFK and LGA NOT yet deployed.
TFDM_AIRPORTS = [
    'KMIA', 'KLAX', 'KCLT', 'KSFO', 'KDCA', 'KIAD', 'KSAT', 'KEWR',
    'KSEA', 'KLAS', 'KTEB', 'KSAN', 'KIAH', 'KPHX', 'KFLL', 'KOAK',
    'KRDU', 'KHOU', 'KIND', 'KSJC', 'KCLE', 'KHPN', 'KAUS', 'KRSW',
    'KDAY', 'KCMH', 'KMDW', 'KGEG',
]


def parse_tfdm(messages: list, airport: str = None, flight: str = None) -> list:
    """
    Parse TFDM NasMessage/TfdmFlightType data (surface management, departure sequencing).
    Filter by airport (AERODROME header) or flight callsign.

    Key fields extracted:
      off_block_time_scheduled  — scheduled gate push (OOOI)
      runway_departure_earliest — TFDM earliest wheels-up time
      runway_departure_estimated— TFDM estimated wheels-up time
      taxi_out_minutes          — total estimated taxi-out time
      queue_wait_minutes        — departure queue wait (surface congestion indicator)
      flight_state              — SCHEDULED / PUSHBACK / AIRBORNE / CANCELLED
      runway_assigned           — assigned departure runway
      runway_predicted          — predicted arrival runway

    NOTE: JFK and LGA are NOT currently in TFDM. Active NY-area airport: KEWR.
    Use tfms-flight for JFK track/ETA data instead.
    """
    results = []
    flight_upper = flight.upper().replace(' ', '') if flight else None
    airport_icao = airport.upper() if airport else None

    for headers, xml_body in messages:
        aerodrome = headers.get('AERODROME', '')
        arr_apt_hdr = headers.get('ARRIVAL_AIRPORT', '')
        dep_apt_hdr = headers.get('DEPARTURE_AIRPORT', '')

        if airport_icao:
            matched = any(
                airport_icao in x.upper()
                for x in [aerodrome, arr_apt_hdr, dep_apt_hdr]
            )
            if not matched:
                continue

        msg_type = headers.get('MESSAGE_TYPE', '')  # FlightAdd, FlightUpdate, FlightDelete
        airline_hdr = headers.get('AIRLINE', '')

        try:
            root = ET.fromstring(xml_body)
        except ET.ParseError:
            continue

        for fl in root.iter():
            if _local(fl.tag) != 'flight':
                continue

            acid = major_carrier = tfdm_id = tfms_id = creator_airport = ''
            for elem in fl.iter():
                lname = _local(elem.tag)
                if lname == 'flightIdentification':
                    acid = elem.get('aircraftIdentification', '')
                    major_carrier = elem.get('majorCarrierIdentifier', '')
                elif lname == 'tfdmId' and elem.text and not tfdm_id:
                    tfdm_id = elem.text
                elif lname == 'tfmId' and elem.text:
                    tfms_id = elem.text
                elif lname == 'tfdmIdCreatorAirport':
                    creator_airport = elem.get('locationIndicator', '')

            if flight_upper and flight_upper not in acid.upper().replace(' ', ''):
                continue

            record = {
                'type': 'tfdm_flight',
                'msg_type': msg_type,
                'flight_id': acid,
                'airline': major_carrier or airline_hdr,
                'tfdm_id': tfdm_id,
                'tfms_id': tfms_id,
                'aerodrome': aerodrome,
                'creator_airport': creator_airport,
                'timestamp': headers.get('TIME_STAMP', ''),
            }

            # ── Departure data ──
            for dep in fl:
                if _local(dep.tag) != 'departure':
                    continue
                record['dep_airport'] = dep.get('departurePointText', '')

                for child in dep:
                    lname = _local(child.tag)

                    if lname == 'offBlockTime':
                        for gc in child:
                            if _local(gc.tag) == 'initial' and gc.text:
                                record['off_block_time_scheduled'] = gc.text

                    elif lname == 'runwayDepartureTime':
                        for timing in child:
                            tlname = _local(timing.tag)
                            for tc in timing:
                                if _local(tc.tag) == 'time' and tc.text:
                                    if tlname == 'earliest':
                                        record['runway_departure_earliest'] = tc.text
                                    elif tlname == 'estimated':
                                        record['runway_departure_estimated'] = tc.text

                    elif lname == 'departureTaxiTime':
                        for tc in child:
                            clname = _local(tc.tag)
                            if clname == 'totalEstimatedTaxiOutTime' and tc.text:
                                record['taxi_out_minutes'] = _parse_duration(tc.text)
                                record['taxi_out_raw'] = tc.text
                            elif clname == 'estimatedDepartureQueueWaitingTime' and tc.text:
                                record['queue_wait_minutes'] = _parse_duration(tc.text)
                                record['queue_wait_raw'] = tc.text
                break

            # ── Arrival data ──
            for arr in fl:
                if _local(arr.tag) != 'arrival':
                    continue
                record['arr_airport'] = arr.get('destinationPointText', '')
                for elem in arr.iter():
                    lname = _local(elem.tag)
                    if lname == 'runwayPredicted':
                        record['runway_predicted'] = elem.get('runwayDesignator', '')
                    elif lname == 'runwayAssigned':
                        record['runway_assigned'] = elem.get('runwayDesignator', '')
                    elif lname == 'designatedPoint' and elem.text:
                        record['arrival_fix'] = elem.text
                break

            # ── Flight state ──
            for elem in fl.iter():
                if _local(elem.tag) == 'tfdmFlightState':
                    record['flight_state'] = elem.get('value', '')
                    break

            results.append(record)

    return results


# ============================================================
# Main CLI
# ============================================================

PARSERS = {
    'tbfm': parse_tbfm,
    'itws': parse_itws,
    'notams': parse_notams,
    'sfdps': parse_sfdps,
    'stdds': parse_stdds,
    'tfms-flight': parse_tfms_flight,
    'tfms-flow': parse_tfms_flow,
    'tfdm': parse_tfdm,
}

# Map from subcommand name to config queue key (for feeds sharing a queue)
FEED_QUEUE_MAP = {
    'tbfm': 'tbfm',
    'itws': 'itws',
    'notams': 'notams',
    'sfdps': 'sfdps',
    'stdds': 'stdds',
    'tfms-flight': 'tfms',  # Both tfms subcommands use the same broker queue
    'tfms-flow': 'tfms',
    'tfdm': 'tfdm',
}


def main():
    parser = argparse.ArgumentParser(
        description='FAA SWIM Consumer — query live FAA data feeds',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  %(prog)s tbfm --airport KJFK --duration 15
  %(prog)s itws --airport KJFK
  %(prog)s notams --airport KJFK --duration 20
  %(prog)s sfdps --flight DAL182
  %(prog)s stdds --airport KJFK --duration 10
        """,
    )
    parser.add_argument(
        'feed',
        choices=['tbfm', 'itws', 'notams', 'sfdps', 'stdds', 'tfms-flight', 'tfms-flow', 'tfdm'],
        help='SWIM feed to consume',
    )
    parser.add_argument('--airport', '-a', help='Airport ICAO code (e.g., KJFK)')
    parser.add_argument('--flight', '-f', help='Flight ID filter (e.g., DAL182, AAL1873)')
    parser.add_argument('--keyword', '-k',
                        help='Keyword filter for tfms-flow (e.g., GDP, EDCT, JFK, HOLD)')
    parser.add_argument('--duration', '-d', type=int, default=12,
                        help='Seconds to consume messages (default: 12)')
    parser.add_argument('--raw', action='store_true',
                        help='Show raw message count and headers')
    parser.add_argument('--limit', '-n', type=int, default=50,
                        help='Max results to return (default: 50)')

    args = parser.parse_args()

    # Get password from environment
    password = os.environ.get('SWIM_PASSWORD', '')
    if not password:
        print(json.dumps({'error': 'SWIM_PASSWORD environment variable not set'}))
        sys.exit(1)

    # Resolve config queue key for this subcommand
    queue_key = FEED_QUEUE_MAP.get(args.feed, args.feed)

    # Consume messages
    print(f"Connecting to SWIM {args.feed.upper()} feed ({queue_key.upper()} queue) for {args.duration}s...",
          file=sys.stderr)
    raw_messages = run_consumer(queue_key, args.duration, password)

    # If the JVM never got off the ground, say so instead of returning an
    # empty result set that looks like "the feed was quiet".
    if not raw_messages:
        problem = stderr_diagnostic()
        if problem:
            print(json.dumps({
                'feed': args.feed,
                'error': 'SWIM consumer failed to run',
                'detail': problem,
                'hint': ('The jumpstart JAR requires Java 25 or newer. '
                         'Run `java -version` to check.')
                        if 'UnsupportedClassVersionError' in problem else '',
                'stderr_tail': LAST_STDERR[-1500:],
            }, indent=2))
            sys.exit(1)

    if args.raw:
        print(f"\nRaw messages received: {len(raw_messages)}", file=sys.stderr)
        for i, (headers, _) in enumerate(raw_messages[:5]):
            print(f"  [{i}] {json.dumps(headers, indent=2)}", file=sys.stderr)

    # Parse and filter
    parse_fn = PARSERS[args.feed]
    kwargs = {}
    if args.airport:
        kwargs['airport'] = args.airport
    if args.flight and args.feed in ('tbfm', 'sfdps', 'tfms-flight', 'tfdm'):
        kwargs['flight'] = args.flight
    if args.keyword and args.feed == 'tfms-flow':
        kwargs['keyword'] = args.keyword

    results = parse_fn(raw_messages, **kwargs)

    # Deduplicate frequent-update feeds
    if args.feed in ('tbfm', 'tfms-flight', 'tfdm'):
        seen = set()
        deduped = []
        for r in results:
            if args.feed == 'tbfm':
                key = (r.get('flight_id', ''), r.get('data_group', ''),
                       json.dumps(r.get('eta', {}), sort_keys=True))
            elif args.feed == 'tfms-flight':
                key = (r.get('flight_id', ''), r.get('msg_type', ''),
                       r.get('position_time', ''), r.get('source_timestamp', ''))
            else:  # tfdm
                key = (r.get('tfdm_id', ''), r.get('msg_type', ''),
                       r.get('timestamp', ''))
            if key not in seen:
                seen.add(key)
                deduped.append(r)
        results = deduped

    # Limit output
    results = results[:args.limit]

    # Output
    output = {
        'feed': args.feed,
        'query': {
            'airport': args.airport,
            'flight': args.flight,
            'duration_seconds': args.duration,
        },
        'timestamp': datetime.now(timezone.utc).isoformat(),
        'total_raw_messages': len(raw_messages),
        'filtered_results': len(results),
        'results': results,
    }

    print(json.dumps(output, indent=2))


if __name__ == '__main__':
    main()
