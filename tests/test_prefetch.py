"""Prefetched status: dict in-process, bounded JSON for subprocess env."""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))), "scripts"))

import app as app_module  # noqa: E402
from flight_data import _prefetched_flights  # noqa: E402


def _payload():
    return {"flight": "UAL123", "date": "2026-09-12",
            "data": {"flights": [{"ident": "UAL123"}]}}


def test_prefetch_env_carries_dict_not_json():
    env = app_module._status_prefetch_env(_payload())
    assert isinstance(env["PFT_PREFETCHED_STATUS"], dict)


def test_prefetch_env_rejects_unusable():
    assert app_module._status_prefetch_env(None) is None
    assert app_module._status_prefetch_env({"data": {}}) is None
    assert app_module._status_prefetch_env({"data": {"flights": []}}) is None


def test_prefetched_flights_accepts_dict_and_json():
    assert _prefetched_flights("UAL123", raw=_payload()) == [{"ident": "UAL123"}]
    assert _prefetched_flights("UAL123", raw=json.dumps(_payload())) == \
        [{"ident": "UAL123"}]
    # Wrong flight / wrong date still rejected.
    assert _prefetched_flights("DAL456", raw=_payload()) is None
    assert _prefetched_flights("UAL123", raw=_payload(),
                               date="2026-09-13") is None


def test_subprocess_env_serializes_within_cap():
    env = app_module._status_prefetch_env(_payload())
    out = app_module._prefetch_env_for_subprocess(env)
    assert isinstance(out["PFT_PREFETCHED_STATUS"], str)
    assert json.loads(out["PFT_PREFETCHED_STATUS"])["flight"] == "UAL123"


def test_subprocess_env_drops_oversize_payload():
    big = _payload()
    big["data"]["flights"] = [{"ident": "UAL123",
                               "pad": "x" * (app_module._PREFETCH_ENV_MAX_BYTES)}]
    env = app_module._status_prefetch_env(big)
    out = app_module._prefetch_env_for_subprocess(env)
    assert "PFT_PREFETCHED_STATUS" not in out  # dropped, not truncated


def test_subprocess_env_passes_through_plain_strings():
    out = app_module._prefetch_env_for_subprocess(
        {"PFT_PREFETCHED_STATUS": '{"a": 1}', "OTHER": "x"})
    assert out == {"PFT_PREFETCHED_STATUS": '{"a": 1}', "OTHER": "x"}
    assert app_module._prefetch_env_for_subprocess(None) == {}
