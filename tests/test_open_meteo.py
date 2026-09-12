"""Open-Meteo summarizer — model guidance, never an aviation category.

Run with: pytest tests/test_open_meteo.py -v
"""
import os
import sys
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                                "scripts"))

import aviation_weather as aw  # noqa: E402
import app as app_module  # noqa: E402


SAMPLE = {
    "current": {
        "time": "2026-09-12T14:00",
        "temperature_2m": 18.2,
        "precipitation": 0.0,
        "weather_code": 3,
        "wind_speed_10m": 12.0,
        "wind_gusts_10m": 22.0,
        "visibility": 16000.0,
    },
    "hourly": {
        "time": [f"2026-09-12T{h:02d}:00" for h in range(14, 26)],
        "precipitation_probability": [10, 20, 40, 80, 60, 30, 10, 5, 0, 0, 0, 0],
        "precipitation": [0, 0, 0.1, 1.2, 0.4, 0, 0, 0, 0, 0, 0, 0],
        "wind_gusts_10m": [18, 20, 24, 28, 22, 16, 14, 12, 10, 10, 8, 8],
        "wind_speed_10m": [10, 12, 14, 16, 14, 10, 8, 8, 6, 6, 5, 5],
        "visibility": [16000, 14000, 8000, 4000, 6000, 12000, 16000, 16000,
                       16000, 16000, 16000, 16000],
        "cloud_cover": [40, 50, 70, 90, 80, 50, 30, 20, 10, 10, 5, 5],
    },
}


def test_summarize_has_no_aviation_category():
    block = aw.summarize_open_meteo(SAMPLE, "KJFK", hours=12,
                                    coords=(40.64, -73.78),
                                    coord_source="airport_table")
    assert block["label"] == "model_guidance"
    assert block["source"] == "open-meteo"
    assert "flight_category" not in block
    assert "flight_category" not in (block.get("current") or {})
    assert "not an official taf" in block["note"].lower()


def test_summarize_next_6h_extrema():
    block = aw.summarize_open_meteo(SAMPLE, "KJFK", hours=12)
    nxt = block["next_6h"]
    assert nxt["max_precip_probability_pct"] == 80
    assert nxt["max_wind_gust_kts"] == 28.0
    assert nxt["min_visibility_m"] == 4000
    assert len(block["hourly"]) == 12
    assert block["current"]["wind_gust_kts"] == 22.0


def test_summarize_tolerates_missing_hourly():
    block = aw.summarize_open_meteo({"current": {}}, "EGLL")
    assert block["hourly"] == []
    assert block["next_6h"]["max_precip_probability_pct"] is None
    assert block["label"] == "model_guidance"


def test_to_icao_maps_lhr_and_klhr():
    assert app_module.to_icao("LHR") == "EGLL"
    assert app_module.to_icao("KLHR") == "EGLL"
    assert app_module.to_icao("JFK") == "KJFK"
    assert app_module.to_icao("HNL") == "PHNL"


def _client():
    app_module.app.config["TESTING"] = True
    return app_module.app.test_client()


def test_open_meteo_endpoint_requires_icao():
    resp = _client().get("/api/weather/open-meteo")
    assert resp.status_code == 400


def test_open_meteo_endpoint_happy_path():
    fake = {
        "pull_time": "2026-09-12T14:00:00+00:00",
        "command": "open-meteo",
        "data": {"KJFK": aw.summarize_open_meteo(SAMPLE, "KJFK")},
        "errors": [],
        "label": "model_guidance",
        "note": aw.OPEN_METEO_NOTE,
    }
    with patch.object(app_module, "run_script", return_value=(fake, 200)):
        resp = _client().get("/api/weather/open-meteo?icao=JFK")
    assert resp.status_code == 200
    body = resp.get_json()
    assert body["label"] == "model_guidance"
    # rekey_airports maps KJFK back to the requested JFK
    assert "JFK" in body["data"] or "KJFK" in body["data"]
    station = body["data"].get("JFK") or body["data"].get("KJFK")
    assert station["label"] == "model_guidance"
    assert "flight_category" not in station
