"""Permanent-vs-transient classification for provider API errors.

The Azure content-policy resource block of 2026-07-26 is the motivating case:
every task-verdict call 403'd for 58 hours while the QA job treated it as
retryable, burning 6/6 attempts per job and re-hitting the blocked endpoint
26x per task.
"""

from __future__ import annotations

from datetime import datetime, timezone

from oddish.workers.queue.provider_failures import classify_provider_failure


# The exact string prod recorded on 6,155 QA jobs.
AZURE_CONTENT_POLICY_403 = (
    "PermissionDeniedError: Error code: 403 - {'error': {'message': 'Your "
    "resource has been temporarily blocked because we detected behavior that "
    "may violate our content policy. For more details on Azure OpenAI service "
    "content policy, please visit https://aka.ms/aoaicodeofconduct'}}"
)

CLAUDE_USAGE_LIMIT = (
    "Claude CLI exited with code 1: API Error: 400 You have reached your "
    "specified API usage limits. You will regain access on 2026-09-01 at "
    "00:00 UTC."
)


def test_claude_usage_limit_is_structured_and_bounded():
    failure = classify_provider_failure(CLAUDE_USAGE_LIMIT)

    assert failure.failure_class == "provider_usage_limit"
    assert failure.provider_status_code == 400
    assert failure.recovery_at == datetime(2026, 9, 1, tzinfo=timezone.utc)
    assert failure.error_summary == CLAUDE_USAGE_LIMIT


def test_usage_limit_without_parseable_recovery_time_keeps_classification():
    failure = classify_provider_failure(
        "API Error: 400 You have reached your specified API usage limits. "
        "You will regain access sometime later."
    )

    assert failure.failure_class == "provider_usage_limit"
    assert failure.provider_status_code == 400
    assert failure.recovery_at is None


def test_usage_limit_with_malformed_recovery_time_keeps_classification():
    failure = classify_provider_failure(
        "API Error: 400 You have reached your specified API usage limits. "
        "You will regain access on 2026-99-40 at 25:90 UTC."
    )

    assert failure.failure_class == "provider_usage_limit"
    assert failure.provider_status_code == 400
    assert failure.recovery_at is None


def test_long_error_summary_is_single_line_and_at_most_500_characters():
    failure = classify_provider_failure("API Error: 500\n" + "x" * 1_000)

    assert failure.failure_class == "provider_unavailable"
    assert failure.error_summary is not None
    assert "\n" not in failure.error_summary
    assert len(failure.error_summary) == 500


def test_azure_content_policy_block_is_permanent():
    assert (
        classify_provider_failure(AZURE_CONTENT_POLICY_403).failure_class
        == "permission_denied"
    )


def test_bare_403_is_permanent():
    assert (
        classify_provider_failure("Error code: 403 - forbidden").failure_class
        == "permission_denied"
    )


def test_permission_denied_without_code_is_permanent():
    assert (
        classify_provider_failure("PermissionDeniedError: nope").failure_class
        == "permission_denied"
    )


def test_none_and_empty_are_not_permanent():
    assert classify_provider_failure(None).failure_class == "unknown"
    assert classify_provider_failure("").failure_class == "unknown"


def test_timeout_is_still_retryable():
    assert classify_provider_failure("TimeoutError: ").failure_class == "timeout"


def test_low_credit_balance_is_still_retryable():
    """A recurring prod 400 that recovers on its own within a minute -- it must
    keep its retries, or a transient billing blip permanently fails the job."""
    err = (
        "BadRequestError: Error code: 400 - {'error': {'message': 'Your credit "
        "balance is too low to access the Anthropic API'}}"
    )
    failure = classify_provider_failure(err)
    assert failure.failure_class == "low_credit"
    assert failure.provider_status_code == 400


def test_rate_limit_is_still_retryable():
    err = "RateLimitError: Error code: 429 - slow down"
    failure = classify_provider_failure(err)
    assert failure.failure_class == "rate_limit"
    assert failure.provider_status_code == 429


def test_token_limit_is_still_retryable():
    err = (
        "BadRequestError: Error code: 400 - {'error': {'message': 'Input tokens "
        "exceed the configured limit of 922000 tokens.'}}"
    )
    failure = classify_provider_failure(err)
    assert failure.failure_class == "token_limit"
    assert failure.provider_status_code == 400


def test_permission_denied_has_distinct_class_and_status():
    failure = classify_provider_failure(AZURE_CONTENT_POLICY_403)

    assert failure.failure_class == "permission_denied"
    assert failure.provider_status_code == 403


def test_403_inside_a_larger_message_still_matches():
    """The QA handler reads ``task.verdict_error``, which prefixes the type."""
    assert (
        classify_provider_failure(
            f"QA task-abc FAILED: {AZURE_CONTENT_POLICY_403}"
        ).failure_class
        == "permission_denied"
    )


def test_bare_429_preserves_rate_limit_classification():
    failure = classify_provider_failure("429")

    assert failure.failure_class == "rate_limit"
    assert failure.provider_status_code == 429


def test_structured_http_status_takes_precedence_over_message_parsing():
    failure = classify_provider_failure(
        "provider request failed",
        http_status=429,
        exception_type="RateLimitError",
    )

    assert failure.failure_class == "rate_limit"
    assert failure.provider_status_code == 429
