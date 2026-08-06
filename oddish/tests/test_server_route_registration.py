"""Standalone server mounts the same browse endpoints as the hosted backend.

Guards the shared-core contract for the experiment-options route: both servers
must expose it (backend/api/routers/tasks.py has the hosted twin), and the
literal path must be registered ahead of the dynamic /tasks/{task_id} routes so
FastAPI's in-order matching hits it.
"""

from oddish.server import api


def test_experiment_options_route_registered_before_task_id() -> None:
    paths = [getattr(route, "path", None) for route in api.routes]

    assert "/tasks/browse/experiment-options" in paths
    assert paths.index("/tasks/browse/experiment-options") < paths.index(
        "/tasks/{task_id}"
    )
