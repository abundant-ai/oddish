from oddish.config import Settings


def test_analyzer_snapshot_uses_agent_setting():
    s = Settings(agent_daytona_snapshot="agent-v1")
    assert s.analyzer_snapshot == "agent-v1"


def test_analyzer_snapshot_empty_when_unset():
    s = Settings(agent_daytona_snapshot="")
    assert s.analyzer_snapshot == ""


def test_analyzer_sandbox_enabled_defaults_on():
    assert Settings().analyzer_sandbox_enabled is True
