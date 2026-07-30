"""The analysis log renders one short line per stream event.

claude-code emits system frames throughout a run; only the init frame is a
session start. Rendering every system frame as "[start]" flooded the log.
"""

import json

from oddish.analyze.analysis_log import render_event_line


def test_system_init_renders_a_start_line():
    line = render_event_line(
        json.dumps({"type": "system", "subtype": "init", "model": "haiku"})
    )
    assert line == "[start] session started (model haiku)"


def test_later_system_frames_are_skipped():
    line = render_event_line(
        json.dumps({"type": "system", "subtype": "thinking_tokens"})
    )
    assert line is None


def test_tool_results_are_skipped():
    assert render_event_line(json.dumps({"type": "user", "message": {}})) is None
