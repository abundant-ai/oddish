from __future__ import annotations

from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import pytest  # noqa: E402

from oddish.analyze import classifier  # noqa: E402

_BEDROCK_HAIKU = "global.anthropic.claude-haiku-4-5-20251001-v1:0"


def _env(**overrides: str) -> dict[str, str]:
    base = {"CLAUDE_CODE_USE_BEDROCK": "1", "AWS_BEARER_TOKEN_BEDROCK": "tok"}
    base.update(overrides)
    return base


def test_bedrock_id_without_api_key_stays_on_bedrock(monkeypatch):
    """No direct-API key -> force-direct is a no-op; keep Bedrock id + env."""
    monkeypatch.setattr(classifier.settings, "claude_code_force_direct_api", True)
    model_id, env = classifier._resolve_analysis_model_and_env(_BEDROCK_HAIKU, _env())
    assert model_id == _BEDROCK_HAIKU
    assert env.get("CLAUDE_CODE_USE_BEDROCK") == "1"
    assert env.get("AWS_BEARER_TOKEN_BEDROCK") == "tok"


def test_bedrock_id_with_api_key_and_flag_on_forces_direct(monkeypatch):
    """Incident mitigation: Bedrock id maps to the plain API id and the Bedrock
    env signals are stripped so the CLI uses ANTHROPIC_API_KEY."""
    monkeypatch.setattr(classifier.settings, "claude_code_force_direct_api", True)
    model_id, env = classifier._resolve_analysis_model_and_env(
        _BEDROCK_HAIKU, _env(ANTHROPIC_API_KEY="sk-ant-test")
    )
    assert model_id == "claude-haiku-4-5"
    assert "CLAUDE_CODE_USE_BEDROCK" not in env
    assert "AWS_BEARER_TOKEN_BEDROCK" not in env
    assert env.get("ANTHROPIC_API_KEY") == "sk-ant-test"


def test_bedrock_id_with_api_key_and_flag_off_stays_on_bedrock(monkeypatch):
    """Flag off restores Bedrock routing even with an ANTHROPIC_API_KEY."""
    monkeypatch.setattr(classifier.settings, "claude_code_force_direct_api", False)
    model_id, env = classifier._resolve_analysis_model_and_env(
        _BEDROCK_HAIKU, _env(ANTHROPIC_API_KEY="sk-ant-test")
    )
    assert model_id == _BEDROCK_HAIKU
    assert env.get("CLAUDE_CODE_USE_BEDROCK") == "1"


@pytest.mark.parametrize("force_direct", [True, False])
def test_plain_model_id_always_routes_direct(monkeypatch, force_direct):
    """A non-Bedrock model id always drops the Bedrock env, regardless of flag."""
    monkeypatch.setattr(
        classifier.settings, "claude_code_force_direct_api", force_direct
    )
    model_id, env = classifier._resolve_analysis_model_and_env(
        "claude-haiku-4-5", _env()
    )
    assert model_id == "claude-haiku-4-5"
    assert "CLAUDE_CODE_USE_BEDROCK" not in env
