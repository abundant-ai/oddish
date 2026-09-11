"""Permanent-vs-transient classification for provider API errors.

The Azure content-policy resource block of 2026-07-26 is the motivating case:
every task-verdict call 403'd for 58 hours while the QA job treated it as
retryable, burning 6/6 attempts per job and re-hitting the blocked endpoint
26x per task.
"""

from __future__ import annotations

from oddish.workers.queue.provider_failures import (
    is_permanent_provider_failure,
    is_setup_failure_without_work,
)


# The exact string prod recorded on 6,155 QA jobs.
AZURE_CONTENT_POLICY_403 = (
    "PermissionDeniedError: Error code: 403 - {'error': {'message': 'Your "
    "resource has been temporarily blocked because we detected behavior that "
    "may violate our content policy. For more details on Azure OpenAI service "
    "content policy, please visit https://aka.ms/aoaicodeofconduct'}}"
)


def test_azure_content_policy_block_is_permanent():
    assert is_permanent_provider_failure(AZURE_CONTENT_POLICY_403) is True


def test_bare_403_is_permanent():
    assert is_permanent_provider_failure("Error code: 403 - forbidden") is True


def test_permission_denied_without_code_is_permanent():
    assert is_permanent_provider_failure("PermissionDeniedError: nope") is True


def test_none_and_empty_are_not_permanent():
    assert is_permanent_provider_failure(None) is False
    assert is_permanent_provider_failure("") is False


def test_timeout_is_still_retryable():
    assert is_permanent_provider_failure("TimeoutError: ") is False


def test_low_credit_balance_is_still_retryable():
    """A recurring prod 400 that recovers on its own within a minute -- it must
    keep its retries, or a transient billing blip permanently fails the job."""
    err = (
        "BadRequestError: Error code: 400 - {'error': {'message': 'Your credit "
        "balance is too low to access the Anthropic API'}}"
    )
    assert is_permanent_provider_failure(err) is False


def test_rate_limit_is_still_retryable():
    assert (
        is_permanent_provider_failure("RateLimitError: Error code: 429 - slow down")
        is False
    )


def test_token_limit_is_still_retryable():
    err = (
        "BadRequestError: Error code: 400 - {'error': {'message': 'Input tokens "
        "exceed the configured limit of 922000 tokens.'}}"
    )
    assert is_permanent_provider_failure(err) is False


def test_403_inside_a_larger_message_still_matches():
    """The QA handler reads ``task.verdict_error``, which prefixes the type."""
    assert (
        is_permanent_provider_failure(f"QA task-abc FAILED: {AZURE_CONTENT_POLICY_403}")
        is True
    )


def _setup_kwargs(**overrides):
    base = dict(
        exception_type="NotFoundError",
        error="NotFoundError: The model does not exist",
        input_tokens=None,
        output_tokens=None,
        has_trajectory=False,
        total_steps=None,
    )
    base.update(overrides)
    return base


def test_not_found_without_work_is_setup_failure():
    assert is_setup_failure_without_work(**_setup_kwargs()) is True


def test_file_not_found_is_not_a_provider_setup_failure():
    assert (
        is_setup_failure_without_work(
            **_setup_kwargs(
                exception_type="FileNotFoundError",
                error="FileNotFoundError: missing reward file",
            )
        )
        is False
    )


def test_not_found_after_real_work_is_not_setup_only():
    assert is_setup_failure_without_work(**_setup_kwargs(input_tokens=12)) is False
    assert is_setup_failure_without_work(**_setup_kwargs(has_trajectory=True)) is False
    assert is_setup_failure_without_work(**_setup_kwargs(total_steps=3)) is False
