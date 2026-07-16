"""Unit tests for the internal-LLM funnel (oddish.core.llm). No network."""

from types import SimpleNamespace

import pytest

import litellm

from oddish.core import llm
from oddish.core.llm import (
    COST_BASIS_API,
    COST_BASIS_OAUTH_FLAT,
    COST_BASIS_UNPRICED,
    LLMUsageRow,
    _resolve_route,
    complete,
    record_usage,
    set_usage_recorder,
    supports_structured_outputs,
)


def _fake_response(
    text: str = "ok",
    *,
    prompt_tokens: int = 100,
    completion_tokens: int = 10,
    cached_tokens: int = 0,
    cache_creation_tokens: int = 0,
):
    usage = SimpleNamespace(
        prompt_tokens=prompt_tokens,
        completion_tokens=completion_tokens,
        prompt_tokens_details=SimpleNamespace(
            cached_tokens=cached_tokens, cache_creation_tokens=0
        ),
        cache_creation_input_tokens=cache_creation_tokens,
    )
    return SimpleNamespace(
        choices=[SimpleNamespace(message=SimpleNamespace(content=text))],
        usage=usage,
    )


class _Recorder:
    def __init__(self):
        self.rows: list[LLMUsageRow] = []

    async def __call__(self, row: LLMUsageRow) -> None:
        self.rows.append(row)


@pytest.fixture
def recorder():
    rec = _Recorder()
    set_usage_recorder(rec)
    yield rec
    set_usage_recorder(None)


@pytest.fixture
def acompletion_calls(monkeypatch):
    calls: list[dict] = []

    async def fake(**kwargs):
        calls.append(kwargs)
        return _fake_response()

    monkeypatch.setattr(litellm, "acompletion", fake)
    return calls


def test_resolve_route_bedrock_id_goes_direct_anthropic():
    model, pricing, kwargs = _resolve_route(
        "global.anthropic.claude-haiku-4-5-20251001-v1:0", None
    )
    assert model == "anthropic/claude-haiku-4-5"
    assert pricing == "claude-haiku-4-5"
    assert kwargs == {}


def test_resolve_route_bare_claude_id():
    model, pricing, _ = _resolve_route("claude-sonnet-4-6", None)
    assert model == "anthropic/claude-sonnet-4-6"
    assert pricing == "claude-sonnet-4-6"


def test_resolve_route_prefixed_id_passes_through():
    model, pricing, kwargs = _resolve_route("openrouter/some/model", None)
    assert model == "openrouter/some/model"
    assert pricing == "openrouter/some/model"
    assert kwargs == {}


def test_supports_structured_outputs_allowlist():
    assert supports_structured_outputs("anthropic/claude-haiku-4-5")
    assert supports_structured_outputs("claude-sonnet-4-6")
    assert not supports_structured_outputs("anthropic/claude-3-5-sonnet")
    assert not supports_structured_outputs("openai/gpt-5.4")


@pytest.mark.asyncio
async def test_complete_sends_native_schema_for_supported_model(
    recorder, acompletion_calls
):
    await complete(
        handler="probe_analysis",
        prompt="p",
        model="claude-haiku-4-5",
        schema={"type": "object"},
    )
    call = acompletion_calls[0]
    assert call["model"] == "anthropic/claude-haiku-4-5"
    assert call["output_config"] == {
        "format": {"type": "json_schema", "schema": {"type": "object"}}
    }


@pytest.mark.asyncio
async def test_complete_skips_schema_for_unsupported_model(
    recorder, acompletion_calls
):
    await complete(
        handler="probe_analysis",
        prompt="p",
        model="claude-3-5-sonnet-20241022",
        schema={"type": "object"},
    )
    assert "output_config" not in acompletion_calls[0]


@pytest.mark.asyncio
async def test_complete_records_usage_and_cost(recorder, monkeypatch):
    async def fake(**kwargs):
        return _fake_response(
            '{"x": 1}',
            prompt_tokens=130,
            completion_tokens=20,
            cached_tokens=30,
            cache_creation_tokens=50,
        )

    monkeypatch.setattr(litellm, "acompletion", fake)
    result = await complete(
        handler="trajectory_summary",
        prompt="p",
        model="claude-haiku-4-5",
        trial_id="trial-9",
    )
    assert result.text == '{"x": 1}'
    assert result.model == "claude-haiku-4-5"

    row = recorder.rows[0]
    assert row.handler == "trajectory_summary"
    assert row.model == "claude-haiku-4-5"
    assert row.trial_id == "trial-9"
    assert (row.input_tokens, row.output_tokens) == (130, 20)
    assert (row.cache_read_tokens, row.cache_write_tokens) == (30, 50)
    assert row.cost_basis == COST_BASIS_API
    assert row.success is True
    # haiku-4-5: 50 uncached in + 30 cached + 50 cache-write + 20 out.
    assert row.cost_usd == pytest.approx(0.0002155)


