from __future__ import annotations

import base64
import json

import pytest

from oddish.core.idempotency import compute_sweep_idempotency_key
from oddish.queue import _encrypt_submission_registry_auth, _trial_job_payload
from oddish.registry_auth import (
    DOCKER_HUB_AUTH_KEY,
    RegistryAuthDecryptError,
    RegistryCredential,
    build_docker_config_json,
    current_registry_credentials,
    decrypt_credentials,
    encrypt_credentials,
    parse_registry_login,
)
from oddish.schemas import TaskSubmission, TaskSweepSubmission, TrialSpec


def test_auth_key_normalizes_docker_hub_aliases():
    for alias in [
        "docker.io",
        "",
        "index.docker.io",
        "registry-1.docker.io",
        "https://index.docker.io/v1/",
    ]:
        assert RegistryCredential("u", "t", alias).auth_key() == DOCKER_HUB_AUTH_KEY


def test_auth_key_keeps_private_registry_host():
    assert RegistryCredential("u", "t", "ghcr.io").auth_key() == "ghcr.io"
    assert (
        RegistryCredential("u", "t", "https://my.reg:5000/").auth_key() == "my.reg:5000"
    )


def test_build_docker_config_json_encodes_basic_auth():
    cfg = json.loads(
        build_docker_config_json([RegistryCredential("alice", "tok", "docker.io")])
    )
    assert (
        cfg["auths"][DOCKER_HUB_AUTH_KEY]["auth"]
        == base64.b64encode(b"alice:tok").decode()
    )


def test_encrypt_decrypt_round_trip_hides_token():
    blob = encrypt_credentials(
        [RegistryCredential("alice", "secrettoken", "docker.io")]
    )
    assert isinstance(blob, str)
    assert "secrettoken" not in blob
    back = decrypt_credentials(blob)
    assert len(back) == 1
    assert back[0].username == "alice"
    assert back[0].token == "secrettoken"
    assert back[0].registry == "docker.io"


def test_encrypt_empty_is_none_and_decrypt_is_resilient():
    assert encrypt_credentials([]) is None
    assert decrypt_credentials(None) == []
    assert decrypt_credentials("") == []
    with pytest.raises(RegistryAuthDecryptError):
        decrypt_credentials("not-a-valid-fernet-token")


def test_parse_registry_login_merges_env_and_flags():
    creds = parse_registry_login(
        ["username=bob,token=tok1", "registry=ghcr.io,username=gh,token=tok2"],
        {"ODDISH_DOCKERHUB_USERNAME": "hub", "ODDISH_DOCKERHUB_TOKEN": "hubtok"},
    )
    by_reg = {c["registry"]: c for c in creds}
    assert by_reg["ghcr.io"]["username"] == "gh"
    assert by_reg["docker.io"]["username"] == "bob"


def test_parse_registry_login_requires_username_and_token():
    with pytest.raises(ValueError):
        parse_registry_login(["registry=docker.io,username=bob"], {})
    with pytest.raises(ValueError):
        parse_registry_login(["bareword"], {})


def test_parse_registry_login_token_may_contain_commas():
    creds = parse_registry_login(["username=bob,token=a,b=c,d"], {})
    assert creds == [{"username": "bob", "token": "a,b=c,d", "registry": "docker.io"}]


def test_parse_registry_login_empty_when_nothing_supplied():
    assert parse_registry_login(None, {}) == []


def test_registry_auth_rejects_empty_username_or_token():
    from pydantic import ValidationError

    from oddish.schemas import RegistryAuth

    RegistryAuth(username="bob", token="tok")
    with pytest.raises(ValidationError):
        RegistryAuth(username=" ", token="tok")
    with pytest.raises(ValidationError):
        RegistryAuth(username="bob", token=" ")


def test_registry_auth_rejects_userinfo_in_registry_without_leaking_token():
    from pydantic import ValidationError

    from oddish.schemas import RegistryAuth

    with pytest.raises(ValidationError) as excinfo:
        RegistryAuth(
            registry="https://user:registrysecret@ghcr.io",
            username="bob",
            token="supersecret",
    )
    message = str(excinfo.value)
    assert "registrysecret" not in message
    assert "supersecret" not in message


