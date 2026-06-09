"""Tests for skill / MCP usage detection in the probe transcript parser.

``_parse_agent_messages`` tags every tool_use with its source (skill / mcp /
builtin) and ``extract_probe_artifacts`` rolls that up into ``tool_usage`` so an
operator can see, deterministically, whether a probe agent reached for a skill
or an MCP server -- the LLM summary never asks about this.
"""

from __future__ import annotations

import json
from pathlib import Path

from oddish.worker.probe_analysis import (
    _classify_tool_use,
    _parse_agent_messages,
    _summarize_tool_usage,
    extract_probe_artifacts,
)


def _assistant_tool_use(name: str, inp: dict) -> str:
    return json.dumps(
        {
            "type": "assistant",
            "message": {
                "content": [{"type": "tool_use", "name": name, "input": inp}]
            },
        }
    )


def _write_log(tmp_path: Path, lines: list[str]) -> Path:
    log = tmp_path / "agent" / "claude-code.txt"
    log.parent.mkdir(parents=True, exist_ok=True)
    log.write_text("\n".join(lines))
    return log


# --------------------------------------------------------------------------
# _classify_tool_use
# --------------------------------------------------------------------------


def test_classify_skill_extracts_slug():
    kind, extras = _classify_tool_use("Skill", {"skill": "deep-research", "args": "x"})
    assert kind == "skill"
    assert extras == {"skill_name": "deep-research"}


def test_classify_skill_without_slug():
    kind, extras = _classify_tool_use("Skill", {})
    assert kind == "skill"
    assert extras == {}


def test_classify_mcp_splits_server_and_tool():
    kind, extras = _classify_tool_use("mcp__claude_ai_Gmail__authenticate", {})
    assert kind == "mcp"
    assert extras == {"mcp_server": "claude_ai_Gmail", "mcp_tool": "authenticate"}


def test_classify_mcp_tool_name_with_extra_separators():
    # Only the first ``__`` after the server splits server vs tool; the rest
    # stays with the tool name.
    kind, extras = _classify_tool_use("mcp__srv__a__b", {})
    assert kind == "mcp"
    assert extras == {"mcp_server": "srv", "mcp_tool": "a__b"}


def test_classify_builtin():
    assert _classify_tool_use("Bash", {"command": "ls"}) == ("builtin", {})
    assert _classify_tool_use("Read", {"file_path": "/x"}) == ("builtin", {})


# --------------------------------------------------------------------------
# _parse_agent_messages tagging
# --------------------------------------------------------------------------


def test_parse_tags_skill_and_mcp_and_builtin(tmp_path):
    log = _write_log(
        tmp_path,
        [
            _assistant_tool_use("Skill", {"skill": "deep-research"}),
            _assistant_tool_use("mcp__ctx7__query-docs", {"q": "x"}),
            _assistant_tool_use("Bash", {"command": "ls", "description": "list"}),
        ],
    )
    messages = _parse_agent_messages(log)

    tool_uses = [m for m in messages if m["kind"] == "tool_use"]
    assert [m["tool_kind"] for m in tool_uses] == ["skill", "mcp", "builtin"]
    assert tool_uses[0]["skill_name"] == "deep-research"
    assert tool_uses[1]["mcp_server"] == "ctx7"
    assert tool_uses[1]["mcp_tool"] == "query-docs"
    # Builtin entries don't carry skill/mcp extras.
    assert "skill_name" not in tool_uses[2]
    assert "mcp_server" not in tool_uses[2]


# --------------------------------------------------------------------------
# _summarize_tool_usage aggregation
# --------------------------------------------------------------------------


def test_summarize_counts_and_orders_by_first_appearance():
    messages = [
        {"kind": "tool_use", "tool_kind": "skill", "skill_name": "deep-research"},
        {"kind": "assistant_text", "text": "thinking"},
        {"kind": "tool_use", "tool_kind": "mcp", "mcp_server": "gmail", "mcp_tool": "send"},
        {"kind": "tool_use", "tool_kind": "skill", "skill_name": "deep-research"},
        {"kind": "tool_use", "tool_kind": "builtin", "name": "Bash"},
        {"kind": "tool_use", "tool_kind": "mcp", "mcp_server": "gmail", "mcp_tool": "send"},
    ]
    usage = _summarize_tool_usage(messages)

    assert usage["used_skills"] is True
    assert usage["used_mcp"] is True
    assert usage["skills"] == [{"name": "deep-research", "count": 2}]
    assert usage["mcp_tools"] == [{"server": "gmail", "tool": "send", "count": 2}]


def test_summarize_empty_when_no_special_tools():
    messages = [
        {"kind": "tool_use", "tool_kind": "builtin", "name": "Read"},
        {"kind": "assistant_text", "text": "done"},
    ]
    usage = _summarize_tool_usage(messages)
    assert usage == {
        "used_skills": False,
        "used_mcp": False,
        "skills": [],
        "mcp_tools": [],
    }


# --------------------------------------------------------------------------
# extract_probe_artifacts surfaces tool_usage end-to-end
# --------------------------------------------------------------------------


def test_extract_probe_artifacts_includes_tool_usage(tmp_path):
    _write_log(
        tmp_path,
        [
            _assistant_tool_use("Skill", {"skill": "frontend-design"}),
            _assistant_tool_use("mcp__ctx7__resolve-library-id", {}),
        ],
    )
    artifacts = extract_probe_artifacts(tmp_path)

    assert artifacts["tool_usage"]["used_skills"] is True
    assert artifacts["tool_usage"]["used_mcp"] is True
    assert artifacts["tool_usage"]["skills"] == [
        {"name": "frontend-design", "count": 1}
    ]
    assert artifacts["tool_usage"]["mcp_tools"] == [
        {"server": "ctx7", "tool": "resolve-library-id", "count": 1}
    ]
