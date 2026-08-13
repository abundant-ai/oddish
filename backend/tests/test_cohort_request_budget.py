"""The cohort comparison generates inline in an API request, so its subprocess
budget has to fit inside the one Modal gives that request.

Kept as a test rather than a derived constant so the service module stays free
of the Modal import: nothing under api/services imports modal_app, and pulling
it in would build Modal objects on every worker and test import.
"""

from modal_app import (
    AGENT_CAPABILITIES_REGEN_TIMEOUT_SECONDS,
    API_TIMEOUT_SECONDS,
)

from api.services.agent_capabilities import (
    ANALYSIS_TIMEOUT,
    REQUEST_OVERHEAD_ALLOWANCE,
)


def test_cohort_timeout_fits_the_request_budget():
    # A budget above the request's is unreachable: Modal kills the container
    # first, so the CLI timeout never fires and the fetch and workspace work is
    # lost. It was 1200s against a 600s request.
    assert ANALYSIS_TIMEOUT + REQUEST_OVERHEAD_ALLOWANCE <= API_TIMEOUT_SECONDS


def test_the_allowance_leaves_the_subprocess_most_of_the_request():
    # The other direction: headroom so large that the tool loop cannot finish
    # anything is its own failure. The subprocess should still own the bulk.
    assert ANALYSIS_TIMEOUT > API_TIMEOUT_SECONDS / 2


def test_the_backgrounded_rebuild_gets_at_least_the_same_budget():
    # The share route's spawned rebuild runs the same generation off the
    # request path. A tighter container budget than the in-request one would
    # make the background path the *weaker* of the two, which is backwards.
    assert (
        ANALYSIS_TIMEOUT + REQUEST_OVERHEAD_ALLOWANCE
        <= AGENT_CAPABILITIES_REGEN_TIMEOUT_SECONDS
    )
