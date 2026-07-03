from models import APIKeyScope, create_api_key


def test_create_api_key_sets_is_internal():
    model, raw = create_api_key(
        org_id="org_1", name="cc-chat:s1", scope=APIKeyScope.READ, is_internal=True
    )
    assert model.is_internal is True
    assert raw.startswith("ok_")


def test_create_api_key_defaults_not_internal():
    model, _ = create_api_key(org_id="org_1", name="user key")
    assert model.is_internal is False


def test_create_api_key_stores_creator_role():
    model, _ = create_api_key(
        org_id="org_1",
        name="member tasks",
        scope=APIKeyScope.TASKS,
        created_by_user_id="user_1",
        created_by_role="member",
    )

    assert model.created_by_role == "member"
