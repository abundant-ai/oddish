from __future__ import annotations

import json
from types import SimpleNamespace

from oddish.workers.harbor.failure_info import (
    classify_harbor_failure,
    parse_claude_provider_failure,
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
    assert failure.session_id == "session-123"
    assert failure.retry_after_seconds == 12.5
    assert failure.retry_reason == "provider_overload"
    assert failure.exception_class.__name__ == "ApiOverloadedError"


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
    assert failure.retryable is False
    assert failure.exception_class.__name__ == "AgentAuthenticationError"
    assert failure.as_failure_info()["retryable"] is False


def test_parse_status_from_structured_result_message_fallback():
    failure = parse_claude_provider_failure(
        _event(api_error_status=None, result="API Error: 529 Overloaded")
    )

    assert failure is not None
    assert failure.http_status == 529
    assert failure.retry_reason == "provider_overload"


def test_parse_permanent_status_from_confirmed_api_error_message():
    failure = parse_claude_provider_failure(
        _event(api_error_status=None, result="API Error: 400 invalid request")
    )

    assert failure is not None
    assert failure.http_status == 400
    assert failure.retryable is False


def test_unstatused_api_error_defaults_to_retryable():
    failure = parse_claude_provider_failure(
        _event(api_error_status=None, result="API Error: upstream disconnected")
    )

    assert failure is not None
    assert failure.http_status is None
    assert failure.retryable is True
    assert failure.exception_class.__name__ == "UnknownApiError"


def test_classify_agent_setup_transport_failure():
    outcome = SimpleNamespace(
        error="NetworkConnectionError: curl: (56) unexpected eof while reading",
        exception_type="NetworkConnectionError",
        phase_timing={"environment_setup": {}, "agent_setup": {}},
        job_dir=None,
    )

    assert classify_harbor_failure(outcome) == {
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
    assert failure["category"] == "environment_build"
    assert failure["phase"] == "environment_setup"
    assert failure["retryable"] is False


def test_classify_reads_structured_provider_failure_from_job_artifact(tmp_path):
    log = tmp_path / "trial" / "agent" / "claude-code.txt"
    log.parent.mkdir(parents=True)
    log.write_text(_event(), encoding="utf-8")
    outcome = SimpleNamespace(
        error="UnknownApiError: command failed",
        exception_type="UnknownApiError",
        phase_timing={"agent_execution": {}},
        job_dir=tmp_path,
    )

    failure = classify_harbor_failure(outcome)

    assert failure == {
        "category": "provider_api",
        "phase": "agent_execution",
        "code": "http_529",
        "retryable": True,
        "retry_reason": "provider_overload",
        "terminal_reason": "api_error",
        "http_status": 529,
        "request_id": "req-456",
        "session_id": "session-123",
        "retry_after_seconds": 12.5,
        "message": "API Error: 529 Overloaded",
    }


def test_classify_unstatused_known_permanent_provider_failure(tmp_path):
    log = tmp_path / "trial" / "agent" / "claude-code.txt"
    log.parent.mkdir(parents=True)
    log.write_text(
        _event(api_error_status=None, result="API Error: Not logged in"),
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
    assert "http_status" not in failure
    assert failure["retryable"] is False
