from types import SimpleNamespace

import pytest

from api.services.blocks.analyzer import sandbox_llm_client as mod
from oddish.blocks.analyzer.analyzer_llm_client import SandboxConfig


class _FakeRuntime:
    installed: list[object] = []

    async def install(self, client, sandbox):
        type(self).installed.append("base")

    async def install_oddish_cli(self, client, sandbox, *, api_key, api_base_url):
        type(self).installed.append(("oddish", api_key, api_base_url))


class _FakeProvisioner:
    last_env = None
    last_create = None

    def __init__(self, *args, **kwargs):
        pass

    async def create(self, **kwargs):
        type(self).last_create = kwargs
        type(self).last_env = kwargs["env_vars"]
        return SimpleNamespace(id="sandbox-test")


class _SessionContext:
    async def __aenter__(self):
        return object()

    async def __aexit__(self, *args):
        return False


def _wire(monkeypatch, *, provisioner=_FakeProvisioner, runtime=_FakeRuntime):
    async def mint(session, *, org_id, name, ttl_minutes, scope):
        return "key_id", "ok_secret"

    monkeypatch.setattr(mod, "Provisioner", provisioner)
    monkeypatch.setattr(mod, "ClaudeCodeRuntime", runtime)
    monkeypatch.setattr(mod, "RealDaytonaClient", lambda **kwargs: object())
    monkeypatch.setattr(mod, "mint_internal_api_key", mint)
    monkeypatch.setattr(mod, "get_session", _SessionContext)
    monkeypatch.setenv("DAYTONA_API_KEY", "daytona")


@pytest.mark.asyncio
async def test_factory_mints_key_and_installs_oddish(monkeypatch):
    _FakeRuntime.installed = []
    _wire(monkeypatch)

    client = await mod.create_sandbox_llm_client(
        model="claude-sonnet-5",
        sandbox_config=SandboxConfig(
            install_oddish_cli=True,
            oddish_org_id="org_1",
            oddish_api_base_url="https://api.test",
            session_id="pre-trial",
        ),
    )

    assert _FakeProvisioner.last_env["ODDISH_API_KEY"] == "ok_secret"
    assert _FakeProvisioner.last_env["ODDISH_API_BASE_URL"] == "https://api.test"
    assert ("oddish", "ok_secret", "https://api.test") in _FakeRuntime.installed
    assert client._internal_api_key_id == "key_id"


@pytest.mark.asyncio
async def test_factory_uses_analyzer_auto_stop_default(monkeypatch):
    _wire(monkeypatch)

    await mod.create_sandbox_llm_client(sandbox_config=SandboxConfig())

    assert _FakeProvisioner.last_create["auto_stop_minutes"] == 15


@pytest.mark.asyncio
async def test_factory_respects_analyzer_auto_stop_override(monkeypatch):
    _wire(monkeypatch)

    await mod.create_sandbox_llm_client(
        sandbox_config=SandboxConfig(auto_stop_minutes=7)
    )

    assert _FakeProvisioner.last_create["auto_stop_minutes"] == 7


@pytest.mark.asyncio
async def test_client_close_deletes_sandbox_and_revokes_key(monkeypatch):
    deleted = []
    revoked = []

    async def delete(client, sandbox):
        deleted.append(sandbox)

    async def revoke(key_id):
        revoked.append(key_id)

    _wire(monkeypatch)
    monkeypatch.setattr(mod, "delete_sandbox_quietly", delete)
    monkeypatch.setattr(mod, "_revoke_key_quietly", revoke)
    client = await mod.create_sandbox_llm_client(
        sandbox_config=SandboxConfig(
            install_oddish_cli=True,
            oddish_org_id="org_1",
            oddish_api_base_url="https://api.test",
        )
    )

    await client.aclose()

    assert deleted == [client._sandbox]
    assert revoked == ["key_id"]


@pytest.mark.asyncio
async def test_install_failure_deletes_sandbox_and_revokes_key(monkeypatch):
    sandbox = object()
    cleaned = {"sandbox": None, "key": None}

    class _Provisioner(_FakeProvisioner):
        async def create(self, **kwargs):
            return sandbox

    class _Runtime(_FakeRuntime):
        async def install_oddish_cli(self, *args, **kwargs):
            raise RuntimeError("install exploded")

    async def delete(client, value):
        cleaned["sandbox"] = value

    async def revoke(key_id):
        cleaned["key"] = key_id

    _wire(monkeypatch, provisioner=_Provisioner, runtime=_Runtime)
    monkeypatch.setattr(mod, "delete_sandbox_quietly", delete)
    monkeypatch.setattr(mod, "_revoke_key_quietly", revoke)

    with pytest.raises(RuntimeError, match="install exploded"):
        await mod.create_sandbox_llm_client(
            sandbox_config=SandboxConfig(
                install_oddish_cli=True,
                oddish_org_id="org_1",
                oddish_api_base_url="https://api.test",
            )
        )

    assert cleaned == {"sandbox": sandbox, "key": "key_id"}


@pytest.mark.asyncio
async def test_factory_creates_nested_upload_directory_before_upload(monkeypatch):
    events = []

    class _Client:
        async def exec_sync(self, sandbox, *, command):
            events.append(("exec", command))
            return 0, ""

        async def upload_file(self, sandbox, *, dest_path, content):
            events.append(("upload", dest_path, content))

    _wire(monkeypatch)
    monkeypatch.setattr(mod, "RealDaytonaClient", lambda **kwargs: _Client())

    await mod.create_sandbox_llm_client(
        sandbox_config=SandboxConfig(
            files_to_upload={"out/findings-1.jsonl": b'{"trial_id":"t1"}\n'},
            setup_commands=("mkdir -p out",),
        )
    )

    assert events == [
        ("exec", "mkdir -p -- out"),
        ("upload", "out/findings-1.jsonl", b'{"trial_id":"t1"}\n'),
        ("exec", "mkdir -p out"),
    ]
