"""Missing organization claims must never create or guess a funded tenant."""

from unittest.mock import AsyncMock
import pytest
from fastapi import HTTPException
from auth.provisioning import get_or_create_user_from_clerk


@pytest.mark.asyncio
@pytest.mark.parametrize("email", [None, "member@example.com"])
async def test_no_active_org_requires_selection_without_database_writes(email):
    session = AsyncMock()
    with pytest.raises(HTTPException) as rejected:
        await get_or_create_user_from_clerk(session, "clerk_user", None, email, None)
    assert rejected.value.status_code == 403
    assert "Select an organization" in rejected.value.detail
    assert session.mock_calls == []
