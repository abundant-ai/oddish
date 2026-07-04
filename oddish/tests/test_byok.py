import pytest

from oddish.config import Settings
from oddish.workers.queue import byok


@pytest.fixture(autouse=True)
def _clear_resolver():
    byok.clear_byok_resolver()
    yield
    byok.clear_byok_resolver()


def _settings(**kw) -> Settings:
    return Settings(_env_file=None, **kw)


def test_uses_direct_anthropic_for_claude_when_forced_direct():
    s = _settings(claude_code_force_direct_api=True)
    assert byok.uses_direct_anthropic("claude-code", "claude-opus-4-8", settings=s)
    # Any agent running a claude model rides the same direct-API rerouting.
    assert byok.uses_direct_anthropic("terminus-2", "claude-opus-4-8", settings=s)


def test_uses_direct_anthropic_false_for_real_bedrock():
    s = _settings(claude_code_force_direct_api=False)
    assert not byok.uses_direct_anthropic("claude-code", "claude-opus-4-8", settings=s)


def test_uses_direct_anthropic_false_for_openai():
    s = _settings(claude_code_force_direct_api=True)
    assert not byok.uses_direct_anthropic("codex", "openai/gpt-5.2", settings=s)


def test_merge_byok_env_probe_wins_and_none_passthrough():
    assert byok.merge_byok_env(None, None) is None
    assert byok.merge_byok_env({"A": "1"}, None) == {"A": "1"}
    assert byok.merge_byok_env(None, {"B": "2"}) == {"B": "2"}
    assert byok.merge_byok_env({"A": "1", "B": "x"}, {"B": "2"}) == {"A": "1", "B": "2"}


@pytest.mark.asyncio
async def test_resolve_byok_no_resolver_returns_none():
    assert (
        await byok.resolve_byok(
            owner_user_id="u1",
            org_id=None,
            experiment_name=None,
            model="claude-opus-4-8",
            agent="claude-code",
        )
        is None
    )


@pytest.mark.asyncio
async def test_resolve_byok_calls_registered_resolver():
    seen = {}

    async def resolver(**ctx):
        seen.update(ctx)
        return byok.ByokResolution(env={"ANTHROPIC_API_KEY": "sk-user"})

    byok.register_byok_resolver(resolver)
    assert byok.byok_resolver_registered()
    res = await byok.resolve_byok(
        owner_user_id="u1",
        org_id="o1",
        experiment_name="e",
        model="claude-opus-4-8",
        agent="claude-code",
    )
    assert res is not None and res.env["ANTHROPIC_API_KEY"] == "sk-user"
    assert seen == {
        "owner_user_id": "u1",
        "org_id": "o1",
        "experiment_name": "e",
        "model": "claude-opus-4-8",
        "agent": "claude-code",
    }


@pytest.mark.asyncio
async def test_resolve_byok_resolver_crash_fails_open():
    async def resolver(**ctx):
        raise RuntimeError("boom")

    byok.register_byok_resolver(resolver)
    assert (
        await byok.resolve_byok(
            owner_user_id="u1",
            org_id=None,
            experiment_name=None,
            model="claude-opus-4-8",
            agent="claude-code",
        )
        is None
    )
