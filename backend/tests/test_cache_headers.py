"""Cache policy for reads the dashboard fetches directly from the API."""

from __future__ import annotations

from fastapi import FastAPI
from fastapi.testclient import TestClient

from api.cache_headers import ROUTE_CACHE_CONTROL, cache_header_middleware


def _app() -> FastAPI:
    app = FastAPI()
    app.middleware("http")(cache_header_middleware)

    @app.get("/tasks/{task_id}/files")
    async def files(task_id: str) -> dict:
        return {"task_id": task_id}

    @app.get("/tasks/{task_id}/open")
    async def open_(task_id: str) -> dict:
        return {"task_id": task_id}

    @app.get("/tasks/{task_id}/detail")
    async def detail(task_id: str) -> dict:
        return {"task_id": task_id}

    @app.get("/users")
    async def users() -> dict:
        from fastapi.responses import JSONResponse

        return JSONResponse({}, headers={"Cache-Control": "no-store"})

    @app.get("/people/search")
    async def people() -> dict:
        from fastapi import HTTPException

        raise HTTPException(status_code=404)

    return app


def test_matched_routes_get_their_policy():
    client = TestClient(_app())
    assert (
        client.get("/tasks/t1/files").headers["cache-control"]
        == ROUTE_CACHE_CONTROL["/tasks/{task_id}/files"]
    )
    assert client.get("/tasks/t1/open").headers["cache-control"] == "no-store"


def test_unlisted_routes_handler_headers_and_errors_are_left_alone():
    client = TestClient(_app())
    assert "cache-control" not in client.get("/tasks/t1/detail").headers
    assert client.get("/users").headers["cache-control"] == "no-store"
    assert "cache-control" not in client.get("/people/search").headers


def test_policy_is_keyed_on_the_route_template_not_the_url():
    """A task id that happens to look like another route never widens a match."""
    client = TestClient(_app())
    response = client.get("/tasks/files/detail")
    assert "cache-control" not in response.headers
