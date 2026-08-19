import pytest

from api.services.sandbox.claude_code_runtime import ClaudeCodeRuntime


class _RecordingClient:
    """Daytona stand-in that scripts an exit code per pip invocation."""

    def __init__(self, results: list[tuple[int, str]]):
        self._results = list(results)
        self.commands: list[str] = []

    async def exec_sync(self, sandbox, *, command):
        self.commands.append(command)
        return self._results.pop(0) if self._results else (0, "")


_PIN_MISSING = (
    1,
    "ERROR: Could not find a version that satisfies the requirement "
    "oddish==0.1.13 (from versions: 0.1.11, 0.1.12)",
)


@pytest.mark.asyncio
async def test_install_falls_back_to_unpinned_when_pin_is_unpublished(monkeypatch):
    """A repo version not yet on PyPI must not kill the run.

    Prod symptom: every pre-trial sandbox died on `pip install oddish==0.1.13`
    while PyPI's newest was 0.1.12, so 42/42 blocks failed and none ever ran.
    """
    monkeypatch.setattr(
        "oddish.workers.agents.claude_code._pinned_oddish_requirement",
        lambda: "oddish==0.1.13",
    )
    client = _RecordingClient([_PIN_MISSING, (0, "Successfully installed oddish")])

    await ClaudeCodeRuntime().install_oddish_cli(
        client, object(), api_key="k", api_base_url="https://api.test"
    )

    assert len(client.commands) == 2, "expected a retry after the pin failed"
    assert "oddish==0.1.13" in client.commands[0]
    assert "oddish==" not in client.commands[1], "retry must drop the version pin"
    assert "install" in client.commands[1] and "oddish" in client.commands[1]


@pytest.mark.asyncio
async def test_install_raises_when_the_unpinned_retry_also_fails(monkeypatch):
    """The CLI is load-bearing for pre-trial, so a real outage still raises."""
    monkeypatch.setattr(
        "oddish.workers.agents.claude_code._pinned_oddish_requirement",
        lambda: "oddish==0.1.13",
    )
    client = _RecordingClient([_PIN_MISSING, (1, "network is unreachable")])

    with pytest.raises(RuntimeError, match="oddish CLI install failed"):
        await ClaudeCodeRuntime().install_oddish_cli(
            client, object(), api_key="k", api_base_url="https://api.test"
        )


@pytest.mark.asyncio
async def test_install_does_not_retry_when_the_pin_resolves(monkeypatch):
    """Happy path stays a single pinned install -- no extra pip call."""
    monkeypatch.setattr(
        "oddish.workers.agents.claude_code._pinned_oddish_requirement",
        lambda: "oddish==0.1.12",
    )
    client = _RecordingClient([(0, "Successfully installed oddish")])

    await ClaudeCodeRuntime().install_oddish_cli(
        client, object(), api_key="k", api_base_url="https://api.test"
    )

    assert len(client.commands) == 1
    assert "oddish==0.1.12" in client.commands[0]
