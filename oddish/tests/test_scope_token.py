import pytest
from oddish.mcp.scope_token import sign_experiment_token, verify_experiment_token


KEY = "test-signing-key"


def test_round_trip():
    tok = sign_experiment_token("exp1", key=KEY, ttl_seconds=60, now=1000)
    assert verify_experiment_token(tok, key=KEY, now=1030) == "exp1"


def test_expired_rejected():
    tok = sign_experiment_token("exp1", key=KEY, ttl_seconds=60, now=1000)
    with pytest.raises(ValueError):
        verify_experiment_token(tok, key=KEY, now=2000)


def test_tampered_rejected():
    tok = sign_experiment_token("exp1", key=KEY, ttl_seconds=60, now=1000)
    bad = tok[:-2] + ("aa" if not tok.endswith("aa") else "bb")
    with pytest.raises(ValueError):
        verify_experiment_token(bad, key=KEY, now=1010)
