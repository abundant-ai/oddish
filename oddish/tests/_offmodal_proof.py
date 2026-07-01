"""Off-Modal portability proof — run as a subprocess by
``test_off_modal_portability.py`` (no ``test_`` prefix, so pytest never
collects it directly).

It installs an import guard that makes ``import modal`` fail loudly, then drives
the **real** off-Modal control plane — enqueue → claim → handler dispatch →
record outcome — to a terminal verdict against a throwaway Postgres
(``ODDISH_DATABASE_URL``). The trial's sandbox execution is stubbed to a
deterministic verdict (Harbor owns the real sandbox; the data plane is already
non-Modal, design spec §3.2), so this isolates and proves the claim that the
worker hot path runs a job to a verdict with **zero ``import modal``**.

Prints ``STATUS=<status>`` and ``MODAL_IMPORTED=<bool>``; exits 0 iff the job
reached SUCCESS without ever importing modal.
"""

from __future__ import annotations

import asyncio
import builtins
import sys
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

# 1. Poison modal: any import on the hot path raises (proves portability).
_real_import = builtins.__import__


def _guard(name, *args, **kwargs):
    if name == "modal" or name.startswith("modal."):
        raise AssertionError(f"modal imported on the worker hot path: {name}")
    return _real_import(name, *args, **kwargs)


builtins.__import__ = _guard

from oddish.config import Settings, settings  # noqa: E402

Settings.db_use_null_pool = True

import oddish.db.connection as _conn  # noqa: E402
from oddish.db import WorkerJobKind, WorkerJobStatus, get_session  # noqa: E402
from oddish.db.models import Base, WorkerJobModel  # noqa: E402
from oddish.workers.jobs.registry import JobOutcome  # noqa: E402
from oddish.workers.queue import worker_job_single_job as wjsj  # noqa: E402

QUEUE_KEY = "offmodal-proof"


class _StubHandler:
    """Stands in for the Harbor trial run with a deterministic verdict."""

    async def run(self, job) -> JobOutcome:
        return JobOutcome.ok(result_summary={"reward": 1.0, "verdict": "pass"})


async def _main() -> int:
    # Rebind the engine/session-maker to the throwaway Postgres (NullPool).
    _conn.engine = _conn._create_engine()
    _conn.async_session_maker = _conn._create_session_maker(_conn.engine)

    # The oddish Base now declares api_keys (relocated for query access) whose
    # FKs target backend-only tables (users/organizations) absent from this
    # Base -- a whole-Base create_all can't resolve them. The proof only needs
    # the queue tables (worker_jobs + the trials/tasks the claim SQL LEFT JOINs),
    # so create every oddish table except api_keys. (Production builds the schema
    # via alembic, not create_all.)
    proof_tables = [
        table
        for name, table in Base.metadata.tables.items()
        if name != "api_keys"
    ]
    async with _conn.engine.begin() as connection:
        await connection.run_sync(
            lambda sync_conn: Base.metadata.create_all(
                sync_conn, tables=proof_tables
            )
        )

    job_id = "wj_" + uuid.uuid4().hex[:12]
    trial_id = "tr_" + uuid.uuid4().hex[:12]
    async with get_session() as session:
        session.add(
            WorkerJobModel(
                id=job_id,
                kind=WorkerJobKind.TRIAL,
                queue_key=QUEUE_KEY,
                status=WorkerJobStatus.QUEUED,
                subject_table="trials",
                subject_id=trial_id,
                payload={},
            )
        )

    # Deterministic verdict in place of the real sandbox run.
    wjsj.get_handler = lambda kind: _StubHandler()

    # The REAL claim + outcome machinery (FOR UPDATE SKIP LOCKED, _record_outcome).
    claimed = await wjsj.run_single_worker_job(
        QUEUE_KEY, worker_id="proof", queue_slot=0
    )
    if not claimed:
        print("STATUS=NOT_CLAIMED")
        return 1

    async with get_session() as session:
        row = await session.get(WorkerJobModel, job_id)
        status = row.status.value if row is not None else "MISSING"

    print(f"STATUS={status}")
    print(f"MODAL_IMPORTED={'modal' in sys.modules}")
    return 0 if status == "SUCCESS" else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(_main()))
