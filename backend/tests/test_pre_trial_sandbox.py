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


def _wire_common(monkeypatch, *, runtime_cls, provisioner_cls):
    """Common fakes for the failure-cleanup tests: record what got cleaned."""
    cleaned = {"sandbox_deleted": None, "key_revoked": None}

    async def fake_mint(session, *, org_id, name, ttl_minutes):
        return ("key_id", "ok_secret")

    async def fake_delete_quietly(client, sandbox):
        cleaned["sandbox_deleted"] = sandbox

    async def fake_revoke(api_key_id):
        cleaned["key_revoked"] = api_key_id

    class _Ctx:
        async def __aenter__(self):
            return object()

        async def __aexit__(self, *a):
            return False

    monkeypatch.setattr(mod, "Provisioner", provisioner_cls)
    monkeypatch.setattr(mod, "ClaudeCodeRuntime", runtime_cls)
    monkeypatch.setattr(mod, "RealDaytonaClient", lambda **k: object())
    monkeypatch.setattr(mod, "mint_internal_read_key", fake_mint)
    monkeypatch.setattr(mod, "delete_sandbox_quietly", fake_delete_quietly)
    monkeypatch.setattr(mod, "_revoke_key_quietly", fake_revoke)
    monkeypatch.setattr(mod, "get_session", lambda: _Ctx())
    monkeypatch.setenv("DAYTONA_API_KEY", "x")
    return cleaned


@pytest.mark.asyncio
async def test_install_failure_deletes_sandbox_and_revokes_key(monkeypatch):
    """A failure after the sandbox exists must delete it AND revoke the minted
    key -- the caller never gets a client, so aclose() can never run."""
    sandbox = object()

    class _Provisioner(_FakeProvisioner):
        async def create(self, **kwargs):
            return sandbox

    class _BoomRuntime(_FakeRuntime):
        async def install_oddish_cli(self, client, sb, *, api_key, api_base_url):
            raise RuntimeError("install exploded")

    cleaned = _wire_common(
        monkeypatch, runtime_cls=_BoomRuntime, provisioner_cls=_Provisioner
    )

    with pytest.raises(RuntimeError, match="install exploded"):
        await mod.provision_oddish_sandbox_client(
            org_id="org_1", model="m", api_key=None, api_base_url="https://api.test"
        )

    assert cleaned["sandbox_deleted"] is sandbox
    assert cleaned["key_revoked"] == "key_id"


@pytest.mark.asyncio
async def test_create_failure_revokes_key_without_sandbox_delete(monkeypatch):
    """When sandbox creation itself fails there is nothing to delete, but the
    already-minted key must still be revoked."""

    class _BoomProvisioner(_FakeProvisioner):
        async def create(self, **kwargs):
            raise RuntimeError("create exploded")

    cleaned = _wire_common(
        monkeypatch, runtime_cls=_FakeRuntime, provisioner_cls=_BoomProvisioner
    )

    with pytest.raises(RuntimeError, match="create exploded"):
        await mod.provision_oddish_sandbox_client(
            org_id="org_1", model="m", api_key=None, api_base_url="https://api.test"
        )

    assert cleaned["sandbox_deleted"] is None
    assert cleaned["key_revoked"] == "key_id"
