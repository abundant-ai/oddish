"""The analyzer reads the neutral agent snapshot setting first, falling back
to the legacy cc_chat-named env var (which predates the removed chat feature
and is still what prod sets)."""

from oddish.config import Settings


def test_analyzer_snapshot_prefers_agent_setting():
    s = Settings(agent_daytona_snapshot="agent-v1", cc_chat_daytona_snapshot="cc-v1")
    assert s.analyzer_snapshot == "agent-v1"


def test_analyzer_snapshot_falls_back_to_legacy_cc_chat_var():
    """Prod today sets only ODDISH_CC_CHAT_DAYTONA_SNAPSHOT; it must keep working."""
    s = Settings(agent_daytona_snapshot="", cc_chat_daytona_snapshot="cc-v1")
    assert s.analyzer_snapshot == "cc-v1"


def test_analyzer_snapshot_empty_when_neither_set():
    s = Settings(agent_daytona_snapshot="", cc_chat_daytona_snapshot="")
    assert s.analyzer_snapshot == ""


def test_analyzer_sandbox_enabled_defaults_on():
    assert Settings().analyzer_sandbox_enabled is True
