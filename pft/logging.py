"""Request-ID logging."""

import sys
from flask import g

def _req_id() -> str:
    """Short request ID for log correlation; 'boot' outside a request."""
    try:
        return g.get("request_id") or "boot"
    except Exception:
        return "boot"


def log(msg: str) -> None:
    """stderr log line prefixed with the request ID when there is one."""
    print(f"[req:{_req_id()}] {msg}", file=sys.stderr, flush=True)
