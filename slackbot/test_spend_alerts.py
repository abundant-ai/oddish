from __future__ import annotations

import sys
import types

import pytest

import app
from spend_alerts import (
    environment_float,
    expensive_experiment_alert,
    expensive_trial_alert,
    experiment_is_expensive,
    trial_is_expensive,
)

tools = types.ModuleType("tools")
sys.modules["tools"] = tools


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("2500", 2500),
        ("0", 0),
        ("invalid", 2000),
        ("nan", 2000),
        ("inf", 2000),
        ("-1", 2000),
    ],
)
def test_environment_float_accepts_only_finite_nonnegative_values(
    monkeypatch: pytest.MonkeyPatch,
    value: str,
    expected: float,
) -> None:
    monkeypatch.setenv("SPEND_THRESHOLD", value)

    assert environment_float("SPEND_THRESHOLD", 2000) == expected


def test_expensive_experiment_threshold_is_inclusive() -> None:
    assert experiment_is_expensive({"cost_usd": 2000}, 2000)
    assert not experiment_is_expensive({"cost_usd": 1999.99}, 2000)


def test_expensive_experiment_alert_identifies_owner_phase_and_link() -> None:
    key, text = expensive_experiment_alert(
        {
            "experiment_id": "team/run 1",
            "name": "Eval <red>",
            "owner_label": "A & B",
            "cost_usd": 2345.67,
            "active_trials": 2,
        },
        2000,
        "https://www.oddish.app/",
    )

    assert key == "expensive_experiment:team/run 1:2000"
    assert "*Eval &lt;red&gt;*" in text
    assert "*$2,345.67*" in text
    assert "2 trials still running" in text
    assert "owner: *A &amp; B*" in text
    assert "/experiments/team%252Frun%25201|open experiment>" in text


@pytest.mark.parametrize(
    ("spend_usd", "average_usd", "expected"),
    [
        (100.01, None, True),
        (100, None, False),
        (201, 100, True),
        (200, 100, False),
        (500, 0, True),
    ],
)
def test_expensive_trial_requires_floor_and_double_other_trial_average(
    spend_usd: float,
    average_usd: float | None,
    expected: bool,
) -> None:
    row = {"cost_usd": spend_usd, "other_trial_average_usd": average_usd}

    assert trial_is_expensive(row, 100, 2) is expected


def test_expensive_trial_alert_reports_experiment_comparison() -> None:
    key, text = expensive_trial_alert(
        {
            "id": "trial<1>",
            "task_id": "task/1",
            "spender_label": "Pat <pat@example.com>",
            "cost_usd": 250,
            "other_trial_average_usd": 100,
        },
        100,
        2,
        "https://www.oddish.app/",
    )

    assert key == "expensive_trial:trial<1>:100:2"
    assert "`trial&lt;1&gt;`" in text
    assert "*$250.00*" in text
    assert "2.5× the experiment's $100.00 average" in text
    assert "owner: *Pat &lt;pat@example.com&gt;*" in text
    assert "<https://www.oddish.app/tasks/task%2F1|open task>" in text


@pytest.mark.asyncio
async def test_expensive_experiment_check_filters_below_threshold(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ODDISH_RO_DATABASE_URL", "configured")

    async def fake_fetch(sql: str) -> list[dict]:
        assert sql == app._RECENT_EXPERIMENT_SPEND_SQL
        return [
            {
                "experiment_id": "below",
            },
            {
                "experiment_id": "at-threshold",
                "active_trials": 1,
            },
        ]

    async def fake_get(path: str, params: dict) -> dict:
        assert path == "/admin/costs"
        assert params == {"window_days": 0, "experiment_limit": 500, "user_limit": 1}
        return {
            "experiments": [
                {"experiment_id": "below", "cost_usd": 1999},
                {
                    "experiment_id": "at-threshold",
                    "cost_usd": 2000,
                    "owner_name": "Taylor",
                },
            ]
        }

    monkeypatch.setattr(tools, "_fetch", fake_fetch, raising=False)
    monkeypatch.setattr(tools, "_get", fake_get, raising=False)

    alerts = await app._check_expensive_experiments()

    assert [key for key, _ in alerts] == [
        "expensive_experiment:at-threshold:2000"
    ]


@pytest.mark.asyncio
async def test_expensive_trial_check_filters_by_floor_and_average(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ODDISH_RO_DATABASE_URL", "configured")

    async def fake_fetch(sql: str) -> list[dict]:
        assert sql == app._RECENT_TRIAL_SPEND_SQL
        return [
            {
                "id": "first",
                "task_id": "task",
                "cost_usd": 101,
                "other_trial_average_usd": None,
            },
            {
                "id": "double",
                "task_id": "task",
                "cost_usd": 200,
                "other_trial_average_usd": 100,
            },
            {
                "id": "over-double",
                "task_id": "task",
                "cost_usd": 201,
                "other_trial_average_usd": 100,
            },
        ]

    monkeypatch.setattr(tools, "_fetch", fake_fetch, raising=False)

    alerts = await app._check_expensive_trials()

    assert [key for key, _ in alerts] == [
        "expensive_trial:first:100:2",
        "expensive_trial:over-double:100:2",
    ]
