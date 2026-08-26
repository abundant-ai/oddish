from __future__ import annotations

import json
from types import SimpleNamespace

from oddish.workers.agents.claude_code import parse_claude_provider_failure
from oddish.workers.harbor.failure_info import (
    PROVIDER_FAILURE_FILENAME,
    ProviderFailureEvidence,
    classify_harbor_failure,
    classify_provider_failure,
)


def _event(**overrides) -> str:
    event = {
        "type": "result",
        "subtype": "success",
        "is_error": True,
        "terminal_reason": "api_error",
        "api_error_status": 529,
        "session_id": "session-123",
        "request_id": "req-456",
        "retry_after_ms": 12500,
        "result": "API Error: 529 Overloaded",
        **overrides,
    }
    return json.dumps(event)


def test_parse_structured_claude_overload_preserves_provider_identifiers():
    failure = parse_claude_provider_failure(_event())

    assert failure is not None
    assert failure.http_status == 529
    assert failure.request_id == "req-456"
    assert failure.resume_token == "session-123"
    assert failure.retry_after_seconds == 12.5
    decision = classify_provider_failure(
        failure,
        exception_type="ApiOverloadedError",
    )
    assert decision.retry_reason == "provider_overload"
    assert decision.retryable is True


def test_parse_uses_final_result_event_after_a_resumed_success():
    stream = "\n".join(
        [
            _event(),
            json.dumps(
                {
                    "type": "result",
                    "subtype": "success",
                    "is_error": False,
                    "terminal_reason": "completed",
                    "session_id": "session-123",
                }
            ),
        ]
    )

    assert parse_claude_provider_failure(stream) is None


def test_stdout_terminal_result_takes_precedence_over_json_stderr():
    success = json.dumps(
        {
            "type": "result",
            "is_error": False,
            "terminal_reason": "completed",
        }
    )

    assert parse_claude_provider_failure(success, _event()) is None


def test_success_result_text_mentioning_529_is_not_a_failure():
    success = json.dumps(
        {
            "type": "result",
            "subtype": "success",
            "is_error": False,
            "terminal_reason": "completed",
            "result": "Handled API Error: 529 in the retry documentation.",
            "session_id": "session-123",
        }
    )

    assert parse_claude_provider_failure(success) is None


def test_authentication_failure_is_permanent_and_typed():
    failure = parse_claude_provider_failure(_event(api_error_status=401))

    assert failure is not None
    decision = classify_provider_failure(
        failure,
        exception_type="AgentAuthenticationError",
    )
    assert decision.retryable is False


def test_parse_status_from_structured_result_message_fallback():
    failure = parse_claude_provider_failure(
        _event(api_error_status=None, result="API Error: 529 Overloaded")
    )

    assert failure is not None
    assert failure.http_status == 529


def test_parse_permanent_status_from_confirmed_api_error_message():
    failure = parse_claude_provider_failure(
        _event(api_error_status=None, result="API Error: 400 invalid request")
    )

    assert failure is not None
    assert failure.http_status == 400
    assert (
        classify_provider_failure(failure, exception_type="UnknownApiError").retryable
        is False
    )


def test_unstatused_unknown_api_error_is_not_assumed_transient():
    failure = parse_claude_provider_failure(
        _event(api_error_status=None, result="API Error: upstream disconnected")
    )

    assert failure is not None
    assert failure.http_status is None
    decision = classify_provider_failure(failure, exception_type="UnknownApiError")
    assert decision.retryable is False


def test_unstatused_typed_connection_error_is_retryable():
    failure = parse_claude_provider_failure(
        _event(
            api_error_status=None,
            result="API Error: Connection closed mid-response",
        )
    )

    assert failure is not None
    decision = classify_provider_failure(
        failure,
        exception_type="ApiConnectionClosedError",
    )
    assert decision.retryable is True
    assert decision.retry_reason == "provider_server_error"


