"""The capability subprocess must remain bounded inside its worker job."""

from modal_app import WORKER_TIMEOUT_SECONDS

from api.services.agent_capabilities import ANALYSIS_TIMEOUT


def test_cohort_timeout_fits_the_worker_budget():
    assert ANALYSIS_TIMEOUT < WORKER_TIMEOUT_SECONDS
