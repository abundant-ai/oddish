"""Oddish Cursor CLI wrapper for restricted-network Compose trials."""

from __future__ import annotations

from typing import Any

from harbor.agents.installed.cursor_cli import CursorCli

_WEB_TOOL_CALLS = ("web_search_tool_call", "web_fetch_tool_call")


class OddishCursorCli(CursorCli):
    """Stock Cursor CLI with narrowly scoped remote-web exclusion.

    Harbor owns filesystem/process isolation and shell egress for these trials.
    Provider-side web tools are excluded because their traffic is served through
    Cursor's already-allowed API and therefore never crosses Harbor's shell
    network namespace. The Cursor binary, sandbox default, prompts, and all
    remaining tools stay untouched.
    """

    def __init__(
        self,
        *args: Any,
        disable_web_tools: bool = False,
        **kwargs: Any,
    ) -> None:
        self._oddish_disable_web_tools = disable_web_tools
        super().__init__(*args, **kwargs)

    def build_cli_flags(self) -> str:
        flag_groups = [super().build_cli_flags()]
        if self._oddish_disable_web_tools:
            for tool_call in _WEB_TOOL_CALLS:
                flag_groups.append(f"--exclude-tools {tool_call}")
        return " ".join(group for group in flag_groups if group)
