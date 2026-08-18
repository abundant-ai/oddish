import inspect

from api.routers.tasks import browse_tasks, router


def test_browse_tasks_accepts_trial_finished_bounds() -> None:
    parameters = inspect.signature(browse_tasks).parameters

    assert "trial_finished_after" in parameters
    assert "trial_finished_before" in parameters


def test_experiment_options_route_mounted() -> None:
    """Hosted server mounts the shared-core experiment-options route; the
    standalone twin is guarded by oddish/tests/test_server_route_registration.py.
    """
    paths = [getattr(route, "path", None) for route in router.routes]

    assert "/tasks/browse/experiment-options" in paths
