from __future__ import annotations

import sys
import types

import pytest

from carl_catfish import (
    catfish_config,
    catfish_query_params,
    drain_catfish_charts,
    fetch_catfish_chart,
    fetch_catfish_costs,
    format_catfish_breakdown,
    format_catfish_costs,
)


PAYLOAD = {
    "query": {
        "startDate": "2026-09-11",
        "endDate": "2026-09-17",
        "range": "7d",
        "provider": "anthropic",
        "group": "model",
        "costKind": None,
    },
    "totals": {"usd": 185734.52, "previousUsd": 108771.13, "changePercent": 70.8},
    "coverage": [{"provider": "anthropic", "note": "Anthropic pending"}],
    "series": [
        {"date": "2026-09-16", "usd": 24556.05, "byProvider": {"Anthropic": 24556.05}},
    ],
    "breakdown": [
        {
            "key": "Anthropic::opus",
            "label": "Claude Opus 5",
            "usd": 128982.45,
            "previousUsd": 80000,
            "changePercent": 61.2,
            "provider": "Anthropic",
        }
    ],
}


def test_query_defaults_and_aliases():
    assert catfish_query_params({}, default_group="provider") == {
        "provider": "all",
        "group": "provider",
        "range": "7d",
    }
    assert catfish_query_params({"range": "24h", "provider": "Anthropic"}, default_group="model") == {
        "provider": "anthropic",
        "group": "model",
        "range": "1d",
    }
    assert catfish_query_params(
        {"start": "2026-09-01", "end": "2026-09-07", "cost_kind": "token"},
        default_group="model",
    ) == {
        "provider": "all",
        "group": "model",
        "start": "2026-09-01",
        "end": "2026-09-07",
        "costKind": "token",
    }


def test_query_rejects_unknown_values():
    assert "Unknown range" in catfish_query_params({"range": "2h"}, default_group="provider")
    assert "Unknown provider" in catfish_query_params(
        {"provider": "heroku"}, default_group="provider"
    )
    assert "Unknown group" in catfish_query_params({"group": "kind"}, default_group="model")
    assert "Unknown cost_kind" in catfish_query_params(
        {"cost_kind": "gpu"}, default_group="model"
    )


def test_missing_token_explains_recycle(monkeypatch):
    monkeypatch.delenv("CATFISH_API_TOKEN", raising=False)
    message = catfish_config()
    assert isinstance(message, str)
    assert "CATFISH_API_TOKEN" in message
    assert "carl_answer" in message


def test_format_costs_warns_on_coverage_gaps():
    text = format_catfish_costs(PAYLOAD)
    assert "*Catfish cloud spend* (anthropic, 7d)" in text
    assert "total: $185,734.52" in text
    assert "+70.8%" in text
    assert "Anthropic pending" in text
    assert "do not treat a missing day as $0" in text
    assert "2026-09-16: $24,556.05" in text
    assert "Open this view" not in text


def test_format_includes_https_view_link():
    view = "https://costs.abundant.run/?start=2026-09-11&end=2026-09-17&provider=anthropic&gby=line"
    costs = format_catfish_costs({**PAYLOAD, "view": view})
    breakdown = format_catfish_breakdown({**PAYLOAD, "view": view})
    assert f"Open this view in Catfish: {view}" in costs
    assert f"Open this view in Catfish: {view}" in breakdown
    assert "<" not in costs and ">" not in breakdown
    assert "javascript:alert(1)" not in format_catfish_costs(
        {**PAYLOAD, "view": "javascript:alert(1)"}
    )


def test_format_breakdown_lists_models():
    text = format_catfish_breakdown(PAYLOAD)
    assert "*Catfish breakdown by model*" in text
    assert "• Claude Opus 5 (Anthropic): $128,982.45" in text


def _install_sdk(monkeypatch):
    sdk = types.ModuleType("claude_agent_sdk")

    def tool(name, _description, _schema):
        def decorate(function):
            function.name = name
            return function

        return decorate

    sdk.tool = tool
    sdk.create_sdk_mcp_server = lambda **kwargs: kwargs
    monkeypatch.setitem(sys.modules, "claude_agent_sdk", sdk)


