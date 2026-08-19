import pytest
from api.services.sandbox.daytona_client import FakeDaytonaClient
from api.services.sandbox.provisioner import Provisioner

pytestmark = pytest.mark.asyncio


async def test_provisioner_forwards_auto_delete_and_labels():
    fake = FakeDaytonaClient()
    sbx = await Provisioner(client=fake).create(
        env_vars={"ANTHROPIC_API_KEY": "x"},
        auto_stop_minutes=30,
        auto_delete_minutes=60,
        labels={"app": "sandbox", "session_id": "cs_1"},
        daytona_session_id="cc",
    )
    rec = fake.sandboxes[sbx.id]
    assert rec["auto_delete"] == 60
    assert rec["labels"]["app"] == "sandbox"
    assert "cc" in rec["sessions"]  # create_session still happens
