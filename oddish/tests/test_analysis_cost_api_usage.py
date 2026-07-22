from types import SimpleNamespace

from oddish.analyze.analysis_cost import usage_from_api_message
from oddish.model_pricing import estimate_cost_usd

MODEL = "claude-opus-4-8"


def _usage(**over):
    kw = dict(
        input_tokens=1000,
        output_tokens=200,
        cache_read_input_tokens=500,
        cache_creation_input_tokens=100,
    )
    kw.update(over)
    return SimpleNamespace(**kw)


def test_returns_none_without_usage():
    assert usage_from_api_message(None, MODEL) is None


def test_returns_none_when_no_tokens_reported():
    assert usage_from_api_message(SimpleNamespace(), MODEL) is None


def test_input_tokens_are_recomposed_to_the_total():
    """The API reports uncached input only, with the cache counts additive; the
    stored field means total input the way estimate_cost_usd reads it."""
    u = usage_from_api_message(_usage(), MODEL)
    assert u.input_tokens == 1000 + 500 + 100
    assert u.cache_read_tokens == 500
    assert u.cache_write_tokens == 100
    assert u.output_tokens == 200


def test_cost_is_estimated_from_pricing():
    u = usage_from_api_message(_usage(), MODEL)
    assert u.source == "estimated"
    assert u.model == MODEL
    # Priced off the recomposed total, so the uncached share is the 1000 the API
    # actually reported -- not 1000 minus its own cache counts.
    assert u.cost_usd == estimate_cost_usd(MODEL, 1600, 200, 500, 100)
    assert u.cost_usd > 0


def test_partial_usage_still_records():
    u = usage_from_api_message(
        SimpleNamespace(input_tokens=10, output_tokens=5), MODEL
    )
    assert u.input_tokens == 10
    assert u.cache_read_tokens is None


def test_unpriced_model_keeps_tokens_without_cost():
    u = usage_from_api_message(_usage(), "some-unknown-model")
    assert u.cost_usd is None
    assert u.input_tokens == 1600
