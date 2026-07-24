"""QA spend reaching ``TaskCostTotals`` on the task detail rollup.

``_aggregate_task_detail_rollups`` is sync and sessionless by design, so the
QA aggregate is resolved by ``get_task_detail_core`` and passed in. These tests
pin that seam without standing up the query stack.
"""

from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from oddish.core.endpoints.task_detail import (  # noqa: E402
    _aggregate_task_detail_rollups,
)


def _trial(**kw):
    base = dict(
        id="t1",
        cost_usd=1.50,
        cost_is_estimated=False,
        task_version_id=None,
        status=SimpleNamespace(value="success"),
        reward=None,
    )
    base.update(kw)
    return SimpleNamespace(**base)


def test_qa_cost_lands_on_totals_without_touching_agent_cost():
    totals, _versions = _aggregate_task_detail_rollups(
        trials=[_trial()],
        version_rows=[],
        current_version_id=None,
        qa_cost_usd=0.11,
    )

    assert totals.qa_cost_usd == pytest.approx(0.11)
    # The headline figure must be exactly what it was before QA existed.
    assert totals.cost_usd == pytest.approx(1.50)


def test_qa_cost_defaults_to_zero_when_absent():
    """Callers that do not resolve QA still get a valid rollup."""
    totals, _versions = _aggregate_task_detail_rollups(
        trials=[_trial()],
        version_rows=[],
        current_version_id=None,
    )

    assert totals.qa_cost_usd == 0.0