@pytest.mark.asyncio
async def test_complete_marks_unpriced_models(recorder, monkeypatch):
    async def fake(**kwargs):
        return _fake_response(prompt_tokens=10, completion_tokens=5)

    monkeypatch.setattr(litellm, "acompletion", fake)
    await complete(
        handler="probe_analysis", prompt="p", model="anthropic/not-a-real-model-xyz"
    )
    assert recorder.rows[0].cost_usd is None
    assert recorder.rows[0].cost_basis == COST_BASIS_UNPRICED


@pytest.mark.asyncio
async def test_complete_records_failure_and_raises(recorder, monkeypatch):
    class _Boom(Exception):
        status_code = 401

    async def fake(**kwargs):
        raise _Boom("no key")

    monkeypatch.setattr(litellm, "acompletion", fake)
    with pytest.raises(_Boom):
        await complete(handler="document_digest", prompt="p", model="claude-haiku-4-5")
    row = recorder.rows[0]
    assert row.success is False
    assert "no key" in (row.error or "")
    assert row.cost_usd is None


@pytest.mark.asyncio
async def test_complete_retries_retryable_then_succeeds(recorder, monkeypatch):
    attempts = []

    class _RateLimited(Exception):
        status_code = 429

    async def fake(**kwargs):
        attempts.append(1)
        if len(attempts) < 3:
            raise _RateLimited("slow down")
        return _fake_response("done")

    async def no_sleep(_):
        pass

    monkeypatch.setattr(litellm, "acompletion", fake)
    monkeypatch.setattr(llm.asyncio, "sleep", no_sleep)
    result = await complete(handler="task_verdict", prompt="p", model="claude-haiku-4-5")
    assert result.text == "done"
    assert len(attempts) == 3


@pytest.mark.asyncio
async def test_complete_does_not_retry_non_retryable(recorder, monkeypatch):
    attempts = []

    class _BadRequest(Exception):
        status_code = 400

    async def fake(**kwargs):
        attempts.append(1)
        raise _BadRequest("bad schema")

    monkeypatch.setattr(litellm, "acompletion", fake)
    with pytest.raises(_BadRequest):
        await complete(handler="task_verdict", prompt="p", model="claude-haiku-4-5")
    assert len(attempts) == 1


@pytest.mark.asyncio
async def test_complete_coerces_list_content_to_text(recorder, monkeypatch):
    async def fake(**kwargs):
        response = _fake_response()
        response.choices[0].message.content = [
            {"type": "text", "text": '{"is_good":'},
            {"type": "text", "text": " true}"},
        ]
        return response

    monkeypatch.setattr(litellm, "acompletion", fake)
    result = await complete(handler="task_verdict", prompt="p", model="claude-haiku-4-5")
    assert result.text == '{"is_good": true}'


@pytest.mark.asyncio
async def test_record_usage_swallows_recorder_errors():
    async def broken(row):
        raise RuntimeError("db down")

    set_usage_recorder(broken)
    try:
        await record_usage(LLMUsageRow(handler="h", model="m"))
    finally:
        set_usage_recorder(None)


@pytest.mark.asyncio
async def test_classifier_cli_usage_oauth_flat(recorder):
    from oddish.analyze.classifier import _record_cli_usage

    payload = {
        "total_cost_usd": 0.42,
        "usage": {
            "input_tokens": 100,
            "output_tokens": 50,
            "cache_read_input_tokens": 30,
            "cache_creation_input_tokens": 20,
        },
    }
    await _record_cli_usage(
        payload,
        "claude-haiku-4-5",
        {"CLAUDE_CODE_OAUTH_TOKEN": "tok"},
        trial_id="t-3",
        duration_ms=1234,
    )
    row = recorder.rows[0]
    assert row.handler == "trial_classifier"
    assert row.cost_usd is None
    assert row.cost_basis == COST_BASIS_OAUTH_FLAT
    assert row.input_tokens == 150  # inclusive of cache read + write
    assert row.trial_id == "t-3"


@pytest.mark.asyncio
async def test_classifier_cli_usage_api_trusts_native_cost(recorder):
    from oddish.analyze.classifier import _record_cli_usage

    payload = {
        "total_cost_usd": 0.42,
        "usage": {"input_tokens": 100, "output_tokens": 50},
    }
    await _record_cli_usage(
        payload,
        "global.anthropic.claude-haiku-4-5-20251001-v1:0",
        {},
        trial_id=None,
        duration_ms=10,
    )
    row = recorder.rows[0]
    assert row.model == "claude-haiku-4-5"
    assert row.cost_usd == pytest.approx(0.42)
    assert row.cost_basis == COST_BASIS_API
