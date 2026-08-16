# Data Sources — API Reference

Complete reference for every data source used by the Pro Flight Tracker.
Scripts implement all of this; read this when debugging a failure or
explaining data to the user.

## Flight Data APIs

### AeroAPI v4 (FlightAware)
- **Secret Label:** `AEROAPI Key` → env `AEROAPI_KEY`
- **Base URL:** `https://aeroapi.flightaware.com/aeroapi`
- **Auth:** `x-apikey` header
- **Endpoints Used:**
  - `GET /flights/{ident}` — flight status, times, gates, tail number
  - `GET /flights/{fa_flight_id}/route` — filed route
  - `GET /flights/{fa_flight_id}/position` — last known position
- **Key Fields:** `fa_flight_id`, `registration`, `aircraft_type`, `inbound_fa_flight_id`, `scheduled_out/in`, `estimated_out/in`, `actual_out/in`, `gate_origin/destination`, `status`
- **Notes:**
  - Primary source for equipment chain tracing (inbound_fa_flight_id)
  - Times in ISO 8601 UTC
  - Rate limits apply — use judiciously

### ADS-B Exchange (RapidAPI)
- **Secret Label:** `adsbexchange-com1.p.rapidapi.com` → env `ADSB_EXCHANGE_KEY`
- **Base URL:** `https://adsbexchange-com1.p.rapidapi.com`
- **Auth:** `x-rapidapi-key` + `x-rapidapi-host` headers
- **Endpoints Used:**
  - `GET /v2/registration/{reg}/` — position by tail number
  - `GET /v2/callsign/{callsign}/` — position by callsign
- **Response:** `ac` array, each with: `lat`, `lon`, `alt_baro` (ft or "ground"), `gs` (kts), `track` (heading), `baro_rate` (ft/min), `gnd` (boolean), `r` (registration), `t` (type), `hex` (ICAO24)
- **Priority:** First choice for real-time position — fastest updates

### OpenSky Network
- **Secret Label:** `OPENSKY API KEY` → env `OPENSKY_API_KEY`
- **Auth:** Basic auth (username:password format in the secret), or anonymous (rate-limited)
- **Base URL:** `https://opensky-network.org/api`
- **Endpoints:**
  - `GET /states/all?icao24={hex}` — by ICAO24 hex code
  - `GET /states/all?callsign={cs}` — by callsign
- **Response:** `states` array of state vectors: [icao24, callsign, origin_country, time_position, last_contact, longitude, latitude, baro_altitude (m), on_ground, velocity (m/s), true_track, vertical_rate (m/s), ...]
- **Convert:** altitude m→ft (×3.28084), velocity m/s→kts (×1.94384)
- **Priority:** Fallback when ADS-B Exchange is unavailable

## Aviation Weather APIs (Free, No Auth)

### aviationweather.gov — METAR
- `GET https://aviationweather.gov/api/data/metar?ids={KJFK,KLGA}&format=json`
- Returns array of observation objects
- Key fields: `rawOb`, `wdir`, `wspd`, `wgst`, `visib`, `clouds[]` (cover, base, type), `temp`, `dewp`, `altim`, `fltCat` (VFR/MVFR/IFR/LIFR), `wxString`

### aviationweather.gov — TAF
- `GET https://aviationweather.gov/api/data/taf?ids={KJFK}&format=json`
- Returns array of TAF objects with `fcsts[]` forecast periods
- Each period: `fcstChange` (FM/BECMG/TEMPO/PROB), `wdir`, `wspd`, `wgst`, `visib`, `clouds[]`, `wxString`, `timeFrom`, `timeTo`
- **Coverage:** typically 24–30 hours from issue time
- **Critical for flight tracker:** the gold standard for airport-specific weather windows

### aviationweather.gov — SIGMET / Convective SIGMET
- `GET https://aviationweather.gov/api/data/airsigmet?format=json&type=sigmet`
- `GET https://aviationweather.gov/api/data/airsigmet?format=json&type=conv`
- Fields: `airSigmetType`, `hazard`, `severity`, `validTimeFrom/To`, `coords[]`, `altitudeHi/Low`, `movementDir/Spd`, `rawAirSigmet`
- **Always check:** expiration time vs. flight arrival window

