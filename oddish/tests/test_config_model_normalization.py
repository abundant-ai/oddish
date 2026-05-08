from __future__ import annotations

from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from oddish.config import normalize_model_id, settings  # noqa: E402


def test_google_gemini_models_canonicalize_to_litellm_gemini_route() -> None:
    assert (
        normalize_model_id("google/gemini-3.1-pro-preview")
        == "gemini/gemini-3.1-pro-preview"
    )
    assert (
        settings.normalize_trial_model(
            "terminus-2", "google/gemini-3.1-pro-preview"
        )
        == "gemini/gemini-3.1-pro-preview"
    )
    assert (
        settings.get_queue_key_for_trial(
            "terminus-2", "google/gemini-3.1-pro-preview"
        )
        == "gemini/gemini-3.1-pro-preview"
    )


def test_bare_gemini_models_use_gemini_litellm_route() -> None:
    assert (
        settings.get_queue_key_for_trial("terminus-2", "gemini-3.1-pro-preview")
        == "gemini/gemini-3.1-pro-preview"
    )
