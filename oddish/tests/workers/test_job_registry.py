import pytest

from oddish.db import WorkerJobKind
from oddish.workers.jobs.registry import (
    HandlerAlreadyRegisteredError,
    clear_handlers,
    get_handler,
    register,
)


class _StubHandler:
    kind = WorkerJobKind.ANALYZER

    def default_queue_key(self, job) -> str:
        return "qa"

    def validate_payload(self, payload: dict) -> dict:
        return payload

    async def run(self, job):
        raise NotImplementedError


class _OtherHandler(_StubHandler):
    pass


def test_register_override_replaces_existing():
    clear_handlers()
    first, second = _StubHandler(), _OtherHandler()
    register(first)
    register(second, override=True)
    assert get_handler(WorkerJobKind.ANALYZER) is second


def test_register_without_override_still_raises():
    """Accidental double-registration must stay fatal: two handlers silently
    racing is worse than the crash."""
    clear_handlers()
    register(_StubHandler())
    with pytest.raises(HandlerAlreadyRegisteredError):
        register(_OtherHandler())