def test_idempotency_key_fingerprints_registry_auth():
    base = {"task_id": "x", "configs": []}
    with_a = {
        **base,
        "registry_auth": [{"username": "a", "token": "1", "registry": "docker.io"}],
    }
    with_b = {
        **base,
        "registry_auth": [{"username": "a", "token": "2", "registry": "docker.io"}],
    }
    assert compute_sweep_idempotency_key(base) != compute_sweep_idempotency_key(with_a)
    assert compute_sweep_idempotency_key(with_a) != compute_sweep_idempotency_key(with_b)
    assert len(compute_sweep_idempotency_key(with_a)) == 64


def test_request_hash_fingerprints_registry_auth():
    from oddish.core.idempotency import compute_request_hash

    base = dict(
        task_id="x",
        configs=[{"agent": "codex", "model": "openai/gpt-5.5", "n_trials": 1}],
    )
    plain = TaskSweepSubmission(**base)
    with_auth = TaskSweepSubmission(
        **base,
        registry_auth=[{"username": "alice", "token": "t", "registry": "ghcr.io"}],
    )
    changed_auth = TaskSweepSubmission(
        **base,
        registry_auth=[{"username": "alice", "token": "u", "registry": "ghcr.io"}],
    )
    assert compute_request_hash(plain) != compute_request_hash(with_auth)
    assert compute_request_hash(with_auth) != compute_request_hash(changed_auth)


def test_submission_masks_token_in_model_dump():
    sub = TaskSweepSubmission(
        task_id="x",
        configs=[{"agent": "codex", "model": "openai/gpt-5.5", "n_trials": 1}],
        registry_auth=[
            {"username": "alice", "token": "supersecret", "registry": "docker.io"}
        ],
    )
    assert "supersecret" not in json.dumps(sub.model_dump(mode="json"))
    assert sub.registry_auth[0].token.get_secret_value() == "supersecret"


def test_queue_helpers_encrypt_and_build_payload():
    ts = TaskSubmission(
        task_path="/x",
        trials=[TrialSpec(agent="codex", model="openai/gpt-5.5")],
        registry_auth=[
            {"username": "alice", "token": "supersecret", "registry": "docker.io"}
        ],
    )
    enc = _encrypt_submission_registry_auth(ts)
    assert enc and "supersecret" not in enc
    assert _trial_job_payload("x-0", enc) == {
        "trial_id": "x-0",
        "registry_auth_enc": enc,
    }
    assert _trial_job_payload("x-0", None) == {"trial_id": "x-0"}

    no_creds = TaskSubmission(
        task_path="/x", trials=[TrialSpec(agent="codex", model="m")]
    )
    assert _encrypt_submission_registry_auth(no_creds) is None


def test_context_var_default_is_none():
    assert current_registry_credentials.get() is None


def test_sweep_expansion_preserves_registry_auth():
    from oddish.core.sweeps import build_task_submission_from_sweep

    sweep = TaskSweepSubmission(
        task_id="x",
        configs=[{"agent": "codex", "model": "openai/gpt-5.5", "n_trials": 1}],
        registry_auth=[
            {"username": "alice", "token": "supersecret", "registry": "docker.io"}
        ],
    )
    expanded = build_task_submission_from_sweep(sweep, task_path="/x", trials=[])
    assert expanded.registry_auth is not None
    assert expanded.registry_auth[0].token.get_secret_value() == "supersecret"


def test_decrypt_failure_is_logged_loudly(caplog):
    import logging

    with caplog.at_level(logging.ERROR, logger="oddish.registry_auth"):
        with pytest.raises(RegistryAuthDecryptError):
            decrypt_credentials("present-but-undecryptable")
    assert any(r.levelno >= logging.ERROR for r in caplog.records)
    caplog.clear()
    with caplog.at_level(logging.ERROR, logger="oddish.registry_auth"):
        assert decrypt_credentials(None) == []
    assert not [r for r in caplog.records if r.levelno >= logging.ERROR]
