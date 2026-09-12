"""Lightning single-flight: one execution per identical concurrent call."""
import os
import sys
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import app as app_module  # noqa: E402


def test_concurrent_identical_calls_share_one_execution():
    calls = []
    barrier = threading.Barrier(5)

    def slow_fn():
        calls.append(1)
        time.sleep(0.3)
        return ({"strikes": 3}, 200)

    key = ("airport_ops.py", "lightning", (("duration", "5"),))
    results = []
    errors = []

    def worker():
        try:
            barrier.wait(timeout=5)
            results.append(app_module._lightning_singleflight(key, slow_fn))
        except Exception as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=worker) for _ in range(5)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=10)

    assert not errors
    assert len(calls) == 1  # the capture ran exactly once
    assert len(results) == 5
    assert all(r == ({"strikes": 3}, 200) for r in results)


def test_exception_propagates_and_key_is_released():
    key = ("airport_ops.py", "lightning", (("duration", "9"),))

    def boom():
        raise RuntimeError("capture failed")

    try:
        app_module._lightning_singleflight(key, boom)
        raise AssertionError("expected RuntimeError")
    except RuntimeError:
        pass

    # Key must be released: a retry runs the function again.
    ran = []
    app_module._lightning_singleflight(key, lambda: ran.append(1) or "ok")
    assert ran == [1]
    assert not app_module._lightning_inflight


def test_different_keys_do_not_coalesce():
    ran = []

    def fn(tag):
        ran.append(tag)
        return tag

    r1 = app_module._lightning_singleflight("k1", lambda: fn("a"))
    r2 = app_module._lightning_singleflight("k2", lambda: fn("b"))
    assert (r1, r2) == ("a", "b")
    assert ran == ["a", "b"]
