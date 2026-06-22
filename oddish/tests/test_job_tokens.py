from __future__ import annotations

import sys
import types
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from oddish.workers.queue import job_tokens


def _now() -> datetime:
    return datetime(2026, 6, 23, 12, 0, 0, tzinfo=timezone.utc)


# --------------------------------------------------------------------------
# Token mint + hash (the revocable credential handle)
# --------------------------------------------------------------------------
def test_mint_returns_raw_token_and_hash() -> None:
    raw, hashed = job_tokens.mint_token()
    assert isinstance(raw, str) and len(raw) >= 32
    assert hashed == job_tokens.hash_token(raw)
    assert hashed != raw  # the raw secret is never stored


# --------------------------------------------------------------------------
# Scoping: least-privilege model env + S3 write prefix
# --------------------------------------------------------------------------
def _fake_settings(**keys) -> types.SimpleNamespace:
    base = dict(
        anthropic_api_key=None,
        openai_api_key=None,
        gemini_api_key=None,
    )
    base.update(keys)
    ns = types.SimpleNamespace(**base)
    ns.get_provider_for_trial = lambda agent, model: _provider_of(model)
    ns.get_openai_agent_env = lambda model: {"OPENAI_API_KEY": base["openai_api_key"]}
    return ns


def _provider_of(model: str) -> str:
    m = (model or "").lower()
    if "claude" in m or "anthropic" in m:
        return "anthropic"
    if "gpt" in m or "openai" in m:
        return "openai"
    if "gemini" in m:
        return "gemini"
    return "anthropic"


def test_scoped_model_env_anthropic_excludes_other_providers() -> None:
    settings = _fake_settings(
        anthropic_api_key="sk-ant", openai_api_key="sk-oai", gemini_api_key="g"
    )
    env = job_tokens.scoped_model_env(
        agent="claude-code", model="anthropic/claude-sonnet-4-5", settings=settings
    )
    assert env.get("ANTHROPIC_API_KEY") == "sk-ant"
    # Least privilege: no OpenAI / Gemini keys leak into a Claude job's bundle.
    assert "OPENAI_API_KEY" not in env
    assert "GEMINI_API_KEY" not in env


def test_scoped_model_env_openai_only_carries_openai() -> None:
    settings = _fake_settings(anthropic_api_key="sk-ant", openai_api_key="sk-oai")
    env = job_tokens.scoped_model_env(
        agent="codex", model="openai/gpt-4o", settings=settings
    )
    assert env.get("OPENAI_API_KEY") == "sk-oai"
    assert "ANTHROPIC_API_KEY" not in env


def test_s3_write_prefix_scopes_to_the_trial() -> None:
    prefix = job_tokens.s3_write_prefix_for("task_abc-0")
    assert prefix == "tasks/task_abc/trials/task_abc-0/"


def test_authorize_s3_key_enforces_prefix() -> None:
    prefix = "tasks/task_abc/trials/task_abc-0/"
    assert job_tokens.authorize_s3_key(f"{prefix}result.json", prefix) is True
    assert job_tokens.authorize_s3_key("tasks/other/trials/x/leak", prefix) is False


# --------------------------------------------------------------------------
# Bundle assembly
# --------------------------------------------------------------------------
def test_merge_agent_env_combines_bundle_and_probe() -> None:
    settings = _fake_settings(anthropic_api_key="sk-ant")
    bundle, _ = job_tokens.build_bundle(
        agent="claude-code",
        model="anthropic/claude-sonnet-4-5",
        trial_id="t-0",
        settings=settings,
        now=_now(),
    )
    merged = job_tokens.merge_agent_env(bundle, {"ODDISH_API_KEY": "probe"})
    assert merged["ANTHROPIC_API_KEY"] == "sk-ant"
    assert merged["ODDISH_API_KEY"] == "probe"


def test_merge_agent_env_no_bundle_returns_probe_env() -> None:
    assert job_tokens.merge_agent_env(None, {"ODDISH_API_KEY": "p"}) == {
        "ODDISH_API_KEY": "p"
    }
    assert job_tokens.merge_agent_env(None, None) is None
    # An empty probe env is preserved as {}, not collapsed to None (flag-off
    # byte-for-byte: the caller's value passes through unchanged).
    assert job_tokens.merge_agent_env(None, {}) == {}


def test_build_bundle_assembles_scoped_credentials() -> None:
    settings = _fake_settings(anthropic_api_key="sk-ant")
    bundle, token_hash = job_tokens.build_bundle(
        agent="claude-code",
        model="anthropic/claude-sonnet-4-5",
        trial_id="task_abc-0",
        settings=settings,
        now=_now(),
        ttl_seconds=3600,
    )
    assert bundle.model_env.get("ANTHROPIC_API_KEY") == "sk-ant"
    assert bundle.s3_write_prefix == "tasks/task_abc/trials/task_abc-0/"
    assert bundle.expires_at == _now() + timedelta(seconds=3600)
    # only the hash is persistable; it matches the bundle's (in-memory) raw token
    assert token_hash == job_tokens.hash_token(bundle.token)
