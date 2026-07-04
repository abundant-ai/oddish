"""Gate-skipped trials must be counted in per-queue stats, not silently dropped.

``_accumulate_queue_stat`` ignores any status not in ``_VALID_QUEUE_STATUSES``.
Since success/failed are counted, ``skipped`` (also terminal) must be too, or a
queue's trial pipeline totals under-count when trials were gate-skipped.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from oddish.queue import (  # noqa: E402
    _VALID_QUEUE_STATUSES,
    _accumulate_queue_stat,
    _empty_queue_counts,
)


def test_empty_counts_has_a_skipped_bucket():
    assert "skipped" in _empty_queue_counts()


def test_skipped_status_is_counted_not_dropped():
    assert "skipped" in _VALID_QUEUE_STATUSES
    stats: dict[str, dict[str, int]] = {}
    _accumulate_queue_stat(stats, "fireworks/kimi-k2p7-code", "SKIPPED", 3)
    assert stats, "skipped status was dropped instead of counted"
    (bucket,) = stats.values()
    assert bucket["skipped"] == 3
