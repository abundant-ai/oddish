from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from oddish.schemas import TrialCollectionRequest


def test_accepts_task_ids_only():
    req = TrialCollectionRequest(name="c", task_ids=["taskA", "taskA", " taskB "])
    assert req.trial_ids == []
    assert req.task_ids == ["taskA", "taskB"]  # deduped + stripped


def test_accepts_trial_ids_only():
    req = TrialCollectionRequest(name="c", trial_ids=["t1", "t1"])
    assert req.trial_ids == ["t1"]
    assert req.task_ids == []


def test_rejects_empty_sources():
    with pytest.raises(ValueError):
        TrialCollectionRequest(name="c")


def test_rejects_blank_name():
    with pytest.raises(ValueError):
        TrialCollectionRequest(name="  ", trial_ids=["t1"])