### aviationweather.gov — PIREPs
- `GET https://aviationweather.gov/api/data/pirep?format=json&id={KJFK}&distance={200}`
- Fields: `pirepType` (UA=routine, UUA=urgent), `tbInt1/2` (turbulence intensity), `tbType1/2`, `icgInt1/2` (icing), `fltLvl`, `acType`
- Turbulence scale: NEG / LGT / MOD / SEV / EXTM

### FAA NASSTATUS
- `GET https://nasstatus.faa.gov/api/airport-status-information`
- **Returns XML** with all active delay programs nationwide
- Sections: Ground Delay Programs, Ground Stops, Arrival/Departure Delays, Airport Closures
- GDP fields: ARPT, Reason, Avg delay, Max delay
- Uses IATA codes (JFK, not KJFK) — script maps ICAO↔IATA

### Beacon Weather Ensemble
- Located at `/home/daytona/agentd/.skills/agent/beacon/scripts/beacon_weather.py`
- Multi-model ensemble: GFS, ECMWF, ICON, GEM, NWS, Pirate Weather, WeatherAPI, MET Norway
- Provides mm totals + inter-model spread (low spread = high confidence)
- Use for precipitation confidence at both airports
- Secrets: `WEATHERAPI_KEY`, `PIRATE_WEATHER_KEY`, `WEATHER_USER_AGENT`

### NWS Hourly Forecast
- Already included in Beacon ensemble (source: `nws`)
- Also accessible directly via `api.weather.gov/points/{lat},{lon}` → forecastHourly
- Extends beyond TAF coverage — useful for TAF gap periods

## Enhanced Operations APIs (v1.1 — No Auth Required)

### aviationweather.gov — G-AIRMET
- `GET https://aviationweather.gov/api/data/gairmet?format=json&hazard={turb-hi|turb-lo|llws|ifr|mt-obsc|sfc-wind|ice}`
- Returns JSON array of polygon objects
- Key fields: `hazard`, `severity` (MOD/SEV/EXTM), `base` (FL as string, e.g. "320"), `top` (FL), `coords[]` (lat/lon as strings), `validTime`, `expireTime` (Unix timestamp), `product` (TANGO/SIERRA/ZULU)
- **Products:** TANGO = turbulence, SIERRA = IFR/mountain obscuration, ZULU = icing/freezing level
- Polygons define geographic areas — script checks if route passes within 75 NM
- **No auth, free**

### Blitzortung — Real-Time Lightning
- WebSocket: `wss://ws1.blitzortung.org/`
- Subscribe with: `{"a": 111}` (global feed)
- Messages are **LZW-compressed** text — decompress before JSON parsing
- Decompressed JSON fields: `lat`, `lon`, `time` (nanosecond timestamp), `pol` (polarity), `mds` (signal duration μs), `mcg` (station count), `sig[]` (detecting stations)
- Worldwide coverage, ~seconds latency
- Filter by haversine distance from airport coordinates
- **Ramp closure threshold: 5 NM**
- **No auth, free, unlimited**

### FAA RVR (Runway Visual Range)
- `GET https://rvr.data.faa.gov/cgi-bin/rvr-details.pl?content=table&airport={FAA_CODE}`
- Returns HTML page with table — parse with BeautifulSoup
- Table columns: RWY (runway), TD (touchdown zone), MP (midpoint), RO (rollout), E (equipment status), C (calibration)
- Values: `>6000` (above 6000 ft), numeric feet, `FFF` (sensor fault), blank (no sensor)
- Updates every 60 seconds, ~120 US airports
- Uses FAA/IATA codes (JFK, not KJFK) — script handles ICAO mapping
- Also parse METAR RVR groups: `R04L/2000V4000FT` pattern
- **No auth, free**

