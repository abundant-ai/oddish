from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from oddish.cli.collect import _build_payload, _guard_sources


def test_build_payload_tasks_and_trials():
    payload = _build_payload(name="c", tasks=["taskA", "taskA"], trial_ids=["t1"])
    assert payload == {"name": "c", "task_ids": ["taskA"], "trial_ids": ["t1"]}


def test_guard_requires_a_source():
    assert _guard_sources(tasks=[], trial_ids=[]) is False
    assert _guard_sources(tasks=["a"], trial_ids=[]) is True