@pytest.mark.asyncio
async def test_costs_tool_uses_catfish_payload(monkeypatch):
    _install_sdk(monkeypatch)
    import carl_tools

    async def fake_fetch(params):
        assert params["provider"] == "anthropic"
        assert params["group"] == "model"
        assert params["range"] == "7d"
        return PAYLOAD

    monkeypatch.setattr(carl_tools, "fetch_catfish_costs", fake_fetch)
    result = await carl_tools.catfish_breakdown(
        {"range": "7d", "provider": "anthropic"}
    )
    text = result["content"][0]["text"]
    assert "Claude Opus 5" in text
    assert "mcp__oddish__catfish_costs" in carl_tools.allowed_tool_names()
    assert "mcp__oddish__catfish_breakdown" in carl_tools.allowed_tool_names()


@pytest.mark.asyncio
async def test_fetch_surfaces_http_error(monkeypatch):
    import httpx

    monkeypatch.setenv("CATFISH_API_TOKEN", "secret")
    monkeypatch.setenv("CATFISH_API_URL", "https://catfish.test")

    class Client:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

        async def get(self, url, headers, params):
            assert url == "https://catfish.test/api/carl/costs"
            assert headers["Authorization"] == "Bearer secret"
            request = httpx.Request("GET", url)
            response = httpx.Response(
                401, text='{"error":"Unauthorized"}', request=request
            )
            raise httpx.HTTPStatusError(
                "unauthorized", request=request, response=response
            )

    monkeypatch.setattr("carl_catfish.RequestTimedAsyncClient", lambda **_k: Client())
    message = await fetch_catfish_costs(
        {"range": "7d", "provider": "all", "group": "provider"}
    )
    assert isinstance(message, str)
    assert "401" in message


@pytest.mark.asyncio
async def test_fetch_chart_uses_same_auth_and_query(monkeypatch):
    import httpx

    monkeypatch.setenv("CATFISH_API_TOKEN", "secret")
    monkeypatch.setenv("CATFISH_API_URL", "https://catfish.test")
    monkeypatch.setenv("CATFISH_VERCEL_BYPASS", "bypass-secret")
    png = b"\x89PNG\r\n\x1a\n" + b"fake-mix"

    class Client:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

        async def get(self, url, headers, params):
            assert url == "https://catfish.test/api/carl/chart"
            assert headers["Authorization"] == "Bearer secret"
            assert headers["x-vercel-protection-bypass"] == "bypass-secret"
            assert params == {
                "range": "7d",
                "provider": "all",
                "group": "provider",
            }
            request = httpx.Request("GET", url)
            return httpx.Response(
                200,
                content=png,
                headers={
                    "content-type": "image/png",
                    "content-disposition": (
                        'inline; filename="catfish-mix-2026-09-11-2026-09-17.png"'
                    ),
                },
                request=request,
            )

    monkeypatch.setattr("carl_catfish.RequestTimedAsyncClient", lambda **_k: Client())
    result = await fetch_catfish_chart(
        {"range": "7d", "provider": "all", "group": "provider"}
    )
    assert result == (png, "catfish-mix-2026-09-11-2026-09-17.png")


@pytest.mark.asyncio
async def test_chart_failure_does_not_hide_costs(monkeypatch):
    _install_sdk(monkeypatch)
    import carl_catfish
    import carl_tools

    async def fake_costs(params):
        assert params["provider"] == "anthropic"
        return PAYLOAD

    async def fake_chart(params):
        assert params["provider"] == "anthropic"
        return "Catfish HTTP 503: chart down"

    drain_catfish_charts()
    monkeypatch.setattr(carl_tools, "fetch_catfish_costs", fake_costs)
    monkeypatch.setattr(carl_catfish, "fetch_catfish_chart", fake_chart)

    result = await carl_tools.catfish_costs(
        {"range": "7d", "provider": "anthropic"}
    )
    text = result["content"][0]["text"]
    assert "$185,734.52" in text
    assert "503" not in text
    assert "chart down" not in text
    assert drain_catfish_charts() == []
