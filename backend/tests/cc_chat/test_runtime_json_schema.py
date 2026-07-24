import json

import pytest
from api.services.cc_chat.claude_code_runtime import ClaudeCodeRuntime
from api.services.cc_chat.daytona_client import FakeDaytonaClient

pytestmark = pytest.mark.asyncio


async def _last_command(fake: FakeDaytonaClient, sbx) -> str:
    return fake.sandboxes[sbx.id]["exec_log"][-1][1]


async def _stream(fake, sbx, **kwargs) -> None:
    agen = ClaudeCodeRuntime().stream_chat(
        fake, sbx, content="hi", claude_session_id=None, **kwargs
    )
    async for _ in agen:
        pass


async def _sandbox(fake: FakeDaytonaClient):
    return await fake.create_sandbox(
        env_vars={}, auto_stop_minutes=30, auto_delete_minutes=60, labels={}
    )


async def test_stream_chat_passes_json_schema():
    fake = FakeDaytonaClient()
    sbx = await _sandbox(fake)
    schema = json.dumps({"type": "object", "properties": {"a b": {"type": "string"}}})

    await _stream(fake, sbx, json_schema=schema)

    command = await _last_command(fake, sbx)
    assert "--json-schema" in command
    # Quoted, or the shell splits the schema on its spaces into stray argv
    # entries and claude rejects the whole invocation.
    assert f"'{schema}'" in command


async def test_stream_chat_omits_json_schema_when_absent():
    fake = FakeDaytonaClient()
    sbx = await _sandbox(fake)

    await _stream(fake, sbx)

    assert "--json-schema" not in await _last_command(fake, sbx)
