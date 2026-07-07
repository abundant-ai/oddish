import json

from oddish.workers.harbor import live_tail
from oddish.workers.harbor.live_tail import (
    ClaudeUsageFold,
    UsageTotals,
    price_totals,
    split_lines,
)


def assistant_line(msg_id, usage, model="claude-opus-4-8"):
    return json.dumps(
        {"type": "assistant", "message": {"id": msg_id, "model": model, "usage": usage}}
    ).encode()


def test_fold_keeps_last_usage_per_message_id():
    fold = ClaudeUsageFold()
    fold.feed_line(assistant_line("msg_1", {"input_tokens": 10, "output_tokens": 1}))
    fold.feed_line(assistant_line("msg_1", {"input_tokens": 10, "output_tokens": 50}))
    fold.feed_line(
        assistant_line(
            "msg_2",
            {
                "input_tokens": 5,
                "cache_read_input_tokens": 100,
                "cache_creation_input_tokens": 20,
                "output_tokens": 7,
            },
        )
    )
    totals = fold.totals()
    assert totals.input_tokens == 10 + (5 + 100 + 20)
    assert totals.cache_tokens == 100
    assert totals.cache_write_tokens == 20
    assert totals.output_tokens == 50 + 7
    assert totals.model == "claude-opus-4-8"


def test_fold_ignores_non_assistant_and_garbage():
    fold = ClaudeUsageFold()
    fold.feed_line(b"")
    fold.feed_line(b"not json {")
    fold.feed_line(b'{"type": "user", "message": {"id": "msg_x", "usage": {"input_tokens": 9}}}')
    fold.feed_line(b'{"type": "assistant", "message": {"id": "msg_y"}}')
    fold.feed_line(b'{"type": "assistant"}')
    fold.feed_line(b'[1, 2]')
    assert fold.usage_by_id == {}
    assert fold.totals() == UsageTotals()


def test_split_lines_carries_partial_tail():
    lines, rest = split_lines(b'{"a":1}\n{"b"')
    assert lines == [b'{"a":1}']
    assert rest == b'{"b"'
    lines, rest = split_lines(rest + b':2}\n')
    assert lines == [b'{"b":2}']
    assert rest == b""


def test_split_lines_survives_multibyte_split():
    euro_line = '{"text":"€"}\n'.encode()
    cut = 9
    first, second = euro_line[:cut], euro_line[cut:]
    assert first.count(b"\n") == 0
    lines, rest = split_lines(first)
    assert lines == [] and rest == first
    lines, rest = split_lines(rest + second)
    assert rest == b""
    assert json.loads(lines[0]) == {"text": "€"}


def test_price_totals_passes_bundled_input_by_keyword(monkeypatch):
    seen = {}

    def fake_estimate(model_name, **kwargs):
        seen["model"] = model_name
        seen.update(kwargs)
        return 1.23

    monkeypatch.setattr(live_tail, "estimate_cost_usd", fake_estimate)
    totals = UsageTotals(
        input_tokens=135,
        cache_tokens=100,
        cache_write_tokens=20,
        output_tokens=57,
        model="claude-opus-4-8",
    )
    assert price_totals(totals, None) == 1.23
    assert seen == {
        "model": "claude-opus-4-8",
        "input_tokens": 135,
        "output_tokens": 57,
        "cached_tokens": 100,
        "cache_write_tokens": 20,
    }


def test_price_totals_falls_back_to_trial_model(monkeypatch):
    monkeypatch.setattr(live_tail, "estimate_cost_usd", lambda model, **_: model)
    totals = UsageTotals(input_tokens=1)
    assert price_totals(totals, "fallback-model") == "fallback-model"
    assert price_totals(totals, None) is None


def test_supports_only_claude_code():
    assert live_tail.supports("claude-code")
    assert live_tail.supports("claude-code@2.1")
    assert not live_tail.supports("codex")
    assert not live_tail.supports(None)
