from api.services.cc_chat.stream_render import event_texts, render_event


def test_init_frame_names_the_model():
    assert render_event(
        {"type": "system", "subtype": "init", "model": "claude-haiku-4-5"}
    ) == "[system:init] model=claude-haiku-4-5"


def test_non_init_system_frames_are_dropped():
    """Later system frames are per-token noise."""
    assert render_event({"type": "system", "subtype": "thinking_tokens"}) is None


def test_bash_tool_use_renders_as_a_shell_line():
    evt = {"type": "assistant", "message": {"content": [
        {"type": "tool_use", "name": "Bash", "input": {"command": "ls -la"}},
    ]}}
    assert render_event(evt) == "$ ls -la"


def test_assistant_text_is_returned():
    evt = {"type": "assistant", "message": {"content": [
        {"type": "text", "text": "working on it\n"},
    ]}}
    assert render_event(evt) == "working on it"


def test_empty_assistant_content_is_dropped():
    assert render_event({"type": "assistant", "message": {"content": []}}) is None


def test_tool_result_is_truncated():
    evt = {"type": "user", "message": {"content": [
        {"type": "tool_result", "content": "x" * 700},
    ]}}
    out = render_event(evt)
    assert out.startswith("  → " + "x" * 600)
    assert "(+100 chars)" in out


def test_result_frame_reports_cost():
    evt = {"type": "result", "subtype": "success",
           "total_cost_usd": 0.0413, "num_turns": 18}
    assert render_event(evt) == "[result:success] cost=$0.0413 turns=18"


def test_result_frame_without_cost_does_not_crash():
    assert render_event({"type": "result", "subtype": "error"}) == \
        "[result:error] cost=? turns=?"


def test_blank_stderr_is_dropped():
    assert render_event({"type": "_stderr", "text": "   "}) is None


def test_unknown_event_type_is_dropped():
    assert render_event({"type": "wat"}) is None


def test_event_texts_returns_tool_results_untruncated():
    evt = {"type": "user", "message": {"content": [
        {"type": "tool_result", "content": "x" * 700},
    ]}}
    assert event_texts(evt) == ["x" * 700]


def test_event_texts_unpacks_tool_use_string_values_unescaped():
    """json.dumps() of the input would backslash-escape nested JSON past
    parsing; callers need the raw value a Write actually carries."""
    evt = {"type": "assistant", "message": {"content": [
        {"type": "tool_use", "name": "Write",
         "input": {"file_path": "/out/f.jsonl", "content": '{"trial_id": "t1"}',
                   "line": 3}},
    ]}}
    assert event_texts(evt) == ["/out/f.jsonl", '{"trial_id": "t1"}']


def test_event_texts_keeps_undecodable_raw_lines():
    assert event_texts({"type": "_invalid_json", "raw": "MAP FINDING: {}"}) == \
        ["MAP FINDING: {}"]


def test_event_texts_drops_frames_with_no_payload():
    assert event_texts({"type": "result", "subtype": "success"}) == []
    assert event_texts({"type": "assistant", "message": {"content": []}}) == []
