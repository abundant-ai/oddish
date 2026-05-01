from unittest.mock import AsyncMock, MagicMock

import pytest


@pytest.mark.asyncio
async def test_start_experiment_session_proxies():
    from api.routers.cc_chat import start_experiment_session
    from api.services.agent_sandbox_client import AgentSandboxClient

    fake_client = MagicMock(spec=AgentSandboxClient)
    fake_client.start_chat_session = AsyncMock(return_value="cc-z")

    request = MagicMock()
    request.app.state.agent_sandbox_client = fake_client

    auth = MagicMock()
    auth.org_id = "org_a"
    auth.user_id = "user_b"
    auth.require_scope = MagicMock()

    result = await start_experiment_session("exp_a", request=request, auth=auth)
    assert result == {"session_id": "cc-z"}
    fake_client.start_chat_session.assert_awaited_once_with(
        org_id="org_a",
        user_id="user_b",
        scope={"kind": "experiment", "experiment_id": "exp_a"},
    )


@pytest.mark.asyncio
async def test_start_task_session_proxies_with_task_probes_scope():
    from api.routers.cc_chat import start_task_session
    from api.services.agent_sandbox_client import AgentSandboxClient

    fake_client = MagicMock(spec=AgentSandboxClient)
    fake_client.start_chat_session = AsyncMock(return_value="cc-y")

    request = MagicMock()
    request.app.state.agent_sandbox_client = fake_client

    auth = MagicMock()
    auth.org_id = "org_a"
    auth.user_id = None
    auth.require_scope = MagicMock()

    result = await start_task_session("task_x", request=request, auth=auth)
    assert result == {"session_id": "cc-y"}
    fake_client.start_chat_session.assert_awaited_once()
    call_kwargs = fake_client.start_chat_session.call_args.kwargs
    assert call_kwargs["scope"] == {"kind": "task_probes", "task_id": "task_x"}


@pytest.mark.asyncio
async def test_close_session_proxies():
    from api.routers.cc_chat import close_experiment_session
    from api.services.agent_sandbox_client import AgentSandboxClient

    fake_client = MagicMock(spec=AgentSandboxClient)
    fake_client.close_chat_session = AsyncMock()

    request = MagicMock()
    request.app.state.agent_sandbox_client = fake_client

    auth = MagicMock()
    auth.require_scope = MagicMock()

    response = await close_experiment_session("exp_a", "cc-z", request=request, auth=auth)
    assert response.status_code == 204
    fake_client.close_chat_session.assert_awaited_once_with("cc-z")


@pytest.mark.asyncio
async def test_export_skills_proxies():
    from api.routers.cc_chat import export_experiment_skills
    from api.services.agent_sandbox_client import AgentSandboxClient

    fake_client = MagicMock(spec=AgentSandboxClient)
    fake_client.export_chat_skills = AsyncMock(return_value=b"\x1f\x8btar")

    request = MagicMock()
    request.app.state.agent_sandbox_client = fake_client

    auth = MagicMock()
    auth.require_scope = MagicMock()

    response = await export_experiment_skills("exp_a", "cc-z", request=request, auth=auth)
    assert response.status_code == 200
    assert response.media_type == "application/gzip"
    assert response.body == b"\x1f\x8btar"
    fake_client.export_chat_skills.assert_awaited_once_with("cc-z")
