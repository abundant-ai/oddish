"""Tests for oddish.error_classification."""

from __future__ import annotations

from oddish.error_classification import (
    classify,
    is_fatal_infra,
    is_harness_error,
)


# ---------------------------------------------------------------------------
# is_harness_error — mirrors frontend's getMatrixStatus rule.
# ---------------------------------------------------------------------------


def test_no_error_message_is_not_harness():
    assert is_harness_error(None, has_reward=True) is False
    assert is_harness_error("", has_reward=False) is False


def test_any_error_message_is_harness():
    assert is_harness_error("Command failed (exit 137)", has_reward=False)
    assert is_harness_error("AuthenticationError: bad key", has_reward=False)


def test_agent_timeout_with_reward_is_not_harness():
    # Special case: harbor sometimes records AgentTimeoutError but
    # still produces a verifier reward. The trial counts as a real
    # eval; matrix UI labels it Fail (red), not Error (yellow).
    assert (
        is_harness_error("AgentTimeoutError: ran 30m", has_reward=True) is False
    )
    assert (
        is_harness_error("Agent execution timed out", has_reward=True) is False
    )


def test_agent_timeout_without_reward_is_still_harness():
    assert is_harness_error("AgentTimeoutError: ran 30m", has_reward=False)


# ---------------------------------------------------------------------------
# is_fatal_infra — strict subset that triggers cascade-cancel.
# ---------------------------------------------------------------------------


def test_no_signal_is_not_fatal():
    assert is_fatal_infra(None) is False
    assert is_fatal_infra("") is False
    assert is_fatal_infra("Some unknown failure") is False


def test_litellm_auth_errors_are_fatal():
    assert is_fatal_infra(None, exception_type="AuthenticationError")
    assert is_fatal_infra(None, exception_type="PermissionDeniedError")


def test_model_not_found_is_fatal():
    assert is_fatal_infra(None, exception_type="NotFoundError")


def test_context_window_errors_are_fatal():
    # No point retrying — task prompt > model context window.
    assert is_fatal_infra(None, exception_type="ContextWindowExceededError")
    assert is_fatal_infra(None, exception_type="ContextLengthExceededError")
    assert is_fatal_infra(None, exception_type="OutputLengthExceededError")


def test_modal_image_pull_is_fatal():
    assert is_fatal_infra(None, exception_type="ImagePullError")
    assert is_fatal_infra(None, exception_type="SandboxCreationError")


def test_oom_exit_137_is_fatal():
    msg = (
        'Command failed (exit 137): export PATH="$HOME/.local/bin:$PATH"; '
        "claude --verbose --output-format=stream-json"
    )
    assert is_fatal_infra(msg)


def test_segfault_exit_139_is_fatal():
    assert is_fatal_infra("Command failed (exit 139): some_binary")


def test_command_not_found_exit_127_is_fatal():
    assert is_fatal_infra("Command failed (exit 127): missing_cli")


def test_generic_exit_1_is_not_fatal():
    # Plain "exit 1" is too noisy to treat as fatal — many legit
    # agent failures exit 1 and should still get retried.
    assert is_fatal_infra("Command failed (exit 1): something") is False


def test_gemini_exit_55_is_not_fatal_by_default():
    # Exit 55 is gemini-cli specific; we don't blanket-fatalize it
    # because it might be a transient quota error. Add it explicitly
    # if data justifies — keeps the fatal set conservative.
    msg = "Command failed (exit 55): gemini --yolo --model=gemini-3.1-pro-preview"
    assert is_fatal_infra(msg) is False


def test_transient_errors_are_not_fatal():
    # These should keep using the existing retry loop.
    for t in (
        "RateLimitError",
        "APIConnectionError",
        "APITimeoutError",
        "InternalServerError",
        "ServiceUnavailableError",
        "AgentTimeoutError",
    ):
        assert is_fatal_infra(None, exception_type=t) is False, t


# ---------------------------------------------------------------------------
# classify — convenience label.
# ---------------------------------------------------------------------------


def test_classify_ok_path():
    assert (
        classify(None, exception_type=None, has_reward=True) == "ok"
    )


def test_classify_agent_failure():
    # Verifier ran, scored 0 — that's a real eval signal.
    assert (
        classify(None, exception_type=None, has_reward=False) == "agent_failure"
    )


def test_classify_harness_error():
    label = classify(
        "AgentTimeoutError: ran out", exception_type=None, has_reward=False
    )
    assert label == "harness_error"


def test_classify_fatal_infra_outranks_harness_error():
    # An auth error ALSO sets error_message — fatal_infra should win.
    label = classify(
        "AuthenticationError: bad key",
        exception_type="AuthenticationError",
        has_reward=False,
    )
    assert label == "fatal_infra"
