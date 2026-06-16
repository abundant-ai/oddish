import pytest
from api.services.cc_chat.archive import (
    native_session_blob_key, project_dir_for, archive_native_session, restore_native_session,
)
from api.services.cc_chat.daytona_client import FakeDaytonaClient

pytestmark = pytest.mark.asyncio


def test_project_dir_transform():
    assert project_dir_for("/home/daytona/workspace") == "-home-daytona-workspace"


def test_blob_key():
    assert native_session_blob_key("cs_1") == "chat-sessions/cs_1/claude-session.jsonl"


class _Blob:
    def __init__(self): self.store = {}
    async def upload_bytes(self, data, key, *, content_type=None): self.store[key] = data
    async def download_bytes(self, key): return self.store[key]
    async def object_exists(self, key): return key in self.store


async def test_archive_then_restore_roundtrip():
    client = FakeDaytonaClient()
    sbx = await client.create_sandbox(env_vars={}, auto_stop_minutes=1, auto_delete_minutes=1, labels={})
    native_path = "/home/daytona/.claude/projects/-home-daytona-workspace/claude-xyz.jsonl"
    await client.upload_file(sbx, dest_path=native_path, content=b'{"line":1}\n')
    # FakeDaytonaClient.exec_sync returns canned output by command-substring:
    client.exec_sync_results = {"find": (0, native_path + "\n")}

    blob = _Blob()
    ok = await archive_native_session(
        client, sbx, blob=blob, session_id="cs_1", claude_session_id="claude-xyz",
    )
    assert ok is True
    assert blob.store[native_session_blob_key("cs_1")] == b'{"line":1}\n'

    fresh = await client.create_sandbox(env_vars={}, auto_stop_minutes=1, auto_delete_minutes=1, labels={})
    restored = await restore_native_session(
        client, fresh, blob=blob, session_id="cs_1", claude_session_id="claude-xyz",
    )
    assert restored is True
    dest = "/home/daytona/.claude/projects/-home-daytona-workspace/claude-xyz.jsonl"
    assert client.sandboxes[fresh.id]["files"][dest] == b'{"line":1}\n'


async def test_restore_missing_archive_returns_false():
    client = FakeDaytonaClient()
    sbx = await client.create_sandbox(env_vars={}, auto_stop_minutes=1, auto_delete_minutes=1, labels={})
    blob = _Blob()
    assert await restore_native_session(
        client, sbx, blob=blob, session_id="nope", claude_session_id="x",
    ) is False


async def test_archive_no_blob_or_no_claude_id_returns_false():
    client = FakeDaytonaClient()
    sbx = await client.create_sandbox(env_vars={}, auto_stop_minutes=1, auto_delete_minutes=1, labels={})
    blob = _Blob()
    assert await archive_native_session(client, sbx, blob=None, session_id="cs_1", claude_session_id="x") is False
    assert await archive_native_session(client, sbx, blob=blob, session_id="cs_1", claude_session_id=None) is False
