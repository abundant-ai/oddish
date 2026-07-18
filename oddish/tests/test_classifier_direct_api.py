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


def test_bedrock_id_stays_on_bedrock():
    """A Bedrock-shaped analysis model keeps the Bedrock id + env."""
    model_id, env = classifier._resolve_analysis_model_and_env(_BEDROCK_HAIKU, _env())
    assert model_id == _BEDROCK_HAIKU
    assert env.get("CLAUDE_CODE_USE_BEDROCK") == "1"
    assert env.get("AWS_BEARER_TOKEN_BEDROCK") == "tok"


def test_bedrock_id_stays_on_bedrock_even_with_api_key():
    """An ambient ANTHROPIC_API_KEY does not hijack a Bedrock-shaped id -- the
    model id shape picks the route."""
    model_id, env = classifier._resolve_analysis_model_and_env(
        _BEDROCK_HAIKU, _env(ANTHROPIC_API_KEY="sk-ant-test")
    )
    assert model_id == _BEDROCK_HAIKU
    assert env.get("CLAUDE_CODE_USE_BEDROCK") == "1"


@pytest.mark.parametrize("model", ["claude-haiku-4-5", "anthropic/claude-haiku-4-5"])
def test_plain_model_id_routes_direct(model):
    """A non-Bedrock model id drops the Bedrock env and runs bare on the
    direct Anthropic API."""
    model_id, env = classifier._resolve_analysis_model_and_env(model, _env())
    assert model_id == "claude-haiku-4-5"
    assert "CLAUDE_CODE_USE_BEDROCK" not in env
    assert "AWS_BEARER_TOKEN_BEDROCK" not in env
