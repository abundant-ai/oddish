import pytest
from api.services.cc_chat.claude_code_runtime import ClaudeCodeRuntime
from api.services.cc_chat.daytona_client import FakeDaytonaClient

pytestmark = pytest.mark.asyncio


# claude-code runs headless (`--print`), so it can never show an interactive
# approval prompt. Without bypassing permissions, every tool call the agent
# makes (e.g. Bash `./oddish-query` in global scope) blocks forever. Both launch
# paths must pass the bypass flag.
_BYPASS = "--permission-mode bypassPermissions"


async def _last_command(fake: FakeDaytonaClient, sbx) -> str:
    return fake.sandboxes[sbx.id]["exec_log"][-1][1]


async def test_stream_chat_bypasses_permissions():
    fake = FakeDaytonaClient()
    sbx = await fake.create_sandbox(
        env_vars={}, auto_stop_minutes=30, auto_delete_minutes=60, labels={}
    )
    agen = ClaudeCodeRuntime().stream_chat(
        fake, sbx, content="hi", claude_session_id=None
    )
    async for _ in agen:
        pass
    assert _BYPASS in await _last_command(fake, sbx)


async def test_run_once_bypasses_permissions():
    fake = FakeDaytonaClient()
    sbx = await fake.create_sandbox(
        env_vars={}, auto_stop_minutes=30, auto_delete_minutes=60, labels={}
    )
    await ClaudeCodeRuntime().run_once(
        fake, sbx, instruction="go", model="claude-sonnet-4-6", timeout_s=5
    )
    assert _BYPASS in await _last_command(fake, sbx)
