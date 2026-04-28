"""Tests for oddish.validation."""

from __future__ import annotations

from unittest import mock

import pytest

from oddish import validation


# Stub the harbor agent set so the test suite doesn't need the full
# harbor package installed and so we can pin the expected list.
_FAKE_AGENTS = {"claude-code", "codex", "gemini-cli", "nop", "oracle", "terminus-2"}


@pytest.fixture(autouse=True)
def _stub_valid_agents(monkeypatch):
    monkeypatch.setattr(validation, "get_valid_agent_names", lambda: _FAKE_AGENTS)


def test_valid_single_agent_passes():
    report = validation.validate_configs(
        [{"agent": "claude-code", "model": "anthropic/claude-sonnet-4-5"}]
    )
    assert report.ok
    assert not report.errors
    assert not report.warnings


def test_unknown_agent_errors_with_suggestion():
    report = validation.validate_configs([{"agent": "claude_code", "model": "x"}])
    assert not report.ok
    err = report.errors[0]
    assert err.code == "agent_unknown"
    # difflib should pick claude-code as the closest match
    assert "claude-code" in err.message


def test_missing_model_errors_for_real_agent():
    report = validation.validate_configs([{"agent": "claude-code"}])
    assert not report.ok
    assert report.errors[0].code == "model_missing"


def test_nop_oracle_skip_model_check():
    report = validation.validate_configs([{"agent": "nop"}, {"agent": "oracle"}])
    assert report.ok


def test_bare_model_warns_no_provider_prefix():
    report = validation.validate_configs(
        [{"agent": "claude-code", "model": "claude-sonnet-4-5"}]
    )
    assert report.ok  # warning, not error
    warns = report.warnings
    assert len(warns) == 1
    assert warns[0].code == "model_no_provider_prefix"


def test_harbor_style_keys_recognized():
    # sweep configs in task-workflows use {name, model_name}
    report = validation.validate_configs(
        [{"name": "claude-code", "model_name": "anthropic/claude-sonnet-4-5"}]
    )
    assert report.ok


def test_validate_and_report_does_not_raise_on_failure():
    report = validation.validate_and_report([{"agent": "bogus"}])
    assert not report.ok


def test_notify_called_only_on_failure():
    calls: list[validation.ValidationReport] = []

    validation.validate_and_report(
        [{"agent": "claude-code", "model": "anthropic/claude-sonnet-4-5"}],
        notify=calls.append,
    )
    assert calls == []

    validation.validate_and_report([{"agent": "bogus"}], notify=calls.append)
    assert len(calls) == 1
    assert not calls[0].ok


def test_ping_skips_already_failing_entries():
    # An entry that fails the local check shouldn't even hit the network
    with mock.patch.object(validation, "_probe_anthropic") as probe:
        report = validation.validate_and_report(
            [{"agent": "bogus", "model": "anthropic/x"}], ping=True
        )
        assert not report.ok
        probe.assert_not_called()


def test_ping_dedupes_unique_provider_model_pairs():
    with mock.patch.object(
        validation, "_probe_anthropic", return_value=(True, "ok")
    ) as probe:
        report = validation.validate_and_report(
            [
                {"agent": "claude-code", "model": "anthropic/claude-sonnet-4-5"},
                {"agent": "claude-code", "model": "anthropic/claude-sonnet-4-5"},
            ],
            ping=True,
        )
        assert report.pinged
        # one probe call despite two entries
        probe.assert_called_once_with("claude-sonnet-4-5")


def test_resolve_provider_handles_bedrock_prefix():
    provider, model_id = validation._resolve_provider(
        "bedrock/us.anthropic.claude-haiku-4-5-20251001-v1:0"
    )
    assert provider == "anthropic"
    assert model_id.startswith("us.anthropic.")


def test_render_report_human_readable():
    report = validation.validate_configs([{"agent": "bogus"}])
    out = validation.render_report(report)
    assert "FAILED" in out
    assert "agent_unknown" in out
