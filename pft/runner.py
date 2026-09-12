"""Script execution pipeline: cache -> breaker -> in-process/subprocess."""

import concurrent.futures
import json
import os
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import store

from pft.script_modules import (
    flight_data as _mod_flight_data,
    aviation_weather as _mod_aviation_weather,
    airport_ops as _mod_airport_ops,
)
from pft.cache import (
    _TTL_BY_CALL, _annotated, _cache_get, _cache_key,
    _cache_put, _nocache_requested, _script_cache,
    _STALE_MAX_SECONDS, _ttl_for,
)
from pft.breakers import (
    _breaker_open, _breaker_record, _is_upstream_failure,
    _upstream_for,
)
from pft.logging import log
from pft.swim_serve import _SwimStoreUnavailable, _swim_daemon_serve

SCRIPTS_DIR = Path(__file__).resolve().parent / "scripts"


DEFAULT_TIMEOUT = 45  # seconds per script call


SWIM_TIMEOUT = 45     # SWIM feeds need time for JMS connection + JVM startup


                      # (must exceed max --duration by ~15s: JVM start, TLS
                      #  handshake, and JMS teardown all happen outside it)
CHECK_TIMEOUT = 120   # full flight check runs many sources


def _run_subprocess(script: str, args: list, timeout: int = DEFAULT_TIMEOUT,
                    env_extras: dict = None) -> tuple[dict, int]:
    """Run a Python script as subprocess, return (parsed_json, http_status)."""
    env = os.environ.copy()
    # Pass through all API keys from Railway env vars. The prefetch
    # payload is serialized behind a byte cap — never unbounded env JSON.
    if env_extras:
        env.update(_prefetch_env_for_subprocess(env_extras))

    cmd = [sys.executable, str(SCRIPTS_DIR / script)] + args

    try:
        result = subprocess.run(
            cmd, capture_output=True, text=True,
            timeout=timeout, env=env, cwd=str(SCRIPTS_DIR)
        )

        if result.returncode != 0:
            # Scripts report real failures as JSON on stdout and exit nonzero.
            # stderr is progress chatter ("Connecting to SWIM ..."), so prefer
            # stdout — otherwise the useful diagnostic gets thrown away.
            stdout = result.stdout.strip()
            if stdout:
                try:
                    payload = json.loads(stdout)
                    if isinstance(payload, dict):
                        payload.setdefault("returncode", result.returncode)
                        return payload, 500
                except json.JSONDecodeError:
                    pass
            return {
                "error": result.stderr.strip() or "Script failed",
                "stdout_tail": stdout[-1000:],
                "returncode": result.returncode
            }, 500

        # Try to parse JSON from stdout
        stdout = result.stdout.strip()
        if not stdout:
            return {"error": "Empty output", "stderr": result.stderr.strip()}, 500

        try:
            data = json.loads(stdout)
            if isinstance(data, dict):
                # Uniform freshness: every envelope carries a top-level
                # fetched_at = when the SOURCE was pulled (the scripts'
                # pull_time), not when this HTTP response was assembled.
                # The client's "Updated Xs ago" should render this.
                data.setdefault("fetched_at",
                                data.get("pull_time")
                                or datetime.now(timezone.utc).isoformat())
            return data, 200
        except json.JSONDecodeError:
            # Some scripts output multiple JSON objects (one per line)
            lines = stdout.split("\n")
            results = []
            for line in lines:
                line = line.strip()
                if line:
                    try:
                        results.append(json.loads(line))
                    except json.JSONDecodeError:
                        continue
            if results:
                return {"results": results,
                        "fetched_at": datetime.now(timezone.utc).isoformat()}, 200
            return {"raw_output": stdout[:2000], "stderr": result.stderr[:500]}, 200

    except subprocess.TimeoutExpired:
        return {"error": f"Script timed out after {timeout}s"}, 504
    except Exception as e:
        return {"error": f"{type(e).__name__}: {str(e)}"}, 500


_INPROC_MODULES = {
    "flight_data.py": _mod_flight_data,
    "aviation_weather.py": _mod_aviation_weather,
    "airport_ops.py": _mod_airport_ops,
}


# Generously sized: these are IO-bound HTTP fetches, and run_scripts_parallel
# workers block on this pool — it must always be deeper than the outer
# fan-out (max 10) or a burst could deadlock waiting on itself.
_INPROC_POOL = ThreadPoolExecutor(max_workers=32, thread_name_prefix="inproc")


