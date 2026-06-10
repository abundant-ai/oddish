"""next_probe_model round-robins over the fixed probe model pool."""
from __future__ import annotations

from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from oddish.config import PROBE_MODEL_ROTATION, next_probe_model  # noqa: E402


def test_pool_is_the_three_agreed_models():
    assert PROBE_MODEL_ROTATION == [
        "claude-haiku-4-5",
        "openrouter/google/gemini-2.5-flash",
        "openrouter/deepseek/deepseek-chat",
    ]


def test_round_robin_cycles_and_wraps():
    picks = [next_probe_model(i) for i in range(7)]
    assert picks == [
        "claude-haiku-4-5",
        "openrouter/google/gemini-2.5-flash",
        "openrouter/deepseek/deepseek-chat",
        "claude-haiku-4-5",
        "openrouter/google/gemini-2.5-flash",
        "openrouter/deepseek/deepseek-chat",
        "claude-haiku-4-5",
    ]
