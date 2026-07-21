"""Guards for the trajectory-summary output budget.

Three independent ways the summary block silently produced unparseable output:

1. `thinking` omitted -> Sonnet 5 runs adaptive thinking (Sonnet 4.6 did not),
   and max_tokens caps thinking + response together, so the budget went to
   reasoning and the JSON truncated mid-array.
2. max_tokens=2048 -> too small regardless of thinking, because the prompt
   assigns EVERY step to a component and emits its step_ids.
3. Free-text JSON with no schema -> one response emitted
   `"diagnosing".replace(...)` instead of a string literal.

Each test below goes RED against the pre-fix code (verified by reverting the
production line, not by assumption).
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from oddish.blocks.analyzer.analyzer_llm_client import ApiAnalyzerLLMClient


class _RecordingStream:
    def __init__(self, kwargs: dict) -> None:
        self.kwargs = kwargs

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_a):
        return False

    @property
    async def text_stream(self):  # pragma: no cover - not consumed here
        return
        yield ""

    async def get_final_message(self):
        return SimpleNamespace(stop_reason="end_turn", usage=None)


class _RecordingMessages:
    def __init__(self, sink: dict) -> None:
        self._sink = sink

    def stream(self, **kwargs):
        self._sink.update(kwargs)
        return _RecordingStream(kwargs)


class _RecordingAnthropic:
    def __init__(self, *a, sink: dict | None = None, **k) -> None:
        self.messages = _RecordingMessages(_RecordingAnthropic.sink)

    sink: dict = {}

    async def close(self):
        pass


def _patch(monkeypatch) -> dict:
    _RecordingAnthropic.sink = {}
    monkeypatch.setattr(
        "oddish.blocks.analyzer.analyzer_llm_client.AsyncAnthropic",
        _RecordingAnthropic,
    )
    return _RecordingAnthropic.sink


async def _drain(client: ApiAnalyzerLLMClient) -> None:
    async for _ in client.stream("prompt"):
        pass


@pytest.mark.asyncio
async def test_thinking_is_disabled_explicitly_not_omitted(monkeypatch):
    """Omitting `thinking` is the bug: on Sonnet 5 that means adaptive thinking,
    which eats max_tokens. The request must carry an explicit value."""
    sink = _patch(monkeypatch)
    await _drain(ApiAnalyzerLLMClient(model="claude-sonnet-5", max_tokens=16000))
    assert "thinking" in sink, (
        "thinking was omitted -- Sonnet 5 will run adaptive thinking and spend "
        "the max_tokens budget on reasoning"
    )
    assert sink["thinking"] == {"type": "disabled"}


@pytest.mark.asyncio
async def test_thinking_can_be_opted_back_in(monkeypatch):
    sink = _patch(monkeypatch)
    await _drain(ApiAnalyzerLLMClient(thinking={"type": "adaptive"}))
    assert sink["thinking"] == {"type": "adaptive"}


@pytest.mark.asyncio
async def test_output_schema_is_sent_via_extra_body(monkeypatch):
    """anthropic 0.76.0 has no output_config kwarg, so it must ride extra_body."""
    sink = _patch(monkeypatch)
    schema = {"type": "object", "properties": {}, "additionalProperties": False}
    await _drain(ApiAnalyzerLLMClient(output_schema=schema))
    fmt = sink["extra_body"]["output_config"]["format"]
    assert fmt["type"] == "json_schema"
    assert fmt["schema"] is schema


@pytest.mark.asyncio
async def test_no_extra_body_when_schema_absent(monkeypatch):
    sink = _patch(monkeypatch)
    await _drain(ApiAnalyzerLLMClient())
    assert "extra_body" not in sink


# --- max_tokens headroom -----------------------------------------------------
#
# A summary must enumerate every step id across its components. 2048 tokens
# (~5k chars) truncates at a few hundred steps; a 2465-step trial needs ~10k
# chars of ids alone. The threshold below is what the old 2048 fails.

MIN_SUMMARY_MAX_TOKENS = 8192


def test_generate_requests_enough_output_budget():
    """summarize_trajectory.generate must not pin the old 2048 cap."""
    from api.services import summarize_trajectory

    src = open(summarize_trajectory.__file__).read()
    assert "max_tokens=2048" not in src, (
        "generate() still pins max_tokens=2048 -- summaries of trials with a few "
        "hundred steps truncate mid-array and fail to parse"
    )
    assert summarize_trajectory.SUMMARY_MAX_TOKENS >= MIN_SUMMARY_MAX_TOKENS


def test_summary_dump_requests_enough_output_budget():
    """summary_dump.summarize_trial copied generate()'s cap; it must track the fix."""
    from api.services import summary_dump
    from api.services.summarize_trajectory import SUMMARY_MAX_TOKENS

    src = open(summary_dump.__file__).read()
    assert "max_tokens=2048" not in src, (
        "summarize_trial still pins max_tokens=2048"
    )
    # It must USE the shared constant, not redefine its own copy -- the drift
    # between these two call sites is what the bug was.
    assert "SUMMARY_MAX_TOKENS" in src
    assert "SUMMARY_MAX_TOKENS = " not in src, (
        "summary_dump redefines the cap instead of importing it"
    )
    assert SUMMARY_MAX_TOKENS >= MIN_SUMMARY_MAX_TOKENS
