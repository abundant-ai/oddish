from __future__ import annotations

import logging
from types import SimpleNamespace

import pytest

from oddish import observability


def test_log_warning_emits_structured_logfire_record_when_configured(
    monkeypatch, caplog
) -> None:
    calls = []
    fake_logfire = SimpleNamespace(
        warning=lambda message, **attributes: calls.append((message, attributes))
    )
    monkeypatch.setitem(__import__("sys").modules, "logfire", fake_logfire)
    monkeypatch.setattr(observability, "_configured", True)
    caplog.set_level(logging.WARNING, logger=observability.__name__)

    observability.log_warning(
        "Trial has token usage but no resolved cost",
        tags=("cost-integrity",),
        metric="trial_cost_unpriced",
        model="unknown-model",
    )

    assert calls == [
        (
            "Trial has token usage but no resolved cost",
            {
                "_tags": ["cost-integrity"],
                "metric": "trial_cost_unpriced",
                "model": "unknown-model",
            },
        )
    ]
    assert "metric='trial_cost_unpriced'" in caplog.text
    assert "model='unknown-model'" in caplog.text


def test_log_warning_keeps_standard_log_when_logfire_is_disabled(
    monkeypatch, caplog
) -> None:
    calls = []
    fake_logfire = SimpleNamespace(
        warning=lambda message, **attributes: calls.append((message, attributes))
    )
    monkeypatch.setitem(__import__("sys").modules, "logfire", fake_logfire)
    monkeypatch.setattr(observability, "_configured", False)
    caplog.set_level(logging.WARNING, logger=observability.__name__)

    observability.log_warning(
        "Trial has token usage but no resolved cost",
        metric="trial_cost_unpriced",
    )

    assert calls == []
    assert "Trial has token usage but no resolved cost" in caplog.text


def test_unpriced_trial_helper_only_logs_null_cost_with_tokens(monkeypatch) -> None:
    calls = []
    monkeypatch.setattr(
        observability,
        "log_warning",
        lambda message, **attributes: calls.append((message, attributes)),
    )
    base = {
        "trial_id": "trial-1",
        "model": "new-model",
        "agent": "claude-code",
        "provider": "openrouter",
        "attempt": 1,
        "cache_tokens": 0,
        "cache_write_tokens": 0,
        "native_cost_usd": 0.0,
        "native_cost_trusted": False,
    }

    assert not observability.log_unpriced_trial_if_needed(
        cost_usd=1.0, input_tokens=100, output_tokens=10, **base
    )
    assert not observability.log_unpriced_trial_if_needed(
        cost_usd=None, input_tokens=0, output_tokens=0, **base
    )
    assert observability.log_unpriced_trial_if_needed(
        cost_usd=None, input_tokens=100, output_tokens=10, **base
    )
    cache_only = {**base, "cache_tokens": 50}
    assert observability.log_unpriced_trial_if_needed(
        cost_usd=None, input_tokens=0, output_tokens=0, **cache_only
    )
    assert len(calls) == 2
    assert calls[0][1]["metric"] == "trial_cost_unpriced"
    assert calls[0][1]["model"] == "new-model"
    assert calls[1][1]["cache_tokens"] == 50


def test_missing_bedrock_metering_warns_after_agent_execution(monkeypatch) -> None:
    calls = []
    monkeypatch.setattr(
        observability,
        "log_warning",
        lambda message, **attributes: calls.append((message, attributes)),
    )

    warned = observability.log_missing_trial_metering_if_needed(
        cost_usd=0.0,
        trial_id="trial-1",
        model="global.anthropic.claude-sonnet-4-6",
        agent="claude-code",
        provider="bedrock",
        attempt=2,
        input_tokens=None,
        cache_tokens=None,
        cache_write_tokens=None,
        output_tokens=None,
        native_cost_usd=0.0,
        has_execution_evidence=True,
    )

    assert warned is True
    assert calls == [
        (
            "Bedrock trial produced execution output but no token or cost metering",
            {
                "tags": ("cost-integrity", "missing-metering"),
                "metric": "trial_cost_missing_metering",
                "trial_id": "trial-1",
                "model": "global.anthropic.claude-sonnet-4-6",
                "agent": "claude-code",
                "provider": "bedrock",
                "attempt": 2,
                "native_cost_usd": 0.0,
            },
        )
    ]


@pytest.mark.parametrize(
    "overrides",
    [
        {"has_execution_evidence": False},
        {"provider": "anthropic"},
        {"agent": "mini-swe-agent"},
        {"cost_usd": 0.01},
        {"input_tokens": 1},
    ],
)
def test_missing_bedrock_metering_ignores_non_gaps(monkeypatch, overrides) -> None:
    calls = []
    monkeypatch.setattr(
        observability,
        "log_warning",
        lambda message, **attributes: calls.append((message, attributes)),
    )
    kwargs = {
        "cost_usd": None,
        "trial_id": "trial-1",
        "model": "global.anthropic.claude-sonnet-4-6",
        "agent": "claude-code",
        "provider": "bedrock",
        "attempt": 1,
        "input_tokens": None,
        "cache_tokens": None,
        "cache_write_tokens": None,
        "output_tokens": None,
        "native_cost_usd": None,
        "has_execution_evidence": True,
    }
    kwargs.update(overrides)

    assert observability.log_missing_trial_metering_if_needed(**kwargs) is False
    assert calls == []
