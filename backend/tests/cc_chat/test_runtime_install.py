import pytest
from api.services.cc_chat.claude_code_runtime import ClaudeCodeRuntime

pytestmark = pytest.mark.asyncio


class _RecordingClient:
    """Records exec_sync commands; answers existence checks from `present`."""

    def __init__(self, *, claude_present: bool, harbor_present: bool):
        self._claude_present = claude_present
        self._harbor_present = harbor_present
        self.commands: list[str] = []

    async def exec_sync(self, sandbox, *, command):
        self.commands.append(command)
        if "test -x" in command:
            return (0 if self._claude_present else 1), ""
        if "import harbor" in command:
            return (0 if self._harbor_present else 1), ""
        return 0, ""


def _installs(commands: list[str]) -> tuple[bool, bool]:
    npm = any("npm install -g @anthropic-ai/claude-code" in c for c in commands)
    pip = any("pip install --user --quiet harbor" in c for c in commands)
    return npm, pip


async def test_install_skips_when_both_present():
    client = _RecordingClient(claude_present=True, harbor_present=True)
    await ClaudeCodeRuntime().install(client, sandbox=object())
    npm, pip = _installs(client.commands)
    assert not npm and not pip  # pre-baked snapshot -> nothing installed


async def test_install_runs_both_when_absent():
    client = _RecordingClient(claude_present=False, harbor_present=False)
    await ClaudeCodeRuntime().install(client, sandbox=object())
    npm, pip = _installs(client.commands)
    assert npm and pip


async def test_install_only_missing_one():
    client = _RecordingClient(claude_present=True, harbor_present=False)
    await ClaudeCodeRuntime().install(client, sandbox=object())
    npm, pip = _installs(client.commands)
    assert not npm and pip


async def test_install_raises_on_failure():
    class _Failing(_RecordingClient):
        async def exec_sync(self, sandbox, *, command):
            self.commands.append(command)
            if "test -x" in command or "import harbor" in command:
                return 1, ""  # both absent -> will attempt install
            return 1, "boom"  # the install itself fails

    client = _Failing(claude_present=False, harbor_present=False)
    with pytest.raises(RuntimeError):
        await ClaudeCodeRuntime().install(client, sandbox=object())
