"""Tests for tag ownership auto-transfer on user deactivation."""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))


def _run(coro):
    return asyncio.run(coro)


class _FakeSession:
    def __init__(self, *, admin_id: str | None = "admin-1", updated_rows: int = 0):
        self.admin_id = admin_id
        self.updated_rows = updated_rows
        self.executed: list[tuple[str, dict]] = []

    async def execute(self, stmt, params=None):
        s = str(stmt)
        self.executed.append((s, dict(params or {})))

        class _R:
            def __init__(self_inner, rows, rc):
                self_inner.rows = rows
                self_inner.rowcount = rc

            def all(self_inner):
                return self_inner.rows

            def scalar(self_inner):
                return self_inner.rows[0][0] if self_inner.rows else None

        if "ORDER BY created_at" in s and "OWNER" in s:
            return _R([(self.admin_id,)] if self.admin_id else [], 1)
        if "UPDATE tags" in s:
            return _R([], self.updated_rows)
        return _R([], 0)

    async def scalar(self, stmt, params=None):
        return self.admin_id

    async def flush(self):
        pass

    async def commit(self):
        pass


def test_transfer_tag_ownership_reassigns_to_org_admin():
    from oddish.core.tags.ownership_transfer import transfer_tag_ownership_to_admin

    session = _FakeSession(admin_id="admin-1", updated_rows=4)
    transferred = _run(
        transfer_tag_ownership_to_admin(
            session, org_id="org-1", deactivated_user_id="u-deact"
        )
    )
    assert transferred == 4
    update_sql = [s for s, _ in session.executed if "UPDATE tags" in s][0]
    assert "owner_user_id = :admin_id" in update_sql
    assert "owner_user_id = :user_id" in update_sql


def test_transfer_is_noop_when_no_admin_present():
    from oddish.core.tags.ownership_transfer import transfer_tag_ownership_to_admin

    session = _FakeSession(admin_id=None, updated_rows=0)
    transferred = _run(
        transfer_tag_ownership_to_admin(
            session, org_id="org-1", deactivated_user_id="u-deact"
        )
    )
    assert transferred == 0