def test_classify_agent_setup_transport_failure():
    outcome = SimpleNamespace(
        error="NetworkConnectionError: curl: (56) unexpected eof while reading",
        exception_type="NetworkConnectionError",
        phase_timing={"environment_setup": {}, "agent_setup": {}},
        job_dir=None,
    )

    failure = classify_harbor_failure(outcome)
    assert failure is not None
    assert failure.as_dict() == {
        "schema_version": 1,
        "category": "agent_install",
        "phase": "agent_setup",
        "code": "NetworkConnectionError",
        "retryable": True,
        "retry_reason": "agent_install",
    }


def test_classify_exact_modal_image_build_failure_as_permanent():
    outcome = SimpleNamespace(
        error="RuntimeError: Image build for im-abc123 failed",
        exception_type="RuntimeError",
        phase_timing={"environment_setup": {}},
        job_dir=None,
    )

    failure = classify_harbor_failure(outcome)

    assert failure is not None
    assert failure.category == "environment_build"
    assert failure.phase == "environment_setup"
    assert failure.retryable is False


def test_image_build_mention_without_exact_failure_remains_retryable():
    outcome = SimpleNamespace(
        error="RuntimeError: Image build for im-abc123 timed out",
        exception_type="RuntimeError",
        phase_timing={"environment_setup": {}},
        job_dir=None,
    )

    failure = classify_harbor_failure(outcome)

    assert failure is not None
    assert failure.category == "environment_build"
    assert failure.retryable is True


def test_classify_reads_structured_provider_failure_from_job_artifact(tmp_path):
    log = tmp_path / "trial" / "agent" / PROVIDER_FAILURE_FILENAME
    log.parent.mkdir(parents=True)
    failure = parse_claude_provider_failure(_event())
    assert failure is not None
    log.write_text(json.dumps(failure.as_dict()), encoding="utf-8")
    outcome = SimpleNamespace(
        error="UnknownApiError: command failed",
        exception_type="UnknownApiError",
        phase_timing={"agent_execution": {}},
        job_dir=tmp_path,
    )

    failure = classify_harbor_failure(outcome)

    assert failure is not None
    assert failure.as_dict() == {
        "category": "provider_api",
        "phase": "agent_execution",
        "code": "http_529",
        "schema_version": 1,
        "retryable": True,
        "retry_reason": "provider_overload",
        "provider": "claude-code",
        "terminal_reason": "api_error",
        "http_status": 529,
        "request_id": "req-456",
        "session_id": "session-123",
        "retry_after_seconds": 12.5,
        "summary": "API Error: 529 Overloaded",
    }


def test_classify_unstatused_known_permanent_provider_failure(tmp_path):
    log = tmp_path / "trial" / "agent" / PROVIDER_FAILURE_FILENAME
    log.parent.mkdir(parents=True)
    log.write_text(
        json.dumps(
            ProviderFailureEvidence(
                provider="claude-code",
                terminal_reason="api_error",
                resume_token="session-123",
                summary="API Error: Not logged in",
            ).as_dict()
        ),
        encoding="utf-8",
    )
    outcome = SimpleNamespace(
        error="AgentAuthenticationError: Not logged in",
        exception_type="AgentAuthenticationError",
        phase_timing={"agent_execution": {}},
        job_dir=tmp_path,
    )

    failure = classify_harbor_failure(outcome)

    assert failure is not None
    assert "http_status" not in failure.as_dict()
    assert failure.retryable is False


def test_provider_failure_artifact_bounds_untrusted_fields():
    payload = ProviderFailureEvidence(
        provider="p" * 200,
        terminal_reason="r" * 200,
        request_id="i" * 400,
        resume_token="s" * 400,
        retry_after_seconds=100_000,
        summary="m" * 2_000,
    ).as_dict()

    assert len(payload["provider"]) == 120
    assert len(payload["terminal_reason"]) == 120
    assert len(payload["request_id"]) == 256
    assert len(payload["resume_token"]) == 256
    assert payload["retry_after_seconds"] == 960.0
    assert len(payload["summary"]) == 500


def test_oddish_specific_terminal_failure_is_consistent_in_metadata():
    outcome = SimpleNamespace(
        error="QuotaPauseControlError: snapshot failed",
        exception_type="QuotaPauseControlError",
        phase_timing={},
        job_dir=None,
    )

    failure = classify_harbor_failure(outcome)

    assert failure is not None
    assert failure.retryable is False
