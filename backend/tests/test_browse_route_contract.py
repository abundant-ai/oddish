import inspect

from api.routers.tasks import browse_tasks, router


def test_browse_tasks_accepts_trial_finished_bounds() -> None:
    parameters = inspect.signature(browse_tasks).parameters

    assert "trial_finished_after" in parameters
    assert "trial_finished_before" in parameters


def test_experiment_options_route_registered_before_task_id() -> None:
    """The literal experiment-options route must be registered ahead of the
    dynamic /tasks/{task_id} routes so FastAPI's in-order matching hits it."""
    paths = [getattr(route, "path", None) for route in router.routes]

    assert "/tasks/browse/experiment-options" in paths
    assert paths.index("/tasks/browse/experiment-options") < paths.index(
        "/tasks/{task_id}"
    )
