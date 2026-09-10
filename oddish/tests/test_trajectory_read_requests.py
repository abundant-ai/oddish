"""Exercise trajectory reads through the real storage wrapper with a fake SDK."""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from botocore.exceptions import ClientError

from oddish.core import trial_io
from oddish.db import storage as storage_module
from oddish.timing import begin_request_timing, reset_request_timing


@pytest.fixture
def trajectory_storage(monkeypatch):
    prefix = "tasks/task-1/trials/task-1-0/attempt-1/"
    manifest_key = prefix + "result.json"
    trajectory_key = prefix + "selected/agent/trajectory.json"
    objects = {
        manifest_key: json.dumps({"oddish_trial_name": "selected"}),
        trajectory_key: json.dumps({"steps": [{"step_id": 1, "message": "hello"}]}),
        prefix + "old/agent/trajectory.json": '{"steps": ["wrong retry"]}',
    }

    async def get_object(*, Bucket, Key):
        await asyncio.sleep(0)
        if Key not in objects:
            raise ClientError({"Error": {"Code": "NoSuchKey"}}, "GetObject")
        value = objects[Key]
        if isinstance(value, Exception):
            raise value
        body = AsyncMock()
        body.__aenter__.return_value = body
        body.read.return_value = value.encode()
        return {"Body": body}

    sdk = SimpleNamespace(
        get_object=AsyncMock(side_effect=get_object),
        head_object=AsyncMock(side_effect=AssertionError("unexpected existence check")),
    )
    client_context = AsyncMock()
    client_context.__aenter__.return_value = sdk
    session = Mock()
    session.client.return_value = client_context
    monkeypatch.setattr(storage_module.aioboto3, "Session", lambda: session)
    storage = storage_module.StorageClient()
    monkeypatch.setattr(trial_io, "get_storage_client", lambda: storage)
    trial_io._TRAJECTORY_CACHE.clear()
    trial_io._TRAJECTORY_LOCKS.clear()
    trial = SimpleNamespace(
        id="task-1-0",
        attempts=1,
        trial_s3_key=prefix,
        finished_at=datetime.now(UTC),
        model="test",
        has_trajectory=True,
    )
    yield trial, objects, sdk, manifest_key, trajectory_key
    trial_io._TRAJECTORY_CACHE.clear()
    trial_io._TRAJECTORY_LOCKS.clear()


@pytest.mark.asyncio
async def test_two_gets_then_no_storage_on_cache_hit(trajectory_storage):
    trial, objects, sdk, manifest_key, trajectory_key = trajectory_storage
    for expected_hit in (False, True):
        timing, *tokens = begin_request_timing()
        try:
            assert await trial_io.read_trial_trajectory(trial) == json.loads(
                objects[trajectory_key]
            )
            assert timing.trajectory_cache_hit is expected_hit
            assert timing.storage_request_count == (0 if expected_hit else 2)
            if not expected_hit:
                assert timing.durations_ms["storage_client_init"] >= 0
                assert timing.storage_bytes == sum(
                    len(objects[k].encode()) for k in (manifest_key, trajectory_key)
                )
            else:
                assert "storage_client_init" not in timing.durations_ms
        finally:
            reset_request_timing(tuple(tokens))
    assert [call.kwargs["Key"] for call in sdk.get_object.await_args_list] == [
        manifest_key,
        trajectory_key,
    ]
    sdk.head_object.assert_not_called()


@pytest.mark.asyncio
async def test_simultaneous_reads_share_the_download(trajectory_storage):
    trial, objects, sdk, _, trajectory_key = trajectory_storage

    async def read():
        timing, *tokens = begin_request_timing()
        try:
            result = await trial_io.read_trial_trajectory(trial)
            return result, timing
        finally:
            reset_request_timing(tuple(tokens))

    results = await asyncio.gather(read(), read())
    assert all(result == json.loads(objects[trajectory_key]) for result, _ in results)
    assert sorted(timing.trajectory_cache_hit for _, timing in results) == [False, True]
    assert all("trajectory_cache_wait" in timing.durations_ms for _, timing in results)
    assert sdk.get_object.await_count == 2


