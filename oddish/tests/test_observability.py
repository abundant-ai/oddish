from __future__ import annotations

import logging
from types import SimpleNamespace

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
