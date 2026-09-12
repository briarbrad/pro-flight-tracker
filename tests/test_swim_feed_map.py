"""app's feed->queue map must be swim_consumer.FEED_QUEUE_MAP itself."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "scripts"))

import app as app_module  # noqa: E402
import swim_consumer as sc  # noqa: E402


def test_feed_queue_map_is_single_source_of_truth():
    assert app_module._SWIM_FEED_TO_QUEUE is sc.FEED_QUEUE_MAP
    # Spot-check the tfms split: both subcommands share one broker queue.
    assert sc.FEED_QUEUE_MAP["tfms-flight"] == "tfms"
    assert sc.FEED_QUEUE_MAP["tfms-flow"] == "tfms"
