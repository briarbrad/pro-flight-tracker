"""SWIM password must never appear in process argv.

write_password_argfile() produces a 0600 java @argfile carrying the
-Dpassword property; the java launcher expands it, so `ps` only ever
sees the argfile path.
"""
import os
import stat
import sys

sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parent.parent / "scripts"))

import swim_consumer as sc


def test_argfile_is_owner_only():
    path = sc.write_password_argfile("s3cret")
    try:
        assert stat.S_IMODE(os.stat(path).st_mode) == 0o600
    finally:
        os.unlink(path)


def test_argfile_carries_password_property():
    path = sc.write_password_argfile("s3cret")
    try:
        content = open(path).read()
        assert content.startswith("-Dpassword=")
        assert "s3cret" in content
    finally:
        os.unlink(path)


def test_argfile_quotes_special_chars():
    # JEP 293 quoting: the java launcher must see one single argument.
    path = sc.write_password_argfile('a"b\\c d$e!')
    try:
        line = open(path).read().strip()
        assert line.startswith('-Dpassword="')
        assert line.endswith('"')
    finally:
        os.unlink(path)


def test_newlines_rejected():
    try:
        sc.write_password_argfile("abc\ndef")
    except ValueError:
        return
    raise AssertionError("expected ValueError for newline in password")


def test_run_consumer_cmd_has_no_password_in_argv(monkeypatch):
    """run_consumer must pass @argfile, never -Dpassword=<secret>, in argv."""
    captured = {}

    class FakeProc:
        def communicate(self, timeout=None):
            return b"", b""

    def fake_popen(cmd, **kwargs):
        captured["cmd"] = cmd
        return FakeProc()

    monkeypatch.setattr(sc.subprocess, "Popen", fake_popen)
    monkeypatch.setattr(sc, "load_config", lambda: {
        "queues": {"tfms": {"queue": "q", "vpn": "v", "broker": "b"}},
        "provider_urls": {"b": "url"},
        "connection_factory": "cf",
        "username": "u",
    })
    monkeypatch.setattr(sc, "parse_raw_output", lambda out: [])

    sc.run_consumer("tfms", 1, "sup3r-s3cret!")
    cmd = captured["cmd"]
    joined = " ".join(cmd)
    assert "sup3r-s3cret!" not in joined
    assert not any(a.startswith("-Dpassword=") for a in cmd)
    assert any(a.startswith("@") and a.endswith(".args") for a in cmd)
    # And the temp file was cleaned up.
    argfile = next(a[1:] for a in cmd if a.startswith("@"))
    assert not os.path.exists(argfile)
