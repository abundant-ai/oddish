from __future__ import annotations

import logging
import shlex

from api.services.cc_chat.daytona_client import CreatedSandbox, DaytonaClient

log = logging.getLogger("oddish.cc_chat.archive")

WORKSPACE_ROOT = "/home/daytona/workspace"
CLAUDE_HOME = "/home/daytona/.claude"


def project_dir_for(workspace_root: str) -> str:
    return workspace_root.replace("/", "-")


def native_session_blob_key(session_id: str) -> str:
    return f"chat-sessions/{session_id}/claude-session.jsonl"


def _native_path(claude_session_id: str) -> str:
    return f"{CLAUDE_HOME}/projects/{project_dir_for(WORKSPACE_ROOT)}/{claude_session_id}.jsonl"


async def archive_native_session(
    client: DaytonaClient,
    sandbox: CreatedSandbox,
    *,
    blob,
    session_id: str,
    claude_session_id: str | None,
) -> bool:
    """Best-effort: copy the Claude Code native session file out of the sandbox
    to blob storage. Returns True on success, False otherwise (never raises)."""
    if blob is None or not claude_session_id:
        return False
    try:
        # Locate robustly; the cwd->dir transform can vary by claude version.
        _, out = await client.exec_sync(
            sandbox,
            command=f"find {shlex.quote(CLAUDE_HOME)}/projects -name {shlex.quote(claude_session_id + '.jsonl')} 2>/dev/null | head -1",
        )
        path = out.strip().splitlines()[0].strip() if out.strip() else _native_path(claude_session_id)
        data = await client.download_file(sandbox, src_path=path)
        if not data:
            return False
        await blob.upload_bytes(
            data, native_session_blob_key(session_id), content_type="application/x-ndjson"
        )
        return True
    except Exception:
        log.exception("archive_native_session failed: %s", session_id)
        return False


async def restore_native_session(
    client: DaytonaClient,
    sandbox: CreatedSandbox,
    *,
    blob,
    session_id: str,
    claude_session_id: str,
) -> bool:
    """Download the archived native session file and write it into ``sandbox`` at
    the deterministic project path so ``claude --resume`` finds it. Returns False
    if no archive exists."""
    if blob is None:
        return False
    key = native_session_blob_key(session_id)
    try:
        if hasattr(blob, "object_exists") and not await blob.object_exists(key):
            return False
        data = await blob.download_bytes(key)
    except Exception:
        log.exception("restore: archive fetch failed: %s", session_id)
        return False
    if not data:
        return False
    # Intentionally NOT swallowing sandbox-write failures here: returning False
    # means "no archive to restore" (caller surfaces a clean 'can't be restored'
    # message), whereas a write failure against a freshly-provisioned sandbox is a
    # real infra error the caller (resume()) should see and clean up after.
    dest = _native_path(claude_session_id)
    await client.exec_sync(sandbox, command=f"mkdir -p {shlex.quote(dest.rsplit('/', 1)[0])}")
    await client.upload_file(sandbox, dest_path=dest, content=data)
    return True
