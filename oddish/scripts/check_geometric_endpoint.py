#!/usr/bin/env python
"""Smoke-test the Geometric endpoint using Oddish's OWN resolved trial config.

Deliberately not a hand-rolled request: every URL, model id, and credential
below is read back from ``_build_agent_config`` for a real trial, so a pass here
means the wiring a trial would get is the wiring that works. Covers both
surfaces the endpoint serves.

    GEOMETRIC_BASE_URL=http://127.0.0.1:8600/v1 \
    GEOMETRIC_API_KEY=... \
    uv run python scripts/check_geometric_endpoint.py

Exits non-zero if either surface fails.
"""

from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.request

from oddish.config import settings
from oddish.workers.harbor import runner as harbor_runner
from oddish.workers.harbor.model_hosts import outbound_hosts_for_model

MODEL = "geometric/glm-5.3"
PROMPT = "Reply with one short sentence naming the model you are."


def _resolve(env_value: str | None) -> str:
    """Resolve the ``${VAR}`` placeholders Harbor expands at exec time.

    Falls back to Settings: a key set in ``.env`` reaches pydantic settings but
    NOT ``os.environ``, so an os.environ-only lookup silently sends no auth.
    """
    raw = (env_value or "").strip()
    if raw.startswith("${") and raw.endswith("}"):
        name = raw[2:-1]
        from_env = os.environ.get(name, "")
        if from_env:
            return from_env
        return (getattr(settings, name.lower(), "") or "").strip()
    return raw


def _post(url: str, payload: dict, headers: dict[str, str]) -> tuple[int, str]:
    request = urllib.request.Request(
        url,
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json", **headers},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            return response.status, response.read().decode()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode()[:400]
    except Exception as exc:  # connection refused, DNS, timeout
        return 0, f"{type(exc).__name__}: {exc}"


def check_models_listing() -> list[str]:
    """GET /v1/models -- the ids here must match --served-model-name."""
    url = settings.geometric_base_url.rstrip("/") + "/models"
    key = (settings.geometric_api_key or "").strip()
    request = urllib.request.Request(
        url, headers={"Authorization": f"Bearer {key}"} if key else {}
    )
    try:
        with urllib.request.urlopen(request, timeout=15) as response:
            body = json.loads(response.read().decode())
    except Exception as exc:
        print(f"  FAIL  {url}\n        {type(exc).__name__}: {exc}")
        return []
    ids = [entry.get("id", "") for entry in body.get("data", [])]
    print(f"  ok    {url}\n        served ids: {ids}")
    return ids


def check_openai_surface() -> bool:
    """The mini-swe-agent route, driven through litellm exactly as the harness does."""
    agent_config = harbor_runner._build_agent_config(
        agent="mini-swe-agent", model=MODEL, raw_harbor_config={}
    )
    env = agent_config.env or {}
    base_url = env["OPENAI_BASE_URL"]
    api_key = _resolve(env.get("OPENAI_API_KEY"))

    # The wire id the harness puts on argv, from the agent class itself.
    from oddish.workers.agents.mini_swe_agent import OddishGeometricMiniSweAgent

    wire_model = f"openai/{OddishGeometricMiniSweAgent._oddish_bare_model_id(MODEL)}"

    print(f"  model={wire_model}  base_url={base_url}")
    import litellm

    try:
        completion = litellm.completion(
            model=wire_model,
            messages=[{"role": "user", "content": PROMPT}],
            api_base=base_url,
            api_key=api_key or "placeholder",
            max_tokens=512,
            timeout=30,
        )
    except Exception as exc:
        print(f"  FAIL  {type(exc).__name__}: {str(exc)[:300]}")
        return False
    print(f"  ok    -> {completion.choices[0].message.content!r}")
    return True


def check_anthropic_surface() -> bool:
    """The claude-code route: POST /v1/messages against the derived root."""
    agent_config = harbor_runner._build_agent_config(
        agent="claude-code", model=MODEL, raw_harbor_config={}
    )
    env = agent_config.env or {}
    base_url = env["ANTHROPIC_BASE_URL"]
    token = _resolve(env.get("ANTHROPIC_AUTH_TOKEN"))
    wire_model = env["ANTHROPIC_MODEL"]

    url = base_url.rstrip("/") + "/v1/messages"
    print(f"  model={wire_model}  url={url}")
    status, body = _post(
        url,
        {
            "model": wire_model,
            "max_tokens": 512,
            "messages": [{"role": "user", "content": PROMPT}],
        },
        {
            "anthropic-version": "2023-06-01",
            # Bearer ONLY. The endpoint's nginx returns 401 when a request
            # carries both x-api-key and Authorization; either alone is fine.
            # Claude Code sends Bearer (from ANTHROPIC_AUTH_TOKEN), so match it.
            **({"Authorization": f"Bearer {token}"} if token else {}),
        },
    )
    if status != 200:
        print(f"  FAIL  HTTP {status or 'no-connection'}: {body[:300]}")
        return False
    text = "".join(
        block.get("text", "") for block in json.loads(body).get("content", [])
    )
    print(f"  ok    -> {text.strip()!r}")
    return True


def main() -> int:
    print(f"GEOMETRIC_BASE_URL     = {settings.geometric_base_url}")
    print(f"anthropic root derived = {settings.get_geometric_anthropic_base_url()}")
    print(f"egress allowlist       = {outbound_hosts_for_model(MODEL)}")
    print(
        f"GEOMETRIC_API_KEY set  = {bool((settings.geometric_api_key or '').strip())}"
    )

    print("\n[1/3] GET /v1/models")
    served = check_models_listing()

    print("\n[2/3] OpenAI surface (mini-swe-agent route)")
    openai_ok = check_openai_surface()

    print("\n[3/3] Anthropic surface (claude-code route)")
    anthropic_ok = check_anthropic_surface()

    print("\n--- summary ---")
    print(f"  mini-swe-agent route : {'PASS' if openai_ok else 'FAIL'}")
    print(f"  claude-code route    : {'PASS' if anthropic_ok else 'FAIL'}")
    if served and "glm-5.3" not in served:
        print(
            f"  WARNING: _GEOMETRIC_SERVED_MODELS says 'glm-5.3' but the endpoint "
            f"serves {served}. Update the set (config.py) or --served-model-name."
        )
    return 0 if (openai_ok and anthropic_ok) else 1


if __name__ == "__main__":
    sys.exit(main())
