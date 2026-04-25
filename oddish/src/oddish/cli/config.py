from __future__ import annotations

import os

import typer
from rich.console import Console

console = Console()
error_console = Console(stderr=True)

# =============================================================================
# Constants
# =============================================================================

DEFAULT_API_URL = os.environ.get(
    "ODDISH_DEFAULT_API_URL", "https://abundant-ai--api.modal.run"
)
DEFAULT_DASHBOARD_URL = os.environ.get(
    "ODDISH_DEFAULT_DASHBOARD_URL", "https://www.oddish.app"
)
# Format string for resolving a PR-preview URL from `ODDISH_PREVIEW_PR`.
# `{n}` is the PR number. Forks override via `ODDISH_PREVIEW_URL_TEMPLATE`.
PREVIEW_URL_TEMPLATE = os.environ.get(
    "ODDISH_PREVIEW_URL_TEMPLATE",
    "https://abundant-ai-preview--oddish-pr-{n}-api.modal.run",
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


def get_auth_headers(api_url: str | None = None) -> dict[str, str]:
    """Build auth headers for API requests."""
    api_key = require_api_key(api_url)
    if not api_key:
        return {}
    return {"Authorization": f"Bearer {api_key}"}
