#!/usr/bin/env python3
"""
Long-running SWIM consumer for Pro Flight Tracker.

One persistent Java jumpstart process per configured queue (default: TFMS +
ITWS), held open by a watchdog thread in the LEADER process only. Messages
are parsed as they arrive with the exact same parsers the request-path
subprocess uses (scripts/swim_consumer.py) and written to the store:

  - every parsed record  -> store.swim_record_events (rolling 60-min window)
  - every EDCT found     -> store.cache_edct (so /api/flight/live?edct=cached
                            returns FAA-controlled times before anyone runs
                            a brief)
  - liveness             -> store.swim_daemon_heartbeat (request paths check
                            this before deciding daemon-serve vs subprocess)

Why this exists: the per-request consumer model had two structural problems.
Each call spawned a JVM + TLS JMS connection (5-15s, hence 45s timeouts on
every brief and check), and JMS queue semantics deliver each message to
exactly ONE consumer — two overlapping requests silently split the stream,
so the request that needed an EDCT could miss it. One consumer per queue,
running continuously, fixes both: no spawn cost, no competition, and the
message that matters is captured whether or not anyone was asking.

Enabled automatically on the leader when SWIM_PASSWORD is set; disable with
SWIM_DAEMON=0. Queues via SWIM_DAEMON_QUEUES (comma-separated config-queue
names, default "tfms,itws").
"""

import os
import subprocess
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent / "scripts"))

import store  # noqa: E402
import analysis  # noqa: E402
import swim_consumer as sc  # noqa: E402  (parsers, config, run script path)

# Which parsers run against each queue's messages. tfms carries both the
# per-flight stream (EDCTs) and flow advisories, so it feeds two parsers.
QUEUE_PARSERS = {
    "tfms": [("tfms-flight", sc.parse_tfms_flight),
             ("tfms-flow", sc.parse_tfms_flow)],
    "itws": [("itws", sc.parse_itws)],
    "tbfm": [("tbfm", sc.parse_tbfm)],
    "tfdm": [("tfdm", sc.parse_tfdm)],
    "stdds": [("stdds", sc.parse_stdds)],
    "sfdps": [("sfdps", sc.parse_sfdps)],
    "notams": [("notams", sc.parse_notams)],
}

# ICAO callsign prefix -> IATA airline code, for keying EDCTs the way the
# client asks for them (DL5187, not DAL5187). Inverse of app._AIRLINE_MAP;
# injected via start() so there is a single source of truth.
_ICAO_TO_IATA_AIRLINE: dict = {}

_HEARTBEAT_SECONDS = 20
_BACKOFF_START = 10
_BACKOFF_MAX = 300

_threads: list = []
_stop = threading.Event()

import collections

_STDERR_RING_MAX = 200
_stderr_rings: dict = {}  # queue_name -> deque[str], last N stderr lines
_stderr_lock = threading.Lock()
_child_procs: list = []   # live JVM consumer handles, so stop() can kill them
_proc_lock = threading.Lock()


class _StreamParser:
    """Incremental version of swim_consumer.parse_raw_output.

    Same three-state machine (properties / xml body), but fed one line at a
    time; a completed (headers, xml) message is emitted as soon as its
    boundary — the next property line or XML declaration — arrives.
    """

    def __init__(self):
        self.headers = {}
        self.xml_lines = []
        self.in_xml = False

    def feed(self, line: str):
        """Feed one stdout line; returns a (headers, xml) tuple or None."""
        stripped = line.strip()
        if not stripped:
            return None

        m = sc.re.match(r"^Property name = (.+?); value = (.+)$", stripped)
        if m:
            emitted = None
            if self.in_xml and self.xml_lines:
                xml_body = "\n".join(self.xml_lines).strip()
                if xml_body:
                    emitted = (dict(self.headers), xml_body)
                self.headers = {}
                self.xml_lines = []
                self.in_xml = False
            self.headers[m.group(1)] = m.group(2)
            return emitted

        if stripped.startswith("<?xml "):
            emitted = None
            if self.in_xml and self.xml_lines:
                xml_body = "\n".join(self.xml_lines).strip()
                if xml_body:
                    emitted = (dict(self.headers), xml_body)
                self.headers = {}
                self.xml_lines = []
            self.in_xml = True
            self.xml_lines = [stripped]
            return emitted

        if self.in_xml:
            self.xml_lines.append(stripped)
        return None

    def flush(self):
        """Emit whatever is pending (call when the process exits)."""
        if self.in_xml and self.xml_lines:
            xml_body = "\n".join(self.xml_lines).strip()
            self.headers, self.xml_lines, self.in_xml = {}, [], False
            if xml_body:
                return (dict(self.headers), xml_body)
        return None


def _log(msg: str) -> None:
    print(f"[SWIM-DAEMON] {msg}", file=sys.stderr, flush=True)


