"""Standalone server mounts the same browse endpoints as the hosted backend.

Guards the shared-core contract for the experiment-options route: both servers
must expose it (backend/api/routers/tasks.py has the hosted twin).
"""

from oddish.server import api


def test_experiment_options_route_mounted() -> None:
    paths = [getattr(route, "path", None) for route in api.routes]

    assert "/tasks/browse/experiment-options" in paths
