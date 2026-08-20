"""Read tool names and string arguments from external ATIF tool calls.

ATIF trajectories are stored JSON produced by several agents. Their tool-call
objects use two tool-name fields and several argument spellings, so this module
owns that untyped boundary. Analysis code can use the normalized values without
repeating partial parsers.
"""

from __future__ import annotations

from collections.abc import Iterable

COMMAND_ARGUMENT_KEYS = ("command", "cmd", "script", "shell_command")
PATH_ARGUMENT_KEYS = (
    "file_path",
    "filePath",
    "path",
    "filename",
    "file",
    "target_file",
    "absolute_path",
)


def tool_name(call: object) -> str | None:
    """Return an ATIF call's ``function_name`` or legacy ``name``."""
    if not isinstance(call, dict):
        return None
    name = call.get("function_name") or call.get("name")
    return name if isinstance(name, str) and name else None


def string_argument(call: object, keys: Iterable[str]) -> str | None:
    """Return the first non-empty string argument under ``keys``."""
    if not isinstance(call, dict):
        return None
    arguments = call.get("arguments")
    if not isinstance(arguments, dict):
        return None
    for key in keys:
        value = arguments.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def string_arguments(call: object) -> list[str]:
    """Return every non-empty string argument in an ATIF tool call."""
    if not isinstance(call, dict):
        return []
    arguments = call.get("arguments")
    if not isinstance(arguments, dict):
        return []
    return [
        value.strip()
        for value in arguments.values()
        if isinstance(value, str) and value.strip()
    ]
