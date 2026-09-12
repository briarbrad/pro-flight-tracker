"""Request IDs: every request gets one; logs and responses carry it."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import app as app_module  # noqa: E402


def test_req_id_defaults_to_boot_outside_request():
    assert app_module._req_id() == "boot"


def test_gate_assigns_request_id_and_header():
    client = app_module.app.test_client()
    resp = client.get("/health")
    rid = resp.headers.get("X-Request-ID")
    assert rid and len(rid) == 8
    # IDs are unique per request.
    rid2 = client.get("/health").headers.get("X-Request-ID")
    assert rid2 and rid2 != rid


def test_log_prefixes_request_id(capsys):
    with app_module.app.test_request_context("/health"):
        app_module.g.request_id = "abc123"
        app_module.log("hello")
    out = capsys.readouterr().err
    assert "[req:abc123] hello" in out
