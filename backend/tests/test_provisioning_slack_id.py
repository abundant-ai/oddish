from __future__ import annotations

import httpx
import pytest

import auth.provisioning as prov
from auth.provisioning import (
    _slack_user_id_from_clerk_payload,
    fetch_slack_user_id_from_clerk,
)


def _payload(**account_overrides) -> dict:
    account = {"provider": "oauth_slack", "provider_user_id": "U0FALLBACK"}
    account.update(account_overrides)
    return {"external_accounts": [account]}


def test_payload_parse_extracts_bare_slack_user_id() -> None:
    assert _slack_user_id_from_clerk_payload(_payload()) == "U0FALLBACK"


def test_payload_parse_extracts_user_id_from_team_composite() -> None:
    # Clerk may store the Slack account as "<team_id>-<user_id>".
    assert (
        _slack_user_id_from_clerk_payload(_payload(provider_user_id="T01TEAM-U02USER"))
        == "U02USER"
    )
    # Enterprise-grid user ids start with W.
    assert (
        _slack_user_id_from_clerk_payload(_payload(provider_user_id="T01TEAM-W02USER"))
        == "W02USER"
    )


def test_payload_parse_unrecognized_shape_is_none() -> None:
    assert _slack_user_id_from_clerk_payload(_payload(provider_user_id="12345")) is None
    assert _slack_user_id_from_clerk_payload(_payload(provider_user_id=None)) is None


def test_payload_parse_no_slack_account_is_none() -> None:
    assert _slack_user_id_from_clerk_payload({"external_accounts": []}) is None
    github = {
        "external_accounts": [{"provider": "oauth_github", "username": "octocat"}]
    }
    assert _slack_user_id_from_clerk_payload(github) is None


def _mock_clerk_http(monkeypatch, handler) -> None:
    real_client = httpx.AsyncClient

    def _factory(*_args, **kwargs):
        kwargs.pop("timeout", None)
        kwargs["transport"] = httpx.MockTransport(handler)
        return real_client(**kwargs)

    monkeypatch.setattr(prov, "RequestTimedAsyncClient", _factory)


@pytest.mark.asyncio
async def test_fetch_returns_linked_slack_user_id(monkeypatch) -> None:
    monkeypatch.setattr(prov, "CLERK_SECRET_KEY", "sk_test")
    _mock_clerk_http(
        monkeypatch,
        lambda _req: httpx.Response(200, json=_payload(provider_user_id="U777")),
    )
    assert await fetch_slack_user_id_from_clerk("clerk_1") == "U777"


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["no_secret", "http_404", "transport_error"])
async def test_fetch_returns_none_for_nondefinitive_errors(monkeypatch, mode) -> None:
    # A None from any transport failure means "unknown", and the DM path falls
    # back to the email-based Slack lookup rather than dropping the message.
    if mode == "no_secret":
        monkeypatch.setattr(prov, "CLERK_SECRET_KEY", "")
    else:
        monkeypatch.setattr(prov, "CLERK_SECRET_KEY", "sk_test")
        if mode == "http_404":
            _mock_clerk_http(monkeypatch, lambda _req: httpx.Response(404))
        else:

            def _boom(_req):
                raise httpx.ConnectError("no route")

            _mock_clerk_http(monkeypatch, _boom)
    assert await fetch_slack_user_id_from_clerk("clerk_1") is None
