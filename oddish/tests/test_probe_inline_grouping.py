from __future__ import annotations

from pathlib import Path
import sys
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from oddish.core.helpers import filter_probe_trials_for_effective_versions


def _probe(trial_id: str, *, task_id: str, task_version_id: str | None):
    return SimpleNamespace(
        id=trial_id, task_id=task_id, task_version_id=task_version_id
    )


def test_keeps_only_probes_on_effective_version():
    probes = [
        _probe("p-old", task_id="t1", task_version_id="t1-v1"),
        _probe("p-cur", task_id="t1", task_version_id="t1-v2"),
        _probe("p-other", task_id="t2", task_version_id="t2-v5"),
    ]
    effective = {"t1": "t1-v2", "t2": "t2-v5"}

    grouped = filter_probe_trials_for_effective_versions(probes, effective)

    assert [t.id for t in grouped["t1"]] == ["p-cur"]
    assert [t.id for t in grouped["t2"]] == ["p-other"]


def test_omits_tasks_without_effective_version():
    probes = [_probe("p1", task_id="t1", task_version_id="t1-v1")]
    grouped = filter_probe_trials_for_effective_versions(probes, {"t1": None})
    assert grouped == {}


def test_omits_tasks_with_no_matching_probe():
    probes = [_probe("p1", task_id="t1", task_version_id="t1-v1")]
    grouped = filter_probe_trials_for_effective_versions(probes, {"t1": "t1-v2"})
    assert grouped == {}