def _run_inprocess(module, script: str, args: list, timeout: int,
                   env_extras: dict = None) -> tuple[dict, int]:
    """Execute a script's dispatch() on a worker thread with a hard timeout.

    A timeout abandons the worker (threads can't be killed) — the underlying
    HTTP calls carry their own 10-12s socket timeouts, so abandoned work
    self-terminates shortly after. Matches subprocess-timeout semantics from
    the caller's point of view.
    """
    try:
        ns = module.build_parser().parse_args([str(a) for a in args])
    except SystemExit:
        # argparse rejected the args — same class of failure as the old
        # "script exited 2" path.
        return {"error": f"Invalid arguments for {script}: {args}"}, 500

    # The prefetch rides on the namespace as the dict itself — no JSON
    # round-trip, and no process-global env var for request threads to race
    # on. (The subprocess path serializes it behind a byte cap instead.)
    if env_extras and env_extras.get("PFT_PREFETCHED_STATUS"):
        ns.prefetched_status = env_extras["PFT_PREFETCHED_STATUS"]

    fut = _INPROC_POOL.submit(module.dispatch, ns)
    try:
        data = fut.result(timeout=timeout)
    except TimeoutError:
        fut.cancel()
        return {"error": f"Script timed out after {timeout}s"}, 504
    except Exception as e:
        return {"error": f"{type(e).__name__}: {str(e)}"}, 500

    if not isinstance(data, dict):
        return {"error": f"{script} returned {type(data).__name__}, expected dict"}, 500
    data.setdefault("fetched_at",
                    data.get("pull_time")
                    or datetime.now(timezone.utc).isoformat())
    return data, 200


def _execute(script: str, args: list, timeout: int,
             env_extras: dict = None) -> tuple[dict, int]:
    module = _INPROC_MODULES.get(script)
    if module is not None:
        return _run_inprocess(module, script, args, timeout, env_extras)
    if script == "swim_consumer.py":
        # Don't spawn a ~10s JVM with an empty password: the script itself
        # exits(1) with a JSON error when SWIM_PASSWORD is unset, so fail
        # loud at the API layer instead of burning the startup cost.
        if not os.environ.get("SWIM_PASSWORD"):
            return {
                "error": "SWIM feed not configured",
                "hint": "Set the SWIM_PASSWORD environment variable to "
                        "enable FAA SWIM feeds.",
            }, 501
        try:
            served = _swim_daemon_serve(args)
        except _SwimStoreUnavailable as exc:
            return {"error": str(exc), "retry_after_seconds": 30}, 503
        if served is not None:
            return served, 200
    return _run_subprocess(script, args, timeout, env_extras)


# --- Lightning single-flight + bounded concurrency ---------------------------
#
# A lightning capture holds its thread for --duration+15s (up to 45s).
# Two problems: (1) N concurrent clients polling the same airport each
# run an identical capture — pure waste; (2) unbounded concurrent
# captures multiply websocket connections without limit. Single-flight
# coalesces identical concurrent requests onto one execution, and a
# semaphore caps how many captures run at once. (Handler threads still
# block waiting — gunicorn's threads-per-worker is the outer bound, and
# --duration caps at 30s.)
_LIGHTNING_MAX_CONCURRENT = 3


_lightning_sem = threading.Semaphore(_LIGHTNING_MAX_CONCURRENT)


_lightning_inflight: dict = {}


_lightning_inflight_lock = threading.Lock()


def _lightning_singleflight(key, fn):
    """Run fn() once per key; concurrent same-key callers share the result."""
    with _lightning_inflight_lock:
        fut = _lightning_inflight.get(key)
        if fut is None:
            fut = concurrent.futures.Future()
            _lightning_inflight[key] = fut
            owner = True
        else:
            owner = False
    if not owner:
        return fut.result()
    try:
        with _lightning_sem:
            result = fn()
    except Exception as exc:
        fut.set_exception(exc)
        raise
    else:
        fut.set_result(result)
        return result
    finally:
        with _lightning_inflight_lock:
            _lightning_inflight.pop(key, None)


def _is_lightning_call(script: str, args: list) -> bool:
    return script == "airport_ops.py" and bool(args) and str(args[0]) == "lightning"


