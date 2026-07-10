"""Upload-time GKE build spawns (api.routers.tasks._spawn_gke_image_builds).

One task's spawn failure must not abort the rest of the batch; a missing
builder function (GKE-less deploy) still short-circuits to the single
outer-catch log.
"""

from __future__ import annotations

import types

import modal
import pytest

pytestmark = pytest.mark.asyncio


class _FakeSession:
    def __init__(self, rows: list[tuple[str, int]]):
        self._rows = rows

    async def execute(self, stmt):
        return self._rows


def _install_builder(monkeypatch, spawn):
    builder = types.SimpleNamespace(spawn=types.SimpleNamespace(aio=spawn))
    monkeypatch.setattr(
        modal.Function, "from_name", staticmethod(lambda *a, **kw: builder)
    )


async def test_one_failed_spawn_does_not_abort_batch(monkeypatch):
    from api.routers.tasks import _spawn_gke_image_builds

    attempted: list[tuple[str, int]] = []

    async def spawn(*, task_id: str, version: int):
        attempted.append((task_id, version))
        if task_id == "t-boom":
            raise RuntimeError("spawn blew up")

    _install_builder(monkeypatch, spawn)
    session = _FakeSession([("t-boom", 1), ("t-ok", 2)])

    await _spawn_gke_image_builds(session, ["t-boom", "t-ok"])

    assert attempted == [("t-boom", 1), ("t-ok", 2)]


async def test_missing_builder_short_circuits_batch(monkeypatch):
    from api.routers.tasks import _spawn_gke_image_builds

    attempted: list[tuple[str, int]] = []

    async def spawn(*, task_id: str, version: int):
        attempted.append((task_id, version))
        raise modal.exception.NotFoundError("no build_gke_task_image here")

    _install_builder(monkeypatch, spawn)
    session = _FakeSession([("t-a", 1), ("t-b", 1)])

    # Swallowed by the outer best-effort catch; only the first task attempts.
    await _spawn_gke_image_builds(session, ["t-a", "t-b"])

    assert attempted == [("t-a", 1)]
