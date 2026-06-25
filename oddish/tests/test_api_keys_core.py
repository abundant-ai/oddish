from datetime import timedelta
from oddish.db.models import APIKeyModel, APIKeyScope, utcnow
from oddish.core.api_keys import (
    create_api_key, generate_api_key, hash_api_key,
)

def test_generate_and_hash_roundtrip():
    raw = generate_api_key()
    assert raw.startswith("ok_")
    assert hash_api_key(raw) == hash_api_key(raw)
    assert len(hash_api_key(raw)) == 64  # sha256 hex

def test_create_api_key_read_internal():
    key, raw = create_api_key(
        org_id="org_1", name="probe:t1",
        scope=APIKeyScope.READ,
        expires_at=utcnow() + timedelta(minutes=30),
        is_internal=True,
    )
    assert isinstance(key, APIKeyModel)
    assert key.scope == APIKeyScope.READ
    assert key.is_internal is True
    assert key.key_hash == hash_api_key(raw)
    assert key.org_id == "org_1"


def test_api_key_model_has_no_cross_stack_foreign_keys():
    assert not APIKeyModel.__table__.foreign_keys
