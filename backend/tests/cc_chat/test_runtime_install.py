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
    # The harbor pin is now a git direct reference resolved from the running
    # harbor (shlex-quoted), e.g. `harbor @ git+https://...@<sha>`.
    pip = any(
        "pip install --user --quiet" in c and "harbor" in c for c in commands
    )
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


async def test_install_raises_when_claude_fails():
    """claude-code is load-bearing: its install failure must abort provisioning."""

    class _Failing(_RecordingClient):
        async def exec_sync(self, sandbox, *, command):
            self.commands.append(command)
            if "test -x" in command or "import harbor" in command:
                return 1, ""  # both absent -> will attempt install
            if "npm install" in command:
                return 1, "boom"  # claude-code install fails
            return 0, ""

    client = _Failing(claude_present=False, harbor_present=False)
    with pytest.raises(RuntimeError):
        await ClaudeCodeRuntime().install(client, sandbox=object())


async def test_install_tolerates_harbor_failure():
    """harbor is a convenience (chat uses oddish-query), so a failed harbor
    install must NOT abort provisioning — otherwise every chat 500s."""

    class _HarborFails(_RecordingClient):
        async def exec_sync(self, sandbox, *, command):
            self.commands.append(command)
            if "test -x" in command or "import harbor" in command:
                return 1, ""  # both absent -> will attempt install
            if "pip install" in command and "harbor" in command:
                return 1, "Could not find a version that satisfies harbor==9.9"
            return 0, ""  # claude-code install succeeds

    client = _HarborFails(claude_present=False, harbor_present=False)
    await ClaudeCodeRuntime().install(client, sandbox=object())  # must not raise
    npm, pip = _installs(client.commands)
    assert npm and pip  # both were attempted; harbor's failure was swallowed
