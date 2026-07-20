import pytest
from api.services.cc_chat.claude_code_runtime import ClaudeCodeRuntime
from api.services.cc_chat.daytona_client import FakeDaytonaClient

pytestmark = pytest.mark.asyncio


# Linux caps a single argv entry at MAX_ARG_STRLEN = 32 pages = 131072 bytes.
# It is not raisable via ulimit (unlike total ARG_MAX). Passing the prompt as an
# argument therefore dies at the shell -- `argument list too long` on stderr,
# claude never execs -- for any cohort whose prompt exceeds it. Real analyzer
# 'good' cohorts hit 217KB at 97 trials, so the prompt must travel via stdin.
_MAX_ARG_STRLEN = 131072


async def _last_command(fake: FakeDaytonaClient, sbx) -> str:
    return fake.sandboxes[sbx.id]["exec_log"][-1][1]


async def _drain_stream_chat(fake, sbx, content: str) -> None:
    agen = ClaudeCodeRuntime().stream_chat(
        fake, sbx, content=content, claude_session_id=None
    )
    async for _ in agen:
        pass


async def _sandbox(fake: FakeDaytonaClient):
    return await fake.create_sandbox(
        env_vars={}, auto_stop_minutes=30, auto_delete_minutes=60, labels={}
    )


async def test_large_prompt_stays_out_of_the_command_line():
    """A prompt over the kernel's argv ceiling must not reach the command."""
    fake = FakeDaytonaClient()
    sbx = await _sandbox(fake)
    content = "x" * (_MAX_ARG_STRLEN + 1)

    await _drain_stream_chat(fake, sbx, content)

    cmd = await _last_command(fake, sbx)
    assert len(cmd) < _MAX_ARG_STRLEN, (
        f"command is {len(cmd)}B; a prompt-sized command dies with E2BIG "
        "before claude starts"
    )
    assert content not in cmd


async def test_prompt_is_delivered_on_stdin():
    """The prompt reaches the agent as a file redirected into stdin."""
    fake = FakeDaytonaClient()
    sbx = await _sandbox(fake)
    content = "analyze these trials"

    await _drain_stream_chat(fake, sbx, content)

    files = fake.sandboxes[sbx.id]["files"]
    written = [p for p, b in files.items() if content.encode("utf-8") == b]
    assert written, f"prompt was not uploaded to the sandbox; files={list(files)}"

    cmd = await _last_command(fake, sbx)
    assert f"< {written[0]}" in cmd, f"prompt file is not on stdin: {cmd}"
    # The old `< /dev/null` must be gone, or the agent reads an empty prompt.
    assert "/dev/null" not in cmd