def _pump_stderr(queue_name: str, pipe) -> None:
    """Drain a consumer's stderr into a bounded ring buffer.

    DEVNULL used to swallow everything — including the JVM's reason for
    dying. The ring keeps the last 200 lines; on an unexpected death the
    watchdog surfaces fatal markers from it.
    """
    ring = collections.deque(maxlen=_STDERR_RING_MAX)
    with _stderr_lock:
        _stderr_rings[queue_name] = ring
    try:
        for raw in pipe:
            ring.append(raw.decode("utf-8", errors="replace").rstrip("\n"))
    except Exception:
        pass
    finally:
        try:
            pipe.close()
        except Exception:
            pass


def _fatal_stderr_line(queue_name: str) -> str:
    """First ring-buffer line matching a known JVM fatal marker, or ''."""
    with _stderr_lock:
        lines = list(_stderr_rings.get(queue_name, ()))
    for line in lines:
        if any(m in line for m in sc.FATAL_STDERR_MARKERS):
            return line.strip()
    return ""


def _stderr_tail(queue_name: str, n: int = 5) -> str:
    with _stderr_lock:
        lines = list(_stderr_rings.get(queue_name, ()))
    return " | ".join(l.strip() for l in lines[-n:] if l.strip())


def _iata_ident_from_callsign(callsign: str) -> str:
    """DAL5187 -> DL5187. Unknown prefixes pass through unchanged."""
    cs = (callsign or "").upper().replace(" ", "")
    for plen in (3, 2):
        prefix, rest = cs[:plen], cs[plen:]
        if rest.isdigit() and prefix in _ICAO_TO_IATA_AIRLINE:
            return _ICAO_TO_IATA_AIRLINE[prefix] + rest
    return cs


def _store_edcts(records: list) -> int:
    """Find EDCT assignments in parsed tfms-flight records; cache per flight."""
    by_flight: dict[str, list] = {}
    for r in records:
        fid = (r.get("flight_id") or "").upper().replace(" ", "")
        if fid:
            by_flight.setdefault(fid, []).append(r)
    stored = 0
    for fid, recs in by_flight.items():
        edct = analysis.extract_edct(recs, fid)
        if not edct.get("edct"):
            continue
        ident = _iata_ident_from_callsign(fid)
        # Key by the slot's own UTC date; get_cached_edct has an any-date
        # fallback for the near-midnight origin-local mismatch.
        date = (edct["edct"] or "")[:10] or \
            datetime.now(timezone.utc).strftime("%Y-%m-%d")
        try:
            store.cache_edct(ident, date, edct)
            stored += 1
        except Exception as exc:
            # cache_edct is best-effort and logs its own failures; this is
            # a last-resort guard so one bad EDCT can't kill the daemon.
            _log(f"cache_edct raised for {ident}: "
                 f"{type(exc).__name__}: {exc}")
    return stored


def _handle_message(queue_name: str, msg: tuple) -> int:
    """Parse one (headers, xml) message with every parser bound to the
    queue and persist the results. Returns records stored."""
    total = 0
    for feed_name, parse_fn in QUEUE_PARSERS.get(queue_name, []):
        try:
            records = parse_fn([msg])
        except Exception as exc:
            _log(f"{queue_name}/{feed_name} parser error: "
                 f"{type(exc).__name__}: {exc}")
            continue
        if not records:
            continue
        if not store.swim_record_events(feed_name, records):
            # Store logs the Postgres failure itself; note here that the
            # daemon's events are degraded to per-worker memory.
            _log(f"{queue_name}/{feed_name}: events fell back to "
                 f"per-worker memory")
        if feed_name == "tfms-flight":
            _store_edcts(records)
        total += len(records)
    return total


def _spawn_consumer(queue_name: str, pw_argfile: str) -> subprocess.Popen:
    """Start the Java client for one queue — long-running, no `timeout`.

    The password reaches the JVM via a 0600 @argfile (never argv — `ps`
    exposes argv to every user on the box). stderr is captured into a
    bounded ring buffer instead of DEVNULL so the watchdog can surface
    the JVM's reason for dying.
    """
    config = sc.load_config()
    feed_cfg = config["queues"][queue_name]
    broker = config["provider_urls"][feed_cfg["broker"]]

    if not os.access(sc.RUN_SCRIPT, os.X_OK):
        try:
            os.chmod(sc.RUN_SCRIPT, 0o755)
        except OSError:
            pass

    cmd = [
        str(sc.RUN_SCRIPT),
        "-Djava.net.preferIPv4Stack=true",
        f"-DproviderUrl={broker}",
        f"-Dqueue={feed_cfg['queue']}",
        f"-DconnectionFactory={config['connection_factory']}",
        f"-Dusername={config['username']}",
        f"@{pw_argfile}",
        f"-Dvpn={feed_cfg['vpn']}",
        "-Doutput=com.harris.cinnato.outputs.StdoutOutput",
        "-Dmetrics=false",
        "-Djson=false",
        "-Dheaders=true",
    ]
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE,
                            cwd=str(sc.SWIM_DIR))
    with _proc_lock:
        _child_procs.append(proc)
    pump = threading.Thread(target=_pump_stderr,
                            args=(queue_name, proc.stderr),
                            daemon=True, name=f"swim-stderr-{queue_name}")
    pump.start()
    return proc


