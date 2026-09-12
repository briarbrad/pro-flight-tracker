"""Railway-style postgres:// URLs must not be left as-is for psycopg3.

Run with: pytest tests/test_database_url.py -v
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from store import _normalize_database_url  # noqa: E402


def test_postgres_scheme_becomes_postgresql():
    assert _normalize_database_url(
        "postgres://user:pass@host:5432/db"
    ) == "postgresql://user:pass@host:5432/db"


def test_postgresql_scheme_unchanged():
    url = "postgresql://user:pass@host:5432/db?sslmode=require"
    assert _normalize_database_url(url) == url


def test_empty_unchanged():
    assert _normalize_database_url("") == ""


if __name__ == "__main__":
    import pytest
    raise SystemExit(pytest.main([__file__, "-v"]))
