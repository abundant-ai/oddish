from __future__ import annotations

import sys
import time
import types

import pytest

import app
from spend_alerts import (
    environment_float,
    expensive_experiment_alert,
    expensive_user_alert,
    experiment_is_expensive,
    user_is_expensive,
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
        (1000, 666.66, True),
        (999.99, 500, False),
        (1000, 666.67, False),
        (5000, 0, False),
    ],
)
def test_expensive_user_requires_absolute_and_relative_thresholds(
    spend_usd: float,
    average_usd: float,
    expected: bool,
) -> None:
    row = {"cost_usd": spend_usd, "average_cost_usd": average_usd}

    assert user_is_expensive(row, 1000, 1.5) is expected


def test_expensive_user_alert_reports_cohort_comparison() -> None:
    key, text = expensive_user_alert(
        {
            "org_id": "org-1",
            "spender": "user-1",
            "spender_label": "Pat <pat@example.com>",
            "cost_usd": 1500,
            "average_cost_usd": 750,
        },
        1000,
        1.5,
        "https://www.oddish.app/",
    )

    assert key == "expensive_user:org-1:user-1:1000:1.5"
    assert "*Pat &lt;pat@example.com&gt;*" in text
    assert "*$1,500.00*" in text
    assert "*2.0×*" in text
    assert "($750.00)" in text
    assert "<https://www.oddish.app/admin|open admin costs>" in text


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
async def test_expensive_user_check_rearms_after_condition_clears(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ODDISH_RO_DATABASE_URL", "configured")
    cleared_key = "expensive_user:org-1:cleared:1000:1.5"
    stale_breach_key = "expensive_user:org-1:stale-breach:1000:1.5"
    truncated_key = "expensive_user:org-1:not-returned:1000:1.5"

    class FakeWatchState(dict):
        def keys(self):
            return iter(list(dict.keys(self)))

    fake_watch_state = FakeWatchState(
        {
            cleared_key: 123,
            stale_breach_key: 123,
            truncated_key: time.time(),
        }
    )
    monkeypatch.setattr(app, "watch_state", fake_watch_state)

    async def fake_fetch(sql: str) -> list[dict]:
        assert sql == app._RECENT_USER_SPEND_SQL
        return [
            {
                "org_id": "org-1",
                "spender": "breached",
            },
        ]

    async def fake_get(path: str, params: dict) -> dict:
        assert path == "/admin/costs"
        assert params == {"window_days": 7, "experiment_limit": 1, "user_limit": 500}
        return {
            "by_user": [
                {
                    "org_id": "org-1",
                    "key": "stale-breach",
                    "owner_user_id": "stale-breach",
                    "cost_usd": 1500,
                },
                {
                    "org_id": "org-1",
                    "key": "breached",
                    "owner_user_id": "breached",
                    "cost_usd": 1500,
                },
                {
                    "org_id": "org-1",
                    "key": "baseline",
                    "owner_user_id": "baseline",
                    "cost_usd": 0,
                },
            ]
        }

    monkeypatch.setattr(tools, "_fetch", fake_fetch, raising=False)
    monkeypatch.setattr(tools, "_get", fake_get, raising=False)

    alerts = await app._check_expensive_users()

    assert cleared_key not in fake_watch_state
    assert stale_breach_key in fake_watch_state
    assert truncated_key in fake_watch_state
    assert [key for key, _ in alerts] == [
        "expensive_user:org-1:breached:1000:1.5"
    ]
