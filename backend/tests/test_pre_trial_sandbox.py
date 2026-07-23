import pytest

import worker.pre_trial_sandbox as mod


class _FakeRuntime:
    def __init__(self):
        self.installed = []

    async def install(self, client, sandbox):
        self.installed.append("base")

    async def install_oddish_cli(self, client, sandbox, *, api_key, api_base_url):
        self.installed.append(("oddish", api_key, api_base_url))


class _FakeProvisioner:
    def __init__(self, *a, **k):
        pass

    async def create(self, **kwargs):
        _FakeProvisioner.last_env = kwargs.get("env_vars")
        return object()


@pytest.mark.asyncio
async def test_provision_mints_key_and_installs_oddish(monkeypatch):
    async def fake_mint(session, *, org_id, name, ttl_minutes):
        return ("key_id", "ok_secret")

    monkeypatch.setattr(mod, "Provisioner", _FakeProvisioner)
    monkeypatch.setattr(mod, "ClaudeCodeRuntime", _FakeRuntime)
    monkeypatch.setattr(mod, "RealDaytonaClient", lambda **k: object())
    monkeypatch.setattr(mod, "mint_internal_read_key", fake_mint)
    monkeypatch.setattr(mod, "SandboxAnalyzerLLMClient", lambda **k: ("client", k))
    monkeypatch.setenv("DAYTONA_API_KEY", "x")

    class _Ctx:
        async def __aenter__(self):
            return object()

        async def __aexit__(self, *a):
            return False

    monkeypatch.setattr(mod, "get_session", lambda: _Ctx())

    client = await mod.provision_oddish_sandbox_client(
        org_id="org_1", model="claude-sonnet-5", api_key=None,
        api_base_url="https://api.test",
    )
    assert client[0] == "client"
    assert _FakeProvisioner.last_env["ODDISH_API_KEY"] == "ok_secret"
    assert _FakeProvisioner.last_env["ODDISH_API_BASE_URL"] == "https://api.test"