@pytest.mark.asyncio
async def test_running_trial_reads_fresh_contents(trajectory_storage):
    trial, objects, sdk, _, trajectory_key = trajectory_storage
    trial.finished_at = None
    await trial_io.read_trial_trajectory(trial)
    objects[trajectory_key] = '{"steps": ["new step"]}'
    assert await trial_io.read_trial_trajectory(trial) == {"steps": ["new step"]}
    assert sdk.get_object.await_count == 4


@pytest.mark.asyncio
@pytest.mark.parametrize("target", ["manifest", "trajectory"])
@pytest.mark.parametrize("code", ["AccessDenied", "SlowDown", "500"])
async def test_storage_failure_is_not_treated_as_missing(
    trajectory_storage, target, code
):
    trial, objects, sdk, manifest_key, trajectory_key = trajectory_storage
    key = manifest_key if target == "manifest" else trajectory_key
    objects[key] = ClientError({"Error": {"Code": code}}, "GetObject")
    with pytest.raises(ClientError) as exc:
        await trial_io.read_trial_trajectory(trial)
    assert exc.value.response["Error"]["Code"] == code
    assert sdk.get_object.await_args_list[-1].kwargs["Key"] == key
    assert not trial_io._TRAJECTORY_CACHE


@pytest.mark.asyncio
@pytest.mark.parametrize("code", [None, "", "404", "NoSuchKey", "NotFound"])
async def test_missing_manifest_uses_legacy_layout(trajectory_storage, code):
    from oddish.core.trial_artifacts import (
        TrialArtifactMode,
        resolve_trial_artifact_layout,
    )

    trial, objects, sdk, manifest_key, _ = trajectory_storage
    objects[manifest_key] = ClientError(
        {"Error": {"Code": code}, "ResponseMetadata": {"HTTPStatusCode": 404}},
        "GetObject",
    )
    layout = await resolve_trial_artifact_layout(trial, trial_io.get_storage_client())
    assert layout.mode is TrialArtifactMode.LEGACY
    assert layout.artifact_prefix == trial.trial_s3_key
    sdk.head_object.assert_not_called()


@pytest.mark.asyncio
async def test_empty_code_404_preserves_selected_step_trajectory(trajectory_storage):
    trial, objects, sdk, _, trajectory_key = trajectory_storage
    missing = ClientError(
        {"Error": {"Code": ""}, "ResponseMetadata": {"HTTPStatusCode": 404}},
        "GetObject",
    )
    objects[trajectory_key] = missing
    prefix = trial.trial_s3_key + "selected/"
    objects[prefix + "result.json"] = json.dumps(
        {"step_results": [{"step_name": "solve"}]}
    )
    objects[prefix + "steps/solve/agent/trajectory.json"] = (
        '{"steps": [{"step_id": 1}]}'
    )

    async def head_object(*, Bucket, Key):
        if Key not in objects:
            raise missing
        return {}

    sdk.head_object.side_effect = head_object
    result = await trial_io.read_trial_trajectory(trial)
    assert result["steps"][0]["step_id"] == 1
    assert all(
        "/old/" not in call.kwargs["Key"] for call in sdk.get_object.await_args_list
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("target", ["manifest", "trajectory"])
@pytest.mark.parametrize(
    "response",
    [
        {"Error": {"Code": ""}},
        {"Error": {"Code": ""}, "ResponseMetadata": {"HTTPStatusCode": 403}},
        {"Error": {"Code": ""}, "ResponseMetadata": {"HTTPStatusCode": 500}},
        {
            "Error": {"Code": "NoSuchBucket"},
            "ResponseMetadata": {"HTTPStatusCode": 404},
        },
        {
            "Error": {"Code": "AccessDenied"},
            "ResponseMetadata": {"HTTPStatusCode": 404},
        },
        {"Error": {"Code": "NoSuchKey"}, "ResponseMetadata": {"HTTPStatusCode": 503}},
    ],
)
async def test_ambiguous_or_service_errors_propagate(
    trajectory_storage, target, response
):
    trial, objects, sdk, manifest_key, trajectory_key = trajectory_storage
    key = manifest_key if target == "manifest" else trajectory_key
    error = ClientError(response, "GetObject")
    objects[key] = error
    with pytest.raises(ClientError) as raised:
        await trial_io.read_trial_trajectory(trial)
    assert raised.value is error
    sdk.head_object.assert_not_called()
    assert not trial_io._TRAJECTORY_CACHE