def run_script(script: str, args: list, timeout: int = DEFAULT_TIMEOUT,
               env_extras: dict = None) -> tuple[dict, int]:
    """Fetch through cache and breaker. Signature unchanged from the
    subprocess era — callers don't know any of this exists."""
    key = _cache_key(script, args)
    upstream = _upstream_for(script, args)
    cached = _cache_get(key)
    now = time.monotonic()

    # 1) Fresh cache hit — no upstream contact at all.
    if cached and not _nocache_requested():
        age = now - cached["stored_at"]
        if age <= cached["ttl"]:
            return _annotated(cached)

    # 2) Breaker open — serve stale if we can, fail fast if we can't.
    # ?nocache=1 never gets stale data: the caller explicitly asked for
    # fresh, so a stale copy would be a lie — fail fast with 503 instead.
    retry_in = _breaker_open(upstream)
    if retry_in:
        if cached and not _nocache_requested() \
                and (now - cached["stored_at"]) <= _STALE_MAX_SECONDS:
            return _annotated(cached, stale=True,
                              reason=f"{upstream} unavailable (circuit open, "
                                     f"retry in {retry_in}s)")
        return {"error": f"Upstream '{upstream}' temporarily unavailable "
                         f"(circuit open after repeated failures)",
                "retry_after_seconds": retry_in}, 503

    # 3) Live fetch. A 200 that's actually an embedded-error response (see
    # _is_upstream_failure) must not read as breaker-success or get cached
    # as if it were good data — previously it did both, which let a real
    # provider outage look healthy and then get served back as "fresh"
    # for the response's full TTL.
    if _is_lightning_call(script, args):
        # Single-flight: identical concurrent captures share one execution
        # instead of each holding a thread for --duration+15s.
        data, status = _lightning_singleflight(
            key, lambda: _execute(script, args, timeout, env_extras))
    else:
        data, status = _execute(script, args, timeout, env_extras)
    ok = status == 200 and not _is_upstream_failure(data)
    _breaker_record(upstream, ok)

    if ok:
        _cache_put(key, data, status, _ttl_for(script, args, data))
        return data, status

    # 4) Fetch failed — a flagged stale answer beats an error for every
    # consumer here (the brief's partial-result envelope, the tracker, the
    # client's cards). The flag keeps it honest.
    if cached and (now - cached["stored_at"]) <= _STALE_MAX_SECONDS:
        return _annotated(cached, stale=True,
                          reason=f"live fetch failed "
                                 f"({(data or {}).get('error', 'unknown')})")
    return data, status


def _status_prefetch_env(status_data) -> dict | None:
    """Build env extras so chain/track reuse a status payload we already paid for.

    /api/check, /api/brief, and the background tracker all fetch `status`
    first and then (sometimes) `chain` for the same ident. Without this,
    cmd_chain re-hits /flights/{ident} — one wasted AeroAPI query per call.
    Returns None when the payload isn't reusable so callers fall through
    to a live fetch.

    The value is the dict itself: the in-process path hands it to the
    script directly (no JSON round-trip), and _run_subprocess serializes it
    for the environment behind a byte cap.
    """
    if not isinstance(status_data, dict):
        return None
    if not (status_data.get("data") or {}).get("flights"):
        return None
    return {"PFT_PREFETCHED_STATUS": status_data}


# Cap for the subprocess env fallback: env vars are size-limited and
# process-global, so an arbitrarily large status payload must not ride the
# environment. Oversize → the prefetch is dropped (the script falls back
# to a live fetch) instead of risking truncation or an exec failure.
_PREFETCH_ENV_MAX_BYTES = 256 * 1024


def _prefetch_env_for_subprocess(env_extras: dict | None) -> dict:
    out = {}
    for k, v in (env_extras or {}).items():
        if k == "PFT_PREFETCHED_STATUS" and not isinstance(v, str):
            try:
                v = json.dumps(v)
            except (TypeError, ValueError):
                continue
            size = len(v.encode("utf-8"))
            if size > _PREFETCH_ENV_MAX_BYTES:
                log(f"[PREFETCH] status payload {size}B exceeds "
                    f"{_PREFETCH_ENV_MAX_BYTES}B env cap; dropping prefetch")
                continue
        out[k] = v
    return out


def run_scripts_parallel(tasks: list[dict], max_workers: int = 6,
                         deadline: float = None) -> dict:
    """Run multiple script calls in parallel.

    tasks: [{"key": "metar", "script": "aviation_weather.py", "args": [...], "timeout": 15}, ...]
    deadline: optional time.monotonic() value; results not in by then are
        returned as 504 "budget exceeded" entries instead of blocking the
        request. (The underlying subprocesses still die on their own
        per-script timeouts; we just stop waiting for them.)
    Returns: {"key": {result_json}, ...}
    """
    results = {}
    pool = ThreadPoolExecutor(max_workers=max_workers)
    try:
        futures = {}
        for task in tasks:
            fut = pool.submit(
                run_script,
                task["script"],
                task["args"],
                task.get("timeout", DEFAULT_TIMEOUT),
                task.get("env_extras")
            )
            futures[fut] = task["key"]

        remaining = None
        if deadline is not None:
            remaining = max(0.5, deadline - time.monotonic())
        try:
            for fut in as_completed(futures, timeout=remaining):
                key = futures[fut]
                try:
                    data, status = fut.result()
                    results[key] = {"data": data, "status": status}
                except Exception as e:
                    results[key] = {"data": {"error": str(e)}, "status": 500}
        except TimeoutError:
            for fut, key in futures.items():
                if key not in results:
                    fut.cancel()
                    results[key] = {
                        "data": {"error": "Skipped — request time budget exceeded"},
                        "status": 504,
                    }
    finally:
        # Don't block the response on threads still babysitting slow
        # subprocesses; they exit when their own timeouts fire.
        pool.shutdown(wait=deadline is None, cancel_futures=True)

    return results
