import pytest
from api.services.cc_chat.daytona_client import FakeDaytonaClient, RealDaytonaClient

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


async def test_real_create_requests_ephemeral_sandbox():
    """Prod's Daytona region rejects non-ephemeral sandboxes
    ("Only ephemeral sandboxes are permitted in this region"), so the real
    client must always request an ephemeral sandbox."""

    class _CapturingSDK:
        def __init__(self):
            self.params = None

        async def create(self, params):
            self.params = params
            return type("Sbx", (), {"id": "sbx_1"})()

    client = RealDaytonaClient(api_key="test-key")
    sdk = _CapturingSDK()
    client._daytona = sdk

    await client.create_sandbox(
        env_vars={"K": "v"},
        auto_stop_minutes=30,
        auto_delete_minutes=60,
        labels={"app": "cc_chat"},
    )

    assert sdk.params.ephemeral is True
    # ephemeral and auto_delete_interval are mutually exclusive in the SDK;
    # passing both emits a UserWarning, so we must not set auto_delete_interval.
    assert not sdk.params.auto_delete_interval
    assert sdk.params.auto_stop_interval == 30
    # No snapshot configured -> default base image.
    assert not sdk.params.snapshot


async def test_real_create_uses_snapshot_when_configured():
    class _CapturingSDK:
        def __init__(self):
            self.params = None

        async def create(self, params):
            self.params = params
            return type("Sbx", (), {"id": "sbx_1"})()

    client = RealDaytonaClient(api_key="test-key", snapshot="cc-chat-base-v1")
    sdk = _CapturingSDK()
    client._daytona = sdk

    await client.create_sandbox(
        env_vars={}, auto_stop_minutes=30, auto_delete_minutes=60, labels={}
    )
    assert sdk.params.snapshot == "cc-chat-base-v1"
