from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from worker import interrupted_workers as recovery


@pytest.mark.asyncio
@pytest.mark.parametrize("finished,raises", [(0, False), (1234, False), (0, True)])
async def test_exact_container_status_fails_closed(monkeypatch, finished, raises):
    get_info = AsyncMock(
        return_value=SimpleNamespace(info=SimpleNamespace(finished_at=finished))
    )
    if raises:
        get_info.side_effect = RuntimeError("status unavailable")
    monkeypatch.setattr(
        recovery._Client,
        "from_env",
        AsyncMock(
            return_value=SimpleNamespace(stub=SimpleNamespace(TaskGetInfo=get_info))
        ),
    )
    result = await recovery.container_stopped_at("ta-original")
    assert (result.timestamp() if result else None) == (1234 if finished else None)
    assert get_info.call_args.args[0].task_id == "ta-original"


@pytest.mark.asyncio
@pytest.mark.parametrize("stopped", [None, "positive-evidence"])
async def test_replacement_uses_call_and_reservation_and_checks_original(
    monkeypatch, stopped
):
    captured = []

    class Session:
        async def execute(self, sql, params):
            captured.append(params)
            return SimpleNamespace(
                mappings=lambda: SimpleNamespace(
                    all=lambda: [
                        dict(
                            worker_job_id="job",
                            attempt=1,
                            worker_id="old",
                            modal_container_id="ta-original",
                        )
                    ]
                )
            )

    @asynccontextmanager
    async def session():
        yield Session()

    monkeypatch.setattr(recovery, "get_session", session)
    check = AsyncMock(return_value=stopped)
    settle = AsyncMock(return_value=True)
    monkeypatch.setattr(recovery, "container_stopped_at", check)
    monkeypatch.setattr(recovery, "settle_interrupted_worker", settle)
    monkeypatch.setattr(recovery, "finish_interrupted_attempt_cleanup", AsyncMock())
    assert await recovery.recover_interrupted_workers(
        function_call_id="fc-shared",
        reservation_token="one-use",
        replacement_container_id="ta-new",
    ) == bool(stopped)
    assert captured == [dict(call="fc-shared", token="one-use", replacement="ta-new")]
    check.assert_awaited_once_with("ta-original")
    assert settle.await_count == bool(stopped)


@pytest.mark.asyncio
async def test_missing_replacement_identity_never_runs_global_recovery(monkeypatch):
    session = AsyncMock()
    monkeypatch.setattr(recovery, "get_session", session)
    assert await recovery.recover_interrupted_workers(reservation_token="token") == 0
    session.assert_not_called()


@pytest.mark.asyncio
async def test_rejected_startup_recovers_without_claiming_or_reusing_slot(monkeypatch):
    from contextlib import nullcontext
    from worker import functions
    from oddish.workers.queue.slots import ReservationRejected

    monkeypatch.setenv("MODAL_TASK_ID", "ta-replacement")
    monkeypatch.setattr(
        functions.modal, "current_function_call_id", lambda: "fc-original"
    )
    monkeypatch.setattr(functions, "_otel_span", lambda *a, **kw: nullcontext())
    monkeypatch.setattr(functions, "configure_storage_paths", AsyncMock())
    monkeypatch.setattr(functions, "close_database_connections", AsyncMock())
    monkeypatch.setattr(
        functions, "_effective_model_concurrency", AsyncMock(return_value=1)
    )
    monkeypatch.setattr(
        functions,
        "acquire_queue_slot",
        AsyncMock(side_effect=ReservationRejected("reservation_already_consumed")),
    )
    recover = AsyncMock(return_value=1)
    monkeypatch.setattr(recovery, "recover_interrupted_workers", recover)
    drain, release, release_token = AsyncMock(), AsyncMock(), AsyncMock()
    monkeypatch.setattr(functions, "drain_worker_jobs", drain)
    monkeypatch.setattr(functions, "release_queue_slot", release)
    monkeypatch.setattr(functions, "release_launch_reservations", release_token)
    await functions._run_one_job("m", reservation_token="old-token")
    recover.assert_awaited_once_with(
        function_call_id="fc-original",
        reservation_token="old-token",
        replacement_container_id="ta-replacement",
    )
    drain.assert_not_called()
    release.assert_not_called()
    release_token.assert_awaited_once_with(["old-token"])
