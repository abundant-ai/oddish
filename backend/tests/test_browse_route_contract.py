import inspect

from api.routers.tasks import browse_tasks


def test_browse_tasks_accepts_trial_finished_bounds() -> None:
    parameters = inspect.signature(browse_tasks).parameters

    assert "trial_finished_after" in parameters
    assert "trial_finished_before" in parameters
