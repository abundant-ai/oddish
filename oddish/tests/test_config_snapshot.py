"""The analyzer shares cc_chat's pre-baked snapshot, but reads a neutral
setting first so the two can diverge without a cc_chat-named knob silently
governing the analyzer."""

from oddish.config import Settings


def test_analyzer_snapshot_prefers_agent_setting():
    s = Settings(agent_daytona_snapshot="agent-v1", cc_chat_daytona_snapshot="cc-v1")
    assert s.analyzer_snapshot == "agent-v1"


def test_analyzer_snapshot_falls_back_to_cc_chat():
    """Prod today sets only ODDISH_CC_CHAT_DAYTONA_SNAPSHOT; it must keep working."""
    s = Settings(agent_daytona_snapshot="", cc_chat_daytona_snapshot="cc-v1")
    assert s.analyzer_snapshot == "cc-v1"


def test_analyzer_snapshot_empty_when_neither_set():
    s = Settings(agent_daytona_snapshot="", cc_chat_daytona_snapshot="")
    assert s.analyzer_snapshot == ""


def test_analyzer_sandbox_enabled_defaults_on():
    assert Settings().analyzer_sandbox_enabled is True


def test_verdict_via_analyzer_block_defaults_off():
    assert Settings().verdict_via_analyzer_block is False
