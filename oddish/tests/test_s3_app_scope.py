import pytest
from oddish.mcp.s3_app import resolve_scope_from_query
from oddish.mcp.scope_token import sign_experiment_token


def test_resolve_scope_sets_experiment(monkeypatch):
    monkeypatch.setattr("oddish.mcp.s3_app._signing_key", lambda: "k")
    monkeypatch.setattr("oddish.mcp.s3_app._now", lambda: 1000)
    tok = sign_experiment_token("exp1", key="k", ttl_seconds=60, now=1000)
    assert resolve_scope_from_query(f"token={tok}") == "exp1"


def test_resolve_scope_rejects_missing(monkeypatch):
    monkeypatch.setattr("oddish.mcp.s3_app._signing_key", lambda: "k")
    monkeypatch.setattr("oddish.mcp.s3_app._now", lambda: 1000)
    with pytest.raises(ValueError):
        resolve_scope_from_query("")
