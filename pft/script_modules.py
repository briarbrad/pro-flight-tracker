"""Importable data scripts (run in-process instead of subprocess-per-call)."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import flight_data  # noqa: E402
import aviation_weather  # noqa: E402
import airport_ops  # noqa: E402
import swim_consumer  # noqa: E402  (FEED_QUEUE_MAP only; never runs the JVM here)