def _consume_queue(queue_name: str, password: str) -> None:
    """Watchdog loop: keep one consumer alive for this queue forever.

    The password @argfile is written once per watchdog thread (not per
    respawn) and deleted when the thread exits.
    """
    pw_argfile = sc.write_password_argfile(password)
    try:
        backoff = _BACKOFF_START
        while not _stop.is_set():
            proc = None
            try:
                proc = _spawn_consumer(queue_name, pw_argfile)
                _log(f"{queue_name}: consumer started (pid={proc.pid})")
                store.swim_daemon_heartbeat(queue_name, restarted=True,
                                            note="started")
                parser = _StreamParser()
                last_beat = time.monotonic()
                got_any = False

                for raw in proc.stdout:  # blocks; ends when process dies
                    if _stop.is_set():
                        break
                    line = raw.decode("utf-8", errors="replace")
                    msg = parser.feed(line)
                    if msg:
                        n = _handle_message(queue_name, msg)
                        got_any = True
                        if n:
                            store.swim_daemon_heartbeat(queue_name,
                                                        messages_delta=n,
                                                        got_message=True)
                            backoff = _BACKOFF_START  # healthy stream
                    if time.monotonic() - last_beat >= _HEARTBEAT_SECONDS:
                        store.swim_daemon_heartbeat(queue_name)
                        last_beat = time.monotonic()

                tail = parser.flush()
                if tail:
                    _handle_message(queue_name, tail)

                rc = proc.wait(timeout=10)
                fatal = _fatal_stderr_line(queue_name)
                if fatal:
                    _log(f"{queue_name}: consumer died rc={rc}: {fatal}")
                else:
                    _log(f"{queue_name}: consumer exited rc={rc}"
                         + ("" if got_any else " (no messages seen)")
                         + (f" stderr tail: {_stderr_tail(queue_name)}"
                            if rc else ""))
            except Exception as exc:
                _log(f"{queue_name}: watchdog error {type(exc).__name__}: {exc}")
            finally:
                if proc:
                    with _proc_lock:
                        if proc in _child_procs:
                            _child_procs.remove(proc)
                    if proc.poll() is None:
                        proc.kill()

            if _stop.is_set():
                return
            store.swim_daemon_heartbeat(queue_name,
                                        note=f"restarting in {backoff}s")
            _stop.wait(backoff)
            backoff = min(backoff * 2, _BACKOFF_MAX)
    finally:
        try:
            os.unlink(pw_argfile)
        except OSError:
            pass


def enabled() -> bool:
    if os.environ.get("SWIM_DAEMON", "").lower() in ("0", "false", "no"):
        return False
    return bool(os.environ.get("SWIM_PASSWORD", ""))


def queues() -> list:
    raw = os.environ.get("SWIM_DAEMON_QUEUES", "tfms,itws")
    return [q.strip() for q in raw.split(",") if q.strip() in QUEUE_PARSERS]


def start(airline_map: dict) -> bool:
    """Start one watchdog thread per configured queue. Leader only.

    airline_map is app.py's IATA->ICAO table; we invert it so EDCTs are
    keyed the way the client queries them.
    """
    global _ICAO_TO_IATA_AIRLINE
    if not enabled():
        _log("disabled (SWIM_DAEMON=0 or SWIM_PASSWORD unset)")
        return False
    _ICAO_TO_IATA_AIRLINE = {v: k for k, v in (airline_map or {}).items()}
    password = os.environ["SWIM_PASSWORD"]
    for q in queues():
        t = threading.Thread(target=_consume_queue, args=(q, password),
                             daemon=True, name=f"swim-daemon-{q}")
        t.start()
        _threads.append(t)
    _log(f"started for queues: {', '.join(queues())}")
    return True


def stop() -> None:
    """Shut the daemon down: stop watchdogs, terminate child JVMs, join up.

    Previously this only set the stop event — the JVM consumers were
    orphaned on every redeploy/restart. Registered via atexit by app.py
    when the daemon starts.
    """
    _stop.set()
    with _proc_lock:
        procs = list(_child_procs)
    for proc in procs:
        try:
            if proc.poll() is None:
                proc.terminate()
        except Exception:
            pass
    for proc in procs:
        try:
            proc.wait(timeout=10)
        except Exception:
            try:
                proc.kill()
            except Exception:
                pass
    for t in list(_threads):
        t.join(timeout=15)
    _log("stopped")
