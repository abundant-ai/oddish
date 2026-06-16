import pytest
from api.services.cc_chat.daytona_client import FakeDaytonaClient

pytestmark = pytest.mark.asyncio


async def test_fake_create_records_auto_delete_and_labels():
    fake = FakeDaytonaClient()
    sbx = await fake.create_sandbox(
        env_vars={"K": "v"},
        auto_stop_minutes=30,
        auto_delete_minutes=60,
        labels={"app": "cc_chat", "session_id": "cs_1"},
    )
    rec = fake.sandboxes[sbx.id]
    assert rec["auto_stop"] == 30
    assert rec["auto_delete"] == 60
    assert rec["labels"] == {"app": "cc_chat", "session_id": "cs_1"}