### ATFM Inference (Eurocontrol Heuristic)
- Not a direct API — infers CTOT (Calculated Take-Off Time) regulation from AeroAPI delay patterns
- Uses **AEROAPI_KEY** (same secret as flight_data.py)
- Checks: destination in Eurocontrol airspace → analyzes departure delay signature
- Direct Eurocontrol Network Manager B2B access requires institutional PKI certificate (airline/ANSP only)
- Eurocontrol ICAO prefixes: E* (Northern Europe), L* (Southern Europe), BI (Iceland), GC/GE (Canary/Ceuta), UD/UG/UK (Caucasus/Ukraine)

## FAA SWIM Direct Feeds (v1.2)

All SWIM feeds use the same infrastructure:
- **Transport:** Solace JMS over TLS (tcps://ems1.swim.faa.gov:55443, ems2 for NOTAMs)
- **Client:** L3Harris jumpstart JAR at `swim/lib/jumpstart-jar-with-dependencies.jar`
- **Config:** `swim/config.json` — queue names, VPNs, broker URLs
- **Auth:** Username (bradleysinger.gmail.com) + password (secret: "SWIFT Portal Connection Password")
- **Java:** Requires OpenJDK 25+ (`-Djava.net.preferIPv4Stack=true` required)
- **Wrapper:** `scripts/swim_consumer.py` — invokes JAR, captures stdout, parses XML, filters, returns JSON

### TBFM (Time-Based Flow Management)
- **Queue VPN:** TBFM
- **Data:** Arrival metering publications — estimated times at meter fixes, runway ETAs, scheduled times
- **XML namespace:** `urn:us:gov:dot:faa:atm:tfm:tbfmmeteringpublication:1.1.0`
- **Key elements:** `<air>` with attributes: `aid` (flight ID), `gufi`, `apt` (dest), `dap` (dep), `airType` (NEW/AMD)
- **ETA fields:** `eta_mfx` (meter fix), `eta_dfx` (departure fix), `eta_sfx` (secondary fix), `eta_rwy` (runway)
- **Header filters:** `DEST_APT`, `DEPART_APT`, `ARTCC`, `DATA_GROUP` (flt/eta/sta/mrp/sch)
- **Volume:** ~1,500+ messages per 12s window (nationwide)
- **Usage:** `swim_consumer.py tbfm --airport KJFK --duration 12`

### SFDPS (Flight Data Publication Service)
- **Queue VPN:** FDPS
- **Data:** FIXM 3.0 flight position reports — lat/lon, altitude, speed, flight status, GUFI
- **XML namespace:** `http://www.faa.aero/nas/3.0` (MessageCollection)
- **Key elements:** `<flight>` with: `<arrival arrivalPoint="KJFK"/>`, `<departure departurePoint="KLAX"/>`, `<enRoute>/<position>` (positionTime, altitude, actualSpeed), `<flightIdentification aircraftIdentification="DAL182"/>`, `<flightStatus fdpsFlightStatus="ACTIVE"/>`
- **Message type:** `BATCH_TH_FIXM` — batch track history, contains multiple flights per message
- **Header filters:** `FDPS_SourceFacility` (ARTCC), `FDPS_MessageType`
- **Volume:** ~1,400+ messages per 10s (nationwide, large XML per message)
- **Usage:** `swim_consumer.py sfdps --airport KJFK --duration 10` or `--flight DAL182`

### ITWS (Integrated Terminal Weather System)
- **Queue VPN:** ITWS
- **Data:** Terminal weather alerts — gust fronts, wind shear, microbursts, storm cells, precipitation, lightning, forecast accuracy
- **XML:** Custom ITWS schema with `<itws_msg>` wrapper, `<product_header>` (product name, gen/expiration times), and specific alert elements (`<gf_eti>`, `<ws_alert>`, `<mb_alert>`, `<storm_motion>`)
- **Header filters:** `airport`, `ITWSsite`, `productID`, `DEX_SOURCE_TYPE` (ITWS_Alert)
- **Airport codes:** 3-letter (JFK, not KJFK)
- **Volume:** Very high (~30,000 messages per 12s nationwide), most are routine products
- **Usage:** `swim_consumer.py itws --airport KJFK --duration 12`

### NOTAMs (AIM FNS — NOTAM Distribution)
- **Queue VPN:** AIM_FNS (on ems2, not ems1)
- **Data:** Real-time NOTAM publication in AIXM 5.1 format — runway/taxiway closures, navaid outages, airspace restrictions
- **XML namespace:** `http://www.aixm.aero/schema/5.1/message` (AIXMBasicMessage)
- **Key elements:** `<event:NOTAM>` with `<event:text>`, `<event:effectiveStart/End>`, `<event:location>`, `<event:number>`, `<event:year>`
- **Header filters:** `us_gov_dot_faa_aim_fns_nds_ICAOId` (e.g., KJFK), `us_gov_dot_faa_aim_fns_nds_NOTAMStatus` (ACTIVE/CANCELLED), `us_gov_dot_faa_aim_fns_nds_NOTAMFunction` (NOTAMN/NOTAMC/NOTAMR)
- **Important:** Event-driven feed — only receives NOTAMs as they're issued/changed. A 15s window may capture 0 NOTAMs for a specific airport. For comprehensive NOTAM review, consume over a longer period.
- **Usage:** `swim_consumer.py notams --airport KJFK --duration 18`

### STDDS (Surface Data Distribution)
- **Queue VPN:** STDDS
- **Data:** Two message types:
  - **SMES (ASDEX):** Surface movement events — aircraft/vehicle positions on taxiways/runways. `<airport>` element directly identifies the airport.
  - **TAIS (Terminal Automation):** TRACON track data — aircraft positions in terminal airspace. Uses TRACON codes, NOT airport codes.
- **TRACON mapping:** N90=JFK/LGA/EWR/TEB/HPN, SCT=LAX/SNA/BUR, NCT=SFO/OAK/SJC, C90=ORD/MDW, A80=ATL, PCT=DCA/IAD/BWI, D10=DFW/DAL
- **Header filters:** `airport` (SMES), `tracon`/`srcTracon` (TAIS), `mex` (SMES/TAIS), `msgType`
- **Usage:** `swim_consumer.py stdds --airport KJFK --duration 10`

### TFMS (Traffic Flow Management System) — v1.3
- **Queue VPN:** TFMS (on **ems2**, not ems1)
- **Broker:** `tcps://ems2.swim.faa.gov:55443`
- **Volume:** ~1,500+ messages per 12s (nationwide, very high volume)
- **TFMDataClass values:**
  - `FlightData` — Flight position and route updates
  - `FlowInformation` — ATCSCC advisories, TMI assignments, restrictions
  - `Status` — System status messages
- **FlightData XML (namespace: `urn:us:gov:dot:faa:atm:tfm:tfmdataservice`):**
  - Root: `tfmDataService/fltdOutput/fltdMessage`
  - `fltdMessage` attrs: `acid` (callsign), `airline`, `arrArpt` (ICAO), `depArpt` (ICAO), `msgType`, `sourceTimeStamp`, `flightRef`
  - `msgType` variants: `trackInformation`, `departureInformation`, `flightPlanAmendmentInformation`, `arrivalInformation`, `boundaryCrossingUpdate`
  - **trackInformation:** `<speed>` (kts), `<simpleAltitude>` (FL×100, e.g., "370" = FL370; suffix "C" = climbing), `<latitudeDMS>` / `<longitudeDMS>` (DMS attrs: degrees/direction/minutes/seconds), `<timeAtPosition>`, `<eta etaType="ESTIMATED" timeValue="..."/>`, `<routeOfFlight>` (legacy format), `<arrivalFixAndTime fixName="JFUND" arrTime="..."/>`, `<departureFixAndTime fixName="RBV" arrTime="..."/>`, `<flightTraversalData2>` (full waypoint + sector sequence)
  - **departureInformation:** `<timeOfDeparture>` (actual), `<etd etdType="ACTUAL" timeValue="..."/>`, `<eta etaType="ESTIMATED" timeValue="..."/>`
- **FlowInformation XML (namespace: `urn:us:gov:dot:faa:atm:tfm:flowinformation`):**
  - Root: `tfmDataService/fiOutput/fiMessage[@msgType]`
  - **GADV** (General Advisory from ATCSCC): `<advisoryTitle>`, `<advisoryText>` (full free-text advisory including GDP scope, average delay, ADL time, CNX notices), `<advisoryNumber>`, `<effectivePeriod>/<startTime>/<endTime>`, `<origin>`, `<dateSent>`
  - **TMI_FLIGHT_LIST** (per-flight FCA assignments): `tmiFlightDataList/flightData` with `<aircraftId>`, `<departurePoint>/<airport>`, `<arrivalPoint>/<airport>`, `<status>` (ACTIVE/COMPLETE), `<tmiFlightInfoList>/<tmi>/<fcaId>` (FCA identifier), entry/exit times
  - **RSTR** (Restrictions): `<element>` (airport/fix), `<elementType>`, `<controlElement>`, `<mitValue>` (MIT distance), `<avgDelay>`
- **Usage:**
  ```
  swim_consumer.py tfms-flight --airport KJFK --duration 14
  swim_consumer.py tfms-flight --flight DAL182 --duration 12
  swim_consumer.py tfms-flow --airport KJFK --duration 15
  swim_consumer.py tfms-flow --keyword GDP --duration 15
  swim_consumer.py tfms-flow --keyword JFK --duration 15
  ```

### TFDM (Terminal Flight Data Manager) — v1.3
- **Queue VPN:** TFDM (on **ems3** — new broker, not ems1/ems2)
- **Broker:** `tcps://ems3.swim.faa.gov:55443`
- **Volume:** ~3,000–4,000 messages per 14s (high volume)
- **Deployment:** Tower-based; NOT at all airports. Active as of Aug 2026: MIA, LAX, CLT, SFO, DCA, IAD, SAT, EWR, SEA, LAS, TEB, SAN, IAH, PHX, FLL, OAK, RDU, HOU, IND, SJC, CLE, HPN, AUS, RSW, DAY, CMH, MDW, GEG. **JFK and LGA are NOT yet deployed.**
- **Message types:** `FlightAdd`, `FlightUpdate`, `FlightDelete`
- **XML schema:** `http://www.faa.aero/nas/4.1` (NasMessage/TfdmFlightType) with FIXM 4.0 flight elements
- **Key XML elements:**
  - `<fx:flightIdentification aircraftIdentification="SWA1565">` — callsign
  - `<nas:tfdmId>` — TFDM internal flight UUID
  - `<fx:departure departurePointText="KLAX">`:
    - `<nas:offBlockTime>/<nas:initial>` — scheduled gate push (UTC)
    - `<nas:runwayDepartureTime>/<nas:earliest>/<nas:time>` — TFDM-computed earliest wheels-up
    - `<nas:runwayDepartureTime>/<nas:estimated>/<nas:time>` — TFDM-computed estimated wheels-up
    - `<nas:departureTaxiTime>/<nas:totalEstimatedTaxiOutTime>` — ISO 8601 duration (PT46M = 46 min)
    - `<nas:departureTaxiTime>/<nas:estimatedDepartureQueueWaitingTime>` — queue congestion time (PT0S = no queue; PT40M = heavy congestion)
  - `<fx:arrival destinationPointText="KOAK">`:
    - `<nas:runwayPredicted runwayDesignator="04L">` — predicted arrival runway
    - `<nas:runwayAssigned runwayDesignator="04L">` — ATC-assigned arrival runway
  - `<nas:flightStatus>/<nas:tfdmFlightState value="SCHEDULED">` — state: SCHEDULED / FILED / RAMP_TAXI_OUT / PUSHBACK / AIRBORNE / CANCELLED
- **Header filters:** `AERODROME` (reporting airport), `ARRIVAL_AIRPORT`, `DEPARTURE_AIRPORT`, `AIRLINE`, `MESSAGE_TYPE`, `PRIVACY_LEVEL`
- **Usage:**
  ```
  swim_consumer.py tfdm --airport KEWR --duration 14
  swim_consumer.py tfdm --airport KLAX --duration 14
  swim_consumer.py tfdm --flight SWA1565 --duration 12
  ```
