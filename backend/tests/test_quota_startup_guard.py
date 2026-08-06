from __future__ import annotations

import logging
from contextlib import asynccontextmanager

import pytest

from api.app import _assert_quota_schema_or_force_off
from oddish.config import QuotaMode, settings


class _FakeSession:
    def __init__(self, schema_ready):
        self._schema_ready = schema_ready
        self.scalar_calls = 0
        self.sql = ""

    async def scalar(self, statement, *args, **kwargs):
        self.scalar_calls += 1
        # The guard probes one object per call, so accumulate rather than
        # overwrite -- assertions look for every object it should have asked for.
        self.sql += str(statement)
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
    monkeypatch.setattr(settings, "allow_quota_schema_degrade", True)
    _patch_session(monkeypatch, _FakeSession(schema_ready=False))

    await _assert_quota_schema_or_force_off()

    assert settings.quota_mode == QuotaMode.OFF


@pytest.mark.asyncio
async def test_guard_leaves_enforce_when_schema_ready_and_probes_all_objects(
    monkeypatch,
):
    monkeypatch.setattr(settings, "quota_mode", QuotaMode.ENFORCE)
    session = _FakeSession(schema_ready=True)
    _patch_session(monkeypatch, session)

    await _assert_quota_schema_or_force_off()

    assert settings.quota_mode == QuotaMode.ENFORCE
    # One probe per object, so an incomplete schema can name what is absent.
    assert session.scalar_calls == 4
    assert "table_name = 'trials'" in session.sql
    assert "column_name = 'billed_user_id'" in session.sql
    assert "table_name = 'quotas'" in session.sql
    assert "table_name = 'quota_bumps'" in session.sql
    assert "table_name = 'org_quotas'" in session.sql


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


@pytest.mark.asyncio
async def test_guard_raises_when_enforce_incomplete_without_opt_in(monkeypatch):
    monkeypatch.setattr(settings, "quota_mode", QuotaMode.ENFORCE)
    monkeypatch.setattr(settings, "allow_quota_schema_degrade", False)
    _patch_session(monkeypatch, _FakeSession(schema_ready=False))

    with pytest.raises(RuntimeError, match="trials.billed_user_id") as exc_info:
        await _assert_quota_schema_or_force_off()

    message = str(exc_info.value)
    assert "quotas" in message
    assert "quota_bumps" in message
    assert "org_quotas" in message
    assert "ODDISH_ALLOW_QUOTA_SCHEMA_DEGRADE" in message
    assert settings.quota_mode == QuotaMode.ENFORCE


@pytest.mark.asyncio
async def test_guard_forces_off_and_logs_when_enforce_incomplete_with_opt_in(
    monkeypatch, caplog
):
    monkeypatch.setattr(settings, "quota_mode", QuotaMode.ENFORCE)
    monkeypatch.setattr(settings, "allow_quota_schema_degrade", True)
    _patch_session(monkeypatch, _FakeSession(schema_ready=False))

    with caplog.at_level(logging.ERROR, logger="api.app"):
        await _assert_quota_schema_or_force_off()

    assert settings.quota_mode == QuotaMode.OFF
    messages = [record.getMessage() for record in caplog.records]
    assert any("metric=quota.schema_incomplete" in message for message in messages)
    assert any("trials.billed_user_id" in message for message in messages)
    assert any("quotas" in message for message in messages)
    assert any("quota_bumps" in message for message in messages)
    assert any("org_quotas" in message for message in messages)


@pytest.mark.asyncio
async def test_guard_does_not_raise_when_shadow_and_schema_incomplete(monkeypatch):
    monkeypatch.setattr(settings, "quota_mode", QuotaMode.SHADOW)
    monkeypatch.setattr(settings, "allow_quota_schema_degrade", False)
    _patch_session(monkeypatch, _FakeSession(schema_ready=False))

    await _assert_quota_schema_or_force_off()

    assert settings.quota_mode == QuotaMode.OFF


@pytest.mark.asyncio
async def test_guard_leaves_shadow_when_schema_ready(monkeypatch):
    monkeypatch.setattr(settings, "quota_mode", QuotaMode.SHADOW)
    _patch_session(monkeypatch, _FakeSession(schema_ready=True))

    await _assert_quota_schema_or_force_off()

    assert settings.quota_mode == QuotaMode.SHADOW
