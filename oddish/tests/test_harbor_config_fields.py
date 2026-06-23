from oddish.schemas import HarborConfig, TrialResponse


def test_harbor_config_carries_override_fields_in_json_dump():
    hc = HarborConfig(
        source="https://github.com/dot-agi/harbor",
        ref="feature/x",
        resolved_sha="a" * 40,
        variant_id="ephemeral",
    )
    dumped = hc.model_dump(mode="json", exclude_defaults=True)
    assert dumped["source"] == "https://github.com/dot-agi/harbor"
    assert dumped["ref"] == "feature/x"
    assert dumped["resolved_sha"] == "a" * 40
    assert dumped["variant_id"] == "ephemeral"


def test_harbor_config_override_fields_default_to_none_and_are_excluded():
    assert HarborConfig().model_dump(mode="json", exclude_defaults=True) == {}


def test_trial_response_accepts_harbor_sha():
    from datetime import datetime

    resp = TrialResponse(
        id="t-0",
        name="t",
        task_id="t",
        task_path="p",
        agent="claude-code",
        provider="bedrock",
        queue_key="q",
        model="m",
        status="success",
        attempts=1,
        max_attempts=6,
        harbor_stage=None,
        reward=None,
        error_message=None,
        result=None,
        harbor_sha="b" * 40,
        created_at=datetime(2026, 1, 1),
        started_at=None,
        finished_at=None,
    )
    assert resp.harbor_sha == "b" * 40
