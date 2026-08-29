"""get_read_session: autocommit reads with no transaction round-trips.

``get_read_session`` exists so read-only endpoints stop paying
BEGIN/COMMIT round-trips to a pooler that sits a network hop away. These
tests pin its two contracts against a real database: statements really do
run outside any enclosing transaction (each in its own implicit one), and
the soft-delete auto-filter still applies because it is keyed on the
Session class, not on transaction handling.
"""

from __future__ import annotations

import pytest
from sqlalchemy import text

from oddish.core.cost_exclusions import CostExclusions, load_cost_exclusions
from oddish.db.connection import get_read_session, get_session


@pytest.mark.asyncio
async def test_read_session_has_no_enclosing_transaction():
    """Two statements see different transaction ids.

    ``txid_current()`` allocates a transaction id and returns the same
    value for every call inside one transaction. Inside ``get_session``'s
    BEGIN...COMMIT bracket the two calls match; in autocommit each
    statement is its own implicit transaction, so they differ.
    """
    async with get_read_session() as session:
        first = await session.scalar(text("SELECT txid_current()"))
        second = await session.scalar(text("SELECT txid_current()"))
    assert first != second


@pytest.mark.asyncio
async def test_read_session_loads_cost_exclusions_without_a_savepoint():
    """Autocommit readers cannot issue SAVEPOINT outside a transaction."""
    async with get_read_session() as session:
        assert session.info["oddish_read_autocommit"] is True
        exclusions = await load_cost_exclusions(session)

    assert isinstance(exclusions, CostExclusions)


@pytest.mark.asyncio
async def test_write_session_runs_one_transaction_for_contrast():
    """The same probe inside get_session sees a single transaction."""
    async with get_session() as session:
        first = await session.scalar(text("SELECT txid_current()"))
        second = await session.scalar(text("SELECT txid_current()"))
    assert first == second


@pytest.mark.asyncio
async def test_read_session_still_applies_soft_delete_filter():
    """A tombstoned experiment is invisible through get_read_session."""
    from oddish.db import ExperimentModel, utcnow
    from sqlalchemy import select

    experiment_id = "read-session-filter-probe"
    async with get_session() as session:
        session.add(
            ExperimentModel(
                id=experiment_id,
                org_id="org-read-session-test",
                name="soft-delete probe",
                deleted_at=utcnow(),
            )
        )

    try:
        async with get_read_session() as session:
            filtered = await session.scalar(
                select(ExperimentModel.id).where(ExperimentModel.id == experiment_id)
            )
            unfiltered = await session.scalar(
                select(ExperimentModel.id)
                .where(ExperimentModel.id == experiment_id)
                .execution_options(include_deleted=True)
            )
        assert filtered is None
        assert unfiltered == experiment_id
    finally:
        async with get_session() as session:
            await session.execute(
                text("DELETE FROM experiments WHERE id = :id"),
                {"id": experiment_id},
            )
