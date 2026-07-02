"""S5-T9: the startup schema guard forces quota_mode=off (never crashes) when
the quota schema is incomplete -- covering BOTH the trials column (oddish
alembic tree) and the backend quotas table, which migrate separately.
"""

from __future__ import annotations

from contextlib import asynccontextmanager

import pytest

from api.app import _assert_quota_schema_or_force_off
from oddish.config import QuotaMode, settings


class _FakeSession:
    def __init__(self, schema_ready):
        self._schema_ready = schema_ready
        self.scalar_calls = 0

    async def scalar(self, *args, **kwargs):
        self.scalar_calls += 1
        if self._schema_ready is _RAISE:
            raise RuntimeError("DB unavailable at startup")
        return self._schema_ready


_RAISE = object()


def _patch_session(monkeypatch, session):
    @asynccontextmanager
    async def fake_get_session():
        yield session

    monkeypatch.setattr("oddish.db.get_session", fake_get_session)


@pytest.mark.asyncio
async def test_guard_forces_off_when_schema_incomplete(monkeypatch):
    monkeypatch.setattr(settings, "quota_mode", QuotaMode.ENFORCE)
    _patch_session(monkeypatch, _FakeSession(schema_ready=False))

    await _assert_quota_schema_or_force_off()

    assert settings.quota_mode == QuotaMode.OFF


@pytest.mark.asyncio
async def test_guard_leaves_enforce_when_schema_ready(monkeypatch):
    monkeypatch.setattr(settings, "quota_mode", QuotaMode.ENFORCE)
    _patch_session(monkeypatch, _FakeSession(schema_ready=True))

    await _assert_quota_schema_or_force_off()

    assert settings.quota_mode == QuotaMode.ENFORCE


@pytest.mark.asyncio
async def test_guard_leaves_mode_as_is_on_transient_db_error(monkeypatch):
    monkeypatch.setattr(settings, "quota_mode", QuotaMode.ENFORCE)
    _patch_session(monkeypatch, _FakeSession(schema_ready=_RAISE))

    await _assert_quota_schema_or_force_off()

    assert settings.quota_mode == QuotaMode.ENFORCE


@pytest.mark.asyncio
async def test_guard_is_a_noop_when_already_off(monkeypatch):
    monkeypatch.setattr(settings, "quota_mode", QuotaMode.OFF)
    session = _FakeSession(schema_ready=False)
    _patch_session(monkeypatch, session)

    await _assert_quota_schema_or_force_off()

    assert settings.quota_mode == QuotaMode.OFF
    assert session.scalar_calls == 0
