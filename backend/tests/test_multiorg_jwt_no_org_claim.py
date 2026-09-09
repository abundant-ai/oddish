"""Missing organization claims must never create or guess a funded tenant."""

from unittest.mock import AsyncMock
import pytest
from fastapi import HTTPException
import auth.provisioning as provisioning
from auth.provisioning import get_or_create_user_from_clerk


@pytest.mark.asyncio
@pytest.mark.parametrize("email", [None, "member@example.com"])
async def test_no_active_org_requires_selection_without_database_writes(
    email, monkeypatch
):
    session = AsyncMock()
    monkeypatch.setattr(provisioning, "get_session", session)
    with pytest.raises(HTTPException) as rejected:
        await get_or_create_user_from_clerk("clerk_user", None, email, None)
    assert rejected.value.status_code == 403
    assert "Select an organization" in rejected.value.detail
    assert session.mock_calls == []
