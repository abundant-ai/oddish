"""Storage absence and provider failures must remain distinguishable."""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from botocore.exceptions import ClientError

from oddish.db.storage import StorageClient


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "response, missing",
    [
        ({"Error": {"Code": "NoSuchKey"}}, True),
        ({"ResponseMetadata": {"HTTPStatusCode": 404}}, True),
        ({"Error": {"Code": ""}, "ResponseMetadata": {"HTTPStatusCode": 404}}, True),
        ({"Error": {"Code": ""}}, False),
        ({"ResponseMetadata": {"HTTPStatusCode": 403}}, False),
        ({"ResponseMetadata": {"HTTPStatusCode": 503}}, False),
        (
            {
                "Error": {"Code": "NoSuchBucket"},
                "ResponseMetadata": {"HTTPStatusCode": 404},
            },
            False,
        ),
    ],
)
async def test_head_and_diagnostics_agree_on_absence(response, missing, caplog):
    error = ClientError(response, "HeadObject")
    storage = StorageClient()
    storage._client = SimpleNamespace(head_object=AsyncMock(side_effect=error))
    if missing:
        assert not await storage.object_exists("task/file.txt")
        assert "S3 head failed" not in caplog.text
    else:
        with pytest.raises(ClientError) as raised:
            await storage.object_exists("task/file.txt")
        assert raised.value is error
        assert "S3 head failed" in caplog.text
