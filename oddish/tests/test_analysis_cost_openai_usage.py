from types import SimpleNamespace

from oddish.analyze.analysis_cost import usage_from_openai_completion
from oddish.model_pricing import estimate_cost_usd

MODEL = "gpt-5.4"


def _usage(cached: int | None = 400, **over):
    kw = dict(
        prompt_tokens=1000,
        completion_tokens=200,
        prompt_tokens_details=(
            SimpleNamespace(cached_tokens=cached) if cached is not None else None
        ),
    )
    kw.update(over)
    return SimpleNamespace(**kw)


def test_returns_none_without_usage():
    assert usage_from_openai_completion(None, MODEL) is None


def test_returns_none_when_no_tokens_reported():
    assert usage_from_openai_completion(SimpleNamespace(), MODEL) is None


def test_prompt_tokens_are_already_the_total():
    """OpenAI's prompt_tokens INCLUDES cached tokens, unlike Anthropic's
    input_tokens which excludes them. Summing here would double-count."""
    u = usage_from_openai_completion(_usage(), MODEL)
    assert u.input_tokens == 1000
    assert u.cache_read_tokens == 400
    assert u.output_tokens == 200
    # No prompt-caching write concept on this path.
    assert u.cache_write_tokens is None


def test_cost_is_estimated_from_pricing():
    u = usage_from_openai_completion(_usage(), MODEL)
    assert u.source == "estimated"
    assert u.model == MODEL
    assert u.cost_usd == estimate_cost_usd(MODEL, 1000, 200, 400, None)
    assert u.cost_usd > 0


def test_missing_cache_details_still_records():
    u = usage_from_openai_completion(_usage(cached=None), MODEL)
    assert u.input_tokens == 1000
    assert u.cache_read_tokens is None
    assert u.cost_usd == estimate_cost_usd(MODEL, 1000, 200, None, None)


def test_unpriced_model_keeps_tokens_without_cost():
    u = usage_from_openai_completion(_usage(), "some-unknown-model")
    assert u.cost_usd is None
    assert u.input_tokens == 1000
