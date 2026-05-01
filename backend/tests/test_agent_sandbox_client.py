import httpx
import pytest
import respx

from api.services.agent_sandbox_client import AgentSandboxClient


@pytest.mark.asyncio
@respx.mock
async def test_submit_probe_returns_id():
    respx.post("https://service.test/v1/probes").mock(
        return_value=httpx.Response(202, json={"probe_run_id": "pr_abc"})
    )
    client = AgentSandboxClient(base_url="https://service.test", api_key="k")
    pid = await client.submit_probe(
        org_id="o", user_id="u", task_id="t",
        agent="claude-code", model="claude-sonnet-4-6",
        extra_instructions="x", timeout_minutes=10,
    )
    assert pid == "pr_abc"
    await client.aclose()


@pytest.mark.asyncio
@respx.mock
async def test_get_probe_returns_envelope():
    respx.get("https://service.test/v1/probes/pr_abc").mock(
        return_value=httpx.Response(200, json={
            "probe_run_id": "pr_abc",
            "status": "succeeded",
            "org_id": "o", "user_id": "u", "task_id": "t",
            "agent": "claude-code", "model": "claude-sonnet-4-6",
            "created_at": "2026-04-30T00:00:00Z",
            "started_at": "2026-04-30T00:00:00Z",
            "finished_at": "2026-04-30T00:00:01Z",
            "verifier": {"exit_code": 0, "reward": 1.0},
            "analyzer_result": {"kind": "probe_summary"},
            "error": None,
            "artifacts": {
                "transcript": "https://...",
                "verifier_stdout": None,
                "verifier_stderr": None,
                "verifier_reward": None,
                "agent_logs": None,
            },
        })
    )
    client = AgentSandboxClient(base_url="https://service.test", api_key="k")
    out = await client.get_probe("pr_abc")
    assert out["status"] == "succeeded"
    assert out["analyzer_result"]["kind"] == "probe_summary"
    await client.aclose()


@pytest.mark.asyncio
@respx.mock
async def test_start_chat_session():
    respx.post("https://service.test/v1/chat-sessions").mock(
        return_value=httpx.Response(200, json={"session_id": "cc-xyz"})
    )
    client = AgentSandboxClient(base_url="https://service.test", api_key="k")
    sid = await client.start_chat_session(
        org_id="o", user_id="u",
        scope={"kind": "experiment", "experiment_id": "exp_a"},
    )
    assert sid == "cc-xyz"
    await client.aclose()


@pytest.mark.asyncio
@respx.mock
async def test_close_chat_session():
    respx.delete("https://service.test/v1/chat-sessions/cc-xyz").mock(
        return_value=httpx.Response(204)
    )
    client = AgentSandboxClient(base_url="https://service.test", api_key="k")
    await client.close_chat_session("cc-xyz")
    await client.aclose()


@pytest.mark.asyncio
@respx.mock
async def test_export_chat_skills():
    respx.get("https://service.test/v1/chat-sessions/cc-xyz/skills.tar.gz").mock(
        return_value=httpx.Response(200, content=b"\x1f\x8btar")
    )
    client = AgentSandboxClient(base_url="https://service.test", api_key="k")
    blob = await client.export_chat_skills("cc-xyz")
    assert blob == b"\x1f\x8btar"
    await client.aclose()


@pytest.mark.asyncio
@respx.mock
async def test_authorization_header_uses_bearer_with_api_key():
    captured = []

    def handler(request):
        captured.append(request.headers.get("authorization"))
        return httpx.Response(202, json={"probe_run_id": "pr_x"})

    respx.post("https://service.test/v1/probes").mock(side_effect=handler)
    client = AgentSandboxClient(base_url="https://service.test", api_key="my-key")
    await client.submit_probe(
        org_id="o", user_id=None, task_id="t",
        agent="claude-code", model="m",
    )
    assert captured == ["Bearer my-key"]
    await client.aclose()
