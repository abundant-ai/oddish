"""Serving a stale comparison to a share, and the background rebuild behind it.

A share page cannot generate -- that is the property
``test_public_analysis_endpoints`` guards. This is the seam that lets it show
the *previous* answer while a worker builds the next one, without acquiring the
ability to spend an LLM call from a cold start.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from api.services import agent_capabilities as ac
from api.services.agent_capabilities import stale_reason

FRESH_META = {"cohort_hash": "aaa", "schema_version": 4, "task_version_id": "tv-1"}
FRESH = {"current_hash": "aaa", "schema_version": 4, "task_version_id": "tv-1"}


# --------------------------------------------------------------------------
# stale_reason: which of the three freshness keys drifted
# --------------------------------------------------------------------------


def test_fresh_metadata_has_no_reason():
    assert stale_reason(FRESH_META, **FRESH) is None


def test_cohort_drift_is_named_separately():
    """The only drift a share may render through. New trials landing on a
    version change the hash while the stored answer stays about the same task,
    at the same schema -- readable, just behind."""
    assert stale_reason({**FRESH_META, "cohort_hash": "bbb"}, **FRESH) == "cohort"


def test_schema_drift_is_named_separately():
    assert stale_reason({**FRESH_META, "schema_version": 3}, **FRESH) == "schema"


def test_task_version_drift_is_named_separately():
    assert stale_reason({**FRESH_META, "task_version_id": "tv-9"}, **FRESH) == "version"


def test_missing_metadata_is_stale_for_an_unknown_reason():
    assert stale_reason(None, **FRESH) == "unknown"


def test_is_stale_still_answers_the_boolean_question():
    """The existing callers ask only whether to regenerate."""
    assert ac.is_stale(None, **FRESH)
    assert ac.is_stale({**FRESH_META, "cohort_hash": "bbb"}, **FRESH)
    assert not ac.is_stale(FRESH_META, **FRESH)


# --------------------------------------------------------------------------
# load_stored_analysis: what the share route is allowed to see
# --------------------------------------------------------------------------


def _cohorts(n: int = 4):
    trials = [{"trial_id": f"t-{i}"} for i in range(n)]
    return AsyncMock(return_value=(trials, trials))


def _session_returning(row):
    session = MagicMock()
    session.execute = AsyncMock(
        return_value=MagicMock(scalar_one_or_none=MagicMock(return_value=row))
    )
    return session


def _block(*, cohort_hash: str, schema_version: int = 4, output=None):
    return SimpleNamespace(
        block_metadata={
            "cohort_hash": cohort_hash,
            "schema_version": schema_version,
            "task_version_id": "tv-1",
        },
        output=output if output is not None else {"categories": []},
    )


@pytest.mark.asyncio
async def test_a_fresh_block_is_not_marked_stale():
    with patch.object(ac, "resolve_cohorts", new=_cohorts()), patch.object(
        ac, "cohort_hash", return_value="aaa"
    ), patch(
        "api.services.blocks.analyzer.cohort.agent_capabilities_block.SCHEMA_VERSION",
        4,
    ):
        got = await ac.load_stored_analysis(
            _session_returning(_block(cohort_hash="aaa")), "tv-1", task_id="task-1"
        )
    assert got is not None
    assert got.stale is False


@pytest.mark.asyncio
async def test_cohort_drift_is_withheld_unless_the_caller_opts_in():
    """The authenticated path regenerates on drift, so it must keep seeing the
    miss it acts on. Only the share asks for the older answer."""
    session = _session_returning(_block(cohort_hash="old"))
    with patch.object(ac, "resolve_cohorts", new=_cohorts()), patch.object(
        ac, "cohort_hash", return_value="new"
    ), patch(
        "api.services.blocks.analyzer.cohort.agent_capabilities_block.SCHEMA_VERSION",
        4,
    ):
        assert await ac.load_stored_analysis(session, "tv-1", task_id="task-1") is None


@pytest.mark.asyncio
async def test_cohort_drift_is_served_and_flagged_when_opted_in():
    session = _session_returning(_block(cohort_hash="old", output={"categories": [1]}))
    with patch.object(ac, "resolve_cohorts", new=_cohorts()), patch.object(
        ac, "cohort_hash", return_value="new"
    ), patch(
        "api.services.blocks.analyzer.cohort.agent_capabilities_block.SCHEMA_VERSION",
        4,
    ):
        got = await ac.load_stored_analysis(
            session, "tv-1", task_id="task-1", allow_cohort_drift=True
        )
    assert got is not None
    assert got.stale is True
    assert got.output == {"categories": [1]}


@pytest.mark.asyncio
async def test_schema_drift_is_never_served_even_when_drift_is_allowed():
    """A schema-3 payload rendered by schema-4 components is missing fields the
    UI reads. Behind-but-readable is the deal; wrong-shape is not."""
    session = _session_returning(_block(cohort_hash="aaa", schema_version=3))
    with patch.object(ac, "resolve_cohorts", new=_cohorts()), patch.object(
        ac, "cohort_hash", return_value="aaa"
    ), patch(
        "api.services.blocks.analyzer.cohort.agent_capabilities_block.SCHEMA_VERSION",
        4,
    ):
        got = await ac.load_stored_analysis(
            session, "tv-1", task_id="task-1", allow_cohort_drift=True
        )
    assert got is None

@pytest.mark.asyncio
async def test_a_thin_cohort_is_still_withheld_from_a_drift_tolerant_caller():
    """MIN_COHORT gates the comparison itself, not its freshness."""
    session = _session_returning(_block(cohort_hash="aaa"))
    with patch.object(ac, "resolve_cohorts", new=_cohorts(n=1)):
        got = await ac.load_stored_analysis(
            session, "tv-1", task_id="task-1", allow_cohort_drift=True
        )
    assert got is None
