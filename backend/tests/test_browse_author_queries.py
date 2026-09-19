"""Browse attribution is resolved once per author and never just to count pins."""

from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import httpx
import pytest
from fastapi import FastAPI

from api.routers import tasks


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "params, expected_tokens, expected_pin",
    [
        ({"author": "me", "pin_author": "me", "ids_only": "true"}, ["me"], ("user-1",)),
        ({"pin_author": "me", "count_only": "true"}, [None], ()),
        ({"author": "me", "pin_author": "someone", "count_only": "true"}, ["me"], ()),
        (
            {"author": "me", "pin_author": "someone", "ids_only": "true"},
            ["me", "someone"],
            ("user-1",),
        ),
    ],
)
async def test_browse_reuses_author_resolution(
    monkeypatch, params, expected_tokens, expected_pin
):
    @asynccontextmanager
    async def session():
        yield object()

    resolve = AsyncMock(return_value=(("user-1",), (), ()))
    browse = AsyncMock(return_value=0 if params.get("count_only") else [])
    monkeypatch.setattr(tasks, "get_read_session", session)
    monkeypatch.setattr(tasks, "_resolve_browse_authors", resolve)
    monkeypatch.setattr(tasks, "browse_tasks_core", browse)
    app = FastAPI()
    app.include_router(tasks.router)
    app.dependency_overrides[tasks.require_auth] = lambda: SimpleNamespace(
        org_id="org-1", require_scope=Mock()
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.get("/tasks/browse", params=params)
    assert response.status_code == 200, response.text
    assert [call.args[2] for call in resolve.await_args_list] == expected_tokens
    assert browse.await_args.kwargs["pin_author_user_ids"] == expected_pin


@pytest.mark.asyncio
async def test_unresolved_me_never_becomes_an_unfiltered_browse(monkeypatch):
    monkeypatch.setattr(tasks, "resolve_experiments_author", AsyncMock(return_value=(None, (), ())))
    assert await tasks._resolve_browse_authors(object(), object(), "me") == (
        (tasks.UNRESOLVED_EXPERIMENTS_OWNER,), (), ()
    )
