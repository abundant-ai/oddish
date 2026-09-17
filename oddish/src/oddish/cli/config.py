from __future__ import annotations

import json
import os
import re

import typer
from rich.console import Console

console = Console()
error_console = Console(stderr=True)

# =============================================================================
# Constants
# =============================================================================

# Canonical API base URL constants live in oddish.config (single source of
# truth, importable without pulling in the CLI's typer/rich deps). Re-exported
# here for the CLI's existing callers.
from oddish.config import DEFAULT_API_URL, PREVIEW_URL_TEMPLATE  # noqa: E402,F401

DEFAULT_DASHBOARD_URL = os.environ.get(
    "ODDISH_DEFAULT_DASHBOARD_URL", "https://www.oddish.app"
)


# =============================================================================
# API URL Helpers
# =============================================================================


def get_api_url() -> str:
    """Get API URL from environment or default.

    Resolution order:
      1. ``ODDISH_API_URL`` (full URL override)
      2. ``ODDISH_PREVIEW_PR`` formatted into ``PREVIEW_URL_TEMPLATE``
      3. ``DEFAULT_API_URL``
    """
    env_url = os.environ.get("ODDISH_API_URL")
    if env_url:
        return env_url
    pr = os.environ.get("ODDISH_PREVIEW_PR", "").strip()
    if pr:
        return PREVIEW_URL_TEMPLATE.format(n=pr)
    return DEFAULT_API_URL


def is_modal_api_url(api_url: str) -> bool:
    """Return True if the API URL targets Modal Cloud."""
    try:
        from urllib.parse import urlparse

        parsed = urlparse(api_url)
        host = (parsed.hostname or "").lower()
    except Exception:
        return False
    return host.endswith(".modal.run")


def get_dashboard_url(api_url: str | None = None) -> str:
    """Get dashboard URL from environment or default."""
    env_url = os.environ.get("ODDISH_DASHBOARD_URL")
    if env_url:
        return env_url.rstrip("/")
    return DEFAULT_DASHBOARD_URL


# =============================================================================
# Authentication
# =============================================================================


def get_api_key() -> str | None:
    """Get API key from environment."""
    env_key = os.environ.get("ODDISH_API_KEY")
    if env_key:
        return env_key
    return None


def require_api_key(api_url: str | None = None) -> str:
    """Require ODDISH_API_KEY for authenticated API access."""
    api_key = get_api_key()
    if not api_key:
        error_console.print(
            "[red]Missing API token.[/red]\n"
            f"Set ODDISH_API_KEY (create one at {DEFAULT_DASHBOARD_URL})."
        )
        raise typer.Exit(1)
    return api_key


def _parent_trace_headers() -> dict[str, str]:
    """Read trace-only controller context without adding an optional SDK dependency."""
    raw = os.environ.get("ODDISH_TRACE_CONTEXT", "")
    if not raw or len(raw) > 2048:
        return {}
    try:
        carrier = json.loads(raw)
    except ValueError:
        return {}
    if not isinstance(carrier, dict):
        return {}
    parent = carrier.get("traceparent")
    if not isinstance(parent, str) or not re.fullmatch(
        r"00-[0-9a-f]{32}-[0-9a-f]{16}-[0-9a-f]{2}", parent
    ):
        return {}
    if parent[3:35] == "0" * 32 or parent[36:52] == "0" * 16:
        return {}
    headers = {"traceparent": parent}
    state = carrier.get("tracestate")
    if not isinstance(state, str) or not state or len(state) > 512:
        return headers
    members = [member.strip() for member in state.split(",")]
    keys = set()
    for member in members:
        key, separator, value = member.partition("=")
        if (
            not separator
            or not re.fullmatch(
                r"(?:[a-z][a-z0-9_*/-]{0,255}|[a-z0-9][a-z0-9_*/-]{0,240}@[a-z][a-z0-9_*/-]{0,13})",
                key,
            )
            or not re.fullmatch(r"[\x20-\x2b\x2d-\x3c\x3e-\x7e]{1,256}", value)
            or value.endswith(" ")
            or key in keys
            or len(members) > 32
        ):
            return headers
        keys.add(key)
    headers["tracestate"] = ",".join(members)
    return headers


def get_auth_headers(api_url: str | None = None) -> dict[str, str]:
    """Build auth headers for API requests."""
    api_key = require_api_key(api_url)
    if not api_key:
        return {}
    return {"Authorization": f"Bearer {api_key}", **_parent_trace_headers()}


# =============================================================================
# JSON output (for CI / agents / scripting)
# =============================================================================


def print_json(payload: object) -> None:
    """Emit a JSON document on stdout for programmatic consumers.

    Uses the plain ``print`` builtin (not a Rich console) so the output is
    never wrapped, colorized, or truncated and can be piped straight into
    ``jq`` or parsed by an agent.
    """
    print(json.dumps(payload, indent=2, default=str))
