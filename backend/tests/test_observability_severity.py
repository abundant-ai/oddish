"""Severity policy: handled 4xx and expected Daytona NotFounds are not errors.

Pure tests of the two policy functions in ``oddish.observability`` that the
backend wires into logfire (httpx response hook + exception callback). No
network, no logfire pipeline — the contract under test is exactly what each
function does to the span/helper it is handed.
"""

from __future__ import annotations

from types import SimpleNamespace

from fastapi.exceptions import HTTPException
from opentelemetry.trace import StatusCode

from oddish.observability import (
    classify_recorded_exception,
    expected_4xx_response_hook,
)

# Stand-ins for the daytona SDK exception classes: the policy matches on
# class name + module (the SDK is not imported by the policy itself).
DaytonaNotFound = type(
    "NotFoundException",
    (Exception,),
    {"__module__": "daytona_api_client_async.exceptions"},
)


class FakeSpan:
    def __init__(self, name: str = "span"):
        self.name = name
        self.statuses: list = []
        self.attributes: dict = {}

    def set_status(self, status):
        self.statuses.append(status)

    def set_attributes(self, attrs):
        self.attributes.update(attrs)


class FakeHelper:
    """The slice of logfire's ExceptionCallbackHelper the policy touches."""

    def __init__(self, exception, span_name="span", event_attributes=None):
        self.exception = exception
        self.span = FakeSpan(span_name)
        self.event_attributes = dict(event_attributes or {})
        self.level = None
        self.create_issue = True
        self.event_dropped = False

    def no_record_exception(self):
        self.event_dropped = True


def _status_codes(span: FakeSpan):
    return [s.status_code for s in span.statuses]


def test_response_hook_downgrades_client_4xx_to_ok_warn():
    span = FakeSpan()
    expected_4xx_response_hook(span, None, SimpleNamespace(status_code=404))
    assert _status_codes(span) == [StatusCode.OK]
    assert span.attributes["logfire.level_num"] == 13


def test_response_hook_leaves_5xx_and_2xx_alone():
    for code in (200, 500, 503):
        span = FakeSpan()
        expected_4xx_response_hook(span, None, SimpleNamespace(status_code=code))
        assert span.statuses == []
        assert span.attributes == {}


def test_handled_http_4xx_on_autotrace_span_downgrades_level_and_status():
    helper = FakeHelper(HTTPException(status_code=404, detail="missing"))
    classify_recorded_exception(helper)
    assert helper.level == "warn"
    assert _status_codes(helper.span) == [StatusCode.OK]
    assert helper.create_issue is False
    assert helper.event_dropped is False


def test_handled_http_4xx_from_fastapi_integration_drops_only_the_event():
    helper = FakeHelper(
        HTTPException(status_code=404, detail="missing"),
        event_attributes={"recorded_by_logfire_fastapi": True},
    )
    classify_recorded_exception(helper)
    # The span here is the whole request span: its status and level must
    # stay untouched; only the duplicate exception event goes away.
    assert helper.event_dropped is True
    assert helper.span.statuses == []
    assert helper.level is None
    assert helper.create_issue is False


def test_http_5xx_stays_an_error():
    helper = FakeHelper(HTTPException(status_code=502, detail="upstream"))
    classify_recorded_exception(helper)
    assert helper.level is None
    assert helper.span.statuses == []
    assert helper.create_issue is True
    assert helper.event_dropped is False


def test_daytona_notfound_on_get_span_is_expected():
    helper = FakeHelper(DaytonaNotFound("(404)"), span_name="AsyncDaytona.get")
    classify_recorded_exception(helper)
    assert helper.level == "warn"
    assert _status_codes(helper.span) == [StatusCode.OK]
    assert helper.create_issue is False


def test_daytona_notfound_on_create_span_stays_an_error():
    """A missing snapshot/image on create is a real failure, not cleanup."""
    helper = FakeHelper(DaytonaNotFound("(404)"), span_name="AsyncDaytona.create")
    classify_recorded_exception(helper)
    assert helper.level is None
    assert helper.span.statuses == []
    assert helper.create_issue is True


def test_unrelated_exceptions_are_untouched():
    helper = FakeHelper(ValueError("boom"), span_name="AsyncDaytona.get")
    classify_recorded_exception(helper)
    assert helper.level is None
    assert helper.span.statuses == []
    assert helper.create_issue is True
    assert helper.event_dropped is False
